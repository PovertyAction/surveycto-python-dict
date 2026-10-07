"""flow.json: which form publishes each dataset, which forms read it, and the stages."""

import json

import pytest

from surveycto_python_dict.contract import form_entry, resolve_datasets
from surveycto_python_dict.flow import build_flow
from surveycto_python_dict.survey import render_flow
from surveycto_python_dict.xml_contract import parse_contract

# listing publishes `households`: hhid and village at the top, plot_size from a repeat.
_LISTING = """<?xml version="1.0"?>
<h:html xmlns="http://www.w3.org/2002/xforms" xmlns:h="http://www.w3.org/1999/xhtml"
        xmlns:jr="http://openrosa.org/javarosa">
  <h:head><model>
    <instance><listing id="listing" version="1">
      <hhid/><village/><zone/><plot jr:template=""><plot_size/></plot>
    </listing></instance>
    <bind nodeset="/listing/hhid" type="string"/>
    <bind nodeset="/listing/village" type="string"/>
    <bind nodeset="/listing/zone" type="select1"/>
    <bind nodeset="/listing/plot/plot_size" type="decimal"/>
  </model></h:head>
  <h:body>
    <input ref="/listing/hhid"><label>Household</label></input>
    <input ref="/listing/village"><label>Village</label></input>
    <select1 ref="/listing/zone" appearance="search('zones')"><label>Zone</label>
      <item><label>zone_name</label><value>zone_id</value></item>
    </select1>
    <group ref="/listing/plot"><label>Plot</label><repeat nodeset="/listing/plot">
      <input ref="/listing/plot/plot_size"><label>Size</label></input>
    </repeat></group>
  </h:body>
</h:html>
"""

# visit reads households (spelled Households) and zones, which it does not attach.
_VISIT = """<?xml version="1.0"?>
<h:html xmlns="http://www.w3.org/2002/xforms" xmlns:h="http://www.w3.org/1999/xhtml">
  <h:head><model>
    <instance><visit id="visit" version="1"><hh/><where/><area/></visit></instance>
    <bind nodeset="/visit/hh" type="select1"/>
    <bind nodeset="/visit/where" type="string" calculate="pulldata('Households', 'village', 'hhid',  /visit/hh )"/>
    <bind nodeset="/visit/area" type="string" calculate="pulldata('zones', 'zone_name', 'zone_id', '1')"/>
  </model></h:head>
  <h:body>
    <select1 ref="/visit/hh" appearance="search('Households')"><label>Household</label>
      <item><label>village</label><value>hhid</value></item>
    </select1>
  </h:body>
</h:html>
"""

_FORM_LIST = b"""<?xml version="1.0"?>
<xforms xmlns="http://openrosa.org/xforms/xformsList">
  <xform><formID>listing</formID><version>1</version>
    <datasets>{"datasets":[{"id":"zones"}]}</datasets><files>{"files":[]}</files></xform>
  <xform><formID>visit</formID><version>1</version>
    <datasets>{"datasets":[{"id":"households"}]}</datasets><files>{"files":[]}</files></xform>
</xforms>"""

_SERVER = [
    {"id": "households", "title": "Households", "uniqueRecordField": "hhid"},
    {"id": "zones", "title": "Zones", "uniqueRecordField": "zone_id"},
    {"id": "spare", "title": "Spare", "uniqueRecordField": None},
]

_CSVS = {
    "households": "hhid,village,plot_size,plot_size_1\nH1,North,0.5,0.5\nH2,South,1,1\n",
    "zones": "zone_id,zone_name\n1,Coast\n",
    "spare": "anything\nx\n",
}


def _flow(tmp_path, xmls, listed=()):
    datasets = tmp_path / "datasets"
    datasets.mkdir()
    for name, text in _CSVS.items():
        (datasets / f"{name}.csv").write_text(text, encoding="utf-8")
    forms = {}
    for form, xml in xmls.items():
        (tmp_path / f"{form}.xml").write_text(xml, encoding="utf-8")
        contract = parse_contract(tmp_path / f"{form}.xml")
        entry = form_entry(_FORM_LIST, form)
        sources = resolve_datasets(contract, entry, {d["id"] for d in _SERVER})
        forms[form] = (contract, sources)
    return build_flow(forms, _SERVER, listed, datasets, {})


@pytest.fixture
def flow(tmp_path):
    return _flow(tmp_path, {"listing": _LISTING, "visit": _VISIT}, ("spare",))


