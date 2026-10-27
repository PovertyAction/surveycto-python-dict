"""search() choice lists: read from the dataset, screened with Presidio before any output."""

import json

import pytest

from surveycto_python_dict import pii
from surveycto_python_dict.contract import DatasetTable, search_universe
from surveycto_python_dict.pii import PiiConfig, screen_search_choices
from surveycto_python_dict.questions import build_questions, search_choices
from surveycto_python_dict.xml_contract import parse_contract

_XML = """<?xml version="1.0"?>
<h:html xmlns="http://www.w3.org/2002/xforms" xmlns:h="http://www.w3.org/1999/xhtml">
  <h:head><model>
    <instance><shop id="shop" version="1"><size/><zone/><town/><buyer/><north/></shop></instance>
    <bind nodeset="/shop/size" type="select1"/>
    <bind nodeset="/shop/zone" type="select1"/>
    <bind nodeset="/shop/town" type="select1"/>
    <bind nodeset="/shop/buyer" type="select1"/>
    <bind nodeset="/shop/north" type="select1"/>
  </model></h:head>
  <h:body>
    <select1 ref="/shop/size" appearance="search('sizes')"><label>Size</label>
      <item><label>size_label</label><value>size_id</value></item>
      <item><label>Other</label><value>-66</value></item>
    </select1>
    <select1 ref="/shop/zone" appearance="search('towns')"><label>Zone</label>
      <item><label>zone_code</label><value>zone_code</value></item>
    </select1>
    <select1 ref="/shop/town" appearance="search('towns', 'matches', 'zone_code', '2')"><label>Town</label>
      <item><label>town_code</label><value>town_code</value></item>
    </select1>
    <select1 ref="/shop/buyer" appearance="search('buyers', 'matches', 'zone_code',  /shop/zone )"><label>Buyer</label>
      <item><label>buyer_name</label><value>buyer_id</value></item>
    </select1>
    <select1 ref="/shop/north" appearance="search('towns', 'contains', 'town_code', '1', 'zone_code', '2')"><label>North</label>
      <item><label>town_code</label><value>town_code</value></item>
    </select1>
  </h:body>
</h:html>
"""

_DATASETS = {
    "sizes.csv": "size_id,size_label\n1,small\n2,large\n",
    "towns.csv": "zone_code,town_code\n1,11\n1,12\n2,21\n2,22\n",
    "buyers.csv": "buyer_id,buyer_name,zone_code\n7,Ana Maria Gomez Restrepo,1\n8,Jorge Perez,2\n",
}


class Spy:
    """Record every text sent to the analyzer."""

    def __init__(self, inner):
        self.inner = inner
        self.texts: list[str] = []

    @property
    def nlp_engine(self):
        return self.inner.nlp_engine

    @property
    def supported_languages(self):
        return self.inner.supported_languages

    def analyze(self, text, language, **kwargs):
        self.texts.append(text)
        return self.inner.analyze(text=text, language=language, **kwargs)


@pytest.fixture
def form(tmp_path):
    (tmp_path / "shop.xml").write_text(_XML, encoding="utf-8")
    datasets = tmp_path / "datasets"
    datasets.mkdir()
    for name, text in _DATASETS.items():
        (datasets / name).write_text(text, encoding="utf-8")
    return tmp_path / "shop.xml", datasets


# Two submissions, both in zone 1: buyer's list is zone 1's buyers.
_ROWS = [{"zone": "1", "KEY": "uuid:a"}, {"zone": "1", "KEY": "uuid:b"}]


def _screen(form, analyzer, config=PiiConfig(), rows=_ROWS):
    xml, datasets = form
    contract = parse_contract(xml)
    spy = Spy(analyzer)
    files = {p.stem: p for p in datasets.glob("*.csv")}
    screen_search_choices(contract, files, config, spy, rows)
    return {q["variable_name"]: q for q in build_questions(contract)}, spy


def test_universe_keeps_literal_extras_and_applies_literal_filters(form):
    xml, datasets = form
    nodes = parse_contract(xml).nodes
    assert search_universe(nodes["size"], DatasetTable(datasets / "sizes.csv")) == [
        {"value": "1", "label": "small"},
        {"value": "2", "label": "large"},
        {"value": "-66", "label": "Other"},
    ]
    assert [
        c["value"]
        for c in search_universe(nodes["zone"], DatasetTable(datasets / "towns.csv"))
    ] == ["1", "2"]
    assert [
        c["value"]
        for c in search_universe(nodes["town"], DatasetTable(datasets / "towns.csv"))
    ] == ["21", "22"]
    # contains searches the first pair as a substring; the second pair is exact.
    assert [
        c["value"]
        for c in search_universe(nodes["north"], DatasetTable(datasets / "towns.csv"))
    ] == ["21"]
    # A filter on a field keeps the rows of the values the field takes, each
    # choice saying which.
    buyers = DatasetTable(datasets / "buyers.csv")
    assert search_universe(nodes["buyer"], buyers, {"zone_code": {"1", "2"}}) == [
        {"value": "7", "label": "Ana Maria Gomez Restrepo", "when": {"zone_code": "1"}},
        {"value": "8", "label": "Jorge Perez", "when": {"zone_code": "2"}},
    ]
    assert [
        c["value"]
        for c in search_universe(nodes["buyer"], buyers, {"zone_code": {"2"}})
    ] == ["8"]
    with pytest.raises(ValueError, match="zone_code"):
        search_universe(nodes["buyer"], buyers)


