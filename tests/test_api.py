"""Stage 1: SurveyCTO API client. HTTP is mocked with ``responses``; no network."""

import base64
import os
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
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
  <mediaFile><filename>prices.csv</filename><hash>md5:abc</hash>
    <downloadUrl>https://acme.surveycto.com/forms/demo_form/files/prices.csv</downloadUrl></mediaFile>
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
    since = datetime(2025, 3, 1, 12, 0, tzinfo=timezone.utc)
    client.pull_wide_json(FORM, since=since)
    query = parse_qs(urlparse(responses.calls[0].request.url).query)
    assert query == {"date": [str(int(since.timestamp()))]}


def test_pull_wide_json_naive_since_raises(client):
    with pytest.raises(ValueError, match="timezone"):
        client.pull_wide_json(FORM, since=datetime(2025, 3, 1))


@responses.activate
def test_pull_design_logs_in_with_csrf_then_parses_json(client):
    responses.head(
        BASE, status=302, headers={"X-csrf-token": "t1", "Location": "/main.html"}
    )
    responses.post(f"{BASE}/login", headers={"X-csrf-token": "t2"})
    # The console serves JSON as text/html.
    responses.get(
        f"{BASE}/forms/{FORM}/design",
        body='{"fieldsRowsAndColumns": [["type", "name"]]}',
        content_type="text/html;charset=UTF-8",
    )
    assert client.pull_design(FORM) == {"fieldsRowsAndColumns": [["type", "name"]]}
    head, login, design = (c.request for c in responses.calls)
    assert login.headers["X-csrf-token"] == "t1"
    assert login.headers["Authorization"] == _basic("user@example.org", "s3cret-pw")
    assert design.headers["X-csrf-token"] == "t2"


@responses.activate
def test_pull_design_without_csrf_token_raises(client):
    responses.head(BASE, status=302)
    with pytest.raises(SurveyCTOError, match="X-csrf-token"):
        client.pull_design(FORM)


@responses.activate
def test_pull_dataset(client):
    responses.get(f"{BASE}/api/v2/datasets/data/csv/roster", body="id,name\n1,a\n")
    assert client.pull_dataset("roster") == "id,name\n1,a\n"


@responses.activate
def test_pull_form_files_downloads_every_manifest_entry(client):
    responses.get(f"{BASE}/forms/{FORM}/manifest", body=_MANIFEST)
    responses.get(f"{BASE}/forms/{FORM}/files/prices.csv", body=b"item,price\nrice,2\n")
    assert client.pull_form_files(FORM) == {"prices.csv": b"item,price\nrice,2\n"}


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
        lambda c, k: c.pull_form_files(FORM),
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


# -- live (SCTO_LIVE=1) ------------------------------------------------------------
# Derive structure first, then assert on it: a failing assert prints its operands.


@pytest.fixture
def live():
    return Client.from_env(), os.environ["SCTO_FORM_NAME"]


@pytest.mark.live
def test_live_form_list_lists_form(live):
    client, form = live
    root = ET.fromstring(client.pull_form_list())
    listed = form in {e.text for e in root.iter() if e.tag.endswith("formID")}
    assert listed


@pytest.mark.live
def test_live_form_xml_is_xform_with_publishable(live):
    client, form = live
    root = ET.fromstring(client.pull_form_xml(form))
    n_publishable = sum(1 for e in root.iter() if "publishable" in e.attrib)
    assert root.tag.endswith("html")
    assert n_publishable > 0


@pytest.mark.live
def test_live_design_has_xlsform_sheets(live):
    client, form = live
    sheets = set(client.pull_design(form))
    assert {
        "fieldsRowsAndColumns",
        "choicesRowsAndColumns",
        "settingsRowsAndColumns",
    } <= sheets


@pytest.mark.live
def test_live_wide_json_keyed_has_more_columns_than_keyless(live):
    client, form = live
    keyed = client.pull_wide_json(form, key_path=env_key_path())
    keyless = client.pull_wide_json(form)
    n_keyed, n_keyless = len(keyed), len(keyless)
    cols_keyed = {c for r in keyed for c in r}
    cols_keyless = {c for r in keyless for c in r}
    same_keys = {r["KEY"] for r in keyed} == {r["KEY"] for r in keyless}
    assert n_keyed == n_keyless > 0
    assert same_keys
    assert len(cols_keyed) > len(cols_keyless)


@pytest.mark.live
def test_live_since_in_future_returns_nothing(live):
    client, form = live
    n_rows = len(
        client.pull_wide_json(form, since=datetime(2100, 1, 1, tzinfo=timezone.utc))
    )
    assert n_rows == 0


@pytest.mark.live
def test_live_form_files(live):
    client, form = live
    names = sorted(client.pull_form_files(form))
    assert isinstance(names, list)


@pytest.mark.live
def test_live_dataset():
    dataset = os.environ.get("SCTO_DATASET_NAME")
    if not dataset:
        pytest.skip("SCTO_DATASET_NAME not set")
    n_lines = len(Client.from_env().pull_dataset(dataset).splitlines())
    assert n_lines >= 1
