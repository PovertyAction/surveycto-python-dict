---
name: survey-expert
description: Read a SurveyCTO project's outputs from surveycto-python-dict - questions.json, the variable dictionary, the variable graph, flow.json, the project database - to explain questions, skip logic, choices, repeat groups and columns, and to tell which columns may hold PII so they are handled carefully. Use when asked what a variable or column means, why it is missing, what gates or feeds it, how a repeat or select_multiple is laid out, or how to handle a column that may hold PII.
---

# Survey expert

You explain a project's SurveyCTO forms and their data from the outputs `scto-build`
writes: one folder per form, `<out_dir>/<form>/`, and the project's `<out_dir>/flow.json`
(see `config.toml`):

| File | What it holds |
|---|---|
| `<form>_questions.json` | One entry per question, keyed by its XLSForm `name`: joins back to the XLSForm row by row |
| `sections/section_<group>.json` | `questions.json` sliced per group level |
| `<form>_variable_dictionary.json` | One entry per parquet column: counts, types, sentinels, `pii`, and the range of a column whose values are shown |
| `<form>_variable_graph.json` | Questions linked by calculation, relevance, constraint and repeat |
| `<form>.parquet` | The submissions, every column. Scripts use it; flagged columns' values stay out of what you print (below) |
| `<form>_pii_review.xlsx` | Every value of a flagged column, for the people cleaning the data to verify the flags. **Never open it** |
| `<out_dir>/<project>.duckdb` | `just duckdb`: one database for the project. A schema per form (`<form>.submissions` with the columns outside repeats, `<form>.<repeat>` long with `KEY` and one `<repeat>_index` per enclosing repeat, and the descriptions as tables; `<form>.dictionary.view_name` names each column's view, `view_column` its name there and `choice_value` a binary's code), `dataset.<id>` for each dataset once, and `main.forms`, `datasets`, `publishes`, `reads`, `joins` for the chain. Every view holds every column; `dictionary.pii_status` says which to handle carefully. `<form>.dataset_links` gives each `pulldata()`/`search()` match; `main.joins` connects a reader's field to the publisher's field across forms. `get_structure_db` summarises it; `get_structure_db(<form>)` maps one schema |
| `<out_dir>/flow.json`, `flow.md` | The project: which form publishes each dataset, which forms read it (fields and columns), each published column's origin, each form's stage; `flow.md` draws it |
| `<form>_summary_stats.csv` | `just stats`: per column, eligible rows under skip logic, sentinels apart, then stats; flagged PII columns get counts only. Typed in `<form>.stats`; pandas reads the CSV with `low_memory=False` |

## Start with what the project is about

Call `get_project` (`<S> project`) first. When it says the project is not described
or has no entities, set them up with the user before answering questions that depend
on them:

1. Ask for what describes the study: a pre-analysis plan, a replication paper, a
   protocol, a codebook. Remind them to share only documents they are allowed to share
   with you.
2. Ask the user to say in a few sentences what the project is about. Use their words
   for `[project] description`; do not write one from the questions.
3. From the documents and the candidate keys `get_project` lists, propose entities: a
   name, the key column (or the columns that identify it together, as a list), a
   one-line description from the documents, and a parent where one entity sits inside
   another. Show them as the TOML below and ask the user to
   confirm or correct each.
   ```toml
   [entities.household]
   key = "hhid"
   description = "A household listed at baseline"
   ```
4. Write the confirmed sections into the project's config, run `just flow <config>`,
   and ask the user to reconnect the MCP so `get_project` serves them.
5. Add one line to the analysis project's CLAUDE.md pointing at `get_project` and the
   config, so the description and entities keep one home.

## How to look things up

Use the `survey-expert` MCP tools when they are connected. Otherwise the same answers
come from `search_survey.py` in this skill's folder: standard-library Python, run next
to the outputs, with no package or MCP needed (`<S>` below):

```sh
python .claude/skills/survey-expert/search_survey.py --dir <out_dir>/<form> <command>
# with the package installed, equivalently (--form when the config has several; the MCP
# form tools take the form's id instead). Searching every form and `lineage` need --config:
uv run scto-survey --config config.toml --form <form> <command>
```

