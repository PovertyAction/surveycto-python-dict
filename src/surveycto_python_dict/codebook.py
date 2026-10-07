"""The xlsx outputs: the PI-facing codebook and the PII review workbook.

``<form>_codebook.xlsx`` has a Variables and a Summary stats sheet, built from the
outputs ``scto-build`` writes; it adds no submission values beyond the summary stats,
which give flagged PII columns counts only. ``<form>_pii_review.xlsx`` is the opposite:
the flagged columns' values, for the people cleaning the data.
"""

import datetime as dt
import json
import re
from pathlib import Path

import pyarrow as pa
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from surveycto_python_dict.stats import COLUMNS as STATS_COLUMNS
from surveycto_python_dict.stats import summary_stats

VARIABLE_COLUMNS = [
    "variable",
    "question",
    "label",
    "type",
    "group",
    "relevance",
    "value_labels",
    "arrow_type",
    "non_null",
    "missing_rate",
    "sentinels",
    "pii_status",
    "pii_basis",
]
_WRAPPED = {"label", "relevance", "value_labels", "frequencies", "sentinel_detail"}
_PERCENT = {"missing_rate", "selected_share"}
_TAG_RE = re.compile(r"<[^>]+>")


def plain_label(text: str) -> str:
    """Return a label without HTML tags, whitespace collapsed."""
    return " ".join(_TAG_RE.sub(" ", text).split())


def _variable_rows(variables: dict) -> list[list]:
    rows = []
    for name, v in variables.items():
        survey = v["survey"] or {}
        gates = [
            *survey.get("group_relevances", []),
            *filter(None, [survey.get("relevance")]),
        ]
        relevance = (
            gates[0] if len(gates) == 1 else " and ".join(f"({g})" for g in gates)
        )
        rows.append(
            [
                name,
                v["question"] or "",
                plain_label(survey.get("question_text") or ""),
                survey.get("type") or ("system" if v.get("system") else ""),
                survey.get("group_path", ""),
                relevance,
                "; ".join(
                    f"{c['value']} = {c['label']}" for c in v.get("value_labels", [])
                ),
                v["parquet"]["arrow_type"],
                v["non_null_count"],
                v["missing_rate"],
                "; ".join(
                    f"{code} {s['label']}: {s['count']}"
                    for code, s in v.get("sentinels", {}).items()
                ),
                v["pii"]["status"],
                ", ".join(v["pii"]["basis"]),
            ]
        )
    return rows


def _sheet(workbook: Workbook, title: str, header: list[str], rows: list[list]) -> None:
    ws = workbook.create_sheet(title)
    ws.append(header)
    for row in rows:
        ws.append([None if value == "" else value for value in row])
    for cell in ws[1]:
        cell.font = Font(bold=True)
        cell.fill = PatternFill("solid", fgColor="DDE5F0")
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    for index, name in enumerate(header, start=1):
        letter = get_column_letter(index)
        longest = max(len(str(c.value)) for c in ws[letter] if c.value is not None)
        ws.column_dimensions[letter].width = min(max(longest, len(name)) + 2, 60)
        for cell in ws[letter][1:]:
            if name in _WRAPPED:
                cell.alignment = Alignment(wrap_text=True, vertical="top")
            if name in _PERCENT and isinstance(cell.value, (int, float)):
                cell.number_format = "0.0%"


REVIEW_COLUMNS = ["column", "label", "status", "basis", "hit_rows", "non_null"]


def write_pii_review(table: pa.Table, vardict: dict, path: Path) -> Path:
    """Write ``KEY`` plus every flagged column to ``path``: a Columns and a Values sheet.

    Text stays text, so IDs keep their leading zeros. Timestamps are UTC, written
    without the zone, which Excel does not store.
    """
    variables = vardict["variables"]
    flagged = [
        c
        for c in table.schema.names
        if variables[c]["pii"]["status"] in ("contains_pii", "may_contain_pii")
    ]
    columns = [
        [
            c,
            plain_label((variables[c]["survey"] or {}).get("question_text") or ""),
            variables[c]["pii"]["status"],
            ", ".join(variables[c]["pii"]["basis"]),
            variables[c]["pii"].get("presidio", {}).get("hit_rows", ""),
            variables[c]["non_null_count"],
        ]
        for c in flagged
    ]
    values = [
        [
            v.replace(tzinfo=None) if isinstance(v, dt.datetime) else v
            for v in row.values()
        ]
        for row in table.select(["KEY", *flagged]).to_pylist()
    ]
    workbook = Workbook()
    workbook.remove(workbook.active)
    _sheet(workbook, "Columns", REVIEW_COLUMNS, columns)
    _sheet(workbook, "Values", ["KEY", *flagged], values)
    workbook.save(path)
    return path


def write_codebook(form_dir: Path, form_id: str, sentinels: dict[int, str]) -> Path:
    """Write ``<form>_codebook.xlsx`` next to the outputs; return its path."""
    form_dir = Path(form_dir)
    dictionary = json.loads(
        (form_dir / f"{form_id}_variable_dictionary.json").read_text(encoding="utf-8")
    )
    stats = summary_stats(form_dir, form_id, sentinels)
    workbook = Workbook()
    workbook.remove(workbook.active)
    _sheet(
        workbook, "Variables", VARIABLE_COLUMNS, _variable_rows(dictionary["variables"])
    )
    _sheet(
        workbook,
        "Summary stats",
        STATS_COLUMNS,
        [[row[c] for c in STATS_COLUMNS] for row in stats],
    )
    path = form_dir / f"{form_id}_codebook.xlsx"
    workbook.save(path)
    return path


def main(argv: list[str] | None = None) -> None:
    """Entry point for ``scto-codebook``."""
    import argparse

    from dotenv import load_dotenv

    from surveycto_python_dict.cli import env_sentinels, load_config

    parser = argparse.ArgumentParser(prog="scto-codebook")
    parser.add_argument("--config", type=Path, default=Path("config.toml"))
    cfg = load_config(parser.parse_args(argv).config)
    load_dotenv()
    sentinels = env_sentinels()
    for form in cfg.forms:
        path = write_codebook(cfg.form_dir(form), form, sentinels)
        print(f"[codebook] {form} -> {path}")
