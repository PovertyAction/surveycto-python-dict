"""Answers the survey-expert MCP tools from one form's built outputs.

Reads ``<form>_questions.json``, ``<form>_variable_dictionary.json`` and
``<form>_variable_graph.json``; every answer is plain text. Nothing here reads the
parquet, so no tool returns a submission value.

Standard library only, so the file also ships in the survey-expert skill as
``search_survey.py`` and runs where the package is not installed::

    python search_survey.py --dir <out_dir>/<form_id> var <name>
"""

import difflib
import json
import math
import re
from collections import Counter
from pathlib import Path

_FLAGGED = {"contains_pii", "may_contain_pii"}
_TOKEN_RE = re.compile(r"\w+")
_TAG_RE = re.compile(r"<[^>]+>")
_REF_RE = re.compile(r"\$\{([^}]+)\}")


def _plain(text: str) -> str:
    """Return ``text`` without HTML tags, whitespace collapsed."""
    return " ".join(_TAG_RE.sub(" ", text).split())


def _tokens(text: str) -> list[str]:
    """Return the words of ``text`` (any script, accents kept), lowercased."""
    return _TOKEN_RE.findall(_plain(text).lower())


def _short(text: str, limit: int = 100) -> str:
    text = _plain(text)
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _withheld(q: dict) -> str:
    columns = q["choices"][0]
    return (
        f"choices withheld ({', '.join(q['choices_withheld'])}); read from the dataset "
        f"in `{q['appearance']}`, value column `{columns['value']}`, "
        f"label column `{columns['label']}`"
    )


_REF_ONLY = re.compile(r"\$\{([^}]+)\}")


def _source_line(source: dict) -> str:
    match = " and ".join(
        f"{source['dataset']}.{m['column']} = {m['value']}" for m in source["match"]
    )
    if source["kind"] == "pulldata":
        return f"pulldata: {source['dataset']}.{source['column']} where {match}"
    return f"search: choices from {source['dataset']}" + (
        f" where {match}" if match else ""
    )


def render_structure(project: str, objects: list[dict]) -> str:
    """Render ``<project>_db.json`` as commented SQL: grain, key, joins and columns."""
    lines = [
        f"-- {project}.duckdb: every table and view, what one row is, its key and joins.",
        "-- Read values only from the *_safe views; the description tables are open.",
    ]
    for o in objects:
        lines += ["", f"-- {o['kind']} {o['name']}: {o['grain']}"]
        lines.append(f"--   key: {', '.join(o['key'])}")
        lines += [f"--   join: {j}" for j in o["joins"]]
        columns = o["columns"]
        if len(columns) > 100:
            lines.append(
                f"--   columns: {len(columns)}, one row each in the dictionary table"
            )
        else:
            lines.append(
                f"--   columns ({len(columns)}): "
                + ", ".join(f"{c['name']} {c['type']}" for c in columns)
            )
    return "\n".join(lines)


def _leaf(path: str) -> str:
    return path.rsplit("/", 1)[-1]


_ORIGINS = ("entered", "calculated", "carried", "metadata")


def _origin_text(d: dict) -> str:
    """Return a published dataset's column origins: ``4 entered, 7 carried from x``."""
    counts = d["origin_counts"]
    carried_from = sorted({x for o in d["origins"].values() for x in o["from"]})
    return ", ".join(
        f"{counts[k]} {k}"
        + (f" from {', '.join(carried_from)}" if k == "carried" else "")
        for k in _ORIGINS
        if k in counts
    )


def _tone(d: dict) -> str:
    """Return how a dataset is filled: entered, carried, mixed or external."""
    if not d["published_by"]:
        return "external"
    counts = d["origin_counts"]
    if not counts.get("carried"):
        return "entered"
    return "mixed" if counts.get("entered") or counts.get("calculated") else "carried"


def _mermaid_id(prefix: str, name: str) -> str:
    return prefix + re.sub(r"\W", "_", name)


def _mermaid_text(text: str) -> str:
    return text.replace('"', "#quot;")


