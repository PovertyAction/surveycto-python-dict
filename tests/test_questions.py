"""Stage 3: questions.json and section slices rebuilt from the compiled XML."""

import json

import pytest

from surveycto_python_dict.questions import build_questions, write_sections
from surveycto_python_dict.xml_contract import parse_contract

# SurveyCTO's compiled shape: expressions use absolute paths wrapped in spaces,
# choices are inline <item>s, notes are read-only strings with original="note".
_XML = """<?xml version="1.0"?>
<h:html xmlns="http://www.w3.org/2002/xforms" xmlns:h="http://www.w3.org/1999/xhtml"
        xmlns:jr="http://openrosa.org/javarosa">
  <h:head>
    <h:title>Shop</h:title>
    <model>
      <instance>
        <shop id="shop" version="5">
          <starttime/><endtime/><deviceid/>
          <intro/>
          <consent/>
          <dur/>
          <ok>
            <owner/>
            <phone/>
            <n_staff/>
            <langs/>
            <photo/>
            <stock>
              <price/>
            </stock>
            <staff jr:template="">
              <staff_name/>
              <staff_age/>
            </staff>
          </ok>
          <meta><instanceID/></meta>
        </shop>
      </instance>
      <bind nodeset="/shop/starttime" type="dateTime" jr:preload="timestamp" jr:preloadParams="start"/>
      <bind nodeset="/shop/endtime" type="dateTime" jr:preload="timestamp" jr:preloadParams="end"/>
      <bind nodeset="/shop/deviceid" type="string" jr:preload="property" jr:preloadParams="deviceid"/>
      <bind nodeset="/shop/intro" type="string" readonly="true()" original="note"/>
      <bind nodeset="/shop/consent" type="select1" required="true()"/>
      <bind nodeset="/shop/dur" type="string" calculate="duration()"/>
      <bind nodeset="/shop/ok" relevant=" /shop/consent =1"/>
      <bind nodeset="/shop/ok/owner" type="string" required="true()"/>
      <bind nodeset="/shop/ok/phone" type="string" relevant="string-length( /shop/ok/owner ) &gt; 0"/>
      <bind nodeset="/shop/ok/n_staff" type="int" constraint=". &gt;= 0 and . &lt;= 50"/>
      <bind nodeset="/shop/ok/langs" type="select" publishable="true()"/>
      <bind nodeset="/shop/ok/photo" type="binary"/>
      <bind nodeset="/shop/ok/stock" relevant=" /shop/ok/n_staff  &gt; 0"/>
      <bind nodeset="/shop/ok/stock/price" type="decimal"/>
      <bind nodeset="/shop/ok/staff/staff_name" type="string"/>
      <bind nodeset="/shop/ok/staff/staff_age" type="int" relevant="0"/>
      <bind nodeset="/shop/meta/instanceID" type="string" readonly="true()" calculate="concat('uuid:', uuid())"/>
    </model>
  </h:head>
  <h:body>
    <input ref="/shop/intro"><label>Welcome</label></input>
    <select1 ref="/shop/consent"><label>Do you consent?</label>
      <item><label>Yes</label><value>1</value></item>
      <item><label>No</label><value>0</value></item>
    </select1>
    <group ref="/shop/ok"><label>Shop details</label>
      <input ref="/shop/ok/owner"><label>Owner name</label><hint>Full name</hint></input>
      <input ref="/shop/ok/phone"><label>Phone of <output value=" /shop/ok/owner "/>?</label></input>
      <input ref="/shop/ok/n_staff" appearance="numbers"><label>Staff count</label></input>
      <select ref="/shop/ok/langs"><label>Languages</label>
        <item><label>English</label><value>1</value></item>
        <item><label>Other</label><value>-77</value></item>
      </select>
      <upload ref="/shop/ok/photo" mediatype="image/*"><label>Photo</label></upload>
      <group ref="/shop/ok/stock" appearance="field-list"><label>Stock</label>
        <input ref="/shop/ok/stock/price"><label>Price</label></input>
      </group>
      <group ref="/shop/ok/staff"><label>Staff</label>
        <repeat nodeset="/shop/ok/staff" jr:count=" /shop/ok/n_staff ">
          <input ref="/shop/ok/staff/staff_name"><label>Staff name</label></input>
          <input ref="/shop/ok/staff/staff_age"><label>Staff age</label></input>
        </repeat>
      </group>
    </group>
  </h:body>
</h:html>
"""

_KEYS = [
    "type",
    "choice_list",
    "choices",
    "choices_per_submission",
    "variable_name",
    "question_text",
    "disabled",
    "constraint",
    "relevance",
    "group_relevances",
    "gates",
    "required",
    "calculation",
    "appearance",
    "references",
    "group_path",
    "publishable",
    "choices_withheld",
    "data_source",
    "question_order",
]


