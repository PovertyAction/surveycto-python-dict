---
name: survey-expert
description: Read a SurveyCTO form's outputs from surveycto-python-dict - questions.json, the variable dictionary, the variable graph - to explain questions, skip logic, choices, repeat groups and columns, and to tell which columns may hold PII before anyone reads their values. Use when asked what a variable or column means, why it is missing, what gates or feeds it, how a repeat or select_multiple is laid out, or whether a column is safe to look at.
---

# Survey expert

You explain a project's SurveyCTO forms and their data from the outputs `scto-build`
writes: one folder per form, `<out_dir>/<form>/`, and the project's `<out_dir>/flow.json`
(see `config.toml`):

| File | What it holds |
|---|---|
| `<form>_questions.json` | One entry per question, keyed by its XLSForm `name`: joins back to the XLSForm row by row |
| `sections/section_<group>.json` | `questions.json` sliced per group level |
| `<form>_variable_dictionary.json` | One entry per parquet column: stats, types, sentinels, `pii` |
| `<form>_variable_graph.json` | Questions linked by calculation, relevance, constraint and repeat |
| `<form>.parquet` | The submissions. **Do not read it** unless the columns you need are cleared (below) |
| `<form>_pii_review.xlsx` | `KEY` plus every flagged column, for the people cleaning the data. **Never read it** |
| `<out_dir>/<project>.duckdb` | `just duckdb`: one database for the project. A schema per form (`<form>.submissions` with the columns outside repeats, `<form>.<repeat>` long with `KEY` and one `<repeat>_index` per enclosing repeat, and the descriptions as tables; `<form>.dictionary.table` names each column's view), `dataset.<id>` for each dataset once, and `main.forms`, `datasets`, `publishes`, `reads`, `joins` for the chain. **Read values only through the `*_safe` views.** `<form>.dataset_links` gives each `pulldata()`/`search()` match; `main.joins` connects a reader's field to the publisher's field across forms. `get_structure_db` summarises it; `get_structure_db(<form>)` maps one schema |
| `<out_dir>/flow.json`, `flow.md` | The project: which form publishes each dataset, which forms read it (fields and columns), each published column's origin, each form's stage; `flow.md` draws it |
| `<form>_summary_stats.csv` | `just stats`: per column, eligible rows under skip logic, sentinels apart, then stats; flagged PII columns get counts only |

## Start with what the project is about

Call `get_project` (`<S> project`) first. When it says the project is not described
or has no entities, set them up with the user before answering questions that depend
on them. If the project holds no personal data (study metadata, say) and almost every
column is `no_hits` or `may_contain_pii`, suggest `[pii] no_personal_data = true` to
the user and a rebuild:

1. Ask for what describes the study: a pre-analysis plan, a replication paper, a
   protocol, a codebook. Remind them to share only documents they are allowed to share
   with you.
2. Ask the user to say in a few sentences what the project is about. Use their words
   for `[project] description`; don't write one from the questions.
3. From the documents and the candidate keys `get_project` lists, propose entities: a
   name, the key column, a one-line description from the documents, and a parent where
   one entity sits inside another. Show them as the TOML below and ask the user to
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
# inside the surveycto-python-dict repo, equivalently (--form when the config has several;
# the MCP form tools take the form's id instead):
uv run scto-survey --config config.toml --form <form> <command>
```

| Need | MCP tool | Shell (`<S> <command>`) |
|---|---|---|
| What a question or column is, and where a value read from a dataset comes from (more than 20 columns are summarised; `columns=True` lists each) | `lookup_variable(form, name)` / `lookup_variables(form, names)` | `var <name> [<name> ...] [--columns]` |
| Find questions by words or exact name, in every form or one | `search_questions(query)` / `search_questions(query, form)` | `search <words>` (every form without `--form`) |
| A select's choices | `get_choice_list(form, name)` | `choices <name>` |
| Why a question is asked | `get_gate_chain(form, name)` | `gates <name>` |
| What feeds, gates or constrains it, and the repeat it is in | `get_variable_neighborhood(form, name, depth)` | `neighborhood <name> --depth N` |
| A group's questions in order | `get_group(form, group)` | `group <group>` |
| Repeat groups, then one repeat's members | `get_repeat_structure(form)` / `get_repeat_structure(form, repeat)` | `repeats [<repeat>]` |
| Form overview | `get_survey_info(form)` | `info` |
| The datasets a form reads and their joins | `get_datasets(form)` | `datasets` |
| What the project is about: description, entities, candidate keys | `get_project()` | `project` |
| How the forms chain: stages, publishers, readers | `get_flow()` | `flow` |
| Where a dataset's columns come from and who reads them | `get_flow(dataset)` | `flow <dataset>` |
| The project database: a summary, one schema, one table's columns | `get_structure_db()` / `get_structure_db(schema)` / `get_structure_db(schema, table)` | `db` / `db <schema>` / `db <schema> <table>` |

A name can be a question (`water_source`) or a column (`water_source_3`); a column
lookup returns its question with every column it produced. Read the JSON files
directly only for what the tools do not show, such as a whole section.

## PII: check before reading values

Every column carries `pii.status` with the `basis` for it:

| Status | Meaning | Values |
|---|---|---|
| `cleared_by_form` | The form marks the field `publishable` | May be read |
| `whitelisted` | Reviewed by the team (`reviewed`), a system column, a number/date, an inline-choice select, or a calculate whose inputs are all cleared (`lineage:...`) | May be read |
| `contains_pii` | Blacklisted by the team (`reviewed`), device metadata, GPS, or an attachment | Do not read |
| `may_contain_pii` | Presidio found names, phones, emails or places (`presidio:PERSON`, ...) in its values | Do not read |
| `no_hits` | Free text where Presidio found nothing | Treat as unreviewed; ask before reading |

This is a signal, not enforcement. When a task needs a flagged column's values, say so
and leave the reading to the user, who reviews `<form>_pii_review.xlsx` and adds the
columns they clear to `[pii] whitelist` in `config.toml` (and personal ones to
`[pii] blacklist`). A `search()` select with `choices_withheld` has no choice list in
the outputs: its dataset held what Presidio flags; `get_choice_list` names the dataset's
value and label columns instead. `presidio.hit_rows` counts the
rows with a hit and `presidio.hit_keys` gives up to five of their `KEY`s; quote counts
and KEYs, never values.

## Reading the dictionary

Each `variables.<column>` entry has:

- `question`: the question it belongs to (null for system columns and notes);
  `survey` repeats that question's text, type, choices and logic.
- `parquet.arrow_type` / `parquet.xml_type`: the stored type and the XForm bind type.
  `parquet.cast_fallback` means the column could not be typed and is kept as text; it
  says why (for example `3 of 80 values do not parse as int64`).
- `non_null_count`, `missing_rate`, `is_all_missing`; `data_min` / `data_max` for
  numbers, dates and timestamps only.
- `sentinels`: counts of non-response codes (`-99` Don't know, `-88` Refused, `-77`
  Not applicable, `-66` Other, `-55` Not in list: the study's table in `SCTO_SENTINELS`). They are
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
  `choices_per_submission` is true (the form filters the list per submission): then
  only the codes someone picked do, in the wide parquet and the long views alike.
- **Choices** are inline per question; a select's `choice_list` is its own name.
- **Repeats**: fields inside a repeat get `_<n>` per instance (`_<n>_<m>` when nested).
  A select_multiple inside a repeat is `<name>_<code>_<n>`: the choice code comes
  before the instance.
  Each repeat has a synthetic `<repeat>_count` question whose `calculation` is the
  repeat count. Repeat siblings belong to the same instance; handle them together.
- **Notes** are exported as always-empty columns with no question.

## Writing cleaning code

Write it for the person who reviews it: a plain script read top to bottom like a
do-file, explicit column names, plain `assert`, comments that say why, and no helpers or
guards for cases that have not happened. Look at the data first (these tools, the
summary stats) and say what you found about missing values before computing from them.

Before writing or reviewing cleaning code on these outputs, read
`references/cleaning.md`: the team's suggested cleaning practice (fail loudly,
SurveyCTO submissions, sentinels, skip logic, other-specify, select_multiple, repeats,
aggregation, verification, PII) in pandas terms. They are guidelines: propose them, and let the people cleaning the data
decide what applies. For a column's eligible rows, sentinel counts and answered-while-skipped flags,
run `just stats` and read `<form>_summary_stats.csv`.

## Answering

Cite the field and file you used, state the logic in the form's own `${}` syntax, and
include the column's `pii.status` whenever you discuss its values.
