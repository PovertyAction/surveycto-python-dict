# surveycto-python-dict

Pulls a project's SurveyCTO forms (compiled XML and wide JSON) and server datasets over
the API and builds, for each form, `questions.json`, sections, typed parquet, a variable
dictionary and graph, and a per-variable PII signal; for the project, `flow.json`, how
the forms chain through the datasets they publish and read.

## Use

```sh
just setup                           # uv sync, including the spaCy models; pre-commit hooks
cp config.example.toml config.toml   # set forms (and server_datasets)
just pull                            # every form's XML + submissions, every server dataset -> <out_dir>
just build                           # per form: dictionaries, parquet, graph, PII review; flow.json; rerun any time
just stats                           # <form>_summary_stats.csv over the rows where each question was asked
just codebook                        # <form>_codebook.xlsx for PIs (Variables + Summary stats) and the stats CSV
just duckdb                          # <project>.duckdb: a schema per form, datasets once, the project tables
just mcp                             # survey-expert MCP server over stdio, one per project
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

A project is one connected study: one or more forms on one server that chain through
the server datasets they publish and read (any number of datasets, none included). It
gets one `out_dir`, one `flow.json` and one database. Surveys that are not connected
are separate projects, each with its own config and `out_dir`. A project's encrypted
forms share the one key in `SCTO_KEY_PATH`.

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
| `<form>_codebook.xlsx` | `just codebook` (which writes `<form>_summary_stats.csv` too): for PIs. Variables (label, type, group, relevance, value labels, missing, sentinels, PII status) and Summary stats; no values of flagged PII columns |
| `<form>_dataset_codebook.json` | One row per column of each dataset the form reads: counts, the questions that read it, and a `pii` block (Presidio, or `dataset.column` on the whitelist/blacklist) |
| `long/<repeat>.parquet` | `just duckdb`: each repeat group long, one row per instance with any value, keyed by `KEY` and one `<repeat>_index` per enclosing repeat |

And for the project, in `<out_dir>`: `flow.json` and `flow.md` (below), and
`<project>.duckdb` with its map `<project>_db.json` / `<project>_db.sql`, where
`<project>` is the `out_dir` folder's name.

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
- **Origins.** Each published column takes the origin of the field behind it:
  `entered` (a question the enumerator answers, a `search()` pick included),
  `calculated` (from the form's own fields), `carried` (a calculate that traces back to
  a `pulldata()` from another dataset, which it names) or `metadata` (a preload).

`flow.md`, next to `flow.json`, draws the chain as a Mermaid diagram (Obsidian, VS Code
and GitHub render it): forms, datasets coloured by how they are filled (green from the
form's answers, orange carried, yellow both, grey external), publishes, and reads, dashed
when the form does not attach the dataset.

`uv run scto-survey --config config.toml --form <form> flow` prints the stages, each
dataset's publisher, origins and readers, and the reads without an attachment;
`flow <dataset>` traces each column from the field that publishes it, with its origin,
to the fields that read it.

## What the project is about: description and entities

The outputs say how the data is stored: a submission, a repeat instance, a dataset row.
What those rows are about (a household, a study, an endline wave) comes from you. Two
config sections hold it:

```toml
[project]
description = "In your words: the study, its design, its units and stages."

[entities.household]
key = "hhid"                         # the column that identifies one
description = "A household listed at baseline"

[entities.member]
key = "member_id"
description = "A household member"
parent = "household"
```

The survey-expert skill fills them with you. Give it what describes the study: a
pre-analysis plan, a replication paper, a protocol, a codebook. It reads them, lists the
candidate keys the outputs carry (`get_project`: each dataset's key and each column the
forms match on, with the forms, tables and datasets that hold it), proposes entities on
those keys and writes the sections once you confirm. The description is yours: the
skill asks for it rather than writing one from the questions. Share only documents you
are allowed to share with the agent: an embargoed plan or unpublished results may not
be.

`just flow` (seconds) rewrites `flow.json` and `flow.md` with them; a key no form or
dataset carries is an error. `get_project` then opens an agent's work on the project:
the description, the entities with where each lives, and the keys not yet named.

## Querying with DuckDB: one database per project

`just duckdb` (after `just stats` or `just codebook`) writes `<project>.duckdb`, one database for the whole
project. A database holds schemas and a schema holds tables and views:

```text
<project>.duckdb
├── <form>          one schema per form
│   ├── submissions, submissions_safe           views over <form>.parquet
│   ├── <repeat>, <repeat>_safe                 views over long/<repeat>.parquet
│   └── dictionary, questions, choices, edges, dataset_links, dataset_codebook, stats
├── dataset         each dataset once, under its flow.json id
│   └── <id>, <id>_safe                         views over raw/datasets/<id>.csv
└── main            the project, from flow.json
    └── forms, datasets, publishes, reads, joins
```

Data stays in its files: the views read `<form>.parquet`, `long/<repeat>.parquet` and
the dataset CSVs, so the database works next to its `out_dir` and `just duckdb` rebuilds
it in seconds. The description and project tables are copies of the JSON outputs. A
CSV attached to a form sits in `dataset` as `"<form>/<file>.csv"`. `[datasets.<id>] key`
in the config names the columns that identify a dataset's rows, checked unique.

`joins` connects the forms: a reader's field, the dataset column it matches, and the
publisher's field behind that column. Each form's `dataset_links` gives the same
matches within the form, so a query can follow a value from one form to the next as
the forms do. Select in SQL, clean in pandas:

```python
import duckdb

con = duckdb.connect("data/out/<project>/<project>.duckdb", read_only=True)
long = con.sql("SELECT * FROM <form>.<repeat>_safe").df()
chained = con.sql("""
    SELECT v.KEY, l.KEY AS listing_key
    FROM visit.submissions_safe v
    JOIN dataset.households_safe d ON d.hhid = v.hhid
    JOIN listing.submissions_safe l ON l.hhid = d.hhid
""").df()
```

The `*_safe` views hold only `cleared_by_form` and `whitelisted` columns (for a
dataset, the columns every reading form clears); agents read values through them.
Adding a column to `[pii] whitelist` (a dataset column as `dataset.column`) and
rebuilding lets it in.

## Survey-expert MCP server and skill

`just mcp` (`uv run scto-mcp --config config.toml`) serves a project's outputs over
stdio to a local agent. The form tools take the form's id: `lookup_variable(s)`,
`search_questions`, `get_choice_list`, `get_gate_chain`, `get_survey_info`,
`get_variable_neighborhood`, `get_repeat_structure`, `get_datasets`. The project tools
take none: `get_project` (the description, entities and candidate keys), `get_flow`
(the chain, or one dataset's columns end to end) and `get_structure_db` (a summary of `<project>.duckdb`, or one schema in full: a form's
tables with the datasets and cross-form joins it uses). `lookup_variable` follows a
field that reads a dataset back to the field another form publishes it from, with its
origin. It reads only the JSON outputs, never the parquet, and shows each column's
`pii` status.

To connect it to Claude Code, copy `.mcp.json.example` to `.mcp.json` (git-ignored) and
point `--config` at your config. The `survey-expert` skill
(`.claude/skills/survey-expert/SKILL.md`) teaches an agent to read the outputs and to
check `pii.status` first; without MCP it uses the same answers from
`uv run scto-survey --config config.toml --form <form>
<var|search|choices|gates|neighborhood|repeats|info|datasets|flow|db>`.

Design: `surveycto-api-extractor-PROMPT.md`; API notes: `docs/`.
