"""Stage 5: variable dictionary and variable graph."""

import json

import pytest
from farm_form import ROWS, SENTINELS, XML

from surveycto_python_dict.cast import cast_wide
from surveycto_python_dict.dictionary import build_dictionary, build_graph
from surveycto_python_dict.questions import build_questions
from surveycto_python_dict.xml_contract import parse_contract


@pytest.fixture
def built(tmp_path):
    path = tmp_path / "farm.xml"
    path.write_text(XML, encoding="utf-8")
    contract = parse_contract(path)
    questions = build_questions(contract)
    table = cast_wide(ROWS, contract)
    return table, questions, build_dictionary(table, questions, contract, SENTINELS)


@pytest.fixture
def variables(built):
    return built[2]["variables"]


def test_top_level_shape(built):
    table, questions, vardict = built
    assert set(vardict) == {"dataset", "summary", "variables"}
    assert vardict["dataset"] == {
        "name": "farm",
        "form_version": "3",
        "n_observations": 2,
        "n_variables": table.num_columns,
        "exclude": None,
        "n_excluded": 0,
    }
    assert list(vardict["variables"]) == table.schema.names


def test_summary(built):
    summary = built[2]["summary"]
    assert summary["n_questions"] == len(built[1])
    assert summary["n_system"] == 7
    assert summary["n_cast_fallback"] == 0
    assert summary["by_arrow_type"]["int8"] == 7  # tools x3 + plot_crop x2 x2


def test_entry_for_a_field(variables):
    area = variables["area"]
    assert area["variable_order"] == list(variables).index("area")
    assert area["non_null_count"] == 1
    assert area["missing_rate"] == 0.5
    assert area["is_all_missing"] is False
    assert area["data_min"] == 1.5 and area["data_max"] == 1.5
    assert area["parquet"] == {"arrow_type": "double", "xml_type": "decimal"}
    assert area["question"] == "area"
    assert area["survey"]["type"] == "decimal"
    assert area["survey"]["constraint"] == ". >= 0"
    assert area["survey"]["relevance"] == "${plots} > 0"
    assert area["survey"]["group_path"] == ""


def test_entry_for_a_choice_binary_in_a_repeat(variables):
    entry = variables["plot_crop_2_1"]
    assert entry["question"] == "plot_crop"
    assert entry["choice_value"] == "2"
    assert entry["repeat_iteration"] == [1]
    assert entry["survey"]["group_path"] == "plot"
    assert entry["survey"]["choices"][1] == {"value": "2", "label": "Rice"}


def test_entry_for_a_system_column(variables):
    key = variables["KEY"]
    assert key["system"] is True
    assert key["question"] is None
    assert key["survey"] is None
    assert key["parquet"] == {"arrow_type": "string", "xml_type": None}


def test_system_column_that_is_also_a_form_field_links_its_question(variables):
    """SurveyCTO's duration() calculate exports under the system name `duration`."""
    duration = variables["duration"]
    assert duration["system"] is True
    assert duration["parquet"]["arrow_type"] == "int64"
    assert duration["question"] == "duration"
    assert duration["survey"]["calculation"] == "duration()"


def test_columns_with_no_question_say_what_they_are(tmp_path):
    path = tmp_path / "farm.xml"
    path.write_text(XML, encoding="utf-8")
    contract = parse_contract(path)
    # intro is a note, empty in every row: dropped. old_field is no field of the form.
    table = cast_wide([dict(ROWS[0], old_field="x")], contract)
    assert "intro" not in table.schema.names
    vardict = build_dictionary(table, build_questions(contract), contract, SENTINELS)
    old = vardict["variables"]["old_field"]
    assert (old["question"], old["survey"], old["column_kind"]) == (
        None,
        None,
        "not_in_form",
    )
    summary = vardict["summary"]
    assert summary["columns_without_question"] == {"not_in_form": 1}
    assert summary["n_empty_notes_and_groups_dropped"] == 1


def test_strings_get_no_min_max(variables):
    assert "data_min" not in variables["owner"]
    assert "data_min" not in variables["crop"]


def test_dates_and_timestamps_min_max_iso(variables):
    assert variables["visit"]["data_min"] == "2025-03-06"
    assert variables["SubmissionDate"]["data_max"] == "2025-03-12T11:00:00+00:00"


def test_all_missing(variables):
    assert variables["plot_size_2"]["is_all_missing"] is True
    assert variables["plot_size_2"]["missing_rate"] == 1.0


def test_sentinels_counted_in_numeric_and_select_codes(variables):
    assert variables["plots"]["sentinels"] == {
        "-99": {"label": "Don't know", "count": 1}
    }
    assert "sentinels" not in variables["area"]


def test_cast_fallback_surfaces(tmp_path):
    path = tmp_path / "farm.xml"
    path.write_text(XML, encoding="utf-8")
    contract = parse_contract(path)
    table = cast_wide([dict(ROWS[0], area="about two")], contract)
    vardict = build_dictionary(table, build_questions(contract), contract, SENTINELS)
    assert vardict["variables"]["area"]["parquet"]["cast_fallback"] == (
        "1 of 1 values do not parse as double"
    )
    assert vardict["summary"]["n_cast_fallback"] == 1


def test_dictionary_is_json_serialisable(built):
    json.dumps(built[2])


# -- graph -------------------------------------------------------------------------


@pytest.fixture
def graph(built):
    return build_graph(built[1], built[2], {})


def _edges(graph, kind):
    return {(e["source"], e["target"]) for e in graph["edges"] if e["type"] == kind}


