"""<project>.duckdb: a schema per form, datasets once, the project tables in main."""

import json

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from farm_form import ROWS, SENTINELS, write_pull

from surveycto_python_dict.cli import (
    SEARCH_CHOICES,
    build,
    dataset_csvs,
    load_config,
)
from surveycto_python_dict.database import write_database
from surveycto_python_dict.stats import summary_stats, write_csv

_PREV = 'dataset."farm/prev_round.csv"'


def _write(cfg, keys=None):
    flow = json.loads((cfg.out_dir / "flow.json").read_text(encoding="utf-8"))
    return write_database(
        cfg.out_dir,
        cfg.project,
        list(cfg.forms),
        dataset_csvs(cfg, flow),
        cfg.dataset_keys if keys is None else keys,
    )


@pytest.fixture
def project(tmp_path, analyzer):
    config = tmp_path / "config.toml"
    config.write_text(
        'forms = ["farm"]\nout_dir = "farmproject"\n\n[pii]\n'
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
    return cfg


@pytest.fixture
def database(project):
    con = duckdb.connect(str(_write(project)), read_only=True)
    yield con, project
    con.close()


def test_one_file_named_after_the_project(project):
    assert _write(project) == project.out_dir / "farmproject.duckdb"


def test_schemas_views_and_tables(database):
    con, _ = database
    objects = con.sql(
        "SELECT table_schema, table_name, table_type FROM information_schema.tables "
        "ORDER BY ALL"
    ).fetchall()
    assert objects == [
        ("dataset", "farm/prev_round.csv", "VIEW"),
        ("farm", "choices", "BASE TABLE"),
        ("farm", "dataset_codebook", "BASE TABLE"),
        ("farm", "dataset_links", "BASE TABLE"),
        ("farm", "dictionary", "BASE TABLE"),
        ("farm", "edges", "BASE TABLE"),
        ("farm", "plot", "VIEW"),
        ("farm", "questions", "BASE TABLE"),
        ("farm", "stats", "BASE TABLE"),
        ("farm", "submissions", "VIEW"),
        ("main", "datasets", "BASE TABLE"),
        ("main", "forms", "BASE TABLE"),
        ("main", "joins", "BASE TABLE"),
        ("main", "publishes", "BASE TABLE"),
        ("main", "reads", "BASE TABLE"),
    ]


def test_submissions_reads_the_parquet(database):
    con, _ = database
    assert con.sql("SELECT KEY FROM farm.submissions ORDER BY KEY").fetchall() == [
        ("uuid:aaa",),
        ("uuid:bbb",),
    ]
    columns = con.sql("SELECT * FROM farm.submissions LIMIT 0").columns
    assert (
        columns
        == con.sql(
            """SELECT column_name FROM farm.dictionary WHERE view_name = 'submissions'
            ORDER BY variable_order"""
        )
        .df()["column_name"]
        .tolist()
    )
    # The repeat's columns are read from its long view.
    assert "plot_size_1" not in columns
    assert con.sql(
        "SELECT DISTINCT view_name FROM farm.dictionary WHERE question = 'plot_size'"
    ).fetchall() == [("plot",)]
    # A long view's column, binaries included, resolves to its code.
    assert con.sql(
        """SELECT DISTINCT view_column, choice_value FROM farm.dictionary
           WHERE view_name = 'plot' AND question = 'plot_crop' ORDER BY view_column"""
    ).fetchall() == [("plot_crop_1", "1"), ("plot_crop_2", "2")]


def test_flagged_columns_stay_in_their_views(database):
    con, _ = database
    flagged = con.sql(
        """SELECT view_column, view_name FROM farm.dictionary
           WHERE pii_status NOT IN ('cleared_by_form', 'whitelisted')"""
    ).fetchall()
    assert ("owner", "submissions") in flagged
    for column, view in flagged:
        assert column in con.sql(f"SELECT * FROM farm.{view} LIMIT 0").columns
    assert {"owner", "phone"} <= set(con.sql(f"SELECT * FROM {_PREV} LIMIT 0").columns)


def test_repeat_is_long_with_one_row_per_answered_instance(database):
    con, _ = database
    rows = con.sql("SELECT KEY, plot_index, plot_size FROM farm.plot").fetchall()
    assert rows == [("uuid:aaa", 1, 0.75), ("uuid:bbb", 2, None)]
    columns = con.sql("SELECT * FROM farm.plot LIMIT 0").columns
    assert columns[:2] == ["KEY", "plot_index"]
    assert "plot_size" in columns
    assert all(not c.endswith(("_1_1", "_2_2")) for c in columns)
    # A select_multiple binary keeps its choice code: plot_crop_1_1 -> plot_crop_1.
    assert con.sql(
        "SELECT plot_crop_1 FROM farm.plot WHERE KEY = 'uuid:aaa'"
    ).fetchone() == (1,)


def test_repeat_no_submission_reached_is_its_keys_without_rows():
    import pyarrow as pa

    from surveycto_python_dict.database import _long_table

    wide = pa.table({"KEY": ["uuid:aaa"], "area": [1.5]})
    long = _long_table(wide, {}, ["KEY", "hh_index", "member_index"])
    assert long.num_rows == 0
    assert long.schema.names == ["KEY", "hh_index", "member_index"]


def test_choices_and_edges(database):
    con, _ = database
    assert con.sql(
        "SELECT value, label FROM farm.choices WHERE question = 'crop' ORDER BY value"
    ).fetchall()
    assert con.sql("SELECT count(*) FROM farm.edges").fetchone()[0] > 0


def test_dictionary_says_what_a_column_without_a_question_is(tmp_path, analyzer):
    config = tmp_path / "config.toml"
    config.write_text('forms = ["farm"]\nout_dir = "out"\n', encoding="utf-8")
    cfg = load_config(config)
    # A column written under another version of the form.
    write_pull(cfg, rows=[{**r, "old_field": "x"} for r in ROWS])
    build(cfg, SENTINELS, analyzer)
    form_dir = cfg.form_dir("farm")
    write_csv(
        summary_stats(form_dir, "farm", SENTINELS), form_dir / "farm_summary_stats.csv"
    )
    con = duckdb.connect(str(_write(cfg)), read_only=True)
    assert con.sql(
        """SELECT column_name, column_kind FROM farm.dictionary
        WHERE question IS NULL AND NOT system"""
    ).fetchall() == [("old_field", "not_in_form")]
    con.close()


def test_withheld_choices_are_in_the_choices_table(project):
    # A withheld search() list, as the build writes it.
    rows = [
        {
            "question": "seller",
            "value": "7",
            "label": "Seven",
            "when": None,
            "withheld": "reviewed",
        }
    ]
    pq.write_table(
        pa.Table.from_pylist(rows, schema=SEARCH_CHOICES),
        project.form_dir("farm") / "farm_search_choices.parquet",
    )
    con = duckdb.connect(str(_write(project)), read_only=True)
    assert con.sql(
        "SELECT value, label, withheld FROM farm.choices WHERE question = 'seller'"
    ).fetchall() == [("7", "Seven", "reviewed")]
    assert (
        con.sql(
            "SELECT count(*) FROM farm.choices WHERE question = 'crop' AND withheld IS NULL"
        ).fetchone()[0]
        > 0
    )
    con.close()


def test_rebuild_replaces_the_file(database):
    con, cfg = database
    con.close()
    again = duckdb.connect(str(_write(cfg, {})), read_only=True)
    assert again.sql("SELECT count(*) FROM farm.submissions").fetchone() == (2,)
    again.close()


def test_dataset_sits_in_its_schema_as_text(database):
    con, _ = database
    assert con.sql(f"SELECT plot_code, owner FROM {_PREV}").fetchall() == [
        ("007", "Fictional Owner")
    ]


def test_dataset_links_spell_out_the_join(database):
    con, _ = database
    assert con.sql(
        "SELECT question, kind, dataset, dataset_id, dataset_column, match_column, "
        "match_value FROM farm.dataset_links"
    ).fetchall() == [
        (
            "prev",
            "pulldata",
            "prev_round",
            "farm/prev_round.csv",
            "owner",
            "phone",
            "${phone}",
        )
    ]


def test_dataset_codebook_lists_readers_and_status(database):
    con, _ = database
    rows = con.sql(
        """SELECT column_name, rows, non_null, read_by, pii_status, pii_basis
           FROM farm.dataset_codebook WHERE dataset = 'prev_round'"""
    ).fetchall()
    by_column = {r[0]: r[1:] for r in rows}
    assert by_column["owner"] == (1, 1, ["prev"], "contains_pii", ["reviewed"])
    assert by_column["plot_code"] == (1, 1, [], "whitelisted", ["reviewed"])
    # Read by the pulldata() as its match column; scanned, as nobody reviewed it.
    assert by_column["phone"][2] == ["prev"]
    assert by_column["phone"][3] in ("may_contain_pii", "no_hits")


def test_project_tables_come_from_the_flow(database):
    con, _ = database
    assert con.sql("SELECT form, stage FROM forms").fetchall() == [("farm", 1)]
    assert con.sql(
        "SELECT dataset, kind, published_by, read_by FROM datasets"
    ).fetchall() == [("farm/prev_round.csv", "form_file", None, ["farm"])]
    assert con.sql("SELECT form, field, via, dataset FROM reads").fetchall() == [
        ("farm", "prev", "pulldata", "farm/prev_round.csv")
    ]
    # One form publishes nothing another reads: no joins across forms.
    assert con.sql("SELECT count(*) FROM joins").fetchone() == (0,)


def test_structure_comes_from_the_finished_database(database):
    from surveycto_python_dict.survey import SurveyStore

    con, cfg = database
    objects = json.loads(
        (cfg.out_dir / "farmproject_db.json").read_text(encoding="utf-8")
    )
    names = [o["name"] for o in objects]
    # Every object in the file, and only those.
    in_file = {
        n if s == "main" else f"{s}.{n}"
        for s, n in con.sql(
            "SELECT table_schema, table_name FROM information_schema.tables"
        ).fetchall()
    }
    assert set(names) == in_file
    plot = next(o for o in objects if o["name"] == "farm.plot")
    assert plot["kind"] == "view"
    assert plot["key"] == ["KEY", "plot_index"]
    assert plot["joins"] == ["farm.plot.KEY = farm.submissions.KEY  (many to one)"]
    assert {"name": "plot_index", "type": "INTEGER"} in plot["columns"]
    prev = next(o for o in objects if o["name"] == "dataset.farm/prev_round.csv")
    assert prev["joins"] == [
        "dataset.farm/prev_round.csv.phone = farm.submissions.phone  "
        "(many to one: the column is unique in the dataset)"
    ]

    store = SurveyStore(cfg.form_dir("farm"), "farm")
    summary = store.get_structure_db()
    assert summary.startswith("-- farmproject.duckdb: 15 tables and views in 3 schemas")
    assert "-- table joins: one row per reader field matched" in summary
    assert "--   farm/prev_round.csv: one row per CSV row" in summary
    assert (
        "-- schema farm: submissions (" in summary
        and "repeat tables: 1, nested up to 1 deep; description tables: 7" in summary
    )
    assert "use them in scripts, print only their counts and KEYs" in summary

    out = store.get_structure_db("farm")
    assert "-- view farm.plot: one row per instance of the repeat plot" in out
    assert "--   join: farm.plot.KEY = farm.submissions.KEY  (many to one)" in out
    assert "plot_size" in store.get_structure_db("farm", "plot")
    assert "-- table farm.dictionary: one row per parquet column; view_name is" in out
    # The datasets the form joins come with it.
    assert "-- view dataset.farm/prev_round.csv:" in out
    with pytest.raises(ValueError, match="no schema 'nope'"):
        store.get_structure_db("nope")

    # The file on disk keeps every object.
    sql = (cfg.out_dir / "farmproject_db.sql").read_text(encoding="utf-8")
    assert "-- view farm.plot:" in sql and "-- table forms:" in sql


def test_declared_dataset_key_is_checked_and_named(database):
    con, cfg = database
    con.close()
    _write(cfg, {"farm/prev_round.csv": ["phone"]})
    objects = json.loads(
        (cfg.out_dir / "farmproject_db.json").read_text(encoding="utf-8")
    )
    prev = next(o for o in objects if o["name"] == "dataset.farm/prev_round.csv")
    assert prev["key"] == ["phone"]
    assert prev["grain"] == "one row per phone"
    (cfg.form_raw("farm") / "files" / "prev_round.csv").write_text(
        "phone,owner,plot_code\n3000000000,Fictional Owner,007\n"
        "3000000001,Fictional Owner,008\n",
        encoding="utf-8",
    )
    with pytest.raises(AssertionError, match="repeats"):
        _write(cfg, {"farm/prev_round.csv": ["owner"]})


def test_dataset_key_naming_no_dataset_raises(database):
    con, cfg = database
    con.close()
    with pytest.raises(ValueError, match=r"\[datasets.farm/prev_rnd.csv\] key"):
        _write(cfg, {"farm/prev_rnd.csv": ["phone"]})


def test_a_match_column_missing_from_the_dataset_is_reported(database):
    con, cfg = database
    con.close()
    codebook_path = cfg.form_dir("farm") / "farm_dataset_codebook.json"
    codebook = json.loads(codebook_path.read_text(encoding="utf-8"))
    codebook_path.write_text(
        json.dumps([c for c in codebook if c["column"] != "phone"]), encoding="utf-8"
    )
    _write(cfg, {})
    objects = json.loads(
        (cfg.out_dir / "farmproject_db.json").read_text(encoding="utf-8")
    )
    prev = next(o for o in objects if o["name"] == "dataset.farm/prev_round.csv")
    assert prev["joins"] == [
        "dataset.farm/prev_round.csv.phone = farm.${phone}  (phone is not a column "
        "of farm/prev_round.csv: farm.prev finds no row)"
    ]


def test_joins_connect_a_reader_field_to_the_publisher_field():
    from surveycto_python_dict.database import _write_project

    flow = {
        "forms": [
            {"form_id": "listing", "stage": 1, "reads_from": [], "publishes": ["hh"]},
            {
                "form_id": "visit",
                "stage": 2,
                "reads_from": ["listing"],
                "publishes": [],
            },
        ],
        "datasets": [
            {
                "id": "hh",
                "kind": "server_dataset",
                "title": "Households",
                "key": "hhid",
                "rows": 2,
                "columns": ["hhid", "village"],
                "published_by": "listing",
                "publish_map": {"hhid": "hhid", "village": "roster/village"},
                "origins": {
                    "hhid": {"kind": "entered", "from": []},
                    "village": {"kind": "entered", "from": []},
                },
                "origin_counts": {"entered": 2},
                "read_by": ["visit"],
            }
        ],
        "reads": [
            {
                "form": "visit",
                "dataset": "hh",
                "name_in_form": "HH",
                "attached": True,
                "fields": [
                    {
                        "field": "where",
                        "via": "pulldata",
                        "columns": ["village", "hhid"],
                    }
                ],
            }
        ],
    }
    match = [{"column": "hhid", "value": "${visit_hh}"}]
    forms = {
        "listing": {"questions": [], "repeat_of": {"village": "roster"}},
        "visit": {
            "questions": [
                {
                    "variable_name": "where",
                    "data_source": {
                        "kind": "pulldata",
                        "dataset": "HH",
                        "match": match,
                    },
                    "choice_columns": None,
                }
            ],
            "repeat_of": {},
            # hhid repeats in the dataset: a match can return another household.
            "codebook": [
                {
                    "dataset": "HH",
                    "column": "hhid",
                    "distinct": 1,
                    "non_null": 2,
                    "rows": 2,
                }
            ],
        },
    }
    con = duckdb.connect()
    structure: dict = {}
    _write_project(con, flow, forms, structure)
    assert con.sql("SELECT * FROM joins").fetchall() == [
        (
            "visit",
            "submissions",
            "visit_hh",
            "hh",
            "hhid",
            "pulldata",
            "listing",
            "submissions",
            "hhid",
            False,
            1,
            2,
        )
    ]
    assert structure["joins"]["joins"] == [
        "visit.submissions.visit_hh = listing.submissions.hhid  (through dataset.hh.hhid; "
        "not unique: 1 distinct values in 2 rows, so a match can return another row)"
    ]
    # A search() filter takes every matching row: no warning.
    forms["visit"]["questions"][0]["data_source"]["kind"] = "search"
    _write_project(duckdb.connect(), flow, forms, structure)
    assert structure["joins"]["joins"] == [
        "visit.submissions.visit_hh = listing.submissions.hhid  (through dataset.hh.hhid; "
        "a search() filter: the choices are every matching row)"
    ]
    # A search() select stores its value column: the field itself joins on it.
    question = forms["visit"]["questions"][0]
    question["data_source"]["match"] = []
    question["choice_columns"] = {"value": "hhid", "label": "village"}
    _write_project(duckdb.connect(), flow, forms, structure)
    assert structure["joins"]["joins"] == [
        "visit.submissions.where = listing.submissions.hhid  (through dataset.hh.hhid; "
        "the select stores this column; not unique: 1 distinct values in 2 rows, so a "
        "match can return another row)"
    ]


def test_outputs_writes_stats_codebook_and_database(tmp_path, analyzer, monkeypatch):
    from surveycto_python_dict.cli import outputs_main

    config = tmp_path / "config.toml"
    config.write_text('forms = ["farm"]\nout_dir = "farmproject"\n', encoding="utf-8")
    cfg = load_config(config)
    write_pull(cfg, "phone,owner\n3000000000,Fictional Owner\n")
    build(cfg, SENTINELS, analyzer)
    monkeypatch.setenv("SCTO_SENTINELS", "-99=Don't know;-88=Refused")
    outputs_main(["--config", str(config)])
    form_dir = cfg.form_dir("farm")
    assert (form_dir / "farm_summary_stats.csv").exists()
    assert (form_dir / "farm_codebook.xlsx").exists()
    con = duckdb.connect(str(cfg.out_dir / "farmproject.duckdb"), read_only=True)
    assert con.sql("SELECT count(*) FROM farm.stats").fetchone() == (28,)


def test_a_project_that_reads_no_dataset(tmp_path, analyzer):
    from farm_form import XML

    config = tmp_path / "config.toml"
    config.write_text('forms = ["farm"]\nout_dir = "plain"\n', encoding="utf-8")
    cfg = load_config(config)
    xml = XML.replace("pulldata('prev_round', 'owner', 'phone',  /farm/phone )", "'x'")
    write_pull(cfg, xml=xml)
    build(cfg, SENTINELS, analyzer)
    form_dir = cfg.form_dir("farm")
    write_csv(
        summary_stats(form_dir, "farm", SENTINELS), form_dir / "farm_summary_stats.csv"
    )
    con = duckdb.connect(str(_write(cfg)), read_only=True)
    assert con.sql("SELECT count(*) FROM main.datasets").fetchone() == (0,)
    con.close()


def test_a_repeat_named_like_a_description_table_raises(tmp_path, analyzer):
    from farm_form import XML

    config = tmp_path / "config.toml"
    config.write_text('forms = ["farm"]\nout_dir = "out"\n', encoding="utf-8")
    cfg = load_config(config)
    xml = (
        XML.replace('"/farm/plot"', '"/farm/questions"')
        .replace("/farm/plot/", "/farm/questions/")
        .replace("<plot jr:", "<questions jr:")
        .replace("</plot>", "</questions>")
    )
    write_pull(cfg, xml=xml)
    build(cfg, SENTINELS, analyzer)
    form_dir = cfg.form_dir("farm")
    write_csv(
        summary_stats(form_dir, "farm", SENTINELS), form_dir / "farm_summary_stats.csv"
    )
    with pytest.raises(ValueError, match="farm: the repeat 'questions'"):
        _write(cfg)


def test_a_dataset_column_named_row_raises(tmp_path, analyzer):
    config = tmp_path / "config.toml"
    config.write_text('forms = ["farm"]\nout_dir = "out"\n', encoding="utf-8")
    cfg = load_config(config)
    write_pull(cfg, "phone,owner,row\n3000000000,Fictional Owner,7\n")
    build(cfg, SENTINELS, analyzer)
    form_dir = cfg.form_dir("farm")
    write_csv(
        summary_stats(form_dir, "farm", SENTINELS), form_dir / "farm_summary_stats.csv"
    )
    with pytest.raises(ValueError, match="renamed row to row_1"):
        _write(cfg)


def test_an_empty_table_types_its_text_columns_as_text():
    import pandas as pd

    from surveycto_python_dict.database import _table

    con = duckdb.connect()
    _table(con, "main", "empty", pd.DataFrame([], columns=["form", "field"]))
    assert con.sql(
        "SELECT data_type FROM information_schema.columns WHERE table_name = 'empty'"
    ).fetchall() == [("VARCHAR",), ("VARCHAR",)]
