"""Stage 5: variable dictionary and variable graph."""

import json

import pytest
from farm_form import ROWS, XML

from surveycto_python_dict.cast import cast_wide
from surveycto_python_dict.dictionary import build_dictionary, build_graph
from surveycto_python_dict.questions import build_questions
from surveycto_python_dict.xml_contract import parse_contract


@pytest.fixture
def built(tmp_path):
    path = tmp_path / "farm.xml"
    path.write_text(XML, encoding="utf-8")
    contract = parse_contract(path)
    questions = build_questions(path)
    table = cast_wide(ROWS, contract)
    return table, questions, build_dictionary(table, questions, contract)


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


def test_entry_for_a_column_whose_field_is_not_a_question(variables):
    """Notes are exported as (empty) columns but are not questions."""
    intro = variables["intro"]
    assert intro["question"] is None
    assert intro["survey"] is None
    assert intro["parquet"] == {"arrow_type": "string", "xml_type": "string"}


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
    vardict = build_dictionary(table, build_questions(path), contract)
    assert vardict["variables"]["area"]["parquet"]["cast_fallback"] == (
        "1 of 1 values do not parse as double"
    )
    assert vardict["summary"]["n_cast_fallback"] == 1


def test_dictionary_is_json_serialisable(built):
    json.dumps(built[2])


# -- graph -------------------------------------------------------------------------


@pytest.fixture
def graph(built):
    return build_graph(built[1], built[2])


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
    assert _edges(graph, "repeat_sibling") == {
        ("plot_crop", "plot_size"),
        ("plot_size", "plot_crop"),
    }


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
