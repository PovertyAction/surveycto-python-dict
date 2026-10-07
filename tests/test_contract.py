"""Stage 2: form-list entry and dataset discovery."""

import json

import pytest

from surveycto_python_dict.contract import (
    DatasetSource,
    UnresolvedDataset,
    form_entry,
    resolve_datasets,
)
from surveycto_python_dict.xml_contract import parse_contract

_DATASETS = {
    "datasets": [
        {
            "id": "roster",
            "title": "Roster",
            "version": 3,
            "timestamp": 1,
            "highestDuo": 0,
            "uniqueRecordField": "hhid",
            "discriminator": "DATASET",
        }
    ]
}
_FILES = {
    "files": [
        {"filename": "prices.csv", "timestamp": 1},
        {"filename": "logo.png", "timestamp": 1},
    ]
}


def _form_list(datasets: str, files: str) -> bytes:
    return f"""<?xml version="1.0"?>
<xforms xmlns="http://openrosa.org/xforms/xformsList">
  <xform><formID>other</formID><version>1</version><datasets></datasets><files>{{"files":[]}}</files></xform>
  <xform>
    <formID>shop</formID><name>Shop</name><version>7</version><hash>md5:x</hash>
    <downloadUrl>https://acme.surveycto.com/forms/shop/xml</downloadUrl>
    <datasets>{datasets}</datasets>
    <files>{files}</files>
  </xform>
</xforms>""".encode()


_XML_SOURCES = """<?xml version="1.0"?>
<h:html xmlns="http://www.w3.org/2002/xforms" xmlns:h="http://www.w3.org/1999/xhtml">
  <h:head><model>
    <instance><shop id="shop" version="7"><hhid/><price/><member/></shop></instance>
    <bind nodeset="/shop/hhid" type="string"/>
    <bind nodeset="/shop/price" type="string" calculate="pulldata('prices', 'price', 'item', /shop/hhid)"/>
    <bind nodeset="/shop/member" type="select1"/>
  </model></h:head>
  <h:body>
    <input ref="/shop/hhid"><label>Household</label></input>
    <select1 ref="/shop/member" appearance="search('roster', 'matches', 'hhid', /shop/hhid)">
      <label>Member</label>
      <item><label>name</label><value>member_id</value></item>
    </select1>
  </h:body>
</h:html>
"""


@pytest.fixture
def sources_contract(tmp_path):
    path = tmp_path / "shop.xml"
    path.write_text(_XML_SOURCES, encoding="utf-8")
    return parse_contract(path)


# -- form list entry ---------------------------------------------------------------


def test_form_entry_reads_datasets_and_files():
    entry = form_entry(_form_list(json.dumps(_DATASETS), json.dumps(_FILES)), "shop")
    assert entry.form_id == "shop"
    assert entry.version == "7"
    assert entry.dataset_ids == {"roster"}
    assert entry.filenames == {"prices.csv", "logo.png"}


def test_form_entry_empty_datasets_element():
    entry = form_entry(_form_list("", json.dumps(_FILES)), "shop")
    assert entry.dataset_ids == set()


def test_form_entry_unlisted_form_raises():
    with pytest.raises(KeyError, match="nope"):
        form_entry(_form_list("", json.dumps(_FILES)), "nope")


# -- dataset discovery -------------------------------------------------------------


def test_resolve_datasets_server_dataset_and_form_file(sources_contract):
    entry = form_entry(_form_list(json.dumps(_DATASETS), json.dumps(_FILES)), "shop")
    assert resolve_datasets(sources_contract, entry) == {
        "prices": DatasetSource(kind="form_file", ref="prices.csv"),
        "roster": DatasetSource(kind="server_dataset", ref="roster"),
    }


def test_resolve_datasets_unresolvable_raises_naming_it(sources_contract):
    entry = form_entry(_form_list("", json.dumps(_FILES)), "shop")
    with pytest.raises(UnresolvedDataset, match="roster"):
        resolve_datasets(sources_contract, entry)


_XML_NAMED = """<?xml version="1.0"?>
<h:html xmlns="http://www.w3.org/2002/xforms" xmlns:h="http://www.w3.org/1999/xhtml">
  <h:head><model>
    <instance><shop id="shop" version="7"><hhid/><roster_name/><member/><fruit/></shop></instance>
    <bind nodeset="/shop/hhid" type="string"/>
    <bind nodeset="/shop/roster_name" type="string" calculate="string('roster')"/>
    <bind nodeset="/shop/member" type="select1"/>
    <bind nodeset="/shop/fruit" type="select1"/>
  </model></h:head>
  <h:body>
    <select1 ref="/shop/member" appearance="search( /shop/roster_name , matches, 'hhid',  /shop/hhid )">
      <label>Member</label>
      <item><label>name</label><value>member_id</value></item>
    </select1>
    <select1 ref="/shop/fruit" appearance="search('prices.csv')">
      <label>Fruit</label>
      <item><label>label</label><value>item</value></item>
    </select1>
  </h:body>
</h:html>
"""


def test_resolve_datasets_named_by_field_with_suffix_and_case(tmp_path):
    path = tmp_path / "named.xml"
    path.write_text(_XML_NAMED, encoding="utf-8")
    files = {"files": [{"filename": "Prices.csv", "timestamp": 1}]}
    entry = form_entry(_form_list(json.dumps(_DATASETS), json.dumps(files)), "shop")
    assert resolve_datasets(parse_contract(path), entry) == {
        "prices": DatasetSource(kind="form_file", ref="Prices.csv"),
        "roster": DatasetSource(kind="server_dataset", ref="roster"),
    }


def test_dataset_named_by_a_non_literal_field_raises(tmp_path):
    path = tmp_path / "named.xml"
    path.write_text(
        _XML_NAMED.replace("string('roster')", "concat('ros', 'ter')"),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="roster_name"):
        parse_contract(path)


def test_resolve_datasets_none_referenced(tmp_path):
    path = tmp_path / "plain.xml"
    path.write_text(
        _XML_SOURCES.replace(
            "pulldata('prices', 'price', 'item', /shop/hhid)", "concat(/shop/hhid, 'x')"
        ).replace(
            " appearance=\"search('roster', 'matches', 'hhid', /shop/hhid)\"", ""
        ),
        encoding="utf-8",
    )
    entry = form_entry(_form_list("", json.dumps(_FILES)), "shop")
    assert resolve_datasets(parse_contract(path), entry) == {}


def test_questions_carry_the_dataset_join(sources_contract):
    from surveycto_python_dict.questions import build_questions

    by_name = {q["variable_name"]: q for q in build_questions(sources_contract)}
    assert by_name["price"]["data_source"] == {
        "kind": "pulldata",
        "dataset": "prices",
        "column": "price",
        "match": [{"column": "item", "value": "${hhid}"}],
    }
    assert by_name["member"]["data_source"] == {
        "kind": "search",
        "dataset": "roster",
        "match": [{"column": "hhid", "value": "${hhid}"}],
    }
    assert by_name["hhid"]["data_source"] is None
