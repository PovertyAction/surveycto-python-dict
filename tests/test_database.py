"""<form>.duckdb: views over the parquet, repeats long, descriptions as tables."""

import json

import duckdb
import pytest
from farm_form import SENTINELS, write_pull

from surveycto_python_dict.cli import build, load_config
from surveycto_python_dict.database import write_database
from surveycto_python_dict.stats import summary_stats, write_csv


def _datasets(form_dir):
    return {"prev_round": form_dir / "raw" / "files" / "prev_round.csv"}


@pytest.fixture
def database(tmp_path, analyzer):
    config = tmp_path / "config.toml"
    config.write_text(
        'forms = ["farm"]\nout_dir = "out"\n\n[pii]\n'
        'whitelist = ["prev_round.plot_code"]\nblacklist = ["prev_round.owner"]\n',
        encoding="utf-8",
    )
    cfg = load_config(config)
    write_pull(cfg, "phone,owner,plot_code\n3000000000,Fictional Owner,007\n")
    build(cfg, SENTINELS, analyzer)
    form_dir = cfg.form_dir("farm")
    write_csv(
        summary_stats(form_dir, "farm", SENTINELS), form_dir / "farm_summary_stats.csv"
    )
    path = write_database(form_dir, "farm", _datasets(form_dir), cfg.dataset_keys)
    con = duckdb.connect(str(path), read_only=True)
    yield con, form_dir
    con.close()


def test_views_and_tables(database):
    con, _ = database
    objects = con.sql(
        "SELECT table_schema, table_name, table_type FROM information_schema.tables "
        "ORDER BY ALL"
    ).fetchall()
    assert [(f"{s}.{n}" if s != "main" else n, t) for s, n, t in objects] == [
        ("dataset.prev_round", "VIEW"),
        ("dataset.prev_round_safe", "VIEW"),
        ("choices", "BASE TABLE"),
        ("dataset_codebook", "BASE TABLE"),
        ("dataset_links", "BASE TABLE"),
        ("dictionary", "BASE TABLE"),
        ("edges", "BASE TABLE"),
        ("plot", "VIEW"),
        ("plot_safe", "VIEW"),
        ("questions", "BASE TABLE"),
        ("stats", "BASE TABLE"),
        ("submissions", "VIEW"),
        ("submissions_safe", "VIEW"),
    ]


def test_submissions_reads_the_parquet(database):
    con, form_dir = database
    assert con.sql("SELECT KEY FROM submissions ORDER BY KEY").fetchall() == [
        ("uuid:aaa",),
        ("uuid:bbb",),
    ]
    columns = con.sql("SELECT * FROM submissions LIMIT 0").columns
    assert (
        columns
        == con.sql('SELECT "column" FROM dictionary ORDER BY variable_order')
        .df()["column"]
        .tolist()
    )


def test_safe_view_has_only_cleared_columns(database):
    con, _ = database
    safe = con.sql("SELECT * FROM submissions_safe LIMIT 0").columns
    cleared = (
        con.sql(
            """SELECT "column" FROM dictionary
           WHERE pii_status IN ('cleared_by_form', 'whitelisted') ORDER BY variable_order"""
        )
        .df()["column"]
        .tolist()
    )
    assert safe == cleared
    assert "owner" not in safe


def test_repeat_is_long_with_one_row_per_answered_instance(database):
    con, _ = database
    rows = con.sql("SELECT KEY, plot_index, plot_size FROM plot").fetchall()
    assert rows == [("uuid:aaa", 1, 0.75), ("uuid:bbb", 2, None)]
    columns = con.sql("SELECT * FROM plot LIMIT 0").columns
    assert columns[:2] == ["KEY", "plot_index"]
    assert "plot_size" in columns
    assert all(not c.endswith(("_1_1", "_2_2")) for c in columns)
    # A select_multiple binary keeps its choice code: plot_crop_1_1 -> plot_crop_1.
    assert con.sql(
        "SELECT plot_crop_1 FROM plot WHERE KEY = 'uuid:aaa'"
    ).fetchone() == (1,)


def test_repeat_no_submission_reached_is_its_keys_without_rows():
    import pyarrow as pa

    from surveycto_python_dict.database import _long_table

    wide = pa.table({"KEY": ["uuid:aaa"], "area": [1.5]})
    long = _long_table(wide, {}, ["KEY", "hh_index", "member_index"])
    assert long.num_rows == 0
    assert long.schema.names == ["KEY", "hh_index", "member_index"]


def test_repeat_safe_view_drops_flagged_columns(database):
    con, _ = database
    flagged = con.sql(
        """SELECT DISTINCT question FROM dictionary
           WHERE repeat_group = 'plot' AND pii_status NOT IN ('cleared_by_form', 'whitelisted')"""
    ).fetchall()
    safe = con.sql("SELECT * FROM plot_safe LIMIT 0").columns
    assert safe[:2] == ["KEY", "plot_index"]
    assert not {q for (q,) in flagged} & set(safe)


def test_choices_and_edges(database):
    con, _ = database
    assert con.sql(
        "SELECT value, label FROM choices WHERE question = 'crop' ORDER BY value"
    ).fetchall()
    assert con.sql("SELECT count(*) FROM edges").fetchone()[0] > 0