def _write(tmp_path, xml=_XML):
    path = tmp_path / "shop.xml"
    path.write_text(xml, encoding="utf-8")
    return path


@pytest.fixture
def questions(tmp_path):
    return build_questions(parse_contract(_write(tmp_path)))


@pytest.fixture
def by_name(questions):
    return {q["variable_name"]: q for q in questions}


def test_form_order_and_exclusions(questions):
    """Notes, start/end/deviceid and instanceID are not questions."""
    assert [q["variable_name"] for q in questions] == [
        "consent",
        "dur",
        "owner",
        "phone",
        "n_staff",
        "langs",
        "photo",
        "price",
        "staff_count",
        "staff_name",
        "staff_age",
    ]
    assert [q["question_order"] for q in questions] == list(range(len(questions)))


def test_every_question_has_the_schema_keys(questions):
    for q in questions:
        expected = _KEYS + (
            ["repeat_group_name"] if q["type"] == "repeat_count" else []
        )
        assert sorted(q) == sorted(expected), q["variable_name"]


def test_types(by_name):
    assert {n: q["type"] for n, q in by_name.items()} == {
        "consent": "select_one",
        "dur": "calculate",
        "owner": "text",
        "phone": "text",
        "n_staff": "integer",
        "langs": "select_multiple",
        "photo": "image",
        "price": "decimal",
        "staff_count": "repeat_count",
        "staff_name": "text",
        "staff_age": "integer",
    }


def test_select_choices_inline_with_own_list_name(by_name):
    consent = by_name["consent"]
    assert consent["choice_list"] == "consent"
    assert consent["choices"] == [
        {"value": "1", "label": "Yes"},
        {"value": "0", "label": "No"},
    ]
    assert by_name["langs"]["choices"][1] == {"value": "-77", "label": "Other"}
    assert by_name["owner"]["choice_list"] is None
    assert by_name["owner"]["choices"] is None


def test_label_output_becomes_ref(by_name):
    assert by_name["phone"]["question_text"] == "Phone of ${owner}?"


def test_calculate_without_label_uses_calculation(by_name):
    assert by_name["dur"]["question_text"] == "duration()"
    assert by_name["dur"]["calculation"] == "duration()"


def test_expressions_use_refs(by_name):
    assert by_name["phone"]["relevance"] == "string-length(${owner}) > 0"
    assert by_name["n_staff"]["constraint"] == ". >= 0 and . <= 50"
    assert by_name["price"]["group_relevances"] == ["${consent}=1", "${n_staff} > 0"]


def test_references(by_name):
    assert by_name["phone"]["references"] == ["owner"]
    assert by_name["owner"]["references"] is None


def test_group_path_and_relevances(by_name):
    assert by_name["consent"]["group_path"] == []
    assert by_name["consent"]["group_relevances"] == []
    assert by_name["owner"]["group_path"] == ["ok"]
    assert by_name["owner"]["group_relevances"] == ["${consent}=1"]
    assert by_name["price"]["group_path"] == ["ok", "stock"]
    assert by_name["staff_name"]["group_path"] == ["ok", "staff"]


def test_repeat_count_row(by_name):
    count = by_name["staff_count"]
    assert count["repeat_group_name"] == "staff"
    assert count["calculation"] == "${n_staff}"
    assert count["group_path"] == ["ok"]
    assert count["group_relevances"] == ["${consent}=1"]
    assert count["question_text"] == "Number of iterations in 'Staff' repeat group"


def test_publishable(by_name):
    assert by_name["langs"]["publishable"] is True
    assert by_name["owner"]["publishable"] is False
    assert by_name["staff_count"]["publishable"] is False


def test_required_disabled_appearance(by_name):
    assert by_name["owner"]["required"] is True
    assert by_name["phone"]["required"] is False
    assert by_name["staff_age"]["disabled"] is True
    assert by_name["staff_name"]["disabled"] is False
    assert by_name["n_staff"]["appearance"] == "numbers"


def test_unknown_field_shape_raises(tmp_path):
    xml = _XML.replace('type="binary"/>', 'type="geowhatever"/>').replace(
        '<upload ref="/shop/ok/photo" mediatype="image/*"><label>Photo</label></upload>',
        '<input ref="/shop/ok/photo"><label>Photo</label></input>',
    )
    with pytest.raises(ValueError, match="ok/photo"):
        build_questions(parse_contract(_write(tmp_path, xml)))


def test_unmapped_path_in_expression_raises(tmp_path):
    xml = _XML.replace(
        "string-length( /shop/ok/owner )", "string-length( /shop/ok/ghost )"
    )
    with pytest.raises(ValueError, match="ghost"):
        build_questions(parse_contract(_write(tmp_path, xml)))