| Need | MCP tool | Shell (`<S> <command>`) |
|---|---|---|
| What a question or column is, and where a value read from a dataset comes from (more than 20 columns or choices are summarised, and every question's columns when you look up several; `columns=True` lists each) | `lookup_variable(form, name)` / `lookup_variables(form, names)` | `var <name> [<name> ...] [--columns]` |
| Find questions by words or exact name, in every form or one | `search_questions(query)` / `search_questions(query, form)` | `search <words>` (every form with `--config` and no `--form`) |
| A select's choices | `get_choice_list(form, name)` | `choices <name>` |
| Why a question is asked | `get_gate_chain(form, name)` | `gates <name>` |
| What feeds, gates or constrains it, and the repeat it is in | `get_variable_neighborhood(form, name, depth)` | `neighborhood <name> --depth N` |
| A group's questions in order | `get_group(form, group)` | `group <group>` |
| Repeat groups, then one repeat's members and what sets its instances (the fields its count reads, the calculates that read `index()`) | `get_repeat_structure(form)` / `get_repeat_structure(form, repeat)` | `repeats [<repeat>]` |
| Form overview | `get_survey_info(form)` | `info` |
| The datasets a form reads and their joins (long lists summarised; one dataset in full, every column on request) | `get_datasets(form)` / `get_datasets(form, dataset, columns=True)` | `datasets [<dataset>] [--columns]` |
| What the project is about: description, entities, candidate keys | `get_project()` | `project` |
| How the forms chain: stages, publishers, readers | `get_flow()` | `flow` |
| Where a dataset's columns come from and who reads them (a wide one by field; every column on request) | `get_flow(dataset)` / `get_flow(dataset, columns=True)` | `flow <dataset> [--columns]` |
| Where a field's value comes from, hop by hop across forms, back to the question someone answered | `trace_lineage(form, name)` | `lineage <name>` (with `--config` and `--form`) |
| The project database: a summary, one schema, one table's columns | `get_structure_db()` / `get_structure_db(schema)` / `get_structure_db(schema, table)` | `db` / `db <schema>` / `db <schema> <table>` |

A name can be a question (`water_source`) or a column (`water_source_3`); a column
lookup returns its question with every column it produced. Read the JSON files
directly only for what the tools do not show, such as a whole section.

## How a form works

Start from the graph, then read the formulas:

- What a gate opens, what a calculate or an id reads, and the repeat it sits in:
  `get_variable_neighborhood` on the gate or the calculate.
- Why a question is asked: `get_gate_chain`.
- Where a value comes from across forms: `trace_lineage`.
- Many formulas at once (every calculate or relevance of a group or repeat): the
  `<form>.questions` table in the database, or `questions.json`.

## PII: use every column, see only unflagged values

Every column carries `pii.status` with the `basis` for it. The status says where
personal data may be so it is handled carefully; it never means drop the column or
work around it. Scripts use every column: join on a coder's name, deduplicate on a
phone number, clean a free-text field, keep them in the output. What changes is what
you see: a flagged column's values never reach the conversation.

| Status | Meaning | You may see |
|---|---|---|
| `cleared_by_form` | The form marks the field `publishable` | Its values |
| `whitelisted` | Reviewed by the team (`reviewed`), a system column (`system`), a number/date, an inline-choice select, a calculate whose inputs are all cleared or that generates its value (`generated`), or any column of a project declared to hold no personal data (`project`) | Its values |
| `contains_pii` | Blacklisted by the team (`reviewed`), collection metadata (device or user, under any name), GPS, an attachment, or a calculate that reads one of these (`lineage:<field>`) | Counts and `KEY`s only |
| `may_contain_pii` | Presidio found names, phones, emails or places (`presidio:PERSON`, ...) in its values, or most are 8 to 12 digits (`digits`) | Counts and `KEY`s only |
| `no_hits` | Free text where Presidio found nothing | Its values; the team may still review it |

For a flagged column, a script prints counts, null rates, distinct counts and the
`KEY`s of the rows a check flags, never the values, `head()` or `value_counts()`.
Whether a column leaves a delivered dataset is the team's decision, not a cleaning
default. A flag the team disagrees with is fixed by a person in
`<form>_pii_review.xlsx`, with a `decision` (`clear` or `personal`) and a `reason`
read back on the next build, or in `[pii] whitelist` and `blacklist`. You propose
decisions in your reply, from the dictionary; you never open the workbook. A list is referred to, not listed, when Presidio
flags a `search()` list, the select (inline or `search()`) is on the blacklist, it has
more than 50 choices, or it is filtered on a field whose values are not shown
(`choices_withheld` says why): `get_choice_list` names its dataset, value and label
columns and filter, and a script reads it from `<form>.choices`, keeping its values out
of what you print. The parquet keeps all its binaries; when a list's codes are not
shown, the tools name its binaries `<name>_<n>`. A `search()` list's filter values are
submission values, so they are only in `<form>.choices`. `presidio.hit_rows` counts the
rows with a hit and `presidio.hit_keys` gives up to five of their `KEY`s; quote counts
and KEYs, never values.

## Reading the dictionary

Each `variables.<column>` entry has:

- `question`: the question it belongs to; null for a system column and for one whose
  `column_kind` says what it is (note, group, preload, `not_in_form`). `survey` repeats that question's text, type, choices and logic.
- `parquet.arrow_type` / `parquet.xml_type`: the stored type and the XForm bind type.
  `parquet.cast_fallback` means the column could not be typed and is kept as text; it
  says why (for example `3 of 80 values do not parse as int64`).
- `non_null_count`, `missing_rate`, `is_all_missing`; `data_min` / `data_max` for
  numbers, dates and timestamps whose values are shown.
- `sentinels`: counts of the study's non-response codes, set in `SCTO_SENTINELS`
  (`-99` Don't know, `-88` Refused, say). They are
  **reported, never recoded**: the parquet keeps `-99` as `-99`, and the user decides
  how to treat it. The team suggests moving the code to `<var>_dk` and setting it
  missing in `<var>` (`references/cleaning.md`, section 3).
- `repeat_iteration`: the repeat instance(s) of a column inside a repeat, outer first.
- `choice_value`: on a select_multiple binary, the choice it stands for.
- `value_labels`: what a coded column's values mean. For a select_one, its choices; for
  a select_multiple binary, only `1` (that choice) and `0` (not chosen), never the
  question's whole list, which stays in `survey.choices`.
- `system: true`: a SurveyCTO column (`KEY`, `SubmissionDate`, `duration`, ...).
  Timestamps are UTC.

## Form patterns

- **Expressions** are in XLSForm syntax: `${name}`, `.` for the field itself,
  `selected(${x}, '2')`.
- **Missing by logic**: a question is asked only when every `group_relevances`
  condition (outer first) and its own `relevance` hold. Use the gate chain before
  calling a missing value an error.
- **select_multiple** comes as one binary per choice, `<name>_<code>` (negative codes
  as `<name>__99`). A binary is `1` when chosen, `0` when another choice of the same
  question was chosen in that row (and repeat instance), and null when none was: the
  question was skipped. Every choice has a column, even one nobody picked, unless
  `choices_per_submission` is true (the form filters the list per submission, or a
  `search()` list has more than 50 choices): then only the codes someone picked do, in
  the wide parquet and the long views alike.
- **Choices** are inline per question, or come from a dataset for a `search()` select
  (`choice_columns` names its value and label columns).
- **Repeats**: fields inside a repeat get `_<n>` per instance (`_<n>_<m>` when nested).
  A select_multiple inside a repeat is `<name>_<code>_<n>`: the choice code comes
  before the instance.
  Each repeat has a synthetic `<repeat>_count` question whose `calculation` is the
  repeat count. Repeat siblings belong to the same instance; handle them together.
- **Notes**: a note with a value in any row is a column with `column_kind` note; one
  empty in every row is left out of the parquet.

## Writing cleaning code

Write it for the person who reviews it: a plain script read top to bottom, explicit
column names, plain `assert`, comments that say why, and no helpers or
guards for cases that have not happened. Look at the data first (these tools, the
summary stats) and say what you found about missing values before computing from them.

Before writing or reviewing cleaning code on these outputs, read
`references/cleaning.md`: the team's suggested cleaning practice (fail loudly,
SurveyCTO submissions, sentinels, skip logic, other-specify, select_multiple, repeats,
aggregation, verification, PII) in pandas terms. They are guidelines: propose them, and let the people cleaning the data
decide what applies. For a column's eligible rows, sentinel counts and answered-while-skipped flags,
run `just stats` and read `<form>_summary_stats.csv`.

When the user decides how to handle something (a code, a gate that never fired, a
field the instrument mislabels), write the decision into the analysis project's
`CLAUDE.md` as a ruling, in their words, as soon as it is made: a decision left in the
conversation is reopened on the next run. Rulings come from the user and the study's
documents; propose one only from those.

## Answering

Cite the field and file you used, state the logic in the form's own `${}` syntax, and
include the column's `pii.status` whenever you discuss its values.
