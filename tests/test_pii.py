"""Stage 6: per-variable PII classification and the review table."""

import json

import pyarrow as pa
import pytest
from farm_form import ROWS, SENTINELS, XML

from surveycto_python_dict.cast import cast_wide
from surveycto_python_dict.dictionary import build_dictionary
from surveycto_python_dict.pii import (
    MissingSpacyModel,
    PiiConfig,
    build_analyzer,
    classify,
    warn_unknown_names,
)
from surveycto_python_dict.questions import build_questions
from surveycto_python_dict.xml_contract import parse_contract

_FICTIONAL = [
    "Ana Maria Gomez Restrepo",
    "3104567890",
    "3204561122",
    "Jorge Perez",
    "Villa Ficticia Uno",
]


class Spy:
    """Record every text sent to the analyzer."""

    def __init__(self, inner):
        self.inner = inner
        self.texts: list[str] = []

    @property
    def nlp_engine(self):
        return self.inner.nlp_engine

    def analyze(self, text, language, **kwargs):
        self.texts.append(text)
        return self.inner.analyze(text=text, language=language, **kwargs)


@pytest.fixture
def form(tmp_path):
    path = tmp_path / "farm.xml"
    path.write_text(XML, encoding="utf-8")
    contract = parse_contract(path)
    questions = build_questions(contract)
    table = cast_wide(ROWS, contract)
    return table, questions, build_dictionary(table, questions, contract, SENTINELS)


def _classify(form, analyzer, config=PiiConfig()):
    table, questions, vardict = form
    spy = Spy(analyzer)
    return classify(table, vardict, questions, config, spy), spy


@pytest.fixture
def blocks(form, analyzer):
    return _classify(form, analyzer)[0]


def test_every_column_gets_a_block(form, blocks):
    assert set(blocks) == set(form[0].schema.names)


def test_publishable_is_cleared_and_never_scanned(form, analyzer):
    blocks, spy = _classify(form, analyzer)
    assert blocks["village"] == {"status": "cleared_by_form", "basis": ["publishable"]}
    assert blocks["plots"]["status"] == "cleared_by_form"
    assert "Villa Ficticia Uno" not in spy.texts


def test_contains_pii_by_type_and_device(blocks):
    assert blocks["photo"] == {"status": "contains_pii", "basis": ["type:image"]}
    assert blocks["deviceid"] == {
        "status": "contains_pii",
        "basis": ["device:deviceid"],
    }


def test_whitelisted_by_type_choices_and_system(blocks):
    assert blocks["area"] == {"status": "whitelisted", "basis": ["type:decimal"]}
    assert blocks["crop"] == {"status": "whitelisted", "basis": ["choices:inline"]}
    assert blocks["tools_1"] == {"status": "whitelisted", "basis": ["choices:inline"]}
    assert blocks["KEY"] == {"status": "whitelisted", "basis": ["system"]}
    assert blocks["SubmissionDate"] == {"status": "whitelisted", "basis": ["system"]}


def test_dates_whitelisted_by_type(blocks):
    assert blocks["visit"] == {"status": "whitelisted", "basis": ["type:date"]}


def test_lineage(blocks):
    assert blocks["per_plot"] == {
        "status": "whitelisted",
        "basis": ["lineage:area,plots"],
    }
    assert blocks["tag"]["status"] == "may_contain_pii"  # concat(owner, phone)
    assert blocks["tag"]["basis"][0] == "type:calculate"
    assert blocks["prev"]["status"] == "may_contain_pii"  # pulldata()
    assert blocks["prev"]["basis"][:2] == ["type:calculate", "pulldata"]


def test_presidio_finds_names_and_phones(blocks):
    owner = blocks["owner"]
    assert owner["status"] == "may_contain_pii"
    assert owner["basis"] == ["type:text", "presidio:PERSON"]
    assert owner["presidio"] == {
        "entities": {"PERSON": 1},
        "distinct_scanned": 1,
        "hit_rows": 1,
        "hit_keys": ["uuid:aaa"],
    }
    phone = blocks["phone"]
    assert phone["basis"] == ["type:text", "presidio:PHONE_NUMBER"]
    assert phone["presidio"]["entities"] == {"PHONE_NUMBER": 2}
    assert phone["presidio"]["hit_keys"] == ["uuid:aaa", "uuid:bbb"]


def test_code_like_text_has_no_hits(blocks):
    assert blocks["plot_code"] == {
        "status": "no_hits",
        "basis": ["type:text"],
        "presidio": {
            "entities": {},
            "distinct_scanned": 2,
            "hit_rows": 0,
            "hit_keys": [],
        },
    }


def test_no_value_from_the_data_in_blocks(blocks):
    dumped = json.dumps(blocks)
    for value in _FICTIONAL:
        assert value not in dumped


def test_deterministic(form, analyzer):
    config = PiiConfig(max_distinct=1, seed=7)
    first, _ = _classify(form, analyzer, config)
    second, _ = _classify(form, analyzer, config)
    assert first == second
    assert first["phone"]["presidio"]["distinct_scanned"] == 1


def test_whitelist_is_reviewed_and_not_scanned(form, analyzer):
    blocks, spy = _classify(form, analyzer, PiiConfig(whitelist=("owner",)))
    assert blocks["owner"] == {"status": "whitelisted", "basis": ["reviewed"]}
    assert "Ana Maria Gomez Restrepo" not in spy.texts


def test_whitelisted_question_covers_its_columns_and_counts_for_lineage(form, analyzer):
    blocks, _ = _classify(form, analyzer, PiiConfig(whitelist=("owner", "phone")))
    assert blocks["tag"] == {"status": "whitelisted", "basis": ["lineage:owner,phone"]}