def test_a_label_naming_several_columns_joins_them(form):
    xml, datasets = form
    node = parse_contract(xml).nodes["size"]
    node.choice_items[0]["label"] = "size_label,size_id"
    assert search_universe(node, DatasetTable(datasets / "sizes.csv"))[:2] == [
        {"value": "1", "label": "small, 1"},
        {"value": "2", "label": "large, 2"},
    ]


def test_a_filter_on_a_field_keeps_the_rows_the_submissions_reach(form):
    questions, _ = _screen(form, NoHits(), PiiConfig(whitelist=("buyer",)))
    assert questions["buyer"]["choices"] == [
        {"value": "7", "label": "Ana Maria Gomez Restrepo"}
    ]


def test_search_filter_values_stay_out_of_questions_json(form):
    """A filter's values are the values a field takes in the submissions."""
    xml, datasets = form
    contract = parse_contract(xml)
    files = {p.stem: p for p in datasets.glob("*.csv")}
    rows = [{"zone": "1", "KEY": "uuid:a"}, {"zone": "2", "KEY": "uuid:b"}]
    screen_search_choices(contract, files, PiiConfig(), NoHits(), rows)
    questions = build_questions(contract)
    assert '"when"' not in json.dumps(questions)
    buyer = [r for r in search_choices(contract) if r["question"] == "buyer"]
    assert [(r["value"], r["when"], r["withheld"]) for r in buyer] == [
        ("7", '{"zone_code": "1"}', None),
        ("8", '{"zone_code": "2"}', None),
    ]


def test_a_filter_on_an_expression_withholds_the_list(form):
    xml, datasets = form
    xml.write_text(
        xml.read_text(encoding="utf-8").replace(
            "'zone_code',  /shop/zone )", "'zone_code', concat( /shop/zone , '0'))"
        ),
        encoding="utf-8",
    )
    questions, _ = _screen(form, NoHits())
    assert questions["buyer"]["choices_withheld"][0].startswith("filtered by concat(")


class NoHits:
    """An analyzer that finds nothing, for the clean path."""

    supported_languages = ["es"]

    class nlp_engine:  # noqa: N801
        @staticmethod
        def process_batch(texts, language, batch_size):
            return ((t, None) for t in texts)

    def analyze(self, text, language, **kwargs):
        return []


def test_clean_list_is_added(form):
    questions, _ = _screen(form, NoHits())
    assert questions["size"]["choices"] == [
        {"value": "1", "label": "small"},
        {"value": "2", "label": "large"},
        {"value": "-66", "label": "Other"},
    ]
    assert questions["size"]["choices_withheld"] is None


def test_any_hit_withholds_the_whole_list(form, analyzer):
    questions, _ = _screen(form, analyzer)
    # Only the literal items stay; the item naming the columns is recorded apart.
    assert questions["buyer"]["choices"] == []
    assert questions["buyer"]["choice_columns"] == {
        "value": "buyer_id",
        "label": "buyer_name",
    }
    assert questions["buyer"]["choices_withheld"] == ["presidio:PERSON"]


def test_whitelisted_list_is_added_unscanned(form, analyzer):
    rows = [{"zone": "1", "KEY": "uuid:a"}, {"zone": "2", "KEY": "uuid:b"}]
    questions, spy = _screen(form, analyzer, PiiConfig(whitelist=("buyer",)), rows)
    assert [c["value"] for c in questions["buyer"]["choices"]] == ["7", "8"]
    assert "Jorge Perez" not in spy.texts


def test_blacklisted_list_is_withheld_unscanned_and_keeps_every_choice(form, analyzer):
    xml, datasets = form
    contract = parse_contract(xml)
    spy = Spy(analyzer)
    files = {p.stem: p for p in datasets.glob("*.csv")}
    screen_search_choices(contract, files, PiiConfig(blacklist=("size",)), spy, _ROWS)
    node = contract.nodes["size"]
    assert node.choices_withheld == ["reviewed"]
    assert node.choice_items == [{"value": "-66", "label": "Other"}]
    # The parquet's binaries come from every choice, withheld or not.
    assert [c["value"] for c in node.all_choices] == ["1", "2", "-66"]
    assert "small" not in spy.texts


def test_a_withheld_list_keeps_every_choice_for_the_database(form, analyzer):
    xml, datasets = form
    contract = parse_contract(xml)
    files = {p.stem: p for p in datasets.glob("*.csv")}
    screen_search_choices(
        contract, files, PiiConfig(blacklist=("size",)), analyzer, _ROWS
    )
    rows = [r for r in search_choices(contract) if r["question"] == "size"]
    assert [(r["question"], r["value"], r["withheld"]) for r in rows] == [
        ("size", "1", "reviewed"),
        ("size", "2", "reviewed"),
        ("size", "-66", "reviewed"),
    ]


def test_no_item_naming_a_column_leaves_the_literals(form):
    xml, datasets = form
    node = parse_contract(xml).nodes["size"]
    node.choice_items = [i for i in node.choice_items if i["value"] != "size_id"]
    assert search_universe(node, DatasetTable(datasets / "sizes.csv")) == [
        {"value": "-66", "label": "Other"}
    ]


def test_list_over_the_cap_stays_in_its_dataset(form, analyzer, monkeypatch):
    monkeypatch.setattr(pii, "MAX_INLINE_CHOICES", 2)
    questions, spy = _screen(form, analyzer)
    assert questions["size"]["choices_withheld"] == ["3 choices"]
    assert "small" not in spy.texts
    assert [c["value"] for c in questions["town"]["choices"]] == ["21", "22"]


def test_name_on_both_lists_raises():
    with pytest.raises(ValueError, match="size"):
        PiiConfig(whitelist=("size",), blacklist=("size",))
