"""SurveyCTO API client: form list, XForm XML, /design, wide JSON, datasets, attachments.

Every non-2xx response raises ``SurveyCTOError`` with status and URL; nothing retries.
Endpoint shapes are recorded in ``docs/api-endpoints.md``.
"""

import json
import os
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

import requests

_MANIFEST_NS = "{http://openrosa.org/xforms/xformsManifest}"


class MissingEnvVar(RuntimeError):
    """A required environment variable is unset or empty."""


class SurveyCTOError(RuntimeError):
    """The server answered a request with a non-2xx status or an unexpected shape."""

    def __init__(self, method: str, url: str, status: int, detail: str = ""):
        """Record the failed request; ``detail`` explains a non-status failure."""
        super().__init__(
            f"{method} {url} -> HTTP {status}{f': {detail}' if detail else ''}"
        )
        self.method = method
        self.url = url
        self.status = status


def _csrf_token(resp: requests.Response) -> str:
    token = resp.headers.get("X-csrf-token")
    if token is None:
        req = resp.request
        raise SurveyCTOError(
            req.method, req.url, resp.status_code, "no X-csrf-token header"
        )
    return token


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
        session = kwargs.pop("session", None) or requests
        resp = session.request(method, url, auth=self._auth, **kwargs)
        if not resp.ok:
            raise SurveyCTOError(method, url, resp.status_code)
        return resp

    def pull_form_list(self) -> bytes:
        """Return the OpenRosa form list XML."""
        return self._send("GET", f"{self.base_url}/formList").content

    def pull_form_xml(self, form_id: str) -> bytes:
        """Return the deployed form's compiled XForm XML."""
        return self._send("GET", f"{self.base_url}/forms/{form_id}/xml").content

    def pull_design(self, form_id: str) -> dict:
        """Return the XLSForm sheets as rows (console endpoint; needs a CSRF login session)."""
        session = requests.Session()
        head = session.head(self.base_url)
        token = _csrf_token(head)
        login = self._send(
            "POST",
            f"{self.base_url}/login",
            headers={"X-csrf-token": token},
            session=session,
        )
        token = _csrf_token(login)
        url = f"{self.base_url}/forms/{form_id}/design"
        resp = self._send("GET", url, headers={"X-csrf-token": token}, session=session)
        # Served as text/html; the body is JSON.
        return json.loads(resp.text)

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
            return self._send("GET", url).json()
        key = Path(key_path).read_bytes()
        return self._send(
            "POST", url, files={"private_key": ("private_key.pem", key)}
        ).json()

    def pull_dataset(self, dataset_id: str) -> str:
        """Return a server dataset as CSV text."""
        url = f"{self.base_url}/api/v2/datasets/data/csv/{dataset_id}"
        return self._send("GET", url).text

    def pull_form_files(self, form_id: str) -> dict[str, bytes]:
        """Return every file attached to the form, keyed by filename, via its manifest."""
        manifest = self._send(
            "GET", f"{self.base_url}/forms/{form_id}/manifest"
        ).content
        files = {}
        for media in ET.fromstring(manifest).iter(f"{_MANIFEST_NS}mediaFile"):
            name = media.findtext(f"{_MANIFEST_NS}filename")
            url = media.findtext(f"{_MANIFEST_NS}downloadUrl")
            files[name] = self._send("GET", url).content
        return files