def test_publisher_is_the_form_holding_every_column(flow):
    households = next(d for d in flow["datasets"] if d["id"] == "households")
    assert households["published_by"] == "listing"
    assert households["publish_map"] == {
        "hhid": "hhid",
        "village": "village",
        "plot_size": "plot/plot_size",
        "plot_size_1": "plot/plot_size",
    }
    assert households["key"] == "hhid"
    assert households["rows"] == 2


def test_stages_follow_publish_then_read(flow):
    assert {f["form_id"]: (f["stage"], f["reads_from"]) for f in flow["forms"]} == {
        "listing": (1, []),
        "visit": (2, ["listing"]),
    }
    assert flow["forms"][0]["publishes"] == ["households"]


def test_external_and_listed_datasets(flow):
    by_id = {d["id"]: d for d in flow["datasets"]}
    assert by_id["zones"]["published_by"] is None
    assert by_id["zones"]["read_by"] == ["listing", "visit"]
    assert by_id["spare"]["read_by"] == []


def test_reads_name_the_fields_and_columns(flow):
    visit = {r["dataset"]: r for r in flow["reads"] if r["form"] == "visit"}
    assert visit["households"]["name_in_form"] == "Households"
    assert visit["households"]["attached"] is True
    assert visit["households"]["fields"] == [
        {"field": "hh", "via": "search", "columns": ["hhid", "village"]},
        {"field": "where", "via": "pulldata", "columns": ["village", "hhid"]},
    ]
    assert visit["zones"]["attached"] is False


def test_two_forms_covering_a_dataset_raise(tmp_path):
    twin = _LISTING.replace('id="listing"', 'id="visit"').replace(
        "/listing/", "/visit/"
    )
    twin = twin.replace("<listing ", "<visit ").replace("</listing>", "</visit>")
    with pytest.raises(ValueError, match="households"):
        _flow(tmp_path, {"listing": _LISTING, "visit": twin}, ("households",))


def test_render_flow_shows_stages_and_column_lineage(flow):
    text = render_flow(json.loads(json.dumps(flow)))
    assert "Stage 1:\n  listing\n    publishes: households" in text
    assert "  visit  (reads datasets published by listing)" in text
    assert "zones: 1 rows, key zone_id; external; read by listing, visit" in text
    assert "  visit reads zones (area)" in text
    lineage = render_flow(flow, "households")
    assert (
        "  village  <- listing.village  -> visit.hh (search), visit.where (pulldata)"
        in (lineage)
    )


# The same two forms with fewer reads: listing without its zones lookup, visit reading
# households only, or nothing.
_LISTING_PLAIN = _LISTING.replace(" appearance=\"search('zones')\"", "")
_VISIT_HOUSEHOLDS = _VISIT.replace(
    "pulldata('zones', 'zone_name', 'zone_id', '1')", "concat('zone', '1')"
)
_VISIT_PLAIN = _VISIT_HOUSEHOLDS.replace(
    "pulldata('Households', 'village', 'hhid',  /visit/hh )", "concat('a', 'b')"
).replace(" appearance=\"search('Households')\"", "")


@pytest.mark.parametrize(
    ("xmls", "listed", "publishers", "stages"),
    [
        pytest.param(
            {"listing": _LISTING_PLAIN}, (), {}, {"listing": 1}, id="1-form-0-datasets"
        ),
        pytest.param(
            {"listing": _LISTING},
            (),
            {"zones": None},
            {"listing": 1},
            id="1-form-1-dataset",
        ),
        pytest.param(
            {"listing": _LISTING},
            ("households",),
            {"households": "listing", "zones": None},
            {"listing": 1},
            id="1-form-2-datasets",
        ),
        pytest.param(
            {"listing": _LISTING_PLAIN, "visit": _VISIT_PLAIN},
            (),
            {},
            {"listing": 1, "visit": 1},
            id="2-forms-0-datasets",
        ),
        pytest.param(
            {"listing": _LISTING_PLAIN, "visit": _VISIT_HOUSEHOLDS},
            (),
            {"households": "listing"},
            {"listing": 1, "visit": 2},
            id="2-forms-1-dataset",
        ),
        pytest.param(
            {"listing": _LISTING, "visit": _VISIT},
            (),
            {"households": "listing", "zones": None},
            {"listing": 1, "visit": 2},
            id="2-forms-2-datasets",
        ),
    ],
)
def test_every_shape_of_forms_and_datasets(tmp_path, xmls, listed, publishers, stages):
    flow = _flow(tmp_path, xmls, listed)
    assert {d["id"]: d["published_by"] for d in flow["datasets"]} == publishers
    assert {f["form_id"]: f["stage"] for f in flow["forms"]} == stages
    assert render_flow(flow).startswith("Stage 1:")
