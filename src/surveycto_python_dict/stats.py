"""Summary statistics per column that follow skip logic and leave sentinel codes out.

A row counts toward a column only where the question was asked: every enclosing
group relevance and the question's own relevance evaluate true on that row (and
repeat instance). Sentinel codes (-99, ...) stay coded in the data; here they are
counted apart and excluded from every statistic. Columns flagged ``contains_pii`` or
``may_contain_pii`` get counts only, never values. One CSV row per parquet column.
"""

import csv
import datetime as dt
import json
import statistics
from pathlib import Path

import pyarrow.parquet as pq

from surveycto_python_dict.xpath import UnsupportedExpression, compile_expression

_FLAGGED = {"contains_pii", "may_contain_pii"}

COLUMNS = [
    "variable",
    "question",
    "type",
    "pii_status",
    "relevance",
    "eligibility_evaluated",
    "eligibility_note",
    "rows",
    "eligible",
    "skipped_by_logic",
    "answered",
    "sentinel",
    "sentinel_detail",
    "missing_when_eligible",
    "answered_when_skipped",
    "n_valid",
    "mean",
    "sd",
    "min",
    "p25",
    "median",
    "p75",
    "max",
    "selected",
    "selected_share",
    "distinct",
    "frequencies",
]


def _plain(value):
    """Return a value the expression evaluator reads: dates as ISO text."""
    if isinstance(value, (dt.date, dt.datetime)):
        return value.isoformat()
    return value


class _Data:
    """Column values plus the lookups relevance needs: question -> column per instance."""

    def __init__(self, table, questions: dict, variables: dict):
        self.values = {
            name: [_plain(v) for v in col] for name, col in table.to_pydict().items()
        }
        self.questions = questions
        self.repeats = {
            q["repeat_group_name"]
            for q in questions.values()
            if q["type"] == "repeat_count"
        }
        self.columns: dict[tuple, str] = {}
        self.binaries: dict[tuple, list[tuple[str, str]]] = {}
        for column, entry in variables.items():
            if entry["question"] is None:
                continue
            key = (entry["question"], tuple(entry.get("repeat_iteration", [])))
            if "choice_value" in entry:
                self.binaries.setdefault(key, []).append(
                    (entry["choice_value"], column)
                )
            else:
                self.columns[key] = column

    def repeat_chain(self, question: str) -> list[str]:
        return [g for g in self.questions[question]["group_path"] if g in self.repeats]

    def value(self, question: str, iterations: tuple[int, ...], row: int):
        """Return ``question``'s value on ``row`` in the given repeat instance."""
        key = (question, iterations)
        if key in self.binaries:
            chosen = [
                code for code, col in self.binaries[key] if self.values[col][row] == 1
            ]
            answered = any(
                self.values[col][row] is not None for _, col in self.binaries[key]
            )
            return " ".join(chosen) if answered else None
        column = self.columns.get(key)
        return None if column is None else self.values[column][row]


class _Row:
    """The evaluation context of one column on one row."""

    def __init__(self, data: _Data, question: str, iterations: tuple, row: int, own):
        self.data, self.question, self.iterations, self.row, self.own = (
            data,
            question,
            iterations,
            row,
            own,
        )

    def ref(self, name: str):
        if name == ".":
            return self.own
        if name not in self.data.questions:
            raise UnsupportedExpression(f"${{{name}}} is not a question in the data")
        chain = self.data.repeat_chain(name)
        if chain != self.data.repeat_chain(self.question)[: len(chain)]:
            raise UnsupportedExpression(
                f"${{{name}}} sits in a repeat that does not enclose {self.question}"
            )
        return self.data.value(name, self.iterations[: len(chain)], self.row)

    def index(self) -> int:
        if not self.iterations:
            raise UnsupportedExpression("index() outside a repeat")
        return self.iterations[-1]


def _eligibility(
    data: _Data, q: dict | None, column: str, iterations: tuple, n_rows: int
):
    """Return (per-row eligibility, composed rule, evaluated, note)."""
    if q is None:
        return [True] * n_rows, "", True, ""
    gates = [*q["group_relevances"], *([q["relevance"]] if q["relevance"] else [])]
    rule = " and ".join(f"({g})" for g in gates)
    if q["disabled"]:
        return [False] * n_rows, rule, True, "disabled"
    own = data.values[column]
    try:
        compiled = [compile_expression(g) for g in gates]
        eligible = [
            all(
                bool(fn(_Row(data, q["variable_name"], iterations, r, own[r])))
                for fn in compiled
            )
            for r in range(n_rows)
        ]
    except UnsupportedExpression as e:
        return [True] * n_rows, rule, False, f"{e}; counted over all rows"
    return eligible, rule, True, ""


def _fmt(x: float) -> float:
    return round(x, 4)


