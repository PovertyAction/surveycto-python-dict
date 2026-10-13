"""Stage 1: SurveyCTO API client. HTTP is mocked with ``responses``; no network."""

import base64
import io
import zipfile
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlparse

import pytest
import responses

from surveycto_python_dict.api import (
    Client,
    MissingEnvVar,
    SurveyCTOError,
    env_key_path,
)

BASE = "https://acme.surveycto.com"
FORM = "demo_form"
KEY_BYTES = b"-----BEGIN RSA PRIVATE KEY-----\nFAKEKEYMATERIAL123\n-----END RSA PRIVATE KEY-----\n"

_XML = b"""<?xml version="1.0"?>
<h:html xmlns="http://www.w3.org/2002/xforms" xmlns:h="http://www.w3.org/1999/xhtml">
  <h:head><model><instance><demo_form id="demo_form" version="1"><age/></demo_form></instance>
  <bind nodeset="/demo_form/age" type="int" publishable="true()"/></model></h:head>
  <h:body><input ref="/demo_form/age"><label>Age</label></input></h:body>
</h:html>"""

_MANIFEST = b"""<?xml version="1.0"?>
<manifest xmlns="http://openrosa.org/xforms/xformsManifest">
  <mediaFile><filename>prices.zip</filename><hash>md5:abc</hash>
    <downloadUrl>https://acme.surveycto.com/forms/demo_form/files/prices.zip</downloadUrl></mediaFile>
  <mediaFile><filename>logo.png</filename><hash>md5:def</hash>
    <downloadUrl>https://acme.surveycto.com/forms/demo_form/files/logo.png</downloadUrl></mediaFile>
</manifest>"""


@pytest.fixture
def client():
    return Client("acme", "user@example.org", "s3cret-pw")


@pytest.fixture
def key_file(tmp_path):
    path = tmp_path / "key.pem"
    path.write_bytes(KEY_BYTES)
    return path


def _basic(user, password):
    return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()


# -- credentials -------------------------------------------------------------------


@pytest.mark.parametrize("missing", ["SCTO_SERVER", "SCTO_USER", "SCTO_PASSWORD"])
def test_from_env_missing_var_raises_naming_it(monkeypatch, missing):
    for name in ("SCTO_SERVER", "SCTO_USER", "SCTO_PASSWORD"):
        monkeypatch.setenv(name, "x")
    monkeypatch.delenv(missing)
    with pytest.raises(MissingEnvVar, match=missing):
        Client.from_env()


def test_from_env_empty_var_counts_as_missing(monkeypatch):
    monkeypatch.setenv("SCTO_SERVER", "")
    monkeypatch.setenv("SCTO_USER", "u")
    monkeypatch.setenv("SCTO_PASSWORD", "p")
    with pytest.raises(MissingEnvVar, match="SCTO_SERVER"):
        Client.from_env()


def test_from_env_builds_base_url_from_bare_server(monkeypatch):
    monkeypatch.setenv("SCTO_SERVER", "acme")
    monkeypatch.setenv("SCTO_USER", "u")
    monkeypatch.setenv("SCTO_PASSWORD", "p")
    assert Client.from_env().base_url == BASE


def test_env_key_path(monkeypatch, key_file):
    monkeypatch.delenv("SCTO_KEY_PATH", raising=False)
    with pytest.raises(MissingEnvVar, match="SCTO_KEY_PATH"):
        env_key_path()
    monkeypatch.setenv("SCTO_KEY_PATH", str(key_file))
    assert env_key_path() == key_file


def test_repr_hides_password(client):
    assert "s3cret-pw" not in repr(client)
    assert "s3cret-pw" not in str(client)


# -- endpoints ---------------------------------------------------------------------


@responses.activate
def test_pull_form_list(client):
    responses.get(f"{BASE}/formList", body=b"<xforms/>")
    assert client.pull_form_list() == b"<xforms/>"


@responses.activate
def test_pull_form_xml_hits_xml_url_with_basic_auth(client):
    responses.get(f"{BASE}/forms/{FORM}/xml", body=_XML)
    assert client.pull_form_xml(FORM) == _XML
    sent = responses.calls[0].request
    assert sent.headers["Authorization"] == _basic("user@example.org", "s3cret-pw")


@responses.activate
def test_pull_wide_json_without_key_is_get_for_all_submissions(client):
    rows = [{"KEY": "uuid:1", "age": "30"}]
    responses.get(f"{BASE}/api/v2/forms/data/wide/json/{FORM}", json=rows)
    assert client.pull_wide_json(FORM) == rows
    sent = responses.calls[0].request
    assert sent.method == "GET"
    # date=0 is throttled to once per 300 s; date=1 also means "everything".
    assert parse_qs(urlparse(sent.url).query) == {"date": ["1"]}


