# surveycto-python-dict

Pulls a SurveyCTO form's compiled XML and wide JSON over the API and builds, from them,
`questions.json`, sections, typed parquet, a variable dictionary and graph, and a
per-variable PII signal.

## Use

```sh
just setup                           # uv sync, including the spaCy models; pre-commit hooks
cp config.example.toml config.toml   # set form_id
just pull                            # form XML + submissions (wide JSON) -> <out_dir>/<form_id>/raw
just build                           # dictionaries, parquet, graph, PII review; rerun any time
just stats                           # <form>_summary_stats.csv over the rows where each question was asked
just codebook                        # <form>_codebook.xlsx for PIs: Variables + Summary stats sheets
just mcp                             # survey-expert MCP server over stdio
```

Each recipe takes another config as an argument (`just build path/to/config.toml`);
`just` lists them all.

The repo follows IPA's `ipa-python-template`: `just lint` runs the pre-commit hooks
(ruff, codespell, markdownlint) that CI runs on every pull request to `main`.

Copy `.env.example` to `.env` and fill it in: `SCTO_SERVER` (the subdomain),
`SCTO_USER`, `SCTO_PASSWORD`, `SCTO_KEY_PATH` for encrypted forms, and
`SCTO_SENTINELS`, the study's non-response codes (`"-99=Don't know;-88=Refused;..."`),
which `just build` and `just stats` require. Never commit `.env`, the `.pem` key,
`config.toml` or `data/`.

Outputs in `<out_dir>/<form_id>/`:

| File | Content |
|---|---|
| `<form>_questions.json` | One entry per question, keyed by its XLSForm name |
| `sections/` | One slice of `questions.json` per group level |
| `<form>.parquet` | Submissions, typed from the XML |
| `<form>_variable_dictionary.json` | One entry per column: stats, types, sentinels, `pii` |
| `<form>_variable_graph.json` | Questions linked by calculation, relevance, constraint, repeat |
| `<form>_pii_review.xlsx` | For the people cleaning the data: a Columns sheet (each flagged column's label, status, basis and Presidio hit rows) and a Values sheet (`KEY` plus every column flagged `contains_pii` / `may_contain_pii`) |
| `<form>_summary_stats.csv` | `just stats`: per column, counts over the rows where it was asked (skip logic evaluated per row), sentinels apart, then mean/quartiles, frequencies or shares; flagged PII columns get counts only |
| `<form>_codebook.xlsx` | `just codebook`: for PIs. Variables (label, type, group, relevance, value labels, missing, sentinels, PII status) and Summary stats; no values of flagged PII columns |
| `<form>_dataset_codebook.json` | One row per column of each dataset the form reads: counts, the questions that read it, and a `pii` block (Presidio, or `dataset.column` on the whitelist/blacklist) |
| `<form>.duckdb` | `just duckdb` (runs `just stats` first): views over the parquet (`submissions`, `submissions_safe`, and each repeat group long as `<repeat>` / `<repeat>_safe`, one row per instance with any value), each dataset next to them (`<dataset>`, `<dataset>_safe`), and the descriptions as tables (`dictionary`, `questions`, `choices`, `edges`, `dataset_links`, `dataset_codebook`, `stats`) |

The `pii` block is a signal for the people cleaning the data, not enforcement:
`cleared_by_form` (publishable), `whitelisted`, `contains_pii`, `may_contain_pii`
or `no_hits`, with the basis for each. Review flagged columns in
`<form>_pii_review.xlsx` and add the ones you clear to `[pii] whitelist`; add
columns you know are personal, publishable or not, to `[pii] blacklist`. A name on both
lists is an error. Set `trust_publishable = false` when the form marks personal fields
publishable (to publish them to a dataset, say): publishable then clears nothing, and
those columns go through the type rules and Presidio like any other.

A `search()` select's choices come from its dataset, never from the XML. The build
scans each list with Presidio and adds it whole when nothing is found; any hit
withholds the whole list (`choices_withheld` gives the entities). A whitelisted select
gets its list unscanned; a blacklisted one never has its dataset read.

## Querying with DuckDB

`<form>.duckdb` holds views, not copies: `<form>.parquet` stays the only copy of the
submissions, and `just duckdb` rebuilds the file. Select in SQL, clean in pandas:

```python
import duckdb

con = duckdb.connect("data/out/<form>/<form>.duckdb", read_only=True)
long = con.sql("SELECT * FROM <repeat>_safe").df()
```

The `*_safe` views hold only `cleared_by_form` and `whitelisted` columns; agents read
values through them. Adding a column to `[pii] whitelist` (a dataset column as
`dataset.column`) and rebuilding lets it in. `dataset_links` says how each
`pulldata()` / `search()` matches dataset rows to the submission, so a script can join
the survey to its dataset as the form does.

## Survey-expert MCP server and skill

`just mcp` (`uv run scto-mcp --config config.toml`) serves one form's outputs over stdio to a local
agent: `lookup_variable(s)`, `search_questions`, `get_choice_list`, `get_gate_chain`,
`get_survey_info`, `get_variable_neighborhood`, `get_repeat_structure`. It reads only
the JSON outputs, never the parquet, and shows each column's `pii` status.

To connect it to Claude Code, copy `.mcp.json.example` to `.mcp.json` (git-ignored) and
point `--config` at your config. The `survey-expert` skill
(`.claude/skills/survey-expert/SKILL.md`) teaches an agent to read the outputs and to
check `pii.status` first; without MCP it uses the same answers from
`uv run scto-survey <var|search|choices|gates|neighborhood|repeats|info>`.

Design: `surveycto-api-extractor-PROMPT.md`; API notes: `docs/`.