def test_unknown_name_warns(form, analyzer):
    config = PiiConfig(whitelist=("ghost", "owner"), blacklist=("roster.ghost",))
    with pytest.warns(UserWarning) as caught:
        warn_unknown_names(config, {"owner"})
    assert [str(w.message) for w in caught] == [
        "`ghost` cannot be found",
        "`roster.ghost` cannot be found",
    ]


def test_no_personal_data_keeps_blacklist_and_device_without_scanning(form):
    config = PiiConfig(no_personal_data=True, blacklist=("phone",))
    blocks, _ = _classify(form, None, config)
    assert blocks["phone"] == {"status": "contains_pii", "basis": ["reviewed"]}
    assert blocks["deviceid"] == {
        "status": "contains_pii",
        "basis": ["device:deviceid"],
    }
    assert blocks["owner"] == {"status": "whitelisted", "basis": ["project"]}
    flagged = {c for c, b in blocks.items() if b["status"] == "contains_pii"}
    assert flagged == {"phone", "deviceid"}


def test_missing_spacy_model_raises():
    with pytest.raises(MissingSpacyModel, match="xx_no_such_model"):
        build_analyzer(PiiConfig(languages={"xx": "xx_no_such_model"}))


def test_hit_keys_capped_at_five_with_the_full_count(analyzer):
    from surveycto_python_dict.pii import _Scans

    phones = [f"31045678{i:02d}" for i in range(8)]
    scans = _Scans(PiiConfig())
    scans.add(
        "phone",
        pa.chunked_array([phones]),
        [f"uuid:{i}" for i in range(8)],
        ["type:text"],
    )
    scans.run(analyzer)
    block = scans.block("phone")
    assert block["presidio"]["hit_rows"] == 8
    assert block["presidio"]["hit_keys"] == [f"uuid:{i}" for i in range(5)]


def test_blacklist_outranks_publishable_and_breaks_lineage(form, analyzer):
    blocks, spy = _classify(
        form, analyzer, PiiConfig(whitelist=("owner",), blacklist=("phone",))
    )
    assert blocks["phone"] == {"status": "contains_pii", "basis": ["reviewed"]}
    assert blocks["tag"]["status"] != "whitelisted"
    assert "3104567890" not in spy.texts


def test_blacklisted_publishable_question_is_contains_pii(form, analyzer):
    blocks, _ = _classify(form, analyzer)
    cleared = next(c for c, b in blocks.items() if b["status"] == "cleared_by_form")
    blocks, _ = _classify(form, analyzer, PiiConfig(blacklist=(cleared,)))
    assert blocks[cleared] == {"status": "contains_pii", "basis": ["reviewed"]}


def test_name_on_both_lists_raises(form, analyzer):
    with pytest.raises(ValueError, match="owner"):
        _classify(form, analyzer, PiiConfig(whitelist=("owner",), blacklist=("owner",)))


def test_untrusted_publishable_goes_through_the_other_rules(form, analyzer):
    blocks, spy = _classify(form, analyzer, PiiConfig(trust_publishable=False))
    assert blocks["plots"] == {
        "status": "whitelisted",
        "basis": ["type:integer", "publishable"],
    }
    assert blocks["village"]["status"] in ("may_contain_pii", "no_hits")
    assert blocks["village"]["basis"][0] == "type:text"
    assert blocks["village"]["basis"][-1] == "publishable"
    assert "Villa Ficticia Uno" in spy.texts


def test_blacklist_and_whitelist_still_rank_above_untrusted_publishable(form, analyzer):
    config = PiiConfig(trust_publishable=False, whitelist=("village",))
    blocks, _ = _classify(form, analyzer, config)
    assert blocks["village"] == {"status": "whitelisted", "basis": ["reviewed"]}


# Collection metadata under names of the form's own: a preload, a calculate calling
# enumerator-name(), and a calculate reading the preload.
_METADATA_XML = XML.replace("<photo/>", "<photo/><my_user/><coder/><tagged/>").replace(
    '<bind nodeset="/farm/photo" type="binary"/>',
    '<bind nodeset="/farm/photo" type="binary"/>'
    '<bind nodeset="/farm/my_user" type="string" jr:preload="property" '
    'jr:preloadParams="username"/>'
    '<bind nodeset="/farm/coder" type="string" calculate="concat(\'by \', '
    'enumerator-name())"/>'
    '<bind nodeset="/farm/tagged" type="string" '
    "calculate=\"concat( /farm/my_user , '-')\"/>",
)


def test_collection_metadata_is_personal_whatever_its_name(tmp_path, analyzer):
    path = tmp_path / "farm.xml"
    path.write_text(_METADATA_XML, encoding="utf-8")
    contract = parse_contract(path)
    questions = build_questions(contract)
    assert next(q for q in questions if q["variable_name"] == "my_user")["type"] == (
        "username"
    )
    rows = [dict(ROWS[0], my_user="u1", coder="by A", tagged="u1-")]
    table = cast_wide(rows, contract)
    vardict = build_dictionary(table, questions, contract, SENTINELS)
    form = (table, questions, vardict)
    blocks, _ = _classify(form, None, PiiConfig(no_personal_data=True))
    assert blocks["my_user"] == {
        "status": "contains_pii",
        "basis": ["metadata:username"],
    }
    assert blocks["coder"] == {
        "status": "contains_pii",
        "basis": ["calls:enumerator-name()"],
    }
    # A calculate reading it is not cleared by lineage: without no_personal_data it
    # is scanned.
    assert blocks["tagged"] == {"status": "whitelisted", "basis": ["project"]}
    rules_blocks, _ = _classify(form, analyzer)
    assert rules_blocks["tagged"]["basis"] == ["type:calculate"]
