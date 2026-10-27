# Changelog

## Unreleased

### Added

- `<form>_pii_review.xlsx` is where the team decides (#109): every column, and each
  `dataset.column` the form reads on a new Datasets sheet, gets a `decision`
  dropdown (`personal` or `clear`) and a `reason`, undecided flagged and scanned
  columns first. The next build reads the decisions back like `[pii] blacklist` and
  `whitelist` (basis `reviewed`) and writes them and their reasons into the new
  workbook, prefilled from the lists. A name decided both ways raises.
- A form with no submissions yet builds every output (#89): its questions, skip logic,
  dictionary and flow are ready before fieldwork, and its tables and views hold the
  columns every submission carries and no rows.

### Fixed

- An int or decimal column parses only the numbers the server writes: `1_0`,
  `nan`, `inf` or ` 3 ` stay text with a `cast_fallback` (#94).
- The join lines in `<project>_db.json` and `_db.sql` quote every name, so a line
  runs as written when a dataset, form or column name holds `/`, `-` or `.` (#92).
- Errors name the input they reject: a dataset CSV that is empty, names a column
  twice or is not UTF-8; a malformed or repeated `SCTO_SENTINELS` or
  `SCTO_DATE_SENTINELS` pair; an HTTP error, which now carries the start of the
  server's reason; a response body that is not JSON. A repeated `[datasets.<id>]`
  key raises a `ValueError` instead of an assertion (#91).

### Changed

- The PII scan reads each column whole, with its question's label, through spaCy's
  multilingual `xx_ent_wiki_sm` (39 MB) instead of the English and Spanish large
  models (1 GB), and checks its hits per column: a value many rows share, or a name in
  a column of repeated values, is a category. A column whose values are mostly 8 to 12
  digits, integers included, is `may_contain_pii` (basis `digits`). A `pulldata()`
  calculate holding its dataset column's value takes the column's status, and a
  calculate reading a flagged field takes its flag. Codes built only from the date and
  random numbers are `whitelisted`, and a dataset's system columns are decided by rule.
  On a labelled project this missed 12 personal columns instead of 13, raised 54 false
  alarms instead of 156, and ran in 6 s instead of 97 s (#108). Birth dates and ID
  numbers are left to the blacklist and `publishable`.
- `[pii] countries` sets the regions whose local phone formats Presidio validates
  (#108).
- A whitelisted `dataset.column` covers every `pulldata()` that holds its value, as a
  blacklisted one already did, so one entry replaces a field and its preload (#108).
- `scripts/pii_compare.py` writes a truth template for a form and scores the
  classification against it (#108).
- `xml_contract` drops `Node.pulldata` (read `data_source`), `map_column`'s
  `ambiguous` and `FormContract.to_dict` (#93).

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