def summary_stats(
    form_dir: Path, form_id: str, sentinels: dict[int, str]
) -> list[dict]:
    """Return one stats row per parquet column, in column order."""
    form_dir = Path(form_dir)
    table = pq.read_table(form_dir / f"{form_id}.parquet")
    questions = {
        q["variable_name"]: q
        for q in json.loads(
            (form_dir / f"{form_id}_questions.json").read_text(encoding="utf-8")
        )
    }
    dictionary = json.loads(
        (form_dir / f"{form_id}_variable_dictionary.json").read_text(encoding="utf-8")
    )
    variables = dictionary["variables"]
    data = _Data(table, questions, variables)
    n_rows = table.num_rows
    sentinel_codes = {
        **{c: m for c, m in sentinels.items()},
        **{str(c): m for c, m in sentinels.items()},
    }

    rows = []
    for column, entry in variables.items():
        q = questions.get(entry["question"]) if entry["question"] else None
        iterations = tuple(entry.get("repeat_iteration", []))
        eligible, rule, evaluated, note = _eligibility(
            data, q, column, iterations, n_rows
        )
        values = data.values[column]
        is_binary = "choice_value" in entry
        sentinel_hits: dict = {}
        valid = []
        answered = missing = outside = 0
        for v, ok in zip(values, eligible, strict=True):
            if v is None:
                missing += ok
                continue
            if not ok:
                outside += 1
                continue
            if not is_binary and not isinstance(v, bool) and v in sentinel_codes:
                sentinel_hits[v] = sentinel_hits.get(v, 0) + 1
                continue
            answered += 1
            valid.append(v)

        pii = entry["pii"]["status"]
        out = dict.fromkeys(COLUMNS, "")
        out.update(
            variable=column,
            question=entry["question"] or "",
            type=q["type"] if q else ("system" if entry.get("system") else ""),
            pii_status=pii,
            relevance=rule,
            eligibility_evaluated=evaluated,
            eligibility_note=note,
            rows=n_rows,
            eligible=sum(eligible),
            skipped_by_logic=n_rows - sum(eligible),
            answered=answered,
            sentinel=sum(sentinel_hits.values()),
            sentinel_detail="; ".join(
                f"{c} {sentinel_codes[c]}: {n}"
                for c, n in sorted(sentinel_hits.items(), key=str)
            ),
            missing_when_eligible=missing,
            answered_when_skipped=outside,
            n_valid=len(valid),
        )
        if pii not in _FLAGGED and valid:
            out.update(_describe(valid, entry, q, is_binary))
        rows.append(out)
    return rows


def _describe(valid: list, entry: dict, q: dict | None, is_binary: bool) -> dict:
    """Value-level statistics for a column that may be read."""
    arrow_type = entry["parquet"]["arrow_type"]
    if is_binary:
        selected = sum(1 for v in valid if v == 1)
        return {"selected": selected, "selected_share": _fmt(selected / len(valid))}
    if arrow_type in ("int64", "double"):
        cuts = (
            statistics.quantiles(valid, n=4, method="inclusive")
            if len(valid) > 1
            else valid * 3
        )
        return {
            "mean": _fmt(statistics.fmean(valid)),
            "sd": _fmt(statistics.stdev(valid)) if len(valid) > 1 else "",
            "min": min(valid),
            "p25": _fmt(cuts[0]),
            "median": _fmt(cuts[1]),
            "p75": _fmt(cuts[2]),
            "max": max(valid),
        }
    if arrow_type.startswith(("date32", "timestamp")):
        ordered = sorted(valid)
        return {
            "min": ordered[0],
            "median": ordered[len(ordered) // 2],
            "max": ordered[-1],
        }
    if q is not None and q["type"] == "select_one":
        labels = {c["value"]: c["label"] for c in q["choices"]}
        counts = {code: 0 for code in labels}
        for v in valid:
            counts[v] = counts.get(v, 0) + 1
        return {
            "frequencies": "; ".join(
                f"{code} {labels.get(code, '(not a choice)')}: {n} ({n / len(valid):.1%})"
                for code, n in counts.items()
            )
        }
    return {"distinct": len(set(valid))}


def write_csv(rows: list[dict], path: Path) -> Path:
    """Write the stats rows as CSV (UTF-8 with BOM, so Excel reads accents)."""
    path = Path(path)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    return path


def main(argv: list[str] | None = None) -> None:
    """Entry point for ``scto-stats``: write ``<form>_summary_stats.csv`` from built outputs."""
    import argparse

    from surveycto_python_dict.cli import load_config

    parser = argparse.ArgumentParser(prog="scto-stats")
    parser.add_argument("--config", type=Path, default=Path("config.toml"))
    cfg = load_config(parser.parse_args(argv).config)
    rows = summary_stats(cfg.form_dir, cfg.form_id, cfg.sentinels)
    path = write_csv(rows, cfg.form_dir / f"{cfg.form_id}_summary_stats.csv")
    unevaluated = sum(1 for r in rows if not r["eligibility_evaluated"])
    flagged = sum(1 for r in rows if r["answered_when_skipped"])
    print(
        f"[stats] {cfg.form_id}: {len(rows)} columns, {unevaluated} with relevance not "
        f"evaluated, {flagged} answered where skip logic says skipped -> {path}"
    )
