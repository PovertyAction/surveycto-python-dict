"""flow.json: which form publishes each dataset, which forms read it, and the stages."""

import json

import pytest

from surveycto_python_dict.contract import form_entry, resolve_datasets
from surveycto_python_dict.flow import build_flow, describe_project
from surveycto_python_dict.survey import (
    render_flow,
    render_mermaid,
    render_project,
    upstream_lines,
)
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


def _flow(tmp_path, xmls, listed=(), csvs=None, published_by=None):
    datasets = tmp_path / "datasets"
    datasets.mkdir(parents=True)
    for name, text in {**_CSVS, **(csvs or {})}.items():
        (datasets / f"{name}.csv").write_text(text, encoding="utf-8")
    forms = {}
    for form, xml in xmls.items():
        (tmp_path / f"{form}.xml").write_text(xml, encoding="utf-8")
        contract = parse_contract(tmp_path / f"{form}.xml")
        entry = form_entry(_FORM_LIST, form)
        sources = resolve_datasets(contract, entry, {d["id"] for d in _SERVER})
        forms[form] = (contract, sources)
    return build_flow(forms, _SERVER, listed, datasets, {}, published_by)


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
        {
            "field": "hh",
            "via": "search",
            "columns": ["hhid", "village"],
            "match": [],
            "missing": [],
            "computed": None,
        },
        {
            "field": "where",
            "via": "pulldata",
            "columns": ["village", "hhid"],
            "match": ["hhid"],
            "missing": [],
            "computed": None,
        },
    ]
    assert visit["zones"]["attached"] is False


def test_a_read_of_a_column_the_dataset_lacks_is_flagged(tmp_path):
    visit = _VISIT.replace("'village', 'hhid'", "'villages', 'hhid'").replace(
        "pulldata('zones', 'zone_name', 'zone_id', '1')",
        "pulldata('zones', concat('zone_', 'name'), 'zone_id', '1')",
    )
    flow = _flow(tmp_path, {"listing": _LISTING, "visit": visit})
    reads = {
        (r["dataset"], f["field"]): f
        for r in flow["reads"]
        if r["form"] == "visit"
        for f in r["fields"]
    }
    assert reads["households", "where"]["missing"] == ["villages"]
    assert reads["zones", "area"]["computed"] == "concat('zone_', 'name')"
    assert reads["zones", "area"]["columns"] == ["zone_id"]
    assert "Comes from: households.villages (no such column)" in upstream_lines(
        flow, "visit", "where"
    )
    assert "  villages  no such column; read by visit.where (pulldata)" in render_flow(
        flow, "households"
    )


def test_two_forms_covering_a_dataset_raise(tmp_path):
    twin = _LISTING.replace('id="listing"', 'id="visit"').replace(
        "/listing/", "/visit/"
    )
    twin = twin.replace("<listing ", "<visit ").replace("</listing>", "</visit>")
    with pytest.raises(ValueError, match=r"\[datasets.households\] published_by"):
        _flow(tmp_path, {"listing": _LISTING, "visit": twin}, ("households",))


def test_a_reader_that_reasks_the_fields_does_not_publish(tmp_path):
    # visit reads households and has fields named like all its columns.
    visit = _VISIT.replace(
        "<hh/><where/><area/>",
        "<hh/><where/><area/><hhid/><village/><plot><plot_size/></plot>",
    )
    flow = _flow(tmp_path, {"listing": _LISTING, "visit": visit})
    households = next(d for d in flow["datasets"] if d["id"] == "households")
    assert households["published_by"] == "listing"


# households with every plot instance and one column no current field explains.
_WIDE = (
    "hhid,village,zone,plot_size,"
    + ",".join(f"plot_size_{i}" for i in range(1, 8))
    + ",{extra}\n"
)


