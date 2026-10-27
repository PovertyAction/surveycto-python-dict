"""Stage 6: per-variable PII classification and the review table."""

import json
import re

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

    @property
    def supported_languages(self):
        return self.inner.supported_languages

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
    return classify(table, vardict, questions, config, spy, {}), spy


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
    assert phone["basis"] == ["type:text", "presidio:PHONE_NUMBER", "digits"]
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
    # concat(owner, phone) holds the blacklisted phone; the pulldata() keyed on it
    # holds its dataset column instead.
    assert blocks["tag"] == {"status": "contains_pii", "basis": ["lineage:phone"]}
    flagged = {c for c, b in blocks.items() if b["status"] == "contains_pii"}
    assert flagged == {"phone", "deviceid", "tag"}
    config = PiiConfig(no_personal_data=True, blacklist=("prev_round.owner",))
    blocks, _ = _classify(form, None, config)
    assert blocks["prev"] == {
        "status": "contains_pii",
        "basis": ["lineage:prev_round.owner"],
    }


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
        "phone",
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
    assert any("Villa Ficticia Uno" in t for t in spy.texts)


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
    # A calculate reading it carries the flag, whatever the project setting.
    carried = {"status": "contains_pii", "basis": ["lineage:my_user"]}
    assert blocks["tagged"] == carried
    rules_blocks, _ = _classify(form, analyzer)
    assert rules_blocks["tagged"] == carried
    # A reviewed whitelist outranks collection metadata, and then nothing carries.
    cleared, _ = _classify(
        form, None, PiiConfig(no_personal_data=True, whitelist=("my_user",))
    )
    assert cleared["my_user"] == {"status": "whitelisted", "basis": ["reviewed"]}
    assert cleared["tagged"] == {"status": "whitelisted", "basis": ["project"]}


def test_a_calculate_that_generates_its_value_is_whitelisted():
    from surveycto_python_dict.pii import _Rules

    def calc(name, calculation):
        return {
            "variable_name": name,
            "type": "calculate",
            "calculation": calculation,
            "references": None,
            "publishable": False,
            "appearance": None,
        }

    rules = _Rules(
        [
            calc("shop_id", "once(uuid(5))"),
            calc("coder", "enumerator-name()"),
            calc("slot", "position(..)"),
        ],
        set(),
        set(),
        False,
    )
    assert rules.static("shop_id") == {"status": "whitelisted", "basis": ["generated"]}
    # A function that reads the device or the user is not one that generates.
    assert rules.static("coder")["status"] == "contains_pii"
    # Reading no field through another function, it is scanned.
    assert rules.static("slot") is None


def test_calculate_over_geopoint_carries_contains_pii():
    from surveycto_python_dict.pii import _Rules

    questions = [
        {"variable_name": "gps", "type": "geopoint", "calculation": None},
        {
            "variable_name": "lat",
            "type": "calculate",
            "calculation": "selected-at(${gps}, 0)",
            "data_source": None,
        },
    ]
    assert _Rules(questions, set(), set(), True).carried("lat") == "gps"
    # With no_personal_data the geopoint is whitelisted, so it carries nothing.
    assert _Rules(questions, set(), set(), True, True).carried("lat") is None


def test_flagged_columns_lose_their_range_and_codes_not_in_list(form, analyzer):
    from surveycto_python_dict.pii import attach

    table, questions, vardict = form
    picked = {
        "n_codes_picked": 2,
        "n_codes_not_in_list": 1,
        "codes_not_in_list": ["02"],
    }
    vardict["summary"]["withheld_choices"] = {"crop": picked}
    config = PiiConfig(blacklist=("visit", "plots", "crop"))
    attach(vardict, _classify(form, analyzer, config)[0], questions, [])
    variables = vardict["variables"]
    for column in ("visit", "plots"):
        assert "data_min" not in variables[column]
        assert "data_max" not in variables[column]
    assert variables["area"]["data_min"] == 1.5
    assert picked == {"n_codes_picked": 2, "n_codes_not_in_list": 1}


@pytest.mark.parametrize(
    ("calculation", "holds"),
    [
        ("pulldata('people', 'phone', 'id', ${id})", True),
        ("coalesce(pulldata('people', 'phone', 'id', ${id}), ${phone})", True),
        ("not(empty(pulldata('people', 'phone', 'phone', ${phone})))", False),
        ("string-length(pulldata('people', 'phone', 'id', ${id}))", False),
        ("pulldata('people', 'phone', 'id', ${id}) = ''", False),
        # One call is tested, the other is the value.
        (
            "if(empty(pulldata('people', 'phone', 'id', ${id})), "
            "pulldata('people', 'phone_2', 'id', ${id}), '')",
            True,
        ),
    ],
)
def test_holds_pulldata(calculation, holds):
    from surveycto_python_dict.pii import _holds_pulldata

    assert _holds_pulldata(calculation) is holds


