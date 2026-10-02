"""Variable dictionary (one entry per parquet column) and variable graph (one per question).

The dictionary is ``{dataset, summary, variables}``; each variable links to its
question in ``questions.json`` by name and carries column statistics, the Arrow and
XML types, sentinel counts, and ``value_labels`` for coded columns (a select_one's
choices; for a select_multiple binary, ``1`` = its choice and ``0`` = not chosen). Sentinel codes are reported, never recoded: the
parquet keeps them as collected and the user decides how to treat them. The graph
links questions through calculations, relevance, group relevance, constraints and
shared repeats.
"""

import datetime as dt
import json
import re
from collections import Counter

import pyarrow as pa
import pyarrow.compute as pc

from surveycto_python_dict.xml_contract import FormContract

_REF_RE = re.compile(r"\$\{([^}]+)\}")
_SURVEY_KEYS = (
    "question_text",
    "type",
    "choice_list",
    "choices",
    "relevance",
    "group_relevances",
    "constraint",
    "calculation",
    "references",
    "disabled",
)


def _json_value(value):
    if isinstance(value, (dt.date, dt.datetime)):
        return value.isoformat()
    return value


def _ordered(arrow_type: pa.DataType) -> bool:
    return (
        pa.types.is_integer(arrow_type)
        or pa.types.is_floating(arrow_type)
        or pa.types.is_temporal(arrow_type)
    )


def _sentinel_counts(column: pa.ChunkedArray, sentinels: dict[int, str]) -> dict:
    if pa.types.is_integer(column.type) or pa.types.is_floating(column.type):
        values = Counter(v for v in column.to_pylist() if v in sentinels)
        found = {int(v): n for v, n in values.items()}
    elif pa.types.is_string(column.type):
        codes = {str(code): code for code in sentinels}
        found = {
            codes[v]: n for v, n in Counter(column.to_pylist()).items() if v in codes
        }
    else:
        return {}
    return {
        str(code): {"label": sentinels[code], "count": found[code]}
        for code in sorted(found)
    }


def build_dictionary(
    table: pa.Table,
    questions: list[dict],
    contract: FormContract,
    sentinels: dict[int, str],
) -> dict:
    """Return the variable dictionary for a cast table."""
    by_name = {q["variable_name"]: q for q in questions}
    n_rows = table.num_rows
    variables = {}
    for order, field in enumerate(table.schema):
        meta = {k.decode(): v.decode() for k, v in (field.metadata or {}).items()}
        column = table.column(field.name)
        non_null = n_rows - column.null_count
        entry = {
            "variable_order": order,
            "non_null_count": non_null,
            "missing_rate": round(column.null_count / n_rows, 4) if n_rows else 1.0,
            "is_all_missing": non_null == 0,
        }
        if _ordered(field.type) and non_null:
            bounds = pc.min_max(column)
            entry["data_min"] = _json_value(bounds["min"].as_py())
            entry["data_max"] = _json_value(bounds["max"].as_py())
        entry["parquet"] = {
            "arrow_type": str(field.type),
            "xml_type": meta.get("xml_type"),
        }
        if "cast_fallback" in meta:
            entry["parquet"]["cast_fallback"] = meta["cast_fallback"]

        system = meta.get("system") == "true"
        node = contract.nodes.get(meta.get("node_path"))
        # A system column can also be a form field (duration() exports as `duration`);
        # notes and unmapped columns have no question.
        question = by_name.get(field.name if system else node.name if node else None)
        if system:
            entry["system"] = True
        if question is None:
            entry.update(question=None, survey=None)
        else:
            entry["question"] = question["variable_name"]
            if not system:
                mapped = contract.map_column(field.name)
                if mapped["repeat_iterations"]:
                    entry["repeat_iteration"] = [
                        i for _, i in mapped["repeat_iterations"]
                    ]
            if "choice_value" in meta:
                # A select_multiple binary stands for one choice: 1 chosen, 0 not.
                entry["choice_value"] = meta["choice_value"]
                label = next(
                    c["label"]
                    for c in question["choices"]
                    if c["value"] == meta["choice_value"]
                )
                entry["value_labels"] = [
                    {"value": "1", "label": label},
                    {"value": "0", "label": "not chosen"},
                ]
            elif question["type"] == "select_one":
                entry["value_labels"] = question["choices"]
            entry["survey"] = {k: question[k] for k in _SURVEY_KEYS}
            entry["survey"]["original_variable_name"] = question["variable_name"]
            entry["survey"]["group_path"] = "/".join(question["group_path"])

        found = _sentinel_counts(column, sentinels)
        if found:
            entry["sentinels"] = found
        variables[field.name] = entry

    return {
        "dataset": {
            "name": contract.formid,
            "form_version": contract.formdef_version,
            "n_observations": n_rows,
            "n_variables": table.num_columns,
        },
        "summary": {
            "n_questions": len(questions),
            "n_system": sum(1 for v in variables.values() if v.get("system")),
            "n_all_missing": sum(1 for v in variables.values() if v["is_all_missing"]),
            "n_cast_fallback": sum(
                1 for v in variables.values() if "cast_fallback" in v["parquet"]
            ),
            "by_arrow_type": dict(
                Counter(v["parquet"]["arrow_type"] for v in variables.values())
            ),
        },
        "variables": variables,
    }


def build_graph(questions: list[dict], vardict: dict) -> dict:
    """Return the question graph as node-link JSON plus the repeat-group tree."""
    names = {q["variable_name"] for q in questions}
    repeats = {
        q["repeat_group_name"]: q for q in questions if q["type"] == "repeat_count"
    }
    columns: dict[str, list[str]] = {}
    for column, entry in vardict["variables"].items():
        if entry["question"] is not None:
            columns.setdefault(entry["question"], []).append(column)

    def repeat_chain(group_path: list[str]) -> list[str]:
        return [g for g in group_path if g in repeats]

    nodes, edges = [], []
    members: dict[str, list[str]] = {}
    for q in questions:
        name = q["variable_name"]
        chain = repeat_chain(q["group_path"])
        nodes.append(
            {
                "id": name,
                "type": q["type"],
                "group_path": "/".join(q["group_path"]),
                "repeat_depth": len(chain),
                "repeat_group": chain[-1] if chain else None,
                "columns": columns.get(name, []),
            }
        )
        if chain:
            members.setdefault(chain[-1], []).append(name)
        sources = [
            ("calculates_from", [q["calculation"]]),
            ("gated_by", [q["relevance"]]),
            ("group_gated_by", q["group_relevances"]),
            ("constrained_by", [q["constraint"]]),
        ]
        for kind, exprs in sources:
            refs = dict.fromkeys(r for e in exprs if e for r in _REF_RE.findall(e))
            edges += [
                {"source": r, "target": name, "type": kind} for r in refs if r in names
            ]

    for group in members.values():
        for left in group:
            edges += [
                {"source": left, "target": right, "type": "repeat_sibling"}
                for right in group
                if right != left
            ]

    repeat_groups = {}
    for name, count_row in repeats.items():
        parents = repeat_chain(count_row["group_path"])
        repeat_groups[name] = {
            "parent": parents[-1] if parents else None,
            "depth": len(parents) + 1,
            "count": count_row["calculation"],
            "members": members.get(name, []),
        }
    return {
        "directed": True,
        "multigraph": True,
        "nodes": nodes,
        "edges": edges,
        "repeat_groups": repeat_groups,
    }


def write_json(data: dict, path) -> None:
    """Write ``data`` as indented UTF-8 JSON."""
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
