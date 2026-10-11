"""Summary statistics that follow skip logic and leave sentinel codes out."""

import csv
import json

import pyarrow as pa
import pytest
from farm_form import ROWS, SENTINELS, XML, write_pull

from surveycto_python_dict.cli import build, load_config
from surveycto_python_dict.dictionary import kept
from surveycto_python_dict.stats import COLUMNS, summary_stats, write_csv


def _build(tmp_path, analyzer, xml=XML, rows=ROWS):
    config = tmp_path / "config.toml"
    config.write_text('forms = ["farm"]\nout_dir = "out"\n', encoding="utf-8")
    cfg = load_config(config)
    write_pull(cfg, xml=xml, rows=rows)
    build(cfg, SENTINELS, analyzer)
    return cfg


@pytest.fixture
def stats(tmp_path, analyzer):
    cfg = _build(tmp_path, analyzer)
    return {
        row["variable"]: row
        for row in summary_stats(cfg.form_dir("farm"), "farm", SENTINELS)
    }


def test_sentinels_are_counted_apart_and_left_out_of_stats(stats):
    plots = stats["plots"]
    assert (plots["rows"], plots["eligible"], plots["answered"], plots["sentinel"]) == (
        2,
        2,
        1,
        1,
    )
    assert plots["sentinel_detail"] == "-99 Don't know: 1"
    assert (plots["n_valid"], plots["mean"], plots["min"], plots["max"]) == (1, 2, 2, 2)


def test_skip_logic_sets_the_denominator(stats):
    area = stats["area"]
    assert area["relevance"] == "${plots} > 0"
    assert (area["required"], stats["KEY"]["required"]) == (False, "")
    assert area["eligibility_evaluated"] is True
    # plots is -99 in row 2, so area is not asked there.
    assert (area["eligible"], area["skipped_by_logic"], area["answered"]) == (1, 1, 1)
    assert area["missing_when_eligible"] == 0
    assert area["mean"] == 1.5


def test_answered_when_skipped_is_flagged(stats):
    """Row 2 holds a plot answer although the plot group's relevance is false there."""
    cell = stats["plot_crop_2_2"]
    assert (
        cell["eligible"],
        cell["answered_when_skipped"],
        cell["missing_when_eligible"],
    ) == (
        1,
        1,
        1,
    )


def test_select_one_frequencies_with_labels(stats):
    assert stats["crop"]["frequencies"] == "01 Maize: 1 (50.0%); 02 Rice: 1 (50.0%)"


def test_select_multiple_binary_share_among_answered(stats):
    assert (stats["tools_2"]["answered"], stats["tools_2"]["selected"]) == (1, 1)
    assert stats["tools_2"]["selected_share"] == 1.0
    assert (stats["tools_1"]["selected"], stats["tools_1"]["selected_share"]) == (
        0,
        0.0,
    )


def test_text_gets_distinct_count_only(stats):
    assert stats["plot_code"]["n_distinct"] == 2
    assert stats["plot_code"]["frequencies"] == ""


def test_flagged_pii_columns_get_counts_only(stats):
    owner = stats["owner"]
    assert owner["pii_status"] == "may_contain_pii"
    assert owner["answered"] == 1
    assert (owner["n_distinct"], owner["frequencies"], owner["min"]) == ("", "", "")


def test_unsupported_relevance_falls_back_to_all_rows(tmp_path, analyzer):
    xml = XML.replace(
        'relevant=" /farm/plots  &gt; 0"/>\n    <bind nodeset="/farm/per_plot"',
        'relevant="today() &gt; 0"/>\n    <bind nodeset="/farm/per_plot"',
    )
    assert xml != XML
    cfg = _build(tmp_path, analyzer, xml)
    area = {
        r["variable"]: r for r in summary_stats(cfg.form_dir("farm"), "farm", SENTINELS)
    }["area"]
    assert area["eligibility_evaluated"] is False
    assert "today()" in area["eligibility_note"]
    assert area["eligible"] == 2


def test_csv_has_every_column_and_no_flagged_value(tmp_path, analyzer):
    cfg = _build(tmp_path, analyzer)
    path = write_csv(
        summary_stats(cfg.form_dir("farm"), "farm", SENTINELS),
        cfg.form_dir("farm") / "s.csv",
    )
    with path.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        assert reader.fieldnames == COLUMNS
        rows = list(reader)
    assert len(rows) == len(
        json.loads(
            (cfg.form_dir("farm") / "farm_variable_dictionary.json").read_text(
                encoding="utf-8"
            )
        )["variables"]
    )
    text = path.read_text(encoding="utf-8-sig")
    for value in ("Ana Maria Gomez Restrepo", "3104567890", "Jorge Perez", "LT-0042"):
        assert value not in text


