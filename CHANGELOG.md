# Changelog

## Unreleased

- A form with no submissions yet builds every output (#89): its questions, skip logic,
  dictionary and flow are ready before fieldwork, and its tables and views hold the
  columns every submission carries and no rows.

## 0.1.0 (2026-10-13)

First public release.

### What it does

- Pulls a SurveyCTO project's forms (compiled XML, wide-JSON submissions, attached
  CSVs) and every server dataset they read, decrypting encrypted forms with the
  project's key.
- Builds per form: `questions.json` and its sections, typed parquet, a variable
  dictionary and graph, summary stats that follow skip logic with sentinel codes
  counted apart, a codebook for PIs, a dataset codebook and a PII review workbook.
- Flags each column's PII status from the form (publishable, collection metadata,
  types, calculate lineage), your whitelist and blacklist, and a Presidio scan.
- Builds `flow.json` and `flow.md` for the project: which form publishes and reads
  each dataset, column origins and stages.
- Writes one DuckDB database per project, with long repeat tables and cross-form joins.
- Serves it all to an agent through the survey-expert MCP server and skill, which
  trace a field's value across forms.

### Fixed in the pre-release audit

- Outputs an agent reads never show a flagged column's values: no range, picked
  codes, `search()` filter values or choice codes in the dictionary, the MCP, the DB
  map or error messages. One rule decides this in every output, and an end-to-end
  test plants values across a synthetic project to check it.
- A blacklisted inline select is referred to, not listed; a calculate over a geo or
  attachment field carries `contains_pii`.
- `[submissions] exclude` takes one statement, and its errors quote no value.
- Server datasets are written as the server's bytes, so accents, line endings and
  quoted newlines survive.
- A form that preloads a dataset is no longer taken for its publisher.
- A raw select_multiple named like `q4_1` is split; a field named like `KEY_x` is a
  question; a choice with only an image label reads as an empty label; geo and time
  columns are text without a cast fallback.
- The build stops with a clear message on a form with no submissions, duplicate or
  malformed `forms`, a form id the database reserves, a repeat named like a
  description table, a dataset key for no dataset, and columns DuckDB would merge.
- CI runs the test suite on Python 3.11 and 3.12.