def test_graph_nodes_are_questions_with_their_columns(graph, built):
    nodes = {n["id"]: n for n in graph["nodes"]}
    assert set(nodes) == {q["variable_name"] for q in built[1]}
    assert nodes["tools"]["columns"] == ["tools_1", "tools_2", "tools_77"]
    assert nodes["plot_size"]["repeat_group"] == "plot"
    assert nodes["plot_size"]["repeat_depth"] == 1
    assert nodes["area"]["repeat_depth"] == 0


def test_graph_edges(graph):
    assert ("area", "per_plot") in _edges(graph, "calculates_from")
    assert ("plots", "per_plot") in _edges(graph, "calculates_from")
    assert ("plots", "area") in _edges(graph, "gated_by")
    assert ("plots", "plot_crop") in _edges(graph, "group_gated_by")
    assert ("plots", "plot_count") in _edges(graph, "calculates_from")
    # A repeat's members are in repeat_groups, not linked pairwise.
    assert _edges(graph, "repeat_sibling") == set()


def test_graph_repeat_groups(graph):
    assert graph["repeat_groups"] == {
        "plot": {
            "parent": None,
            "depth": 1,
            "count": "${plots}",
            "members": ["plot_crop", "plot_size"],
        }
    }


def test_graph_is_json_serialisable(graph):
    json.dumps(graph)


def test_choice_binary_value_labels_are_its_own(variables):
    """A select_multiple binary stands for one choice: 1 chosen, 0 not."""
    assert variables["tools_77"]["value_labels"] == [
        {"value": "1", "label": "Other"},
        {"value": "0", "label": "not chosen"},
    ]
    assert variables["tools_77"]["survey"]["choices"][2] == {
        "value": "77",
        "label": "Other",
    }


def test_select_one_value_labels_are_its_choices(variables):
    assert variables["crop"]["value_labels"] == [
        {"value": "01", "label": "Maize"},
        {"value": "02", "label": "Rice"},
    ]


def test_uncoded_columns_have_no_value_labels(variables):
    assert "value_labels" not in variables["area"]
    assert "value_labels" not in variables["KEY"]


def test_sentinels_take_the_question_label_and_dates_count():
    import datetime as dt

    import pyarrow as pa

    from surveycto_python_dict.dictionary import _sentinel_counts

    sentinels = {-88: "Refused", dt.date(1900, 1, 1): "Don't know"}
    codes = pa.chunked_array([["-88", "1", "-88"]])
    assert _sentinel_counts(codes, sentinels, {"-88": "Other (specify)"}) == {
        "-88": {"label": "Other (specify)", "count": 2}
    }
    days = pa.chunked_array([[dt.date(1900, 1, 1), dt.date(2020, 5, 1), None]])
    assert _sentinel_counts(days, sentinels, {}) == {
        "1900-01-01": {"label": "Don't know", "count": 1}
    }


def test_text_columns_report_their_longest_value(variables):
    owner = variables["owner"]
    # "Ana Maria Gomez Restrepo" is the only non-empty owner.
    assert (owner["max_length"], owner["at_max_length"]) == (24, 1)


def test_conditions_on_codes_a_select_lacks_are_listed(tmp_path):
    from surveycto_python_dict.dictionary import absent_codes

    path = tmp_path / "farm.xml"
    path.write_text(XML, encoding="utf-8")
    contract = parse_contract(path)
    questions = build_questions(contract)
    area = next(q for q in questions if q["variable_name"] == "area")
    area["relevance"] = "${crop} = '02' or ${crop} = '-99' or selected(${tools}, '77')"
    assert absent_codes(questions, contract) == [
        {"name": "area", "in": "relevance", "select": "crop", "code": "-99"}
    ]


def test_a_group_gated_on_a_code_the_select_lacks_is_listed_once(tmp_path):
    from surveycto_python_dict.dictionary import absent_codes

    path = tmp_path / "farm.xml"
    path.write_text(XML, encoding="utf-8")
    contract = parse_contract(path)
    questions = build_questions(contract)
    for q in questions:
        for gate in q["gates"]:
            if gate["group"] == "plot":
                gate["relevance"] = "${crop} != -99"
    assert absent_codes(questions, contract) == [
        {"name": "plot", "in": "group relevance", "select": "crop", "code": "-99"}
    ]


def test_the_codes_a_constraint_accepts():
    from surveycto_python_dict.dictionary import allowed_codes

    codes = {-99: "Don't know", -88: "Refused", -77: "N/A"}
    assert allowed_codes(None, codes) == [-99, -88, -77]
    assert allowed_codes(". >= 0 or . = -99", codes) == [-99]
    assert allowed_codes(". != -88", codes) == [-99, -77]
    assert allowed_codes(". <= ${plots}", codes) is None


def test_withheld_lists_count_the_codes_picked_and_those_not_in_the_list(tmp_path):
    path = tmp_path / "farm.xml"
    path.write_text(XML, encoding="utf-8")
    contract = parse_contract(path)
    questions = build_questions(contract)
    table = cast_wide(ROWS, contract)
    # Both lists withheld; their datasets now offer one code each.
    for q in questions:
        if q["variable_name"] in ("crop", "tools"):
            q["choices_withheld"] = ["presidio:PERSON"]
    contract.nodes["crop"].choice_universe = [{"value": "01", "label": "x"}]
    contract.nodes["tools"].choice_universe = [{"value": "1", "label": "x"}]
    summary = build_dictionary(table, questions, contract, SENTINELS)["summary"]
    assert summary["withheld_choices"] == {
        "crop": {
            "n_codes_picked": 2,
            "n_codes_not_in_list": 1,
            "codes_not_in_list": ["02"],
        },
        "tools": {
            "n_codes_picked": 1,
            "n_codes_not_in_list": 1,
            "codes_not_in_list": ["2"],
        },
    }