def render_mermaid(flow: dict) -> str:
    """Render ``flow.json`` as a Mermaid flowchart: forms, datasets, publishes, reads.

    A dataset's colour says how it is filled: green when its columns come from the
    publishing form's answers (entered or calculated from them), orange when they are
    carried from another dataset, yellow when both, grey when no project form fills it.
    """
    lines = ["flowchart LR"]
    for f in flow["forms"]:
        fid = _mermaid_id("f_", f["form_id"])
        lines.append(f'  {fid}["{_mermaid_text(f["form_id"])}<br/>stage {f["stage"]}"]')
        lines.append(f"  class {fid} form")
    for d in flow["datasets"]:
        did = _mermaid_id("d_", d["id"])
        label = f"{d['id']}<br/>{d['rows']} rows"
        if d["published_by"]:
            label += f"<br/>{_origin_text(d)}"
        lines.append(f'  {did}[("{_mermaid_text(label)}")]')
        lines.append(f"  class {did} {_tone(d)}")
        if d["published_by"]:
            lines.append(
                f"  {_mermaid_id('f_', d['published_by'])} -->|publishes| {did}"
            )
    for r in flow["reads"]:
        arrow = "-->" if r["attached"] else "-.->"
        label = f"{len(r['fields'])} fields" + (
            "" if r["attached"] else ", not attached"
        )
        lines.append(
            f"  {_mermaid_id('d_', r['dataset'])} {arrow}|{label}| "
            f"{_mermaid_id('f_', r['form'])}"
        )
    lines += [
        "  classDef form fill:#e3f2fd,stroke:#1565c0",
        "  classDef entered fill:#c8e6c9,stroke:#2e7d32",
        "  classDef carried fill:#ffe0b2,stroke:#ef6c00",
        "  classDef mixed fill:#fff59d,stroke:#f9a825",
        "  classDef external fill:#eeeeee,stroke:#757575",
    ]
    return "\n".join(lines)


def render_flow(flow: dict, dataset: str | None = None) -> str:
    """Render ``flow.json``: forms by stage, then each dataset's publisher and readers.

    With ``dataset``, list its columns instead: the field each one is published from
    and the fields that read it.
    """
    datasets = {d["id"]: d for d in flow["datasets"]}
    if dataset is not None:
        d = datasets[dataset]
        lines = [
            f"{d['id']} ({d['title']}): {d['rows']} rows, key {d['key'] or '-'}, "
            f"published by {d['published_by'] or 'no project form (external)'}"
        ]
        readers: dict[str, list[str]] = {}
        for r in flow["reads"]:
            if r["dataset"] != dataset:
                continue
            for f in r["fields"]:
                for column in f["columns"]:
                    readers.setdefault(column, []).append(
                        f"{r['form']}.{_leaf(f['field'])} ({f['via']})"
                    )
        for column in d["columns"]:
            source = d["publish_map"].get(column)
            line = f"  {column}"
            if source:
                origin = d["origins"][column]
                line += f"  <- {d['published_by']}.{source} ({origin['kind']}"
                line += f" from {', '.join(origin['from'])})" if origin["from"] else ")"
            if column in readers:
                line += f"  -> {', '.join(readers[column])}"
            lines.append(line)
        return "\n".join(lines)

    lines = []
    for stage in sorted({f["stage"] for f in flow["forms"]}):
        lines.append(f"Stage {stage}:")
        for f in flow["forms"]:
            if f["stage"] != stage:
                continue
            line = f"  {f['form_id']}"
            if f["reads_from"]:
                line += f"  (reads datasets published by {', '.join(f['reads_from'])})"
            lines.append(line)
            if f["publishes"]:
                lines.append(f"    publishes: {', '.join(f['publishes'])}")
    lines += ["", "Datasets:"]
    for d in flow["datasets"]:
        source = (
            f"published by {d['published_by']} ({_origin_text(d)})"
            if d["published_by"]
            else ("attached file" if d["kind"] == "form_file" else "external")
        )
        readers = ", ".join(d["read_by"]) or "no project form"
        lines.append(
            f"  {d['id']}: {d['rows']} rows, key {d['key'] or '-'}; {source}; "
            f"read by {readers}"
        )
    unattached = [r for r in flow["reads"] if not r["attached"]]
    if unattached:
        lines += ["", "Read without being attached to the form on the server:"]
        lines += [
            f"  {r['form']} reads {r['dataset']} ("
            + ", ".join(_leaf(f["field"]) for f in r["fields"])
            + ")"
            for r in unattached
        ]
    return "\n".join(lines)


