"""SurveyCTO API client: form list, XForm XML, wide JSON, datasets, attachments.

Every non-2xx response raises ``SurveyCTOError`` with the status, the URL and the start
of the server's reason; nothing retries.
Endpoint shapes are recorded in ``docs/api-endpoints.md``.
"""

import io
import os
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime
from pathlib import Path

import requests

_MANIFEST_NS = "{http://openrosa.org/xforms/xformsManifest}"


class MissingEnvVar(RuntimeError):
    """A required environment variable is unset or empty."""


class SurveyCTOError(RuntimeError):
    """The server answered a request with a non-2xx status or a body that is not JSON."""

    def __init__(self, method: str, url: str, status: int, reason: str):
        """Record the failed request and why it failed."""
        super().__init__(f"{method} {url} -> HTTP {status}: {reason}")
        self.method = method
        self.url = url
        self.status = status
        self.reason = reason


def _env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise MissingEnvVar(f"environment variable {name} is not set")
    return value


def env_key_path() -> Path:
    """Return the private key path from ``SCTO_KEY_PATH``."""
    return Path(_env("SCTO_KEY_PATH"))


class Client:
    """Basic-auth client for one SurveyCTO server."""

    def __init__(self, server: str, user: str, password: str):
        """Target ``https://{server}.surveycto.com`` with Basic auth."""
        self.server = server
        self.base_url = f"https://{server}.surveycto.com"
        self._auth = requests.auth.HTTPBasicAuth(user, password)
        self._user = user

    @classmethod
    def from_env(cls) -> "Client":
        """Build from ``SCTO_SERVER`` (bare subdomain), ``SCTO_USER``, ``SCTO_PASSWORD``."""
        return cls(_env("SCTO_SERVER"), _env("SCTO_USER"), _env("SCTO_PASSWORD"))

    def __repr__(self) -> str:
        return f"Client(server={self.server!r}, user={self._user!r})"

    def _send(self, method: str, url: str, **kwargs) -> requests.Response:
        resp = requests.request(method, url, auth=self._auth, **kwargs)
        if not resp.ok:
            # The server explains a refusal in the body ("wait 300 seconds").
            raise SurveyCTOError(method, url, resp.status_code, resp.text[:300])
        return resp

    def _send_json(self, method: str, url: str, **kwargs):
        resp = self._send(method, url, **kwargs)
        try:
            return resp.json()
        except requests.exceptions.JSONDecodeError as e:
            # A body cut short can hold submissions: its type and size, not its text.
            raise SurveyCTOError(
                method,
                url,
                resp.status_code,
                f"the body is not JSON ({resp.headers.get('Content-Type')}, "
                f"{len(resp.content)} bytes)",
            ) from e

    def pull_form_list(self) -> bytes:
        """Return the OpenRosa form list XML."""
        return self._send("GET", f"{self.base_url}/formList").content

    def pull_form_xml(self, form_id: str) -> bytes:
        """Return the deployed form's compiled XForm XML."""
        return self._send("GET", f"{self.base_url}/forms/{form_id}/xml").content

    def pull_wide_json(
        self,
        form_id: str,
        *,
        key_path: Path | None = None,
        since: datetime | None = None,
    ) -> list[dict]:
        """Return wide JSON submissions completed at or after ``since`` (all when None).

        With ``key_path`` the key is POSTed as multipart ``private_key`` and encrypted
        fields come back decrypted; without it only publishable fields are returned.
        """
        if since is not None and since.tzinfo is None:
            raise ValueError("since must be timezone-aware")
        # date=0 is throttled to once per 300 s; date=1 also means "everything".
        date = 1 if since is None else int(since.timestamp())
        url = f"{self.base_url}/api/v2/forms/data/wide/json/{form_id}?date={date}"
        if key_path is None:
            return self._send_json("GET", url)
        key = Path(key_path).read_bytes()
        return self._send_json(
            "POST", url, files={"private_key": ("private_key.pem", key)}
        )

    def list_datasets(self) -> list[dict]:
        """Return every server dataset's metadata (no records), following ``nextCursor``."""
        url = f"{self.base_url}/api/v2/datasets"
        page = self._send_json("GET", url)
        datasets = page["data"]
        while page["nextCursor"]:
            page = self._send_json("GET", url, params={"cursor": page["nextCursor"]})
            datasets += page["data"]
        return datasets

    def pull_dataset(self, dataset_id: str) -> bytes:
        """Return a server dataset's CSV as the server's bytes.

        Decoding would read ``text/csv`` without a charset as Latin-1.
        """
        url = f"{self.base_url}/api/v2/datasets/data/csv/{dataset_id}"
        return self._send("GET", url).content

    def pull_form_files(self, form_id: str, filenames: list[str]) -> dict[str, bytes]:
        """Return the form's attached CSVs ``filenames``, keyed by filename.

        The manifest serves each attached CSV zipped: ``prices.csv`` is the single
        member of ``prices.zip``.
        """
        manifest = self._send(
            "GET", f"{self.base_url}/forms/{form_id}/manifest"
        ).content
        urls = {
            media.findtext(f"{_MANIFEST_NS}filename"): media.findtext(
                f"{_MANIFEST_NS}downloadUrl"
            )
            for media in ET.fromstring(manifest).iter(f"{_MANIFEST_NS}mediaFile")
        }
        files = {}
        for name in filenames:
            url = urls[f"{name.removesuffix('.csv')}.zip"]
            with zipfile.ZipFile(io.BytesIO(self._send("GET", url).content)) as z:
                files[name] = z.read(name)
        return files