def test_rebuild_replaces_the_file(database):
    con, form_dir = database
    con.close()
    write_database(form_dir, "farm", _datasets(form_dir), {})
    again = duckdb.connect(str(form_dir / "farm.duckdb"), read_only=True)
    assert again.sql("SELECT count(*) FROM submissions").fetchone() == (2,)
    again.close()


def test_dataset_sits_next_to_the_survey_as_text(database):
    con, _ = database
    assert con.sql("SELECT plot_code, owner FROM dataset.prev_round").fetchall() == [
        ("007", "Fictional Owner")
    ]


def test_dataset_links_spell_out_the_join(database):
    con, _ = database
    assert con.sql(
        "SELECT question, kind, dataset, dataset_column, match_column, match_value FROM dataset_links"
    ).fetchall() == [("prev", "pulldata", "prev_round", "owner", "phone", "${phone}")]


def test_dataset_codebook_lists_readers_and_status(database):
    con, _ = database
    rows = con.sql(
        """SELECT "column", rows, non_null, read_by, pii_status, pii_basis
           FROM dataset_codebook WHERE dataset = 'prev_round'"""
    ).fetchall()
    by_column = {r[0]: r[1:] for r in rows}
    assert by_column["owner"] == (1, 1, ["prev"], "contains_pii", ["reviewed"])
    assert by_column["plot_code"] == (1, 1, [], "whitelisted", ["reviewed"])
    # Read by the pulldata() as its match column; scanned, as nobody reviewed it.
    assert by_column["phone"][2] == ["prev"]
    assert by_column["phone"][3] in ("may_contain_pii", "no_hits")


def test_dataset_safe_view_has_row_and_whitelisted_columns(database):
    con, _ = database
    assert con.sql("SELECT * FROM dataset.prev_round_safe").fetchall() == [(1, "007")]
    assert con.sql("SELECT * FROM dataset.prev_round_safe LIMIT 0").columns == [
        "row",
        "plot_code",
    ]


def test_structure_comes_from_the_finished_database(database):
    from surveycto_python_dict.survey import SurveyStore

    con, form_dir = database
    objects = json.loads((form_dir / "farm_db.json").read_text(encoding="utf-8"))
    names = [o["name"] for o in objects]
    # Every object in the file, and only those.
    in_file = {
        n if s == "main" else f"{s}.{n}"
        for s, n in con.sql(
            "SELECT table_schema, table_name FROM information_schema.tables"
        ).fetchall()
    }
    assert set(names) == in_file
    plot = next(o for o in objects if o["name"] == "plot_safe")
    assert plot["kind"] == "view"
    assert plot["key"] == ["KEY", "plot_index"]
    assert plot["joins"] == ["plot_safe.KEY = submissions_safe.KEY  (many to one)"]
    assert {"name": "plot_index", "type": "INTEGER"} in plot["columns"]
    prev = next(o for o in objects if o["name"] == "dataset.prev_round_safe")
    assert prev["joins"] == [
        "dataset.prev_round_safe.phone = submissions_safe.phone  "
        "(many to one: the column is unique in the dataset)"
    ]

    out = SurveyStore(form_dir, "farm").get_structure_db()
    assert "-- view plot_safe: one row per instance of the repeat plot" in out
    assert "--   join: plot_safe.KEY = submissions_safe.KEY  (many to one)" in out
    assert "-- table dictionary: one row per submissions column" in out
    assert (form_dir / "farm_db.sql").read_text(encoding="utf-8") == out + "\n"


def test_declared_dataset_key_is_checked_and_named(database):
    con, form_dir = database
    con.close()
    write_database(form_dir, "farm", _datasets(form_dir), {"prev_round": ["phone"]})
    objects = json.loads((form_dir / "farm_db.json").read_text(encoding="utf-8"))
    prev = next(o for o in objects if o["name"] == "dataset.prev_round")
    assert prev["key"] == ["phone"]
    assert prev["grain"] == "one row per phone"
    _datasets(form_dir)["prev_round"].write_text(
        "phone,owner,plot_code\n3000000000,Fictional Owner,007\n"
        "3000000001,Fictional Owner,008\n",
        encoding="utf-8",
    )
    with pytest.raises(AssertionError, match="repeats"):
        write_database(form_dir, "farm", _datasets(form_dir), {"prev_round": ["owner"]})


def test_a_match_column_missing_from_the_dataset_is_reported(database):
    con, form_dir = database
    con.close()
    codebook_path = form_dir / "farm_dataset_codebook.json"
    codebook = json.loads(codebook_path.read_text(encoding="utf-8"))
    codebook_path.write_text(
        json.dumps([c for c in codebook if c["column"] != "phone"]), encoding="utf-8"
    )
    write_database(form_dir, "farm", _datasets(form_dir), {})
    objects = json.loads((form_dir / "farm_db.json").read_text(encoding="utf-8"))
    prev = next(o for o in objects if o["name"] == "dataset.prev_round")
    assert prev["joins"] == [
        "dataset.prev_round.phone = ${phone}  (phone is not a column of prev_round: prev finds no row)"
    ]
