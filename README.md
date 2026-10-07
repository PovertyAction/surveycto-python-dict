# surveycto-python-dict

Pulls a project's SurveyCTO forms (compiled XML and wide JSON) and server datasets over
the API and builds, for each form, `questions.json`, sections, typed parquet, a variable
dictionary and graph, and a per-variable PII signal; for the project, `flow.json`, how
the forms chain through the datasets they publish and read.

## Use

```sh
just setup                           # uv sync, including the spaCy models
cp config.example.toml config.toml   # set forms (and server_datasets)
just pull                            # every form's XML + submissions, every server dataset -> <out_dir>
just build                           # per form: dictionaries, parquet, graph, PII review; flow.json; rerun any time
just stats                           # <form>_summary_stats.csv over the rows where each question was asked
just codebook                        # <form>_codebook.xlsx for PIs: Variables + Summary stats sheets
just mcp                             # survey-expert MCP server over stdio
```

Each recipe takes another config as an argument (`just build path/to/config.toml`);
`just` lists them all.

Copy `.env.example` to `.env` and fill it in: `SCTO_SERVER` (the subdomain),
`SCTO_USER`, `SCTO_PASSWORD`, `SCTO_KEY_PATH` for encrypted forms, and
`SCTO_SENTINELS`, the study's non-response codes (`"-99=Don't know;-88=Refused;..."`),
which `just build` and `just stats` require. Never commit `.env`, the `.pem` key,
`config.toml` or `data/`.

`<out_dir>` is the project's folder. `raw/` holds the form list, the server's dataset
list and every server dataset pulled (`raw/datasets/<id>.csv`): the ones in
`server_datasets` and every one a form reads, each once. Each form has
`<out_dir>/<form>/raw/` (XML, submissions, attached CSVs) and its outputs in
`<out_dir>/<form>/`:

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
those columns go through the type rules and Presidio like any other. Set
`no_personal_data = true` for a project that holds none (study metadata, say): every
column, dataset column and `search()` list that is not blacklisted or device metadata
(`username`, `deviceid`, ...) is `whitelisted` with basis `project`, and Presidio
never runs.

A `search()` select's choices come from its dataset, never from the XML. The build
scans each list with Presidio and adds it whole when nothing is found; any hit
withholds the whole list (`choices_withheld` gives the entities). A whitelisted select
gets its list unscanned; a blacklisted one never has its dataset read.

## How the forms chain: `flow.json`

`just build` writes `<out_dir>/flow.json` from the XML and the pulled datasets:

- **Reads.** A form reads every dataset its `pulldata()` and `search()` name, matched to
  a server dataset id or an attached CSV regardless of case. Each read lists the
  fields and the dataset columns they use. A server dataset the form reads without
  attaching it on the server is kept and marked `attached: false`.
- **Publishes.** A form publishes a dataset when every dataset column is one of its
  fields: by the field's name, or by a repeat field's wide column (`score_2`).
  `publish_map` gives the field behind each column. When two forms cover a dataset the
  build stops; a dataset no form covers is external (uploaded, or published from
  outside the project).
- **Stages.** A form is stage 1 when it reads nothing another project form publishes,
  else one more than the latest stage it reads from.

`uv run scto-survey --config config.toml --form <form> flow` prints the stages, each
dataset's publisher and readers, and the reads without an attachment; `flow <dataset>`
traces each column from the field that publishes it to the fields that read it.

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
`get_survey_info`, `get_variable_neighborhood`, `get_repeat_structure`, and the
project's `get_flow`. It reads only the JSON outputs, never the parquet, and shows each
column's `pii` status. A config with several forms needs `--form <form>`: one server
per form.

To connect it to Claude Code, copy `.mcp.json.example` to `.mcp.json` (git-ignored) and
point `--config` at your config. The `survey-expert` skill
(`.claude/skills/survey-expert/SKILL.md`) teaches an agent to read the outputs and to
check `pii.status` first; without MCP it uses the same answers from
`uv run scto-survey <var|search|choices|gates|neighborhood|repeats|info>`.

Design: `surveycto-api-extractor-PROMPT.md`; API notes: `docs/`.
