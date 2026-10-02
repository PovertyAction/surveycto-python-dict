"""Stage 0 discovery probe: pull everything into data/stage0/, print structure only.

Never prints a submission value. It prints HTTP status, URL shapes (server redacted),
column names, per-column presence and non-empty counts in each pull, XML bind
attribute names. Formats of system date
columns are printed as masks (digits -> 9, letters -> a).

Run: uv run python scripts/stage0_probe.py
"""

import json
import re
import sys
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path

import requests
from dotenv import dotenv_values
from surveycto_extractor.parsers.xml_contract import is_system_column, parse_contract

ENV = dotenv_values(".env")
SERVER = ENV["SCTO_SERVER"]
FORM = ENV["SCTO_FORM_NAME"]
BASE = f"https://{SERVER}.surveycto.com"
AUTH = requests.auth.HTTPBasicAuth(ENV["SCTO_USER"], ENV["SCTO_PASSWORD"])
OUT = Path("data/stage0")
OUT.mkdir(parents=True, exist_ok=True)


def shown(url: str) -> str:
    return url.replace(SERVER, "{server}").replace(FORM, "{form_id}")


def check(resp: requests.Response, label: str) -> requests.Response:
    print(
        f"[{label}] {resp.request.method} {shown(resp.url)} -> {resp.status_code} "
        f"({resp.headers.get('Content-Type', '?')}, {len(resp.content)} bytes)"
    )
    if not resp.ok:
        sys.exit(f"[{label}] failed with {resp.status_code}; body not printed")
    return resp


def mask(value: str) -> str:
    return re.sub(r"[A-Za-z]", "a", re.sub(r"\d", "9", value))


def strip_ns(tag: str) -> str:
    return tag.split("}", 1)[-1]


# 1. formList ------------------------------------------------------------------
fl = check(requests.get(f"{BASE}/formList", auth=AUTH), "formList")
(OUT / "formList.xml").write_bytes(fl.content)
fl_root = ET.fromstring(fl.content)
entry = None
for xform in fl_root.iter():
    if strip_ns(xform.tag) == "xform":
        fields = {strip_ns(c.tag): (c.text or "") for c in xform}
        if fields.get("formID") == FORM:
            entry = fields
print(f"[formList] form listed: {entry is not None}")
if entry:
    print(f"[formList] entry keys: {sorted(entry)}")
    for k in ("downloadUrl", "manifestUrl"):
        if entry.get(k):
            print(f"[formList] {k}: {shown(entry[k])}")

# 2. XML -------------------------------------------------------------------------
xml_url = f"{BASE}/forms/{FORM}/xml"
xml = check(requests.get(xml_url, auth=AUTH), "xml")
xml_path = OUT / f"{FORM}.xml"
xml_path.write_bytes(xml.content)
if entry and entry.get("downloadUrl"):
    dl = check(requests.get(entry["downloadUrl"], auth=AUTH), "downloadUrl")
    print(
        f"[xml] downloadUrl body identical to /forms/{{id}}/xml: {dl.content == xml.content}"
    )

root = ET.fromstring(xml.content)
bind_attrs = Counter()
publish_binds = {}
for el in root.iter():
    if strip_ns(el.tag) == "bind":
        for a, val in el.attrib.items():
            bind_attrs[a] += 1
            if "publish" in a.lower():
                publish_binds[el.attrib.get("nodeset")] = (a, val)
print(f"[xml] bind attribute names (count): {dict(bind_attrs)}")
print(f"[xml] binds with a publish* attribute: {len(publish_binds)}")
for nodeset, (a, val) in publish_binds.items():
    print(f"    {nodeset}  {a}={val!r}")
print(
    f"[xml] itext present: {any(strip_ns(e.tag) == 'itext' for e in root.iter())}; "
    f"languages: {[t.get('lang') or t.get('{http://www.w3.org/XML/1998/namespace}lang') for t in root.iter() if strip_ns(t.tag) == 'translation']}"
)
print(
    f"[xml] public key present: {any('base64RsaPublicKey' in a for e in root.iter() for a in e.attrib)}"
)

contract = parse_contract(xml_path)
types = Counter((n.xml_type, n.control) for n in contract.nodes.values() if n.is_leaf)
print(
    f"[contract] formid={contract.formid} version={contract.formdef_version} "
    f"leaves={sum(types.values())} repeats={contract.repeat_groups}"
)
print(f"[contract] (xml_type, control) counts: {dict(types)}")
print(
    f"[contract] data sources: {[n.data_source for n in contract.nodes.values() if n.data_source]}"
)

# 3. wide JSON with and without key ----------------------------------------------
wide_url = f"{BASE}/api/v2/forms/data/wide/json/{FORM}?date=1"
with open(ENV["SCTO_KEY_PATH"], "rb") as key:
    with_key = check(
        requests.post(wide_url, auth=AUTH, files={"private_key": key}),
        "wide+key",
    )
(OUT / "wide_with_key.json").write_bytes(with_key.content)
no_key = check(requests.get(wide_url, auth=AUTH), "wide-nokey")
(OUT / "wide_without_key.json").write_bytes(no_key.content)

rows_k, rows_n = with_key.json(), no_key.json()
print(f"[wide] rows with key={len(rows_k)}, without key={len(rows_n)}")
cols_k = list(dict.fromkeys(c for r in rows_k for c in r))
cols_n = list(dict.fromkeys(c for r in rows_n for c in r))
nonempty_k = Counter(c for r in rows_k for c, v in r.items() if v not in ("", None))
nonempty_n = Counter(c for r in rows_n for c, v in r.items() if v not in ("", None))
print(
    f"[wide] columns with key={len(cols_k)}, without key={len(cols_n)}, "
    f"only without key={[c for c in cols_n if c not in cols_k]}"
)
print(
    "[wide] column | system | map kind -> node | xml_type/control | in_nokey | nonempty key/nokey"
)
oracle = set()
for c in cols_k:
    sysc = is_system_column(c)
    m = {"kind": "system"} if sysc else contract.map_column(c)
    node = contract.nodes.get(m.get("node_path")) if m["kind"] == "matched" else None
    t = f"{node.xml_type}/{node.control}" if node else "-"
    print(
        f"    {c} | {sysc} | {m['kind']} -> {m.get('node_path', '-')} | {t} | "
        f"{c in cols_n} | {nonempty_k[c]}/{nonempty_n[c]}"
    )
    if node and c in cols_n:
        oracle.add(m["node_path"])
print(f"[oracle] fields present in both pulls: {sorted(oracle)}")
print(f"[oracle] fields with a publish* bind: {sorted(publish_binds)}")

for c in ("SubmissionDate", "CompletionDate", "starttime", "endtime"):
    masks = Counter(mask(r[c]) for r in rows_k if r.get(c))
    print(f"[format] {c}: {dict(masks)}")

summary = {
    "columns_with_key": cols_k,
    "columns_without_key": cols_n,
    "oracle": sorted(oracle),
    "publish_binds": sorted(publish_binds),
}
(OUT / "summary.json").write_text(json.dumps(summary, indent=2))
print(f"[done] raw pulls in {OUT}/ (git-ignored)")
