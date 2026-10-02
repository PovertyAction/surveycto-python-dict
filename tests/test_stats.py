"""Summary statistics that follow skip logic and leave sentinel codes out."""

import csv
import json

import pytest
from farm_form import ROWS, XML

from surveycto_python_dict.cli import build, load_config
from surveycto_python_dict.stats import COLUMNS, summary_stats, write_csv


def _build(tmp_path, analyzer, xml=XML):
    config = tmp_path / "config.toml"
    config.write_text('form_id = "farm"\nout_dir = "out"\n', encoding="utf-8")
    cfg = load_config(config)
    cfg.raw_dir.mkdir(parents=True)
    (cfg.raw_dir / "form.xml").write_text(xml, encoding="utf-8")
    (cfg.raw_dir / "wide.json").write_text(json.dumps(ROWS), encoding="utf-8")
    build(cfg, analyzer)
    return cfg


@pytest.fixture
def stats(tmp_path, analyzer):
    cfg = _build(tmp_path, analyzer)
    return {
        row["variable"]: row
        for row in summary_stats(cfg.form_dir, "farm", cfg.sentinels)
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
    assert area["relevance"] == "(${plots} > 0)"
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
    assert stats["plot_code"]["distinct"] == 2
    assert stats["plot_code"]["frequencies"] == ""


def test_flagged_pii_columns_get_counts_only(stats):
    owner = stats["owner"]
    assert owner["pii_status"] == "may_contain_pii"
    assert owner["answered"] == 1
    assert (owner["distinct"], owner["frequencies"], owner["min"]) == ("", "", "")


def test_unsupported_relevance_falls_back_to_all_rows(tmp_path, analyzer):
    xml = XML.replace(
        'relevant=" /farm/plots  &gt; 0"/>\n    <bind nodeset="/farm/per_plot"',
        'relevant="today() &gt; 0"/>\n    <bind nodeset="/farm/per_plot"',
    )
    assert xml != XML
    cfg = _build(tmp_path, analyzer, xml)
    area = {
        r["variable"]: r for r in summary_stats(cfg.form_dir, "farm", cfg.sentinels)
    }["area"]
    assert area["eligibility_evaluated"] is False
    assert "today()" in area["eligibility_note"]
    assert area["eligible"] == 2


def test_csv_has_every_column_and_no_flagged_value(tmp_path, analyzer):
    cfg = _build(tmp_path, analyzer)
    path = write_csv(
        summary_stats(cfg.form_dir, "farm", cfg.sentinels), cfg.form_dir / "s.csv"
    )
    with path.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        assert reader.fieldnames == COLUMNS
        rows = list(reader)
    assert len(rows) == len(
        json.loads(
            (cfg.form_dir / "farm_variable_dictionary.json").read_text(encoding="utf-8")
        )["variables"]
    )
    text = path.read_text(encoding="utf-8-sig")
    for value in ("Ana Maria Gomez Restrepo", "3104567890", "Jorge Perez", "LT-0042"):
        assert value not in text