@responses.activate
def test_pull_wide_json_with_key_posts_multipart_private_key(client, key_file):
    responses.post(f"{BASE}/api/v2/forms/data/wide/json/{FORM}", json=[])
    client.pull_wide_json(FORM, key_path=key_file)
    sent = responses.calls[0].request
    assert sent.method == "POST"
    assert sent.headers["Content-Type"].startswith("multipart/form-data")
    assert b'name="private_key"' in sent.body
    assert KEY_BYTES in sent.body


@responses.activate
def test_pull_wide_json_since_sends_epoch_seconds(client):
    responses.get(f"{BASE}/api/v2/forms/data/wide/json/{FORM}", json=[])
    since = datetime(2025, 3, 1, 12, 0, tzinfo=UTC)
    client.pull_wide_json(FORM, since=since)
    query = parse_qs(urlparse(responses.calls[0].request.url).query)
    assert query == {"date": [str(int(since.timestamp()))]}


def test_pull_wide_json_naive_since_raises(client):
    with pytest.raises(ValueError, match="timezone"):
        client.pull_wide_json(FORM, since=datetime(2025, 3, 1))


@responses.activate
def test_pull_dataset(client):
    responses.get(f"{BASE}/api/v2/datasets/data/csv/roster", body="id,name\n1,a\n")
    assert client.pull_dataset("roster") == b"id,name\n1,a\n"


@responses.activate
def test_list_datasets_follows_the_cursor(client):
    responses.get(
        f"{BASE}/api/v2/datasets",
        json={"data": [{"id": "b"}], "nextCursor": None},
        match=[responses.matchers.query_param_matcher({"cursor": "c1"})],
    )
    responses.get(
        f"{BASE}/api/v2/datasets",
        json={"data": [{"id": "a"}], "nextCursor": "c1"},
        match=[responses.matchers.query_param_matcher({})],
    )
    assert client.list_datasets() == [{"id": "a"}, {"id": "b"}]


def _zipped(name: str, content: bytes) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(name, content)
    return buf.getvalue()


@responses.activate
def test_pull_form_files_unzips_only_the_requested_csvs(client):
    responses.get(f"{BASE}/forms/{FORM}/manifest", body=_MANIFEST)
    responses.get(
        f"{BASE}/forms/{FORM}/files/prices.zip",
        body=_zipped("prices.csv", b"item,price\nrice,2\n"),
    )
    assert client.pull_form_files(FORM, ["prices.csv"]) == {
        "prices.csv": b"item,price\nrice,2\n"
    }


# -- failures ----------------------------------------------------------------------

_CALLS = {
    "form_list": (responses.GET, f"{BASE}/formList", lambda c, k: c.pull_form_list()),
    "xml": (
        responses.GET,
        f"{BASE}/forms/{FORM}/xml",
        lambda c, k: c.pull_form_xml(FORM),
    ),
    "wide": (
        responses.GET,
        f"{BASE}/api/v2/forms/data/wide/json/{FORM}",
        lambda c, k: c.pull_wide_json(FORM),
    ),
    "wide_key": (
        responses.POST,
        f"{BASE}/api/v2/forms/data/wide/json/{FORM}",
        lambda c, k: c.pull_wide_json(FORM, key_path=k),
    ),
    "dataset": (
        responses.GET,
        f"{BASE}/api/v2/datasets/data/csv/roster",
        lambda c, k: c.pull_dataset("roster"),
    ),
    "manifest": (
        responses.GET,
        f"{BASE}/forms/{FORM}/manifest",
        lambda c, k: c.pull_form_files(FORM, ["prices.csv"]),
    ),
}


@pytest.mark.parametrize("status", [401, 402, 417, 500])
@pytest.mark.parametrize("name", list(_CALLS))
@responses.activate
def test_non_2xx_raises_with_status_and_url_no_retry(client, key_file, name, status):
    method, url, call = _CALLS[name]
    responses.add(method, url, status=status, body="server says no")
    with pytest.raises(SurveyCTOError) as err:
        call(client, key_file)
    assert err.value.status == status
    assert str(status) in str(err.value)
    assert urlparse(url).path in str(err.value)
    assert len(responses.calls) == 1


@responses.activate
def test_key_and_password_never_in_error(client, key_file):
    responses.post(f"{BASE}/api/v2/forms/data/wide/json/{FORM}", status=500)
    with pytest.raises(SurveyCTOError) as err:
        client.pull_wide_json(FORM, key_path=key_file)
    text = f"{err.value}{err.value!r}{err.value.args}"
    assert "FAKEKEYMATERIAL123" not in text
    assert "s3cret-pw" not in text
