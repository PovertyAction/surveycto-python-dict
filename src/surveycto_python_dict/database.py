"""``<project>.duckdb``: one database for a connected project, for people and agents.

The project is its forms and the datasets that chain them (``flow.json``). The database
holds one schema per form, a ``dataset`` schema and the project tables in ``main``.
Views read the files on disk; nothing is copied into the database:

- ``<form>.submissions``: one row per submission, from ``<form>.parquet``: every
  column outside the repeat groups, which are in their long views.
- ``<form>.<repeat>``: each repeat group long, from ``long/<repeat>.parquet``, which this
  step reshapes from the wide parquet: one row per instance that has any value, keyed
  by ``KEY`` and one ``<repeat>_index`` per repeat from the outermost down
  (``KEY, hh_index, member_index``). A column keeps its wide name without the instance
  suffix: ``score_3`` -> ``score``. Columns written under another form version, with
  more or fewer instance numbers than the field's nesting (``ragged`` in the
  dictionary), stay wide in ``submissions``. The ``dictionary`` table's
  ``view_name`` names each column's view and ``view_column`` its name there.
- ``dataset.<id>``: each dataset once, under its ``flow.json`` id (the server id, or
  ``<form>/<file>.csv`` for a CSV attached to a form), every column as text, with
  ``row`` (1 = first data row).

Every view holds every column. ``dictionary.pii_status`` and
``dataset_codebook.pii_status`` say which may hold personal data: scripts use those
columns like any other, and their values stay out of what an agent prints.

Tables copy the descriptions. Per form: ``dictionary`` (one row per column),
``questions``, ``choices``, ``edges`` (the variable graph), ``dataset_links`` (how each
``pulldata()``/``search()`` matches dataset rows to the submission),
``dataset_codebook`` (one row per dataset column: counts, readers, pii) and ``stats``
(``<form>_summary_stats.csv``, from ``scto-stats`` or ``scto-codebook``). In ``main``, from
``flow.json``: ``forms``, ``datasets``, ``publishes``, ``reads`` and ``joins`` (a
reader's field to the publisher's field through the dataset column between them).

When the file is built, its own ``information_schema`` (every object, column and type)
is written with each object's grain, key and joins to ``<project>_db.json``, the map
the MCP's ``get_structure_db`` reads, and rendered for people as ``<project>_db.sql``.
"""

import json
from collections import Counter
from functools import reduce
from pathlib import Path

import duckdb
import pandas as pd
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq
from dotenv import load_dotenv

from surveycto_python_dict.stats import SQL_TYPES
from surveycto_python_dict.survey import render_structure


