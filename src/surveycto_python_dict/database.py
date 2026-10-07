"""``<form>.duckdb``: SQL over the built outputs, for people and agents.

Views read ``<form>.parquet`` directly, so the parquet stays the only copy of the
submissions:

- ``submissions``: every column, wide.
- ``submissions_safe``: only the ``cleared_by_form`` and ``whitelisted`` columns.
- ``<repeat>``: each repeat group long, one row per instance that has any value, keyed
  by ``KEY`` and ``instance`` (``instance_1``, ``instance_2``, ... when nested). A
  column keeps its wide name without the instance suffix: ``score_3`` -> ``score``.
- ``<repeat>_safe``: the same, limited to cleared columns.
- ``<dataset>``: each server dataset or attached CSV the form reads, named as the
  form names it, every column as text, with ``row`` (1 = first data row).
- ``<dataset>_safe``: ``row`` plus the dataset's whitelisted columns.

Agents read values only through the ``*_safe`` views. That is a convention, like the
``pii`` signal itself, not access control.

Tables copy the descriptions: ``dictionary`` (one row per column), ``questions``,
``choices``, ``edges`` (the variable graph), ``dataset_links`` (how each
``pulldata()``/``search()`` matches dataset rows to the submission),
``dataset_codebook`` (one row per dataset column: counts, readers, pii) and ``stats``
(``<form>_summary_stats.csv``, so ``scto-stats`` runs first).

When the file is built, its own ``information_schema`` (every object, column and type)
is written with each object's grain, key and joins to ``<form>_db.json``, the map the
MCP's ``get_structure_db`` reads, and rendered for people as ``<form>_db.sql``.
"""

import json
from pathlib import Path

import duckdb
import pandas as pd

from surveycto_python_dict.survey import render_structure

_SAFE = {"cleared_by_form", "whitelisted"}