class SurveyStore:
    """One form's questions, dictionary and graph."""

    def __init__(self, form_dir: Path, form_id: str):
        """Load the three JSON outputs from ``form_dir``."""

        def load(suffix: str):
            path = Path(form_dir) / f"{form_id}_{suffix}.json"
            return json.loads(path.read_text(encoding="utf-8"))

        self.form_id = form_id
        self.form_dir = Path(form_dir)
        self.questions = {q["variable_name"]: q for q in load("questions")}
        dictionary = load("variable_dictionary")
        self.dataset, self.summary = dictionary["dataset"], dictionary["summary"]
        self.variables = dictionary["variables"]
        self.graph = load("variable_graph")
        self.columns = {n["id"]: n["columns"] for n in self.graph["nodes"]}
        self.repeat_of = {n["id"]: n["repeat_group"] for n in self.graph["nodes"]}
        self.dataset_columns = load("dataset_codebook")
        self.review_file = f"{form_id}_pii_review.xlsx"

    # -- lookups -------------------------------------------------------------------

    def _column_line(self, name: str) -> str:
        v = self.variables[name]
        parts = [
            f"  {name}  {v['parquet']['arrow_type']}  missing {v['missing_rate']:.1%}"
        ]
        if "choice_value" in v:
            parts.append(
                ", ".join(f"{c['value']} = {c['label']}" for c in v["value_labels"])
            )
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
        if q["choices_withheld"]:
            lines.append(_withheld(q))
        elif q["choices"]:
            lines.append(
                "Choices: "
                + "; ".join(f"{c['value']} = {c['label']}" for c in q["choices"])
            )
        if q["data_source"]:
            lines.append(f"Data source: {_source_line(q['data_source'])}")
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
        close = difflib.get_close_matches(
            name, list(dict.fromkeys([*self.questions, *self.variables])), n=5
        )
        hint = f" Close matches: {', '.join(close)}." if close else ""
        return f"No variable or question named {name!r}.{hint}"

    def lookup_variables(self, names: list[str]) -> str:
        """Describe several variables."""
        return "\n\n".join(self.lookup_variable(n) for n in names)

    def search_questions(self, query: str, max_results: int = 20) -> str:
        """Rank questions by the IDF of the query words their name or text contains."""
        docs = {
            n: _tokens(f"{n.replace('_', ' ')} {q['question_text']}")
            for n, q in self.questions.items()
        }
        df = Counter(t for toks in docs.values() for t in set(toks))
        idf = {t: math.log(len(docs) / df[t]) + 1 for t in df}
        # Words in more than half the questions ("de", "the") only rank when alone.
        terms = [t for t in _tokens(query) if df.get(t, 0) <= len(docs) / 2] or _tokens(
            query
        )
        # Each word counts once per question, so long texts do not win on common words.
        scores = {
            n: sum(idf.get(t, 0) for t in set(terms) if t in set(toks))
            for n, toks in docs.items()
        }
        ranked = sorted(
            ((s, n) for n, s in scores.items() if s > 0), key=lambda x: (-x[0], x[1])
        )[:max_results]
        if not ranked:
            return f"No question matches {query!r}."
        return "\n".join(
            f"{n} ({self.questions[n]['type']}): {_short(self.questions[n]['question_text'])}"
            for _, n in ranked
        )

    def get_choice_list(self, name: str) -> str:
        """List a select question's choices."""
        q = self.questions.get(name)
        if q is None:
            return self.lookup_variable(name)
        if q["choices_withheld"]:
            return f"{name} ({q['type']}): {_withheld(q)}"
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

    def _table_of(self, value: str | None) -> str:
        """Return the table a ``${field}`` lives in: submissions or its repeat view."""
        match = _REF_ONLY.fullmatch(value or "")
        if match is None:
            return "a literal or expression"
        return self.repeat_of.get(match.group(1)) or "submissions"

    def get_datasets(self) -> str:
        """Describe each dataset the form reads and how its rows join to the form."""
        by_dataset: dict[str, list[dict]] = {}
        for column in self.dataset_columns:
            by_dataset.setdefault(column["dataset"], []).append(column)
        if not by_dataset:
            return "The form reads no datasets."
        flow = json.loads((self.form_dir.parent / "flow.json").read_text("utf-8"))
        id_of = {
            r["name_in_form"]: r["dataset"]
            for r in flow["reads"]
            if r["form"] == self.form_id
        }
        project = self.form_dir.parent.name
        blocks = []
        for dataset, columns in by_dataset.items():
            n_rows = columns[0]["rows"]
            statuses = dict(Counter(c["pii"]["status"] for c in columns))
            view = f"dataset.{id_of[dataset]}"
            lines = [
                f"{dataset}: {n_rows} rows, {len(columns)} columns, pii {statuses}",
                f"  In {project}.duckdb: {view} (every column, plus row) and "
                f"{view}_safe (row plus cleared columns).",
                "  Joins to the form (dataset column = form value):",
            ]
            joins: dict[tuple, list[str]] = {}
            for q in self.questions.values():
                source = q["data_source"]
                if not source or source["dataset"] != dataset:
                    continue
                for m in source["match"] or [{"column": None, "value": None}]:
                    what = (
                        f"{q['variable_name']} reads {source['column']}"
                        if source["kind"] == "pulldata"
                        else f"{q['variable_name']} takes its choices from these rows"
                    )
                    joins.setdefault((m["column"], m["value"]), []).append(what)
            by_name = {c["column"]: c for c in columns}
            for (column, value), uses in joins.items():
                if column is None:
                    lines.append(f"    (no filter): {'; '.join(uses)}")
                    continue
                key = by_name[column]
                unique = (
                    "unique in the dataset: one dataset row per match"
                    if key["distinct"] == key["non_null"] == n_rows
                    else f"not unique: {key['distinct']} distinct in {key['non_null']} rows"
                )
                lines.append(
                    f"    {dataset}.{column} = {value} (in {self._table_of(value)}); "
                    f"{unique}"
                )
                lines += [f"      {use}" for use in uses]
            lines.append("  Columns:")
            lines += [
                f"    {c['column']}  non-null {c['non_null']}/{n_rows}, "
                f"distinct {c['distinct']}  pii: {c['pii']['status']}"
                + (f"  read by {', '.join(c['read_by'])}" if c["read_by"] else "")
                for c in columns
            ]
            blocks.append("\n".join(lines))
        return "\n\n".join(blocks)

    def get_structure_db(self) -> str:
        """Map ``<project>.duckdb``: each object, what one row is, its key and joins."""
        project = self.form_dir.parent.name
        path = self.form_dir.parent / f"{project}_db.json"
        objects = json.loads(path.read_text(encoding="utf-8"))
        return render_structure(project, objects)

    def get_flow(self, dataset: str | None = None) -> str:
        """Render the project's ``flow.json``, or one dataset's columns with ``dataset``."""
        path = self.form_dir.parent / "flow.json"
        return render_flow(json.loads(path.read_text(encoding="utf-8")), dataset)

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


