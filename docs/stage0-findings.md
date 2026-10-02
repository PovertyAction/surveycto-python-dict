# Stage 0 findings (2026-10-02)

Verified against a live encrypted form with `select_one`, `select_multiple` and `binary`
fields. `scripts/stage0_probe.py` produced these facts; it prints structure only and keeps
raw pulls in git-ignored `data/`. **Nothing from a real form enters git**: test fixtures are
synthetic and reproduce the shapes below.

## Publishable

- **`publishable` survives into the compiled XML** as an un-namespaced bind attribute,
  `publishable="true()"`. Fields that are not publishable carry no attribute.
  `parse_contract` drops it today (unknown bind attributes are discarded), so stage 2 adds
  `Node.publishable` upstream.
- **Oracle check holds:** the fields present in both the keyed and the keyless pull (after
  dropping `is_system_column` columns and mapping through `map_column`) are exactly the
  XML-publishable fields.

## Wide JSON shape

- Every value is a JSON string; rows are flat objects.
- **Keys are sparse per row.** A field that is not relevant or not selected is absent from
  the row, not `""`. Casting treats an absent key like an empty string (null).
- **select_multiple comes as per-choice binaries** `<field>_<code>`, with no space-separated
  `<field>` column. A binary is present, with value `"1"`, only when that choice was
  selected. Whether an unselected binary on an answered row becomes `0` or null is a
  stage 4 decision.
- **Encrypted, non-publishable fields without the key are absent**, not present and empty.
  The keyless pull keeps `CompletionDate`, `SubmissionDate`, `formdef_version`,
  `review_quality`, `review_status`, `KEY` and the publishable fields. It drops
  `starttime`, `endtime`, `deviceid`, `duration` and `instanceID`.
- The keyless pull adds one column per group whose children are all encrypted (including
  `meta`), named after the group and always `""`. These are placeholders, not fields: drop
  them from the oracle and from casting.
- Both pulls return the same `KEY` set.
- `dateTime` and system timestamps use `Mon D, YYYY H:MM:SS AM` (for example
  `Mar 7, 2025 4:06:12 PM`). The REST API returns them in UTC
  ([SurveyCTO: exporting in different time zones](https://support.surveycto.com/hc/en-us/articles/360046966333-Exporting-data-in-different-time-zones)),
  so stage 4 parses them into `timestamp[ms, tz=UTC]`.

## Stage 3 source

`questions.json` is rebuilt from the compiled XML; the XML is the only form definition the
pipeline reads.
