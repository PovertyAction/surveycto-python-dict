# surveycto-python-dict

This is tooling. It reads a SurveyCTO project and reports what it holds: questions,
skip logic, lineage, and where personal data may be. It informs; the people cleaning
the data decide.

## PII: handle carefully, never drop

- **A `pii.status` flag says where personal data may be, so it is handled carefully.**
  It never means drop the column, avoid it or work around it.
- **The tool keeps the data whole.** The parquet, the long tables and every view in
  `<project>.duckdb` hold every column, flagged or not, and a select keeps all its
  binaries. A flag changes what is shown, never what is kept.
- **A choice list flagged by Presidio, blacklisted or over 50 choices is referred to,
  not listed:** the MCP names its dataset, value and label columns and filter, and a
  script reads it.
- **An agent never sees a flagged column's values:** no printing, quoting, `head()`,
  `value_counts()` or `describe()` of them. Counts, null rates, distinct counts and
  `KEY`s are fine, so the values never enter the conversation.
- **Scripts use every column,** flagged ones included: join on it, clean it,
  deduplicate on it, keep it.
- **Removing a column from a delivered dataset is the team's decision at delivery,**
  not a default of the tool or of a cleaning script.
