# surveycto-python-dict

The tool carries a SurveyCTO project into cleaning. It pulls the project's forms
(compiled XML and submissions) and server datasets over the API, then builds for each
form a questions file, typed parquet, a variable dictionary and graph, summary stats, a
codebook and a per-column PII flag. For the project it builds `flow.json` (how the
forms chain through the datasets they publish and read), one DuckDB database and an
MCP server that explains all of it to an agent. It reports what the data holds; the
people cleaning it decide what to do.

## Before you start

- [uv](https://docs.astral.sh/uv/) and [just](https://just.systems); uv installs
  Python 3.11 or later for the project.
- About 40 MB of disk for spaCy's multilingual model, which Presidio uses to scan text
  for personal data. `just setup` downloads it once.
- A SurveyCTO user with API access and permission to download the project's data, and
  the private key if a form is encrypted.

## Quick start

```sh
git clone https://github.com/PovertyAction/surveycto-python-dict
cd surveycto-python-dict
just setup                           # uv sync with the spaCy model; pre-commit hooks
cp .env.example .env                 # server, user, password, key path, sentinel codes
cp config.example.toml config.toml   # the project's forms and out_dir
just run                             # pull, then every output
just mcp                             # the survey-expert MCP server, for an agent
```

`just` lists every recipe:

| Recipe | Command | What it does |
|---|---|---|
| `just pull` | `scto-pull` | Every form's XML and submissions and every server dataset the project uses, into `<out_dir>` |
| `just build` | `scto-build` | Per form: questions, sections, parquet, dictionary, graph, PII review, dataset codebook; then `flow.json` |
| `just stats` | `scto-stats` | `<form>_summary_stats.csv`, counted over the rows where each question was asked |
| `just codebook` | `scto-codebook` | `<form>_codebook.xlsx` for PIs, and the stats CSV |
| `just duckdb` | `scto-duckdb` | `<project>.duckdb` and its map |
| `just outputs` | `scto-outputs` | `build`, then the stats, the codebooks and the database |
| `just run` | | `pull`, then `outputs` (`scto-run` pulls and builds) |
| `just flow` | `scto-flow` | Rewrites `flow.json` and `flow.md` alone, after editing `[project]` or `[entities]` |
| `just mcp` | `scto-mcp` | The survey-expert MCP server over stdio |
| `just survey <args>` | `scto-survey` | The MCP's answers from the shell (below) |
| `just test`, `just lint` | | The offline test suite; the pre-commit hooks |

The recipes from `pull` to `mcp` take another config as an argument
(`just build path/to/config.toml`); `survey` takes it as `just config=path survey ...`.
Each command takes `--config config.toml`.

### Use from another project

```sh
uv add git+https://github.com/PovertyAction/surveycto-python-dict
uv run scto-skill          # the survey-expert skill -> .claude/skills/survey-expert
```

Copy `config.example.toml`, `.env.example` and `.mcp.json.example` from this repository
into the project and fill them in. The `justfile` recipes from `pull` to `mcp` can be
copied as they are. Rerun `scto-skill` after updating the package, so the skill's
`search_survey.py` matches it.

## Configure

`.env` holds the credentials and the study's codes:

- `SCTO_SERVER` (the subdomain), `SCTO_USER`, `SCTO_PASSWORD`, and `SCTO_KEY_PATH` for
  encrypted forms. A project's encrypted forms share the one key.
- `SCTO_SENTINELS`, the study's non-response codes
  (`"-99=Don't know;-88=Refused;..."`). `build`, `stats`, `codebook`, `outputs` and
  `run` require it. A question that declares one of these codes as a choice keeps its
  own label for it.
- `SCTO_DATE_SENTINELS`, optional: dates the team types into a date question to code
  the same answers (`"1900-01-01=Don't know"`). Date columns count them apart, as
  numbers count the codes.

`config.toml` names the project. `forms` (a list of distinct form ids) and `out_dir` are
required; `config.example.toml` documents every other key. A form with no submissions
yet builds: its questions, skip logic and dictionary are ready before fieldwork, and
its tables hold no rows.

A project is one connected study: one or more forms on one server that chain through
the server datasets they publish and read. It gets one `out_dir`, one `flow.json` and
one database. Surveys that are not connected are separate projects, each with its own
config and `out_dir`.

Submissions the project does not use (tests, say) can be left out of the counts:
`[submissions] exclude` takes one SQL condition on the parquet's columns
(`"username = 'test'"`), evaluated over every row of the table `submissions`. A window
can leave out the submissions a resubmission replaced
(`"row_number() OVER (PARTITION BY hhid ORDER BY SubmissionDate DESC) > 1"`), and a
subquery can read a file of `KEY`s by its absolute path. The dictionary's counts and
the stats then cover the other rows, `get_survey_info` says how many were left out,
and the parquet keeps every row. An error in the condition names only its type, so no
value reaches the terminal; run the condition in a script to see why it fails.

Never commit `.env`, the `.pem` key, `config.toml` or `<out_dir>`; see
[SECURITY.md](SECURITY.md).

## Outputs

`<out_dir>/raw/` holds the form list, the server's dataset list and every server
dataset pulled (`raw/datasets/<id>.csv`, the server's bytes): the ones in
`server_datasets` and every one a form reads, each once. Each form has
`<out_dir>/<form>/raw/` (XML, submissions, attached CSVs) and its outputs in
`<out_dir>/<form>/`:

| File | Content |
|---|---|
| `<form>_questions.json` | One entry per question, keyed by its XLSForm name |
| `sections/` | One slice of `questions.json` per group level |
| `<form>.parquet` | Submissions, typed from the XML; a note's or a group's column empty in every row is left out |
| `<form>_variable_dictionary.json` | One entry per column: counts, types, sentinels, `pii`, and the range of a column whose values are shown; a column with no question says what it is (`column_kind`: note, group, preload, `not_in_form`) |
| `<form>_variable_graph.json` | Questions linked by calculation, relevance, constraint, repeat |
| `<form>_pii_review.xlsx` | For the people cleaning the data: a Columns sheet (each flagged column's label, status, basis and Presidio hit rows) and a Values sheet, one row per value of a flagged column (`KEY`, column, value) |
| `<form>_summary_stats.csv` | Per column, counts over the rows where it was asked (skip logic evaluated per row), sentinels apart, then mean and quartiles, frequencies or shares; a flagged column gets counts only |
| `<form>_codebook.xlsx` | For PIs: Variables (label, type, group, relevance, value labels, missing, sentinels, PII status) and Summary stats; a flagged column gets counts only |
| `<form>_dataset_codebook.json` | One row per column of each dataset the form reads: counts, the questions that read it, and a `pii` block |
| `<form>_search_choices.parquet` | Every choice of every `search()` list, with the submission values a filter selects it by, and the choices of a blacklisted inline list: `<form>.choices` in the database |
| `long/<repeat>.parquet` | `just duckdb`: each repeat group long, one row per instance with any value, keyed by `KEY` and one `<repeat>_index` per enclosing repeat |

For the project, `<out_dir>` holds `flow.json` and `flow.md` (below), and
`<project>.duckdb` with its map `<project>_db.json` / `<project>_db.sql`, where
`<project>` is the `out_dir` folder's name.

## Personal data

Each column's `pii` block says where personal data may be, so the people cleaning the
data handle it carefully. The flag changes what the outputs show and nothing else: the
parquet, the long tables and every view in `<project>.duckdb` keep every column.
Cleaning scripts use flagged columns like any other (join on a coder's name,
deduplicate on a phone number). Removing a column is a delivery decision for the team.

The status is one of:

- `cleared_by_form`: the form marks the field publishable.
- `whitelisted`: on `[pii] whitelist`, or cleared by its type (numbers, dates, inline
  selects, system columns, calculates over cleared fields).
- `contains_pii`: on `[pii] blacklist`, collection metadata, a geo or attachment field,
  or a calculate that reads one of these.
- `may_contain_pii`: Presidio found personal data in its values, or most of them are
  8 to 12 digits (basis `digits`).
- `no_hits`: Presidio scanned it and found nothing.

`contains_pii` and `may_contain_pii` are the flagged statuses. An output an agent reads
(the MCP and the skill, the dictionary, the stats, the codebook, the DB map, error
messages) shows a flagged column's counts, null rates, distinct counts and `KEY`s, and
never its values. Every other status shows values too. Only `<form>_pii_review.xlsx`
lists flagged values, for the people who verify the flags.

Your rules decide first and the scan is a safety net. Birth dates and ID numbers look
like any other date or code, so the scan cannot find them: put them on the blacklist,
or mark them publishable when they may be shared.

The rules apply in order, and the first that matches decides:

1. **Your lists.** A name on `[pii] blacklist` is `contains_pii`, one on
   `[pii] whitelist` is `whitelisted`; a name on both lists is an error. Review the
   flagged columns in `<form>_pii_review.xlsx` and whitelist the ones you clear;
   blacklist the columns you know are personal, publishable or not.
2. **Publishable.** A field the form marks publishable is `cleared_by_form`. Set
   `trust_publishable = false` when the form marks personal fields publishable (to
   publish them to a dataset, say): publishable then clears nothing.
3. **Collection metadata.** Device columns (`username`, `deviceid`, ...), a field that
   preloads them, an enumerator field (it stores `"name (id)"`), and a calculate that
   calls `enumerator-id()`, `enumerator-name()` or `username()` are `contains_pii`
   under any name. So is a calculate whose formula reads such a field, a geo or
   attachment field or a blacklisted one, directly or through other calculates (basis
   `lineage:<field>`), and a `pulldata()` of a blacklisted `dataset.column`.
4. **Types.** Geo and attachment fields are `contains_pii`. System columns, numbers,
   dates, inline selects, calculates over cleared fields, and codes built only from the
   date and random numbers are `whitelisted`. An integer question whose values are
   mostly 8 to 12 digits is `may_contain_pii` (basis `digits`): a phone or an ID
   number stored as a number.
5. **The scan** reads the rest (text, `search()` selects, calculates fed by text or a
   dataset) one column at a time: Presidio with spaCy's multilingual model reads its
   distinct values after its question's label, a sample of 100 for short values and
   all of them for free text. A hit on a value many rows share, or a name or place in
   a column of repeated values, is a category and does not count. Most values of 8 to
   12 digits, a leading `+` and country code allowed, flag it too (basis `digits`).
   `may_contain_pii` on a hit, `no_hits` otherwise. A `pulldata()` calculate holding
   its dataset column's value takes the column's status, and a calculate reading a
   flagged field takes its flag (basis `lineage:<name>`).

Set `no_personal_data = true` for a project that holds none: every column, dataset
column and `search()` list that rules 1-3 do not decide is `whitelisted` with basis
`project` (geo and attachment fields included, and nothing carries from them), and
Presidio never runs.

A choice list is listed in the outputs or referred to. A `search()` select's choices
come from its dataset, plus the form's own literal choices (an "Other", say).
`choice_columns` names the dataset's value and label columns. The build scans each
list with Presidio and lists it whole when nothing is found. A list with a hit, a
blacklisted select's list, a list over 50 choices and a list filtered on a field whose
values are not shown are referred to instead: `choices_withheld` says why, and the
choices are in `<form>.choices`. A select_multiple whose list is over 50 choices or
filtered per submission has binaries only for the codes picked. When a list's codes
are not shown, the MCP names its binaries `<field>_<n>`.

## How the forms chain: `flow.json`

`just build` writes `<out_dir>/flow.json` from the XML and the pulled datasets:

- **Reads.** A form reads every dataset its `pulldata()` and `search()` name, matched to
  a server dataset id or an attached CSV regardless of case. Each read lists the
  fields and the dataset columns they use. An enumerator field reads the `name` and
  `id` of the enumerator dataset the form list names. A server dataset the form reads
  without attaching it on the server is kept and marked `attached: false`.
- **Publishes.** A form publishes a server dataset when at least 90% of the dataset's
  columns are its fields: by the field's name, or by a repeat field's wide column
  (`score_2`). A field that copies a dataset column under the column's own name
  (`pulldata('roster', 'village', ...)` into `village`) reads that dataset and does not
  count toward publishing it, so a form that preloads a dataset is not its publisher.
  `publish_map` gives the field behind each column; `legacy_columns` lists the rest,
  fields the form's current version no longer has or columns added on the server.
  When several forms cover a dataset, the forms that read it drop out, and the one
  covering most columns publishes it. A tie stops the build and asks for
  `[datasets.<id>] published_by`, which also names the publisher of a dataset under
  90% or one a form republishes from its own copies. A dataset no form covers is
  external (uploaded, or published from outside the project); `closest` names the form
  covering most of its columns.
- **Stages.** A form is stage 1 when it reads nothing another project form publishes,
  else one more than the latest stage it reads from. A cycle of forms stops the build.
- **Origins.** Each published column takes the origin of the field behind it:
  `entered` (a question the enumerator answers, a `search()` pick included),
  `calculated` (from the form's own fields), `carried` (a calculate that traces back to
  a `pulldata()` from another dataset, named by its flow id) or `metadata` (a preload).

`flow.md`, next to `flow.json`, draws the chain as a Mermaid diagram (Obsidian, VS Code
and GitHub render it): forms, datasets coloured by how they are filled (green from the
form's answers, orange carried, yellow both, grey external), publishes, and reads, dashed
when the form does not attach the dataset.

`uv run scto-survey --config config.toml flow` prints the stages, each dataset's
publisher, origins and readers, and the reads without an attachment;
`flow <dataset>` traces each column from the field that publishes it, with its origin,
to the fields that read it.

## What the project is about: description and entities

The outputs say how the data is stored: a submission, a repeat instance, a dataset row.
What those rows are about (a household, a visit, a member) comes from you. Two config
sections hold it:

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

[entities.visit]
key = ["hhid", "round"]              # several columns that identify one together
description = "One round's visit to a household"
parent = "household"
```

The survey-expert skill fills them with you. Give it what describes the study: a
pre-analysis plan, a protocol, a codebook. It reads them, lists the candidate keys the
outputs carry (`get_project`: each dataset's key and each column a `pulldata()` matches
on, with the forms, tables and datasets that hold it), proposes entities on those keys
and writes the sections once you confirm. The description is yours: the skill asks for
it rather than writing one from the questions. Share only documents you are allowed to
share with the agent: an embargoed plan or unpublished results may not be.

`just flow` (seconds) rewrites `flow.json` and `flow.md` with them; a key column no
form or dataset carries is an error, and so are key columns no form or dataset carries
together. `get_project` then opens an agent's work on the project: the description,
the entities with where each lives, and the keys not yet named.

## Querying with DuckDB: one database per project

`just duckdb` (after `just stats` or `just codebook`) writes `<project>.duckdb`, one
database for the whole project. A database holds schemas and a schema holds tables and
views:

```text
<project>.duckdb
├── <form>          one schema per form
│   ├── submissions                 view over <form>.parquet, columns outside repeats
│   ├── <repeat>                    view over long/<repeat>.parquet
│   └── dictionary, questions, choices, edges, dataset_links, dataset_codebook, stats
├── dataset         each dataset once, under its flow.json id
│   └── <id>                        view over raw/datasets/<id>.csv
└── main            the project, from flow.json
    └── forms, datasets, publishes, reads, joins
```

Data stays in its files: the views read `<form>.parquet`, `long/<repeat>.parquet` and
the dataset CSVs, so the database works next to its `out_dir` and `just duckdb` rebuilds
it in seconds. A repeat's columns are read from its long view:
`<form>.dictionary.view_name` names each column's view, `view_column` its name there,
and `choice_value` a select_multiple binary's code. The description and project tables
are copies of the JSON outputs, with no SQL keyword as a column name. A CSV attached to
a form sits in `dataset` as `"<form>/<file>.csv"`. `[datasets.<id>] key` in the config
names the columns that identify a dataset's rows, checked unique.

The build stops on a name the database cannot hold: a form id `main` or `dataset`, a
repeat named `submissions` or like a description table, a `[datasets.<id>] key` for a
dataset the flow does not have, and two columns DuckDB would merge (names that differ
only in case, or a dataset column named `row`).

`joins` connects the forms: a reader's field, the dataset column it matches, and the
publisher's field behind that column, `via` (`pulldata`, `search` or `search value`),
and `is_unique`: whether that column identifies one dataset row. A `pulldata()` needs
it to: when it does not (a label shared by several rows), the form's match can return
another row, and so can a query that follows it. A `search()` filter takes every
matching row. Each form's `dataset_links` gives the same matches within the form, so a
query can follow a value from one form to the next as the forms do. Select in SQL,
clean in pandas:

```python
import duckdb

con = duckdb.connect("data/out/<project>/<project>.duckdb", read_only=True)
long = con.sql("SELECT * FROM <form>.<repeat>").df()
chained = con.sql("""
    SELECT v.KEY, l.KEY AS listing_key
    FROM visit.submissions v
    JOIN dataset.households d ON d.hhid = v.hhid
    JOIN listing.submissions l ON l.hhid = d.hhid
""").df()
```

Every view holds every column; `<form>.dictionary.pii_status` and
`<form>.dataset_codebook.pii_status` say which may hold personal data.

## Survey-expert MCP server and skill

`just mcp` (`uv run scto-mcp --config config.toml`) serves a project's outputs over
stdio to a local agent. The form tools take the form's id: `lookup_variable(s)`,
`search_questions` (every form when no id is given), `get_group`, `get_choice_list`,
`get_gate_chain`, `get_survey_info`, `get_variable_neighborhood`,
`get_repeat_structure` (with a repeat, what sets its instances), `get_datasets`, and
`trace_lineage`, which follows a field's value across forms: a calculate to the fields
it reads, a `pulldata()` or `search()` to the field another form publishes the dataset
column from, back to the questions someone answered. The project tools take no form
id: `get_project` (the description, entities and candidate keys), `get_flow` (the
chain, or one dataset's columns end to end) and `get_structure_db` (a summary of
`<project>.duckdb`, one schema: a form's tables with the datasets and cross-form joins
it uses, or one table's columns). Long lists are summarised with the argument that
lists them in full. The tools read the JSON outputs, never the parquet, and show each
column's `pii` status.

To connect it to Claude Code, copy `.mcp.json.example` to `.mcp.json` (git-ignored) and
point `--config` at your config. The `survey-expert` skill
(`.claude/skills/survey-expert/SKILL.md`) teaches an agent to read the outputs and to
check `pii.status` first. Without MCP it gets the same answers from
`uv run scto-survey --config config.toml [--form <form>] <command>`, where the command
is `var`, `search`, `group`, `choices`, `gates`, `neighborhood`, `repeats`, `info`,
`datasets`, `flow`, `db`, `project` or `lineage`.

## Terms

- **Wide JSON:** SurveyCTO's submission export, one flat row per submission, with
  repeat fields as numbered columns (`score_1`, `score_2`).
- **Server dataset:** a table on the SurveyCTO server that a form publishes to or reads
  from (`pulldata()`, `search()`).
- **Publishable:** a form setting that marks a field safe to export without the
  encryption key.
- **Sentinel:** a code standing for a non-response (`-99` don't know).
- **Gate:** the relevance conditions that decide whether a question is asked.
- **Presidio:** Microsoft's open-source scanner for personal data in text.

## More

- [docs/api-endpoints.md](docs/api-endpoints.md): the SurveyCTO API calls and the
  shapes they return.
- [CONTRIBUTING.md](CONTRIBUTING.md), [SECURITY.md](SECURITY.md),
  [CHANGELOG.md](CHANGELOG.md).
- CI runs the tests and the pre-commit hooks on every pull request to `main`.
