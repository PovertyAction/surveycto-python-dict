"""Answers the survey-expert MCP tools from one form's built outputs.

Reads ``<form>_questions.json``, ``<form>_variable_dictionary.json`` and
``<form>_variable_graph.json``; every answer is plain text. Nothing here reads the
parquet, so no tool returns a submission value.
"""

import difflib
import json
import math
import re
from collections import Counter
from pathlib import Path

_FLAGGED = {"contains_pii", "may_contain_pii"}
_TOKEN_RE = re.compile(r"[a-z0-9]+")
_REF_RE = re.compile(r"\$\{([^}]+)\}")


def _tokens(text: str) -> list[str]:
    return _TOKEN_RE.findall(text.lower())


class SurveyStore:
    """One form's questions, dictionary and graph."""

    def __init__(self, form_dir: Path, form_id: str):
        """Load the three JSON outputs from ``form_dir``."""

        def load(suffix: str):
            path = Path(form_dir) / f"{form_id}_{suffix}.json"
            return json.loads(path.read_text(encoding="utf-8"))

        self.form_id = form_id
        self.questions = {q["variable_name"]: q for q in load("questions")}
        dictionary = load("variable_dictionary")
        self.dataset, self.summary = dictionary["dataset"], dictionary["summary"]
        self.variables = dictionary["variables"]
        self.graph = load("variable_graph")
        self.columns = {n["id"]: n["columns"] for n in self.graph["nodes"]}
        self.review_file = f"{form_id}_pii_review.parquet"

    # -- lookups -------------------------------------------------------------------

    def _column_line(self, name: str) -> str:
        v = self.variables[name]
        parts = [
            f"  {name}  {v['parquet']['arrow_type']}  missing {v['missing_rate']:.1%}"
        ]
        if "data_min" in v:
            parts.append(f"range {v['data_min']}..{v['data_max']}")
        if "sentinels" in v:
            codes = ", ".join(
                f"{c} {s['label']} x{s['count']}" for c, s in v["sentinels"].items()
            )
            parts.append(f"sentinels {codes}")
        if "cast_fallback" in v["parquet"]:
            parts.append(f"kept as text: {v['parquet']['cast_fallback']}")
        pii = v["pii"]
        parts.append(f"pii: {pii['status']} ({', '.join(pii['basis'])})")
        return "  ".join(parts)

    def _render_question(self, q: dict) -> str:
        lines = [f"{q['variable_name']} ({q['type']})"]
        if q["question_text"]:
            lines.append(f"Text: {q['question_text']}")
        if q["group_path"]:
            lines.append(f"Group: {'/'.join(q['group_path'])}")
        for label, key in (
            ("Relevance", "relevance"),
            ("Constraint", "constraint"),
            ("Calculation", "calculation"),
        ):
            if q[key]:
                lines.append(f"{label}: {q[key]}")
        for gate in q["group_relevances"]:
            lines.append(f"Group relevance: {gate}")
        if q["choices"]:
            lines.append(
                "Choices: "
                + "; ".join(f"{c['value']} = {c['label']}" for c in q["choices"])
            )
        if q["publishable"]:
            lines.append("Publishable: yes")
        columns = self.columns.get(q["variable_name"], [])
        lines.append("Columns:" if columns else "Columns: none in the data")
        lines += [self._column_line(c) for c in columns]
        if any(self.variables[c]["pii"]["status"] in _FLAGGED for c in columns):
            lines.append(
                f"PII: check pii.status before reading these values; review {self.review_file}."
            )
        return "\n".join(lines)

    def lookup_variable(self, name: str) -> str:
        """Describe a question, or the column ``name`` and its question."""
        if name in self.questions:
            return self._render_question(self.questions[name])
        if name in self.variables:
            question = self.variables[name]["question"]
            if question is not None:
                return self._render_question(self.questions[question])
            return f"{name} (column without a question)\n{self._column_line(name)}"
        close = difflib.get_close_matches(name, [*self.questions, *self.variables], n=5)
        hint = f" Close matches: {', '.join(close)}." if close else ""
        return f"No variable or question named {name!r}.{hint}"

    def lookup_variables(self, names: list[str]) -> str:
        """Describe several variables."""
        return "\n\n".join(self.lookup_variable(n) for n in names)

    def search_questions(self, query: str, max_results: int = 20) -> str:
        """Rank questions by TF-IDF over their name and text."""
        docs = {
            n: _tokens(f"{n.replace('_', ' ')} {q['question_text']}")
            for n, q in self.questions.items()
        }
        df = Counter(t for toks in docs.values() for t in set(toks))
        idf = {t: math.log(len(docs) / df[t]) + 1 for t in df}
        terms = _tokens(query)
        scores = {
            n: sum(Counter(toks)[t] * idf.get(t, 0) for t in terms)
            for n, toks in docs.items()
        }
        ranked = sorted(
            ((s, n) for n, s in scores.items() if s > 0), key=lambda x: (-x[0], x[1])
        )[:max_results]
        if not ranked:
            return f"No question matches {query!r}."
        return "\n".join(
            f"{n} ({self.questions[n]['type']}): {self.questions[n]['question_text']}"
            for _, n in ranked
        )

    def get_choice_list(self, name: str) -> str:
        """List a select question's choices."""
        q = self.questions.get(name)
        if q is None:
            return self.lookup_variable(name)
        if not q["choices"]:
            return f"{name} ({q['type']}) is not a select question."
        return f"{name} ({q['type']}):\n" + "\n".join(
            f"  {c['value']} = {c['label']}" for c in q["choices"]
        )

    def get_gate_chain(self, name: str, depth: int = 3) -> str:
        """Show the conditions that must hold for ``name`` to be asked, and their gates."""
        if name not in self.questions:
            return self.lookup_variable(name)
        lines: list[str] = []
        seen: set[str] = set()

        def walk(var: str, level: int) -> None:
            q = self.questions[var]
            pad = "  " * level
            gates = [
                *q["group_relevances"],
                *([q["relevance"]] if q["relevance"] else []),
            ]
            lines.append(
                f"{pad}{var}: {' and '.join(f'({g})' for g in gates) or 'always asked'}"
            )
            seen.add(var)
            if level >= depth:
                return
            for ref in dict.fromkeys(r for g in gates for r in _REF_RE.findall(g)):
                if ref in self.questions and ref not in seen:
                    walk(ref, level + 1)

        walk(name, 0)
        return "\n".join(lines)

    def get_variable_neighborhood(self, name: str, depth: int = 1) -> str:
        """List graph edges within ``depth`` hops of ``name``."""
        if name not in self.columns:
            return self.lookup_variable(name)
        reached, frontier = {name}, {name}
        for _ in range(depth):
            frontier = {
                other
                for e in self.graph["edges"]
                for node, other in (
                    (e["source"], e["target"]),
                    (e["target"], e["source"]),
                )
                if node in frontier and other not in reached
            }
            reached |= frontier
        edges = sorted(
            {
                f"{e['source']} -> {e['target']} ({e['type']})"
                for e in self.graph["edges"]
                if e["source"] in reached and e["target"] in reached
            }
        )
        return "\n".join(edges) or f"{name} has no relationships."

    def get_repeat_structure(self) -> str:
        """Show the repeat groups, their nesting, counts and members."""
        groups = self.graph["repeat_groups"]
        if not groups:
            return "The form has no repeat groups."
        return "\n".join(
            f"{name} (depth {g['depth']}, parent {g['parent'] or '-'}, count: {g['count']})\n"
            f"  members: {', '.join(g['members'])}"
            for name, g in groups.items()
        )

    def get_survey_info(self) -> str:
        """Summarise the form and its outputs."""
        d, s = self.dataset, self.summary
        top_groups = sorted(
            {q["group_path"][0] for q in self.questions.values() if q["group_path"]}
        )
        return "\n".join(
            [
                f"{d['name']} (version {d['form_version']}): submissions: {d['n_observations']}, "
                f"columns: {d['n_variables']}, questions: {s['n_questions']}",
                f"pii: {s['pii']}",
                f"columns kept as text: {s['n_cast_fallback']}; all missing: {s['n_all_missing']}",
                f"top-level groups: {', '.join(top_groups) or '-'}",
                f"PII review file: {self.review_file}",
            ]
        )