def test_a_column_no_field_explains_is_legacy(tmp_path):
    csvs = {"households": _WIDE.format(extra="retired")}
    flow = _flow(tmp_path, {"listing": _LISTING, "visit": _VISIT}, csvs=csvs)
    households = next(d for d in flow["datasets"] if d["id"] == "households")
    assert households["published_by"] == "listing"
    assert households["legacy_columns"] == ["retired"]
    assert "retired" not in households["publish_map"]
    assert "; 1 columns no field of its current version" in render_flow(flow)
    assert "  retired  (no field of listing's current version)" in render_flow(
        flow, "households"
    )


def test_a_dataset_mostly_unexplained_is_external_unless_named(tmp_path):
    extra = ",".join(f"old_{i}" for i in range(4))
    csvs = {"households": _WIDE.format(extra=extra)}
    flow = _flow(tmp_path, {"listing": _LISTING, "visit": _VISIT}, csvs=csvs)
    households = next(d for d in flow["datasets"] if d["id"] == "households")
    assert households["published_by"] is None
    assert households["closest"] == {"form": "listing", "columns": 11}
    assert upstream_lines(flow, "visit", "where") == [
        "Comes from: households (external: no project form fills it; 11 of its 15 "
        "columns are fields of listing: set [datasets.households] published_by if "
        "it publishes it)"
    ]
    named = _flow(
        tmp_path / "named",
        {"listing": _LISTING, "visit": _VISIT},
        csvs=csvs,
        published_by={"households": "listing"},
    )
    households = next(d for d in named["datasets"] if d["id"] == "households")
    assert households["published_by"] == "listing"
    assert households["legacy_columns"] == [f"old_{i}" for i in range(4)]


def test_published_by_must_name_a_covering_form_and_a_read_dataset(tmp_path):
    xmls = {"listing": _LISTING, "visit": _VISIT}
    with pytest.raises(ValueError, match="no column is a field of visit"):
        _flow(tmp_path, xmls, published_by={"households": "visit"})
    with pytest.raises(ValueError, match="no server dataset nowhere"):
        _flow(tmp_path / "b", xmls, published_by={"nowhere": "listing"})


def test_render_flow_shows_stages_and_column_lineage(flow):
    text = render_flow(json.loads(json.dumps(flow)))
    assert "Stage 1:\n  listing\n    publishes: households" in text
    assert "  visit  (reads datasets published by listing)" in text
    assert "zones: 1 rows, key zone_id; external; read by listing, visit" in text
    assert "  visit reads zones (area)" in text
    lineage = render_flow(flow, "households")
    assert (
        "  village  <- listing.village (entered)  -> visit.hh (search), "
        "visit.where (pulldata)" in lineage
    )


def test_published_columns_carry_their_origin(tmp_path):
    # village filled from the zones lookup through a calculate: carried, not entered.
    listing = _LISTING.replace(
        '<bind nodeset="/listing/village" type="string"/>',
        '<bind nodeset="/listing/hhid_zone" type="string" '
        "calculate=\"pulldata('zones', 'zone_name', 'zone_id',  /listing/zone )\"/>"
        '<bind nodeset="/listing/village" type="string" '
        "calculate=\"concat( /listing/hhid_zone , '-')\"/>",
    ).replace("<hhid/>", "<hhid/><hhid_zone/>")
    flow = _flow(tmp_path, {"listing": listing, "visit": _VISIT})
    households = next(d for d in flow["datasets"] if d["id"] == "households")
    assert households["origins"]["village"] == {"kind": "carried", "from": ["zones"]}
    assert households["origins"]["hhid"] == {"kind": "entered", "from": []}
    assert households["origin_counts"] == {"entered": 3, "carried": 1}
    assert "published by listing (3 entered, 1 carried from zones)" in render_flow(flow)


def test_mermaid_colours_datasets_by_how_they_are_filled(flow):
    chart = render_mermaid(flow)
    assert chart.startswith("flowchart LR")
    assert '  d_households[("households<br/>2 rows<br/>4 entered")]' in chart
    assert "  class d_households entered" in chart
    assert "  class d_zones external" in chart
    assert "  f_listing -->|publishes| d_households" in chart
    # visit reads zones without attaching it: a dashed edge.
    assert "  d_zones -.->|1 fields, not attached| f_visit" in chart


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


