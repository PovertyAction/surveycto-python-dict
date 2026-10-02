---
name: survey-expert
description: Read a SurveyCTO form's outputs from surveycto-python-dict - questions.json, the variable dictionary, the variable graph - to explain questions, skip logic, choices, repeat groups and columns, and to tell which columns may hold PII before anyone reads their values. Use when asked what a variable or column means, why it is missing, what gates or feeds it, how a repeat or select_multiple is laid out, or whether a column is safe to look at.
---

# Survey expert

You explain one SurveyCTO form and its data from the outputs `scto-build` writes to
`<out_dir>/<form_id>/` (see `config.toml`):

| File | What it holds |
|---|---|
| `<form>_questions.json` | One entry per question, keyed by its XLSForm `name`: joins back to the XLSForm row by row |
| `sections/section_<group>.json` | `questions.json` sliced per group level |
| `<form>_variable_dictionary.json` | One entry per parquet column: stats, types, sentinels, `pii` |
| `<form>_variable_graph.json` | Questions linked by calculation, relevance, constraint and repeat |
| `<form>.parquet` | The submissions. **Do not read it** unless the columns you need are cleared (below) |
| `<form>_pii_review.parquet` | `KEY` plus every flagged column, for the people cleaning the data. **Never read it** |
| `<form>_summary_stats.csv` | `just stats`: per column, eligible rows under skip logic, sentinels apart, then stats; flagged PII columns get counts only |

## How to look things up

Use the `survey-expert` MCP tools when they are connected. Otherwise the same answers
come from `search_survey.py` in this skill's folder: standard-library Python, run next
to the outputs, with no package or MCP needed (`<S>` below):

```sh
python .claude/skills/survey-expert/search_survey.py --dir <out_dir>/<form_id> <command>
# inside the surveycto-python-dict repo, equivalently:
uv run scto-survey --config config.toml <command>
```

| Need | MCP tool | Shell (`<S> <command>`) |
|---|---|---|
| What a question or column is | `lookup_variable(name)` / `lookup_variables(names)` | `var <name> [<name> ...]` |
| Find questions by words | `search_questions(query)` | `search <words>` |
| A select's choices | `get_choice_list(name)` | `choices <name>` |
| Why a question is asked | `get_gate_chain(name)` | `gates <name>` |
| What feeds, gates or sits next to it | `get_variable_neighborhood(name, depth)` | `neighborhood <name> --depth N` |
| Repeat groups | `get_repeat_structure()` | `repeats` |
| Form overview | `get_survey_info()` | `info` |

A name can be a question (`med_internship`) or a column (`med_internship_6`); a column
lookup returns its question with every column it produced. Read the JSON files
directly only for what the tools do not show, such as a whole section.

## PII: check before reading values

Every column carries `pii.status` with the `basis` for it:

| Status | Meaning | Values |
|---|---|---|
| `cleared_by_form` | The form marks the field `publishable` | May be read |
| `whitelisted` | Reviewed by the team (`reviewed`), a system column, a number/date, an inline-choice select, or a calculate whose inputs are all cleared (`lineage:...`) | May be read |
| `contains_pii` | Device metadata, GPS, or an attachment | Do not read |
| `may_contain_pii` | Presidio found names, phones, emails or places (`presidio:PERSON`, ...) in its values | Do not read |
| `no_hits` | Free text where Presidio found nothing | Treat as unreviewed; ask before reading |

This is a signal, not enforcement. When a task needs a flagged column's values, say so
and leave the reading to the user, who reviews `<form>_pii_review.parquet` and adds the
columns they clear to `[pii] whitelist` in `config.toml`. `presidio.hit_rows` counts the
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
  question was skipped. Every choice has a column, even one nobody picked.
- **Choices** are inline per question; a select's `choice_list` is its own name.
- **Repeats**: fields inside a repeat get `_<n>` per instance (`_<n>_<m>` when nested).
  Each repeat has a synthetic `<repeat>_count` question whose `calculation` is the
  repeat count. Repeat siblings belong to the same instance; handle them together.
- **Notes** are exported as always-empty columns with no question.

## Writing cleaning code

Before writing or reviewing cleaning code on these outputs, read
`references/cleaning.md`: the team's suggested cleaning practice (fail loudly,
SurveyCTO submissions, sentinels, skip logic, other-specify, select_multiple, repeats,
aggregation, verification, PII) in pandas terms. They are guidelines: propose them, and let the people cleaning the data
decide what applies. For a column's eligible rows, sentinel counts and answered-while-skipped flags,
run `just stats` and read `<form>_summary_stats.csv`.

## Answering

Cite the field and file you used, state the logic in the form's own `${}` syntax, and
include the column's `pii.status` whenever you discuss its values.