def _lineage_blocks(dataset_status):
    from surveycto_python_dict.pii import _inherit, _Rules

    source = {"kind": "pulldata", "dataset": "people", "column": "phone"}
    questions = [
        {
            "variable_name": "pl_phone",
            "type": "calculate",
            "calculation": "pulldata('people', 'phone', 'id', ${id})",
            "data_source": source,
        },
        {
            "variable_name": "has_phone",
            "type": "calculate",
            "calculation": "not(empty(pulldata('people', 'phone', 'id', ${id})))",
            "data_source": source,
        },
        {
            "variable_name": "phone_copy",
            "type": "calculate",
            "calculation": "${pl_phone}",
            "data_source": None,
        },
    ]
    names = [q["variable_name"] for q in questions]
    blocks = {n: {"status": "no_hits", "basis": ["type:calculate"]} for n in names}
    variables = {n: {"question": n} for n in names}
    _inherit(
        blocks,
        variables,
        _Rules(questions, set(), set(), True),
        {"people.phone": {"status": dataset_status}},
    )
    return blocks


def test_pulldata_and_its_readers_take_the_dataset_columns_flag():
    blocks = _lineage_blocks("may_contain_pii")
    assert blocks["pl_phone"] == {
        "status": "may_contain_pii",
        "basis": ["type:calculate", "lineage:people.phone"],
    }
    assert blocks["phone_copy"]["basis"] == ["type:calculate", "lineage:pl_phone"]
    # A test of the value holds no value.
    assert blocks["has_phone"]["status"] == "no_hits"


def test_a_clean_dataset_column_flags_nothing():
    blocks = _lineage_blocks("no_hits")
    assert {b["status"] for b in blocks.values()} == {"no_hits"}


class FindsName:
    """An analyzer that reports every "Jorge Perez" in a text as a person."""

    supported_languages = ["xx"]

    def __init__(self):
        self.texts: list[str] = []

    def analyze(self, text, language, entities, score_threshold):
        from presidio_analyzer import RecognizerResult

        self.texts.append(text)
        return [
            RecognizerResult("PERSON", m.start(), m.end(), 0.9)
            for m in re.finditer("Jorge Perez", text)
        ]


def test_zero_shot_reads_a_column_with_its_context():
    from surveycto_python_dict.pii import _CHUNK_CHARS, _column_entities

    analyzer = FindsName()
    values = ["Jorge Perez"] + [f"value {i}" for i in range(200)]
    found = _column_entities("Name of respondent", values, PiiConfig(), analyzer)
    assert found["Jorge Perez"] == {"PERSON"}
    assert {v for v, hits in found.items() if hits} == {"Jorge Perez"}
    # Several values per text, each text led by the context and within the limit.
    assert 1 < len(analyzer.texts) < len(values)
    assert all(t.startswith("Name of respondent: ") for t in analyzer.texts)
    assert all(len(t) <= _CHUNK_CHARS for t in analyzer.texts)


def test_a_hit_in_the_context_counts_for_no_value():
    from surveycto_python_dict.pii import _column_entities

    found = _column_entities("Jorge Perez", ["1", "2"], PiiConfig(), FindsName())
    assert found == {"1": set(), "2": set()}


def test_context_is_the_label_or_the_column_a_calculate_reads():
    from surveycto_python_dict.pii import _context

    question = {"type": "text", "question_text": "<b>Name</b> of respondent"}
    assert _context({**question, "data_source": None}, "name") == "Name of respondent"
    preload = {
        "type": "calculate",
        "question_text": "pulldata('people', 'full_name', 'id', ${id})",
        "data_source": {"kind": "pulldata", "column": "full_name"},
    }
    assert _context(preload, "pl_name") == "full_name"
    assert _context(None, "name_2") == "name_2"


def test_short_value_columns_are_sampled_and_free_text_read_whole():
    from surveycto_python_dict.pii import _SHORT_SAMPLE, _Scans

    scans = _Scans(PiiConfig())
    short = [f"Name {i}" for i in range(500)]
    notes = [f"we met at the market on day {i}" for i in range(500)]
    for name, values in {"short": short, "notes": notes}.items():
        scans.add(name, pa.chunked_array([values], pa.string()), [], [], name)
    assert len(scans.columns["short"][1]) == _SHORT_SAMPLE
    assert len(scans.columns["notes"][1]) == 500


def test_a_preload_takes_its_dataset_columns_status_without_a_scan():
    from surveycto_python_dict.pii import _held_column

    q = {
        "type": "calculate",
        "calculation": "pulldata('people', 'phone', 'id', ${id})",
        "data_source": {"kind": "pulldata", "dataset": "people", "column": "phone"},
    }
    assert _held_column(q, {"people.phone": {"status": "no_hits"}}) == "people.phone"
    # A column the dataset lacks, or a test of the value, holds nothing from it.
    assert _held_column(q, {}) is None
    tested = {**q, "calculation": f"empty({q['calculation']})"}
    assert _held_column(tested, {"people.phone": {"status": "no_hits"}}) is None