def _ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _literal(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def _q(schema: str, name: str) -> str:
    """Return ``schema.name`` quoted for SQL."""
    return f"{_ident(schema)}.{_ident(name)}"


def _load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _repeat_keys(repeat: str, groups: dict) -> list[str]:
    """Return ``KEY`` and one ``<repeat>_index`` per repeat from the outermost to ``repeat``."""
    chain = []
    while repeat is not None:
        chain.insert(0, repeat)
        repeat = groups[repeat]["parent"]
    return ["KEY", *(f"{r}_index" for r in chain)]


def _long_table(
    table: pa.Table, columns: dict[str, list[int]], keys: list[str]
) -> pa.Table:
    """Stack a repeat's wide columns into one row per instance that has any value.

    ``columns`` maps each wide column to its repeat instances, outer first; a column
    keeps its name without the instance suffix: ``score_3`` -> ``score``.
    """
    by_instance: dict[tuple[int, ...], list[str]] = {}
    for column, iterations in columns.items():
        by_instance.setdefault(tuple(iterations), []).append(column)
    parts = []
    for iterations, wide in sorted(by_instance.items()):
        suffix = "".join(f"_{i}" for i in iterations)
        rows = table.select(["KEY", *wide])
        rows = rows.filter(reduce(pc.or_, [pc.is_valid(rows[c]) for c in wide]))
        index = [pa.array([i] * rows.num_rows, pa.int32()) for i in iterations]
        parts.append(
            pa.Table.from_arrays(
                [rows["KEY"].combine_chunks(), *index]
                + [rows[c].combine_chunks() for c in wide],
                names=[*keys, *(c.removesuffix(suffix) for c in wide)],
            )
        )
    if not parts:
        # No submission reached the repeat: its long table is the keys, no rows.
        types = [table.schema.field("KEY").type] + [pa.int32()] * (len(keys) - 1)
        return pa.schema(list(zip(keys, types, strict=True))).empty_table()
    long = pa.concat_tables(parts, promote_options="permissive")
    return long.sort_by([(k, "ascending") for k in keys])


_ORIGIN_KINDS = ("entered", "calculated", "carried", "metadata")


def _table(con, schema: str, name: str, frame: pd.DataFrame) -> None:
    con.register("frame", frame)
    con.execute(f"CREATE TABLE {_q(schema, name)} AS SELECT * FROM frame")
    con.unregister("frame")


# Each form's description tables: what one row is, the key, and what each refers to.
# `{s}` is the form's schema.
_DESCRIPTIONS = {
    "dictionary": {
        "grain": "one row per parquet column; view_name is the view it is in and "
        "view_column its name there (score_3 in submissions is score in a long "
        "view)",
        "key": ["column_name"],
        "joins": ["{s}.dictionary.question = {s}.questions.question"],
    },
    "questions": {"grain": "one row per question", "key": ["question"], "joins": []},
    "choices": {
        "grain": "one row per choice of a select, search() lists included; "
        "filter_values: the search() filter values of its list; withheld: why "
        "get_choice_list does not list it",
        "key": ["question", "filter_values", "value"],
        "joins": ["{s}.choices.question = {s}.questions.question"],
    },
    "edges": {
        "grain": "one row per link in the variable graph",
        "key": ["source", "target", "type"],
        "joins": [
            "{s}.edges.source = {s}.questions.question",
            "{s}.edges.target = {s}.questions.question",
        ],
    },
    "dataset_links": {
        "grain": "one row per pulldata()/search() match condition; a search() "
        "names the value_column and label_column its choices come from",
        "key": ["question", "match_column"],
        "joins": [
            "{s}.dataset_links.question = {s}.questions.question",
            "{s}.dataset_links.dataset_id names the view dataset.<dataset_id>",
        ],
    },
    "dataset_codebook": {
        "grain": "one row per column of a dataset the form reads",
        "key": ["dataset", "column_name"],
        "joins": [],
    },
    "stats": {
        "grain": "one row per parquet column",
        "key": ["variable"],
        "joins": ["{s}.stats.variable = {s}.dictionary.column_name"],
    },
}

# The project tables in main, from flow.json.
_PROJECT = {
    "forms": {"grain": "one row per form", "key": ["form"], "joins": []},
    "datasets": {
        "grain": "one row per dataset",
        "key": ["dataset"],
        "joins": ["datasets.published_by = forms.form"],
    },
    "publishes": {
        "grain": "one row per column of a published dataset",
        "key": ["dataset", "dataset_column"],
        "joins": [
            "publishes.dataset = datasets.dataset",
            "publishes.form = forms.form",
        ],
    },
    "reads": {
        "grain": "one row per field that reads a dataset",
        "key": ["form", "field", "dataset"],
        "joins": ["reads.dataset = datasets.dataset", "reads.form = forms.form"],
    },
    "joins": {
        "grain": "one row per reader field matched to the publisher field it comes "
        "from; via: pulldata or search; is_unique: the dataset column identifies one "
        "row, which a pulldata() needs and a search() filter does not",
        "key": ["reader_form", "reader_field", "dataset", "dataset_column"],
        "joins": [
            "<reader_form>.<reader_table>.<reader_field> = "
            "<publisher_form>.<publisher_table>.<publisher_field>"
        ],
    },
}


def _write_form(con, form: str, form_dir: Path, ids: dict, structure: dict) -> dict:
    """Write one form's schema; return what the project layer needs from it."""
    dictionary = _load(form_dir / f"{form}_variable_dictionary.json")
    questions = _load(form_dir / f"{form}_questions.json")
    graph = _load(form_dir / f"{form}_variable_graph.json")
    codebook = _load(form_dir / f"{form}_dataset_codebook.json")
    variables = dictionary["variables"]
    repeat_of = {node["id"]: node["repeat_group"] for node in graph["nodes"]}
    # The view each column is read from: its repeat's long view, or submissions.
    table_of = {
        c: repeat_of[v["question"]]
        if v["question"] is not None
        and repeat_of[v["question"]]
        and not v.get("ragged")
        else "submissions"
        for c, v in variables.items()
    }
    # Its name there: a long view drops the instance suffix, score_3 -> score.
    view_column = {
        c: c
        if table_of[c] == "submissions"
        else c.removesuffix("".join(f"_{i}" for i in v["repeat_iteration"]))
        for c, v in variables.items()
    }
    wide = [c for c in variables if table_of[c] == "submissions"]

    con.execute(f"CREATE SCHEMA {_ident(form)}")
    parquet = (form_dir / f"{form}.parquet").resolve().as_posix()
    # Straight from the file, binding only its columns: DuckDB takes seconds per ten
    # thousand columns to bind a view.
    con.execute(
        f"CREATE VIEW {_q(form, 'submissions')} AS SELECT "
        + ", ".join(_ident(c) for c in wide)
        + f" FROM read_parquet({_literal(parquet)})"
    )
    structure[f"{form}.submissions"] = {
        "grain": "one row per submission",
        "key": ["KEY"],
        "joins": [],
    }

    groups = graph["repeat_groups"]
    long_dir = form_dir / "long"
    long_dir.mkdir(exist_ok=True)
    for old in long_dir.glob("*.parquet"):
        old.unlink()
    table = pq.read_table(parquet) if groups else None
    for repeat, group in groups.items():
        keys = _repeat_keys(repeat, groups)
        columns = {
            c: v["repeat_iteration"]
            for c, v in variables.items()
            if table_of[c] == repeat
        }
        long_path = long_dir / f"{repeat}.parquet"
        pq.write_table(_long_table(table, columns, keys), long_path)
        source = _literal(long_path.resolve().as_posix())
        con.execute(
            f"CREATE VIEW {_q(form, repeat)} AS SELECT * FROM read_parquet({source})"
        )
        # An instance joins its parent on the parent's keys, named alike in both.
        this = f"{form}.{repeat}"
        parent = f"{form}.{group['parent'] or 'submissions'}"
        on = " AND ".join(f"{this}.{k} = {parent}.{k}" for k in keys[:-1])
        structure[this] = {
            "grain": f"one row per instance of the repeat {repeat} that has any value",
            "key": keys,
            "joins": [f"{on}  (many to one)"],
        }

    n_columns = Counter(v["question"] for v in variables.values())
    tables = {
        "dictionary": pd.DataFrame(
            [
                {
                    "column_name": c,
                    "view_name": table_of[c],
                    "view_column": view_column[c],
                    "variable_order": v["variable_order"],
                    "question": v["question"],
                    "column_kind": v.get("column_kind"),
                    "repeat_group": repeat_of.get(v["question"]),
                    "repeat_iteration": v.get("repeat_iteration"),
                    "ragged": v.get("ragged", False),
                    "choice_value": v.get("choice_value"),
                    "system": v.get("system", False),
                    "arrow_type": v["parquet"]["arrow_type"],
                    "xml_type": v["parquet"]["xml_type"],
                    "cast_fallback": v["parquet"].get("cast_fallback"),
                    "non_null_count": v["non_null_count"],
                    "max_length": v.get("max_length"),
                    "at_max_length": v.get("at_max_length"),
                    "missing_rate": v["missing_rate"],
                    "pii_status": v["pii"]["status"],
                    "pii_basis": v["pii"]["basis"],
                }
                for c, v in variables.items()
            ]
        ),
        "questions": pd.DataFrame(
            [
                {
                    "question": q["variable_name"],
                    "question_order": q["question_order"],
                    "type": q["type"],
                    "question_text": q["question_text"],
                    "group_path": q["group_path"],
                    "repeat_group": repeat_of.get(q["variable_name"]),
                    # 0: no submission holds a value, so the data has no column.
                    "n_columns": n_columns.get(q["variable_name"], 0),
                    "relevance": q["relevance"],
                    "group_relevances": q["group_relevances"],
                    "constraint_expr": q["constraint"],
                    "constraint_message": q["constraint_message"],
                    "hint": q["hint"],
                    "calculation": q["calculation"],
                    "calculation_is_default": q["calculation_is_default"],
                    "appearance": q["appearance"],
                    "required": q["required"],
                    "publishable": q["publishable"],
                    "choices_withheld": q["choices_withheld"],
                    "choices_per_submission": q["choices_per_submission"],
                }
                for q in questions
            ]
        ),
        # The withheld search() lists' choices come from the build's parquet:
        # questions.json refers to those lists without listing them.
        "choices": pd.concat(
            [
                pd.DataFrame(
                    [
                        {
                            "question": q["variable_name"],
                            "value": c["value"],
                            "label": c["label"],
                            "filter_values": json.dumps(c["when"])
                            if "when" in c
                            else None,
                            "withheld": None,
                        }
                        for q in questions
                        if q["choices"] and not q["choices_withheld"]
                        for c in q["choices"]
                    ],
                    columns=["question", "value", "label", "filter_values", "withheld"],
                ),
                pq.read_table(form_dir / f"{form}_search_choices.parquet")
                .to_pandas()
                .rename(columns={"when": "filter_values"}),
            ],
            ignore_index=True,
        ),
        "edges": pd.DataFrame(graph["edges"], columns=["source", "target", "type"]),
        "dataset_codebook": pd.DataFrame(
            [
                {
                    "dataset": c["dataset"],
                    "dataset_id": ids[c["dataset"]],
                    "column_name": c["column"],
                    "rows": c["rows"],
                    "non_null": c["non_null"],
                    "n_distinct": c["distinct"],
                    "max_length": c["max_length"],
                    "at_max_length": c["at_max_length"],
                    "read_by": c["read_by"],
                    "pii_status": c["pii"]["status"],
                    "pii_basis": c["pii"]["basis"],
                    "hit_rows": c["pii"].get("presidio", {}).get("hit_rows"),
                }
                for c in codebook
            ],
            columns=[
                "dataset",
                "dataset_id",
                "column_name",
                "rows",
                "non_null",
                "n_distinct",
                "max_length",
                "at_max_length",
                "read_by",
                "pii_status",
                "pii_basis",
                "hit_rows",
            ],
        ),
        # How each pulldata()/search() matches dataset rows to the submission: one row
        # per condition, dataset.match_column = match_value (a ${field} or a literal).
        "dataset_links": pd.DataFrame(
            [
                {
                    "question": q["variable_name"],
                    "kind": q["data_source"]["kind"],
                    "dataset": q["data_source"]["dataset"],
                    "dataset_id": ids[q["data_source"]["dataset"]],
                    "dataset_column": q["data_source"].get("column"),
                    "match_column": m["column"],
                    "match_value": m["value"],
                    "mode": q["data_source"].get("mode"),
                    "value_column": (q["choice_columns"] or {}).get("value"),
                    "label_column": (q["choice_columns"] or {}).get("label"),
                }
                for q in questions
                if q["data_source"]
                for m in q["data_source"]["match"] or [{"column": None, "value": None}]
            ],
            columns=[
                "question",
                "kind",
                "dataset",
                "dataset_id",
                "dataset_column",
                "match_column",
                "match_value",
                "mode",
                "value_column",
                "label_column",
            ],
        ),
    }
    for name, frame in tables.items():
        _table(con, form, name, frame)
    stats = (form_dir / f"{form}_summary_stats.csv").resolve().as_posix()
    # The dialect and types stated: a sniff of the first rows can miss the quoting.
    columns = ", ".join(f"{_literal(c)}: {_literal(t)}" for c, t in SQL_TYPES.items())
    con.execute(
        f"CREATE TABLE {_q(form, 'stats')} AS SELECT * FROM read_csv("
        f"{_literal(stats)}, header = true, delim = ',', quote = '\"', "
        f"escape = '\"', columns = {{{columns}}})"
    )
    for name, described in _DESCRIPTIONS.items():
        structure[f"{form}.{name}"] = {
            **described,
            "joins": [j.format(s=form) for j in described["joins"]],
        }
    return {"questions": questions, "repeat_of": repeat_of, "codebook": codebook}


def _write_datasets(
    con,
    flow: dict,
    csvs: dict,
    forms: dict,
    dataset_keys: dict,
    structure: dict,
) -> None:
    """Write each dataset once in the ``dataset`` schema, with its joins to the forms."""
    con.execute("CREATE SCHEMA dataset")
    id_of = {(r["form"], r["name_in_form"]): r["dataset"] for r in flow["reads"]}
    for d in flow["datasets"]:
        did = d["id"]
        view = _q("dataset", did)
        source = _literal(csvs[did].resolve().as_posix())
        con.execute(
            f"CREATE VIEW {view} AS SELECT row_number() OVER () AS row, * "
            f"FROM read_csv({source}, header = true, all_varchar = true)"
        )
        entries = [
            c
            for form in d["read_by"]
            for c in forms[form]["codebook"]
            if id_of[form, c["dataset"]] == did
        ]
        # The key the config declares ([datasets.<id>] key), checked on the rows that
        # have all of it; without one, the CSV row is the key.
        key = list(dataset_keys.get(did, ["row"]))
        grain = (
            "one row per CSV row" if key == ["row"] else f"one row per {', '.join(key)}"
        )
        if d["published_by"]:
            grain += f"; published by {d['published_by']}"
        if key != ["row"]:
            cols = ", ".join(_ident(c) for c in key)
            complete = " AND ".join(f"{_ident(c)} IS NOT NULL" for c in key)
            repeated, partial = con.sql(
                f"SELECT (SELECT count(*) FROM (SELECT {cols} FROM {view} "
                f"WHERE {complete} GROUP BY ALL HAVING count(*) > 1)), "
                f"(SELECT count(*) FROM {view} WHERE NOT ({complete}))"
            ).fetchone()
            assert repeated == 0, f"{did}: key {key} repeats in {repeated} groups"
            if partial:
                grain += f"; {partial} rows lack part of the key"
        # The joins each reading form's pulldata()/search() use, the dataset column on
        # the left; a literal match is a filter.
        stats = {c["column"]: c for c in entries}
        name = f"dataset.{did}"
        joins = []
        for form in d["read_by"]:
            repeat_of = forms[form]["repeat_of"]
            for q in forms[form]["questions"]:
                source = q["data_source"]
                if not source or id_of[form, source["dataset"]] != did:
                    continue
                for m in source["match"]:
                    value = m["value"] or ""
                    if not (value.startswith("${") and value.endswith("}")):
                        line = f"filter: {name}.{m['column']} = {value}"
                    elif m["column"] not in stats:
                        # The form names a column the dataset does not have: the
                        # lookup never finds a row.
                        line = (
                            f"{name}.{m['column']} = {form}.${{{value[2:-1]}}}  "
                            f"({m['column']} is not a column of {did}: "
                            f"{form}.{q['variable_name']} finds no row)"
                        )
                    else:
                        field = value[2:-1]
                        table = f"{form}.{repeat_of.get(field) or 'submissions'}"
                        column = stats[m["column"]]
                        unique = (
                            column["distinct"] == column["non_null"] == column["rows"]
                        )
                        line = f"{name}.{m['column']} = {table}.{field}  " + (
                            "(a search() filter: the choices are every matching row)"
                            if source["kind"] == "search"
                            else "(many to one: the column is unique in the dataset)"
                            if unique
                            else f"(not unique: {column['distinct']} distinct "
                            f"values in {column['non_null']} rows)"
                        )
                    if line not in joins:
                        joins.append(line)
        structure[name] = {"grain": grain, "key": key, "joins": joins}


def _write_project(con, flow: dict, forms: dict, structure: dict) -> None:
    """Write the project tables in ``main`` from ``flow.json``."""
    datasets = {d["id"]: d for d in flow["datasets"]}
    joins = []
    for r in flow["reads"]:
        d = datasets[r["dataset"]]
        publisher = d["published_by"]
        if publisher is None or publisher == r["form"]:
            continue
        reader = forms[r["form"]]
        stats = {
            c["column"]: c
            for c in reader["codebook"]
            if c["dataset"] == r["name_in_form"]
        }
        for q in reader["questions"]:
            source = q["data_source"]
            if not source or source["dataset"] != r["name_in_form"]:
                continue
            for m in source["match"]:
                value = m["value"] or ""
                path = d["publish_map"].get(m["column"])
                if path is None or not (value.startswith("${") and value.endswith("}")):
                    continue
                field, published = value[2:-1], path.rsplit("/", 1)[-1]
                column = stats[m["column"]]
                row = {
                    "reader_form": r["form"],
                    "reader_table": reader["repeat_of"].get(field) or "submissions",
                    "reader_field": field,
                    "dataset": d["id"],
                    "dataset_column": m["column"],
                    "via": source["kind"],
                    "publisher_form": publisher,
                    "publisher_table": forms[publisher]["repeat_of"].get(published)
                    or "submissions",
                    "publisher_field": published,
                    # Unique: each value matches one dataset row. Otherwise a
                    # pulldata() can return another row's value; a search() takes
                    # every matching row by design.
                    "is_unique": column["distinct"]
                    == column["non_null"]
                    == column["rows"],
                    "distinct_values": column["distinct"],
                    "dataset_rows": column["rows"],
                }
                if row not in joins:
                    joins.append(row)
    tables = {
        "forms": pd.DataFrame(
            [
                {
                    "form": f["form_id"],
                    "stage": f["stage"],
                    "reads_from": f["reads_from"],
                    "publishes": f["publishes"],
                }
                for f in flow["forms"]
            ]
        ),
        "datasets": pd.DataFrame(
            [
                {
                    "dataset": d["id"],
                    "kind": d["kind"],
                    "title": d["title"],
                    "key": d["key"],
                    "rows": d["rows"],
                    "published_by": d["published_by"],
                    "read_by": d["read_by"],
                    **{k: d["origin_counts"].get(k, 0) for k in _ORIGIN_KINDS},
                }
                for d in flow["datasets"]
            ],
            # A project can read no dataset.
            columns=[
                "dataset",
                "kind",
                "title",
                "key",
                "rows",
                "published_by",
                "read_by",
                *_ORIGIN_KINDS,
            ],
        ),
        "publishes": pd.DataFrame(
            [
                {
                    "dataset": d["id"],
                    "dataset_column": column,
                    "form": d["published_by"],
                    "field": path,
                    "origin": d["origins"][column]["kind"],
                    "carried_from": d["origins"][column]["from"],
                }
                for d in flow["datasets"]
                for column, path in d["publish_map"].items()
            ],
            columns=[
                "dataset",
                "dataset_column",
                "form",
                "field",
                "origin",
                "carried_from",
            ],
        ),
        "reads": pd.DataFrame(
            [
                {
                    "form": r["form"],
                    "field": f["field"],
                    "via": f["via"],
                    "dataset": r["dataset"],
                    "columns": f["columns"],
                    "attached": r["attached"],
                }
                for r in flow["reads"]
                for f in r["fields"]
            ],
            columns=["form", "field", "via", "dataset", "columns", "attached"],
        ),
        "joins": pd.DataFrame(
            joins,
            columns=[
                "reader_form",
                "reader_table",
                "reader_field",
                "dataset",
                "dataset_column",
                "via",
                "publisher_form",
                "publisher_table",
                "publisher_field",
                "is_unique",
                "distinct_values",
                "dataset_rows",
            ],
        ),
    }
    for name, frame in tables.items():
        _table(con, "main", name, frame)
    structure.update(_PROJECT)
    structure["joins"] = {
        **_PROJECT["joins"],
        "joins": [
            f"{j['reader_form']}.{j['reader_table']}.{j['reader_field']} = "
            f"{j['publisher_form']}.{j['publisher_table']}.{j['publisher_field']}  "
            f"(through dataset.{j['dataset']}.{j['dataset_column']}"
            + (
                "; a search() filter: the choices are every matching row)"
                if j["via"] == "search"
                else ")"
                if j["is_unique"]
                else f"; not unique: {j['distinct_values']} distinct values in "
                f"{j['dataset_rows']} rows, so a match can return another row)"
            )
            for j in joins
        ],
    }


def write_database(
    out_dir: Path,
    project: str,
    forms: list[str],
    csvs: dict[str, Path],
    dataset_keys: dict[str, list[str]],
) -> Path:
    """Write ``<project>.duckdb`` in ``out_dir``, replacing any earlier one.

    ``csvs`` gives each ``flow.json`` dataset's CSV; ``dataset_keys`` the columns that
    identify a dataset's rows, by dataset id.
    """
    out_dir = Path(out_dir)
    flow = _load(out_dir / "flow.json")
    ids = {
        form: {
            r["name_in_form"]: r["dataset"] for r in flow["reads"] if r["form"] == form
        }
        for form in forms
    }
    path = out_dir / f"{project}.duckdb"
    path.unlink(missing_ok=True)
    con = duckdb.connect(str(path))
    # Each object's grain, key and joins, recorded as it is created.
    structure: dict[str, dict] = {}
    loaded = {
        form: _write_form(con, form, out_dir / form, ids[form], structure)
        for form in forms
    }
    _write_datasets(con, flow, csvs, loaded, dataset_keys, structure)
    _write_project(con, flow, loaded, structure)

    # The finished file's own schema, with what each object is. One query for every
    # column: each query binds every view again.
    columns_of: dict[tuple[str, str], list[tuple[str, str]]] = {}
    for schema, table_name, column, data_type in con.sql(
        "SELECT table_schema, table_name, column_name, data_type "
        "FROM information_schema.columns ORDER BY table_schema, table_name, "
        "ordinal_position"
    ).fetchall():
        columns_of.setdefault((schema, table_name), []).append((column, data_type))
    objects = []
    for schema, table_name, kind in con.sql(
        "SELECT table_schema, table_name, table_type FROM information_schema.tables"
    ).fetchall():
        name = table_name if schema == "main" else f"{schema}.{table_name}"
        objects.append(
            {
                "name": name,
                "kind": "view" if kind == "VIEW" else "table",
                **structure[name],
                "columns": [
                    {"name": c, "type": t} for c, t in columns_of[schema, table_name]
                ],
            }
        )
    con.close()
    order = {name: i for i, name in enumerate(structure)}
    objects.sort(key=lambda o: order[o["name"]])
    (out_dir / f"{project}_db.json").write_text(
        json.dumps(objects, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    (out_dir / f"{project}_db.sql").write_text(
        render_structure(project, objects) + "\n", encoding="utf-8"
    )
    return path


def main(argv: list[str] | None = None) -> None:
    """Entry point for ``scto-duckdb``."""
    from surveycto_python_dict.cli import config_arg, dataset_csvs

    cfg = config_arg(argv)
    load_dotenv()
    flow = _load(cfg.out_dir / "flow.json")
    path = write_database(
        cfg.out_dir,
        cfg.project,
        list(cfg.forms),
        dataset_csvs(cfg, flow),
        cfg.dataset_keys,
    )
    con = duckdb.connect(str(path), read_only=True)
    for form in cfg.forms:
        (ragged,) = con.sql(
            f"SELECT count(*) FROM {_q(form, 'dictionary')} WHERE ragged"
        ).fetchone()
        if ragged:
            print(
                f"[duckdb] {form}: {ragged} columns from other form versions stay wide; "
                f"{form}.dictionary where ragged lists them"
            )
    con.close()
    print(f"[duckdb] {len(cfg.forms)} forms -> {path}")