def test_a_read_traces_back_to_the_publishing_field(flow):
    assert upstream_lines(flow, "visit", "where") == [
        "Comes from: households.village <- listing.village (entered)",
        "Comes from: households.hhid <- listing.hhid (entered)",
    ]
    assert upstream_lines(flow, "visit", "area") == [
        "Comes from: zones (external: no project form fills it)"
    ]
    assert upstream_lines(flow, "listing", "hhid") == []


def test_candidate_keys_say_where_each_column_lives(flow):
    keys = {k["column"]: k for k in flow["keys"]}
    assert keys["hhid"]["key_of"] == ["households"]
    assert keys["hhid"]["matched_by"] == 1
    assert keys["hhid"]["forms"] == {"listing": ["submissions"]}
    assert keys["hhid"]["datasets"] == ["households"]
    assert keys["zone_id"]["key_of"] == ["zones"]


def _contracts(tmp_path):
    contracts = {}
    for form, xml in {"listing": _LISTING, "visit": _VISIT}.items():
        (tmp_path / f"{form}-c.xml").write_text(xml, encoding="utf-8")
        contracts[form] = parse_contract(tmp_path / f"{form}-c.xml")
    return contracts


def test_project_entities_are_the_users_and_checked(flow, tmp_path):
    contracts = _contracts(tmp_path)
    entities = {
        "household": {"key": ["hhid"], "description": "A listed household"},
        "plot": {
            "key": ["plot_size"],
            "description": "A plot",
            "parent": "household",
        },
    }
    project = describe_project(flow, contracts, "A farm census.", entities)
    assert project["description"] == "A farm census."
    household, plot = project["entities"]
    assert household["forms"] == {"listing": ["submissions"]}
    assert household["datasets"] == ["households"]
    assert plot["forms"] == {"listing": ["plot"]}
    assert plot["parent"] == "household"
    flow = {**flow, "project": project}
    text = render_project(flow)
    assert text.startswith("About: A farm census.")
    assert "  household (hhid): A listed household" in text
    assert "  plot (plot_size): A plot; part of household" in text
    # A key the user named is no longer a candidate.
    assert "  hhid:" not in text
    assert "  zone_id: key of zones" in text


def test_project_entity_on_a_missing_column_or_parent_raises(flow, tmp_path):
    contracts = _contracts(tmp_path)
    with pytest.raises(ValueError, match="no form or dataset has a column 'nope'"):
        describe_project(
            flow, contracts, None, {"x": {"key": ["nope"], "description": ""}}
        )
    with pytest.raises(ValueError, match="parent 'ghost' is not an entity"):
        describe_project(
            flow,
            contracts,
            None,
            {"x": {"key": ["hhid"], "description": "", "parent": "ghost"}},
        )


def test_project_without_description_asks_for_one(flow):
    text = render_project({**flow, "project": {"description": None, "entities": []}})
    assert text.startswith("About: not described yet. Ask the user")
    assert "Entities: none named yet." in text


def test_an_entity_keyed_by_two_columns_lives_where_both_are(flow, tmp_path):
    contracts = _contracts(tmp_path)
    entities = {"stay": {"key": ["hhid", "village"], "description": "A stay"}}
    stay = describe_project(flow, contracts, None, entities)["entities"][0]
    assert stay["forms"] == {"listing": ["submissions"]}
    assert stay["datasets"] == ["households"]
    text = render_project(
        {**flow, "project": {"description": None, "entities": [stay]}}
    )
    assert "  stay (hhid, village): A stay" in text
    with pytest.raises(ValueError, match="no form or dataset has all of hhid, zone_id"):
        describe_project(
            flow,
            contracts,
            None,
            {"x": {"key": ["hhid", "zone_id"], "description": ""}},
        )