_ITEXT = """<itext>
        <translation lang="french">
          <text id="owner:label"><value>Nom du proprietaire</value></text>
          <text id="consent-1:label"><value>Oui</value></text>
        </translation>
        <translation default="true()" lang="english">
          <text id="owner:label"><value>Owner of <output value=" /shop/ok/n_staff "/></value>
            <value form="image">jr://images/owner.jpg</value></text>
          <text id="consent-1:label"><value>Yes</value></text>
        </translation>
      </itext>
    </model>"""


def test_itext_labels_come_from_the_default_translation(tmp_path):
    xml = (
        _XML.replace("</model>", _ITEXT)
        .replace(
            "<label>Owner name</label>", "<label ref=\"jr:itext('owner:label')\"/>"
        )
        .replace("<label>Yes</label>", "<label ref=\"jr:itext('consent-1:label')\"/>")
    )
    by_name = {
        q["variable_name"]: q
        for q in build_questions(parse_contract(_write(tmp_path, xml)))
    }
    assert by_name["owner"]["question_text"] == "Owner of ${n_staff}"
    assert by_name["consent"]["choices"][0] == {"value": "1", "label": "Yes"}


# -- sections ----------------------------------------------------------------------


def test_write_sections_one_file_per_group_level(questions, tmp_path):
    paths = write_sections(questions, tmp_path / "sections")
    assert sorted(p.name for p in paths.values()) == [
        "section_ok.json",
        "section_ok_staff.json",
        "section_ok_stock.json",
        "section_ungrouped.json",
    ]
    ok = json.loads(paths["ok"].read_text(encoding="utf-8"))
    stock = json.loads(paths["ok/stock"].read_text(encoding="utf-8"))
    assert [q["variable_name"] for q in stock] == ["price"]
    assert "price" in [q["variable_name"] for q in ok]
    ungrouped = json.loads(paths["ungrouped"].read_text(encoding="utf-8"))
    assert [q["variable_name"] for q in ungrouped] == ["consent", "dur"]


def test_caseid_preload_is_typed_caseid(tmp_path):
    path = tmp_path / "cases.xml"
    path.write_text(
        """<?xml version="1.0"?>
<h:html xmlns="http://www.w3.org/2002/xforms" xmlns:h="http://www.w3.org/1999/xhtml"
        xmlns:jr="http://openrosa.org/javarosa">
  <h:head><model>
    <instance><visit id="visit" version="1"><caseid/></visit></instance>
    <bind nodeset="/visit/caseid" type="string" jr:preload="property" jr:preloadParams="caseid"/>
  </model></h:head>
  <h:body/>
</h:html>""",
        encoding="utf-8",
    )
    assert [
        (q["variable_name"], q["type"]) for q in build_questions(parse_contract(path))
    ] == [("caseid", "caseid")]


def test_compiled_repeat_count_calculate_is_the_count_entry(tmp_path):
    path = tmp_path / "counted.xml"
    path.write_text(
        """<?xml version="1.0"?>
<h:html xmlns="http://www.w3.org/2002/xforms" xmlns:h="http://www.w3.org/1999/xhtml"
        xmlns:jr="http://openrosa.org/javarosa">
  <h:head><model>
    <instance><visit id="visit" version="1">
      <go/><n_kids/><kids_count/><kids jr:template=""><kid_age/></kids>
    </visit></instance>
    <bind nodeset="/visit/go" type="select1"/>
    <bind nodeset="/visit/n_kids" type="int"/>
    <bind nodeset="/visit/kids_count" type="string" readonly="true()" calculate=" /visit/n_kids  + 1"/>
    <bind nodeset="/visit/kids" relevant=" /visit/go  = 1"/>
    <bind nodeset="/visit/kids/kid_age" type="int"/>
  </model></h:head>
  <h:body>
    <select1 ref="/visit/go"><label>Go?</label>
      <item><label>Yes</label><value>1</value></item>
    </select1>
    <input ref="/visit/n_kids"><label>Kids</label></input>
    <group ref="/visit/kids"><label>Kids</label>
      <repeat jr:count=" /visit/kids_count " nodeset="/visit/kids">
        <input ref="/visit/kids/kid_age"><label>Age</label></input>
      </repeat>
    </group>
  </h:body>
</h:html>""",
        encoding="utf-8",
    )
    questions = build_questions(parse_contract(path))
    counts = [q for q in questions if q["variable_name"] == "kids_count"]
    assert len(counts) == 1
    assert counts[0]["type"] == "repeat_count"
    assert counts[0]["relevance"] is None
    assert counts[0]["calculation"] == "${n_kids} + 1"


def test_appearance_paths_are_written_as_names(tmp_path):
    xml = _XML.replace(
        'appearance="numbers"',
        "appearance=\"search('roster', 'matches', 'id',  /shop/ok/owner )\"",
    )
    by_name = {
        q["variable_name"]: q
        for q in build_questions(parse_contract(_write(tmp_path, xml)))
    }
    assert (
        by_name["n_staff"]["appearance"]
        == "search('roster', 'matches', 'id', ${owner})"
    )