def test_a_phone_hit_needs_its_digits():
    from surveycto_python_dict.pii import _shaped

    assert _shaped("PHONE_NUMBER", "310 456 7890")
    assert not _shaped("PHONE_NUMBER", "Yes")
    assert not _shaped("PHONE_NUMBER", "1 3 5 11 12")
    assert _shaped("PERSON", "Jorge Perez")


def test_a_hit_on_a_category_does_not_count():
    from surveycto_python_dict.pii import _rare_hits

    values = ["Savings"] * 60 + ["Checking"] * 40
    found = {"Savings": {"PERSON"}, "Checking": set()}
    assert _rare_hits(found, values, sorted(set(values))) == {
        "Savings": set(),
        "Checking": set(),
    }
    # Mostly "no", with a few written comments: free text, so a name keeps its hit.
    comments = ["no"] * 90 + [
        "spoke with Jorge Perez about the plot",
        "the neighbour helped with the harvest this year",
        "asked to come back next week after lunch",
    ]
    hits = {v: set() for v in set(comments)} | {comments[90]: {"PERSON"}}
    assert _rare_hits(hits, comments, sorted(set(comments)))[comments[90]] == {"PERSON"}
    # A name or place in a column whose rare values repeat too reads as a category.
    cities = [f"City {i % 60}" for i in range(360)]
    hits = {v: {"LOCATION"} for v in set(cities)}
    assert not any(_rare_hits(hits, cities, sorted(set(cities))).values())
    # Free text keeps every hit: the same answer on many rows can still name someone.
    notes = ["I spoke with Jorge Perez at home"] * 60
    hits = {notes[0]: {"PERSON"}}
    assert _rare_hits(hits, notes, [notes[0]]) == hits


def test_a_code_built_from_the_date_and_random_numbers_is_generated():
    from surveycto_python_dict.pii import _Rules

    def calc(name, calculation, refs=(), source=None):
        return {
            "variable_name": name,
            "type": "calculate",
            "publishable": False,
            "appearance": None,
            "calculation": calculation,
            "references": list(refs),
            "data_source": source,
        }

    questions = [
        calc("year", "once(format-date-time(today(), '%y'))"),
        calc("digits", "substr(concat('00', int(once(random()) * 999)), -3, 5)"),
        calc("code", "concat('XX', ${year}, ${digits})", ["year", "digits"]),
        calc(
            "taken",
            "if(not(empty(pulldata('staff', 'code', 'code', ${code}))), 1, 0)",
            ["code"],
            {"kind": "pulldata", "dataset": "staff", "column": "code"},
        ),
        calc("new_code", "join-if('', ${code}, ${taken} = 0)", ["code", "taken"]),
    ]
    rules = _Rules(questions, set(), set(), True)
    for name in ("year", "digits", "code", "taken", "new_code"):
        assert rules.static(name)["status"] == "whitelisted", name


def test_a_datasets_system_columns_are_decided_by_rule(tmp_path, analyzer):
    from surveycto_python_dict.pii import dataset_codebook

    csv_path = tmp_path / "people.csv"
    csv_path.write_text(
        "KEY,SubmissionDate,deviceid,full_name\nuuid:1,2025-01-01,dev1,Jorge Perez\n",
        encoding="utf-8",
    )
    rows = dataset_codebook({"people": csv_path}, [], PiiConfig(), analyzer, {})
    status = {r["column"]: (r["pii"]["status"], r["pii"]["basis"]) for r in rows}
    assert status["KEY"] == ("whitelisted", ["system"])
    assert status["SubmissionDate"] == ("whitelisted", ["system"])
    assert status["deviceid"] == ("contains_pii", ["device:deviceid"])
    assert status["full_name"][1][0] == "dataset"


@pytest.mark.parametrize(
    ("values", "numbers"),
    [
        (
            ["3104567890", "+57 310 456 7891", None, "-99"],
            {"3104567890", "+57 310 456 7891"},
        ),
        (
            ["0241234567", "0541234567", "1023456789"],
            {"0241234567", "0541234567", "1023456789"},
        ),
        (["3104567890", "12", "7", "3"], set()),  # a minority
        (["2015-06-30", "30/06/2015", "20150630"], set()),  # dates
        (["15000000", "250000000", "1500000.00"], set()),  # amounts
        (["1234", "56789"], set()),  # short codes
    ],
)
def test_digit_rule(values, numbers):
    from surveycto_python_dict.pii import _digit_numbers

    assert _digit_numbers(values) == numbers