def _ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _literal(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def _instance_keys(depth: int) -> list[str]:
    return (
        ["instance"] if depth == 1 else [f"instance_{i}" for i in range(1, depth + 1)]
    )


def _repeat_view(repeat: str, columns: dict[str, list[int]], depth: int) -> str:
    """Return the SELECT that stacks ``repeat``'s wide columns into one row per instance.

    ``columns`` maps each wide column to its ``repeat_iteration`` (outer first).
    """
    by_instance: dict[tuple[int, ...], list[str]] = {}
    for column, iterations in columns.items():
        by_instance.setdefault(tuple(iterations), []).append(column)
    selects = []
    for iterations, wide in sorted(by_instance.items()):
        suffix = "".join(f"_{i}" for i in iterations)
        assert all(c.endswith(suffix) for c in wide), (repeat, iterations)
        keys = [
            f"{i} AS {_ident(k)}"
            for i, k in zip(iterations, _instance_keys(depth), strict=True)
        ]
        values = [f"{_ident(c)} AS {_ident(c[: -len(suffix)])}" for c in wide]
        present = " OR ".join(f"{_ident(c)} IS NOT NULL" for c in wide)
        selects.append(
            f"SELECT KEY, {', '.join(keys + values)} FROM submissions WHERE {present}"
        )
    order = ", ".join(["KEY", *_instance_keys(depth)])
    return "\nUNION ALL BY NAME\n".join(selects) + f"\nORDER BY {order}"


# The description tables: what one row is, the key, and what each refers to.
_DESCRIPTIONS = {
    "dictionary": {
        "grain": "one row per submissions column",
        "key": ["column"],
        "joins": ["dictionary.question = questions.question"],
    },
    "questions": {"grain": "one row per question", "key": ["question"], "joins": []},
    "choices": {
        "grain": "one row per choice of a select",
        "key": ["question", "value"],
        "joins": ["choices.question = questions.question"],
    },
    "edges": {
        "grain": "one row per link in the variable graph",
        "key": ["source", "target", "type"],
        "joins": [
            "edges.source = questions.question",
            "edges.target = questions.question",
        ],
    },
    "dataset_links": {
        "grain": "one row per pulldata()/search() match condition",
        "key": ["question", "match_column"],
        "joins": ["dataset_links.question = questions.question"],
    },
    "dataset_codebook": {
        "grain": "one row per dataset column",
        "key": ["dataset", "column"],
        "joins": [],
    },
    "stats": {
        "grain": "one row per submissions column",
        "key": ["variable"],
        "joins": ["stats.variable = dictionary.column"],
    },
}


def write_database(
    form_dir: Path,
    form_id: str,
    datasets: dict[str, Path],
    dataset_keys: dict[str, list[str]],
) -> Path:
    """Write ``<form>.duckdb`` next to the outputs, replacing any earlier one."""
    form_dir = Path(form_dir)
    dictionary = json.loads(
        (form_dir / f"{form_id}_variable_dictionary.json").read_text(encoding="utf-8")
    )
    questions = json.loads(
        (form_dir / f"{form_id}_questions.json").read_text(encoding="utf-8")
    )
    graph = json.loads(
        (form_dir / f"{form_id}_variable_graph.json").read_text(encoding="utf-8")
    )
    variables = dictionary["variables"]
    repeat_of = {node["id"]: node["repeat_group"] for node in graph["nodes"]}
    safe = [c for c, v in variables.items() if v["pii"]["status"] in _SAFE]
    assert "KEY" in safe

    path = form_dir / f"{form_id}.duckdb"
    path.unlink(missing_ok=True)
    con = duckdb.connect(str(path))
    # Each object's grain, key and joins, recorded as it is created.
    structure: dict[str, dict] = {}
    for name in ("submissions", "submissions_safe"):
        structure[name] = {
            "grain": "one row per submission",
            "key": ["KEY"],
            "joins": [],
        }

    parquet = (form_dir / f"{form_id}.parquet").resolve().as_posix()
    con.execute(
        f"CREATE VIEW submissions AS SELECT * FROM read_parquet({_literal(parquet)})"
    )
    con.execute(
        "CREATE VIEW submissions_safe AS SELECT "
        + ", ".join(_ident(c) for c in safe)
        + " FROM submissions"
    )

    for repeat, group in graph["repeat_groups"].items():
        columns = {
            c: v["repeat_iteration"]
            for c, v in variables.items()
            if v["question"] is not None and repeat_of[v["question"]] == repeat
        }
        con.execute(
            f"CREATE VIEW {_ident(repeat)} AS {_repeat_view(repeat, columns, group['depth'])}"
        )
        # A long column is cleared when every wide column it stacks is.
        long_safe: dict[str, bool] = {}
        for column, iterations in columns.items():
            name = column[: -len("".join(f"_{i}" for i in iterations))]
            long_safe[name] = long_safe.get(name, True) and column in safe
        keep = ["KEY", *_instance_keys(group["depth"])]
        keep += [name for name, cleared in long_safe.items() if cleared]
        con.execute(
            f"CREATE VIEW {_ident(repeat + '_safe')} AS SELECT "
            + ", ".join(_ident(c) for c in keep)
            + f" FROM {_ident(repeat)}"
        )
        # An instance joins its parent on KEY and the parent's instance numbers.
        keys = ["KEY", *_instance_keys(group["depth"])]
        parent_keys = ["KEY"]
        if group["depth"] > 1:
            parent_keys += _instance_keys(group["depth"] - 1)
        for suffix in ("", "_safe"):
            parent = (group["parent"] or "submissions") + suffix
            on = " AND ".join(
                f"{repeat}{suffix}.{k} = {parent}.{p}"
                for k, p in zip(keys, parent_keys, strict=False)
            )
            structure[repeat + suffix] = {
                "grain": f"one row per instance of the repeat {repeat} that has any value",
                "key": keys,
                "joins": [f"{on}  (many to one)"],
            }

    # The datasets the form reads (pulldata/search), as text so IDs keep leading zeros.
    codebook = json.loads(
        (form_dir / f"{form_id}_dataset_codebook.json").read_text(encoding="utf-8")
    )
    for dataset, csv_path in sorted(datasets.items()):
        source = _literal(csv_path.resolve().as_posix())
        con.execute(
            f"CREATE VIEW {_ident(dataset)} AS SELECT row_number() OVER () AS row, * "
            f"FROM read_csv({source}, header = true, all_varchar = true)"
        )
        cleared = [
            c["column"]
            for c in codebook
            if c["dataset"] == dataset and c["pii"]["status"] in _SAFE
        ]
        con.execute(
            f"CREATE VIEW {_ident(dataset + '_safe')} AS SELECT "
            + ", ".join(_ident(c) for c in ["row", *cleared])
            + f" FROM {_ident(dataset)}"
        )
        # The key the config declares for the dataset ([datasets.<name>] key), checked
        # on the rows that have all of it; without one, the CSV row is the key.
        key = list(dataset_keys.get(dataset, ["row"]))
        grain = (
            "one row per CSV row" if key == ["row"] else f"one row per {', '.join(key)}"
        )
        note = ""
        if key != ["row"]:
            cols = ", ".join(_ident(c) for c in key)
            complete = " AND ".join(f"{_ident(c)} IS NOT NULL" for c in key)
            repeated, partial = con.sql(
                f"SELECT (SELECT count(*) FROM (SELECT {cols} FROM {_ident(dataset)} "
                f"WHERE {complete} GROUP BY ALL HAVING count(*) > 1)), "
                f"(SELECT count(*) FROM {_ident(dataset)} WHERE NOT ({complete}))"
            ).fetchone()
            assert repeated == 0, f"{dataset}: key {key} repeats in {repeated} groups"
            if partial:
                note = f"; {partial} rows lack part of the key"
        # The joins the form's pulldata()/search() use, as the dataset column on the
        # left; a literal match is a filter.
        by_column = {c["column"]: c for c in codebook if c["dataset"] == dataset}
        for suffix in ("", "_safe"):
            joins = []
            for q in questions:
                source = q["data_source"]
                if not source or source["dataset"] != dataset:
                    continue
                for m in source["match"]:
                    value = m["value"] or ""
                    if not (value.startswith("${") and value.endswith("}")):
                        line = f"filter: {dataset}{suffix}.{m['column']} = {value}"
                    elif m["column"] not in by_column:
                        # The form names a column the dataset does not have: the
                        # lookup never finds a row.
                        line = (
                            f"{dataset}{suffix}.{m['column']} = ${{{value[2:-1]}}}  "
                            f"({m['column']} is not a column of {dataset}: "
                            f"{q['variable_name']} finds no row)"
                        )
                    else:
                        field = value[2:-1]
                        table = (repeat_of.get(field) or "submissions") + suffix
                        column = by_column[m["column"]]
                        unique = (
                            column["distinct"] == column["non_null"] == column["rows"]
                        )
                        line = (
                            f"{dataset}{suffix}.{m['column']} = {table}.{field}  "
                            + (
                                "(many to one: the column is unique in the dataset)"
                                if unique
                                else f"(not unique: {column['distinct']} distinct "
                                f"values in {column['non_null']} rows)"
                            )
                        )
                    if line not in joins:
                        joins.append(line)
            structure[dataset + suffix] = {
                "grain": grain + note,
                "key": key,
                "joins": joins,
            }

    tables = {
        "dictionary": pd.DataFrame(
            [
                {
                    "column": c,
                    "variable_order": v["variable_order"],
                    "question": v["question"],
                    "repeat_group": repeat_of.get(v["question"]),
                    "repeat_iteration": v.get("repeat_iteration"),
                    "choice_value": v.get("choice_value"),
                    "system": v.get("system", False),
                    "arrow_type": v["parquet"]["arrow_type"],
                    "xml_type": v["parquet"]["xml_type"],
                    "cast_fallback": v["parquet"].get("cast_fallback"),
                    "non_null_count": v["non_null_count"],
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
                    "relevance": q["relevance"],
                    "group_relevances": q["group_relevances"],
                    "constraint": q["constraint"],
                    "calculation": q["calculation"],
                    "required": q["required"],
                    "publishable": q["publishable"],
                    "choices_withheld": q["choices_withheld"],
                }
                for q in questions
            ]
        ),
        "choices": pd.DataFrame(
            [
                {
                    "question": q["variable_name"],
                    "value": c["value"],
                    "label": c["label"],
                }
                for q in questions
                if q["choices"] and not q["choices_withheld"]
                for c in q["choices"]
            ],
            columns=["question", "value", "label"],
        ),
        "edges": pd.DataFrame(graph["edges"], columns=["source", "target", "type"]),
        "dataset_codebook": pd.DataFrame(
            [
                {
                    "dataset": c["dataset"],
                    "column": c["column"],
                    "rows": c["rows"],
                    "non_null": c["non_null"],
                    "distinct": c["distinct"],
                    "read_by": c["read_by"],
                    "pii_status": c["pii"]["status"],
                    "pii_basis": c["pii"]["basis"],
                    "hit_rows": c["pii"].get("presidio", {}).get("hit_rows"),
                }
                for c in codebook
            ],
            columns=[
                "dataset",
                "column",
                "rows",
                "non_null",
                "distinct",
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
                    "dataset_column": q["data_source"].get("column"),
                    "match_column": m["column"],
                    "match_value": m["value"],
                }
                for q in questions
                if q["data_source"]
                for m in q["data_source"]["match"] or [{"column": None, "value": None}]
            ],
            columns=[
                "question",
                "kind",
                "dataset",
                "dataset_column",
                "match_column",
                "match_value",
            ],
        ),
    }
    for name, frame in tables.items():
        con.register("frame", frame)
        con.execute(f"CREATE TABLE {name} AS SELECT * FROM frame")
        con.unregister("frame")
    stats = (form_dir / f"{form_id}_summary_stats.csv").resolve().as_posix()
    con.execute(f"CREATE TABLE stats AS SELECT * FROM read_csv({_literal(stats)})")
    structure.update(_DESCRIPTIONS)

    # The finished file's own schema, with what each object is.
    objects = []
    for name, kind in con.sql(
        "SELECT table_name, table_type FROM information_schema.tables"
    ).fetchall():
        columns = con.sql(
            "SELECT column_name, data_type FROM information_schema.columns "
            f"WHERE table_name = {_literal(name)} ORDER BY ordinal_position"
        ).fetchall()
        objects.append(
            {
                "name": name,
                "kind": "view" if kind == "VIEW" else "table",
                **structure[name],
                "columns": [{"name": c, "type": t} for c, t in columns],
            }
        )
    con.close()
    order = list(structure)
    objects.sort(key=lambda o: order.index(o["name"]))
    (form_dir / f"{form_id}_db.json").write_text(
        json.dumps(objects, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    (form_dir / f"{form_id}_db.sql").write_text(
        render_structure(form_id, objects) + "\n", encoding="utf-8"
    )
    return path


def main(argv: list[str] | None = None) -> None:
    """Entry point for ``scto-duckdb``."""
    import argparse

    from surveycto_python_dict.cli import form_datasets, load_config

    parser = argparse.ArgumentParser(prog="scto-duckdb")
    parser.add_argument("--config", type=Path, default=Path("config.toml"))
    cfg = load_config(parser.parse_args(argv).config)
    for form in cfg.forms:
        path = write_database(
            cfg.form_dir(form), form, form_datasets(cfg, form), cfg.dataset_keys
        )
        print(f"[duckdb] {form} -> {path}")