def _open(config: Path | None, directory: Path, form: str | None) -> SurveyStore:
    """Open a form's outputs named by a config file, or the one form in ``directory``.

    A config with several forms needs ``form``.
    """
    if config is not None:
        import tomllib

        data = tomllib.loads(config.read_text(encoding="utf-8"))
        forms = data["forms"]
        if form is None:
            if len(forms) > 1:
                raise ValueError(f"the config has several forms; pass --form: {forms}")
            (form,) = forms
        if form not in forms:
            raise ValueError(f"{form!r} is not in the config's forms: {forms}")
        return SurveyStore(config.parent / data["out_dir"] / form, form)
    found = sorted(directory.glob("*_questions.json"))
    if len(found) != 1:
        raise FileNotFoundError(
            f"expected one <form>_questions.json in {directory}, found {len(found)}"
        )
    return SurveyStore(directory, found[0].name.removesuffix("_questions.json"))


def main(argv: list[str] | None = None, store: SurveyStore | None = None) -> None:
    """Entry point for ``scto-survey`` and ``search_survey.py``: the MCP tools as commands."""
    import argparse

    parser = argparse.ArgumentParser(prog="scto-survey")
    parser.add_argument(
        "--config", type=Path, help="config.toml naming the forms and out_dir"
    )
    parser.add_argument("--form", help="which form, when the config has several")
    parser.add_argument(
        "--dir",
        type=Path,
        default=Path("."),
        help="a form's output folder (default: here)",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("var", help="describe questions or columns").add_argument(
        "names", nargs="+"
    )
    search = commands.add_parser("search", help="find questions by name or text")
    search.add_argument("query", nargs="+")
    search.add_argument("--max", type=int, default=20)
    commands.add_parser("choices", help="a select's choices").add_argument("name")
    commands.add_parser("gates", help="the relevance chain").add_argument("name")
    hood = commands.add_parser("neighborhood", help="related questions")
    hood.add_argument("name")
    hood.add_argument("--depth", type=int, default=1)
    commands.add_parser("repeats", help="repeat-group structure")
    commands.add_parser("info", help="form summary")
    commands.add_parser("datasets", help="datasets the form reads and their joins")
    commands.add_parser("db", help="map of <project>.duckdb: tables, keys and joins")
    flow = commands.add_parser("flow", help="how the project's forms chain")
    flow.add_argument("dataset", nargs="?", help="one dataset's columns, end to end")
    args = parser.parse_args(argv)

    if store is None:
        store = _open(args.config, args.dir, args.form)
    answer = {
        "var": lambda: store.lookup_variables(args.names),
        "search": lambda: store.search_questions(" ".join(args.query), args.max),
        "choices": lambda: store.get_choice_list(args.name),
        "gates": lambda: store.get_gate_chain(args.name),
        "neighborhood": lambda: store.get_variable_neighborhood(args.name, args.depth),
        "repeats": store.get_repeat_structure,
        "info": store.get_survey_info,
        "datasets": store.get_datasets,
        "db": store.get_structure_db,
        "flow": lambda: store.get_flow(args.dataset),
    }[args.command]()
    print(answer)


if __name__ == "__main__":
    main()