# A third submission with one plot: instance 2 does not exist for it.
_ONE_PLOT = {
    "SubmissionDate": "Mar 13, 2025 9:00:00 AM",
    "plots": "1",
    "plot_size_1": "0.5",
    "plot_crop_1_1": "1",
    "KEY": "uuid:ccc",
}


def _by_name(cfg):
    return {
        r["variable"]: r for r in summary_stats(cfg.form_dir("farm"), "farm", SENTINELS)
    }


def test_repeat_instance_beyond_the_count_is_not_eligible(tmp_path, analyzer):
    stats = _by_name(_build(tmp_path, analyzer, rows=[*ROWS, _ONE_PLOT]))
    # Row 1 has plots=2 (instances 1, 2); row 2 plots=-99 (none); row 3 plots=1.
    assert (stats["plot_size_1"]["eligible"], stats["plot_size_2"]["eligible"]) == (
        2,
        1,
    )
    assert stats["plot_size_2"]["missing_when_eligible"] == 1  # row 1's second plot
    assert stats["plot_crop_1_1"]["answered"] == 2


def test_repeat_without_count_uses_the_instances_present(tmp_path, analyzer):
    xml = XML.replace(' jr:count=" /farm/plots "', "")
    assert xml != XML
    stats = _by_name(_build(tmp_path, analyzer, xml=xml, rows=[*ROWS, _ONE_PLOT]))
    # Row 1 filled instance 1 only; row 3 instance 1 only; row 2 has no plots (gate false).
    assert (stats["plot_size_1"]["eligible"], stats["plot_size_2"]["eligible"]) == (
        2,
        0,
    )


def test_integer_select_one_gets_frequencies_not_a_mean():
    from surveycto_python_dict.stats import _describe

    entry = {"parquet": {"arrow_type": "int64"}}
    q = {
        "type": "select_one",
        "choices": [{"value": "0", "label": "No"}, {"value": "1", "label": "Yes"}],
        "choices_withheld": None,
    }
    out = _describe([1, 1, 0], entry, q, is_binary=False)
    assert out == {"frequencies": "0 No: 1 (33.3%); 1 Yes: 2 (66.7%)"}


def test_a_group_condition_uses_its_own_repeat_index(tmp_path, analyzer):
    # plot's condition `index() <= 1` is about the plot, also for a parcel inside it.
    xml = (
        XML.replace(
            '<plot jr:template=""><plot_crop/><plot_size/></plot>',
            '<plot jr:template=""><plot_crop/><plot_size/>'
            '<parcel jr:template=""><parcel_size/></parcel></plot>',
        )
        .replace(
            '<bind nodeset="/farm/plot" relevant=" /farm/plots  &gt; 0"/>',
            '<bind nodeset="/farm/plot" relevant="index() &lt;= 1"/>'
            '<bind nodeset="/farm/plot/parcel/parcel_size" type="decimal"/>',
        )
        .replace(
            '<input ref="/farm/plot/plot_size"><label>Plot size</label></input>',
            '<input ref="/farm/plot/plot_size"><label>Plot size</label></input>'
            '<group ref="/farm/plot/parcel"><label>Parcel</label>'
            '<repeat nodeset="/farm/plot/parcel">'
            '<input ref="/farm/plot/parcel/parcel_size"><label>Parcel size</label></input>'
            "</repeat></group>",
        )
    )
    rows = [{**ROWS[0], "parcel_size_1_1": "1", "parcel_size_1_2": "2"}, ROWS[1]]
    stats = _by_name(_build(tmp_path, analyzer, xml=xml, rows=rows))
    second = stats["parcel_size_1_2"]
    assert (
        second["eligible"],
        second["answered"],
        second["answered_when_skipped"],
    ) == (
        1,
        1,
        0,
    )


def test_a_column_with_no_question_has_no_missing_count(tmp_path, analyzer):
    rows = [dict(ROWS[0], old_field="x"), ROWS[1]]
    stats = _by_name(_build(tmp_path, analyzer, rows=rows))
    assert stats["old_field"]["missing_when_eligible"] == ""


def test_a_date_sentinel_is_counted_apart(tmp_path, analyzer):
    import datetime as dt

    cfg = _build(tmp_path, analyzer)
    sentinels = {**SENTINELS, dt.date(2025, 3, 6): "Not reported"}
    visit = {
        r["variable"]: r for r in summary_stats(cfg.form_dir("farm"), "farm", sentinels)
    }["visit"]
    assert (visit["sentinel"], visit["sentinel_detail"], visit["min"]) == (
        1,
        "2025-03-06 Not reported: 1",
        "",
    )


def test_a_ragged_column_is_not_evaluated_and_not_mistaken_for_its_instance(
    tmp_path, analyzer
):
    # plot_size_1_3 has two indices for a depth-1 repeat: another form version.
    xml = XML.replace(' jr:count=" /farm/plots "', "")
    rows = [ROWS[0], {**ROWS[1], "plot_size_1_3": "4"}]
    cfg = _build(tmp_path, analyzer, xml=xml, rows=rows)
    stats = _by_name(cfg)
    dictionary = cfg.form_dir("farm") / "farm_variable_dictionary.json"
    assert (
        json.loads(dictionary.read_text(encoding="utf-8"))["summary"]["n_ragged"] == 1
    )
    ragged = stats["plot_size_1_3"]
    assert ragged["eligibility_evaluated"] is False
    assert (
        ragged["eligibility_note"] == "layout from another form version; not evaluated"
    )
    assert ragged["answered_when_skipped"] == ""
    assert ragged["missing_when_eligible"] == ""
    # Row 2 still has no plot instance: the ragged value is not plot_size_1's.
    assert stats["plot_size_1"]["eligible"] == 1


def test_excluded_submissions_are_left_out_of_counts_and_stats(tmp_path, analyzer):
    config = tmp_path / "config.toml"
    config.write_text(
        'forms = ["farm"]\nout_dir = "out"\n[submissions]\n'
        "exclude = \"KEY = 'uuid:bbb'\"\n",
        encoding="utf-8",
    )
    cfg = load_config(config)
    write_pull(cfg)
    build(cfg, SENTINELS, analyzer)
    dictionary = cfg.form_dir("farm") / "farm_variable_dictionary.json"
    data = json.loads(dictionary.read_text(encoding="utf-8"))
    assert (data["dataset"]["n_observations"], data["dataset"]["n_excluded"]) == (1, 1)
    stats = _by_name(cfg)
    assert stats["plots"]["rows"] == 1
    assert stats["plots"]["sentinel"] == 0  # the -99 was on the excluded row
    # The parquet keeps every submission.
    import pyarrow.parquet as pq

    assert pq.read_table(cfg.form_dir("farm") / "farm.parquet").num_rows == 2


def test_an_exclude_can_keep_the_latest_submission_of_each_entity():
    table = pa.table(
        {
            "KEY": ["a", "b", "c"],
            "hhid": ["1", "1", "2"],
            "SubmissionDate": [1, 2, 1],
        }
    )
    latest = kept(
        table,
        "row_number() OVER (PARTITION BY hhid ORDER BY SubmissionDate DESC) > 1",
    )
    assert latest.column("KEY").to_pylist() == ["b", "c"]


def test_an_exclude_that_does_not_parse_raises(tmp_path, analyzer):
    config = tmp_path / "config.toml"
    config.write_text(
        'forms = ["farm"]\nout_dir = "out"\n[submissions]\nexclude = "KEY = ="\n',
        encoding="utf-8",
    )
    cfg = load_config(config)
    write_pull(cfg)
    with pytest.raises(ValueError, match=r"\[submissions\] exclude"):
        build(cfg, SENTINELS, analyzer)


def test_reached_counts_the_submissions_a_question_with_no_column_reaches(tmp_path):
    from surveycto_python_dict.cast import cast_wide
    from surveycto_python_dict.dictionary import build_dictionary
    from surveycto_python_dict.questions import build_questions
    from surveycto_python_dict.stats import reached
    from surveycto_python_dict.xml_contract import parse_contract

    # Nobody answered visit or plot_size.
    rows = [
        {k: v for k, v in r.items() if k != "visit" and not k.startswith("plot_size")}
        for r in ROWS
    ]
    path = tmp_path / "farm.xml"
    path.write_text(XML, encoding="utf-8")
    contract = parse_contract(path)
    questions = build_questions(contract)
    table = cast_wide(rows, contract)
    vardict = build_dictionary(table, questions, contract, SENTINELS)
    found = reached(table, questions, vardict)
    assert found["visit"] == 2
    # The plot repeat is asked when plots > 0, which holds on one submission.
    assert found["plot_size"] == 1
    size = next(q for q in questions if q["variable_name"] == "plot_size")
    # index() is the plot instance: no submission has a third plot.
    size["gates"].append({"group": None, "relevance": "index() = 3", "repeat_depth": 1})
    assert reached(table, questions, vardict)["plot_size"] == 0
    size["gates"][-1]["relevance"] = "${nowhere} = 1"
    assert reached(table, questions, vardict)["plot_size"] is None
