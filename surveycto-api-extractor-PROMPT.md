<!-- markdownlint-disable MD029 -->
# Build prompt: `surveycto-api-extractor` (working name)

Paste everything below the line into a fresh Claude Code session started in the new,
empty repo directory. Work strictly test-first: for every stage, write the failing
tests, show them failing, then implement until green. Do not start a stage until the
previous one is green and committed.

---

## Amendments (2026-10-02, override the sections they name)

1. **Stage 3 source is decided by stage 0.** If `/design` JSON carries the XLSForm
   survey and choices rows, build `questions.json` with upstream `json_extractor`
   (via the explicit-path entry point from decision 1); the XML then supplies only the
   contract, types and `publishable`. If it does not, build from the XML as stage 3
   describes. There is no `.xlsx` download; record which case holds in
   `docs/stage0-findings.md`.
2. **The `pii` block lives only in `<form>_variable_dictionary.json`.** No data output
   (parquet, Arrow field metadata) carries PII classification. `<form>_pii_review.parquet`
   stays as stage 6 describes: a local file where the people cleaning decide what is
   actual PII.
5. **The cleaner's decision is a forced whitelist the user maintains in config.** A
   variable on it is rule 0 in stage 6: `status: whitelisted`, `basis: ["reviewed"]`,
   Presidio never called. `scto-build` applies it every run, so the decision shows in
   `<form>_variable_dictionary.json` and survives regeneration. A whitelisted name not
   in the contract warns "variable `<name>` cannot be found" and the run continues.
   There is only the whitelist; users maintain it, and there is no "confirmed PII" list.
3. **`/forms/{form_id}/design` is a console endpoint, not the documented `/api/`.**
   SurveyCTO can change it without notice; record its auth and shape in
   `docs/api-endpoints.md` and list it as a risk in `docs/design.md`.
4. **The MCP server is local** to the user and the agent deploying it on their own
   form; it is not a service and serves no one else.

## 1. What we are building

A Python-only pipeline for teams that **have SurveyCTO API access**. It pulls a form's
data as JSON and its compiled XForm XML, and from those produces the same knowledge
surface `surveycto-extractor` produces from an `.xlsx` + `.dta`, plus a PII
classification per variable:

```text
SurveyCTO API ──> form XML ──> contract (xml_contract) ──> questions.json + sections
             └──> wide JSON ─┐                                    │
             └──> datasets the XML references (pulldata/search) ──┤
                             └──> typed parquet (cast via contract)
                                        │
             parquet + questions.json ──> variable dictionary (+ pii block)
                                       ──> variable graph
                                       ──> <form>_pii_review.parquet
             all of the above ──> survey-expert MCP server (upstream, unchanged schema)
```

No Stata outputs, no `.dta`, no media/attachment downloads. Everything is
pandas/pyarrow and parquet.

## 2. Projects to start from (read these first)

| Path | Take from it |
|---|---|
| `C:\Users\Daniel Martinez\projects\surveycto_extractor_takeout` | The upstream library. Package `src/surveycto_extractor/`. This repo **depends on it** (uv path/git dependency); it does not copy from it. |
| `...\surveycto_extractor_takeout\src\surveycto_extractor\parsers\xml_contract.py` | `parse_contract`, `FormContract`, `Node`, `FormContract.map_column`, `is_system_column` / `_SYS_EXACT`, `_parse_pulldata`, `_parse_search`, `Node.data_source`, `Node.choice_items`. The structural spine: wide column -> node -> type. Stdlib only, no config. |
| `...\src\surveycto_extractor\extractors\json_extractor.py` | The **questions.json schema** the XML builder must reproduce (keys listed in stage 3). It builds from XLSForm; we build the same shape from XML. |
| `...\src\surveycto_extractor\cli\vardict.py` | Variable dictionary schema (`{dataset, summary, variables{name: entry}}`), `_read_parquet_column_stats`, `build_variable_graph(questions, vardict_json, graph_path)`. |
| `...\src\surveycto_extractor\core\sentinels.py` | Sentinel detection (-99/-98/-88 etc.). Reuse as-is. |
| `...\src\surveycto_extractor\generators\section_splitter.py`, `diagram_generator.py` | Section JSON slices and structure diagram. No config coupling; reuse as-is. |
| `...\src\surveycto_extractor\mcp_server\survey_server.py` | The survey-expert MCP server. Loads `questions_json` + `output_json` per dataset from config, and `*_variable_graph.json` next to the vardict. We feed it, and extend it upstream to render the `pii` block. |
| `...\coding_guidelines\surveycto_refs\xlsform.md` (line 52) | `publishable` semantics: leaves a field unencrypted on encrypted forms; ignored by SurveyCTO on unencrypted forms. |
| `...\sample\output\household_survey_*` | Reference outputs: `_questions.json`, `_variable_dictionary.json`, `_variable_graph.json`, `sections/`. Tests assert schema parity against these. |
| `...\tests\test_xml_contract.py` | Test style: inline synthetic XForm strings, no network. Follow it. |
| `C:\Users\Daniel Martinez\projects\pysurveycto-stata\downloader_scto.py` | API flow reference only: key sent as multipart `private_key` POST, `format="json"`, `oldest_completion_date`, `get_server_dataset`. **Do not copy its error handling** -- it prints and swallows exceptions; this repo fails loud. |
| pysurveycto 0.0.15 (IDinsight; source in the uv cache under `archive-v0/*/pysurveycto/pysurveycto.py`) | Known endpoints: `/api/v2/forms/data/wide/json/{form_id}?date=`, `/api/v1/forms/files/csv/{form_id}`, `/api/v2/datasets/data/csv/{dataset_id}`, `/forms/{form_id}/design` (JSON form definition), `/console/forms-groups-datasets/get`. **It has no XForm XML endpoint** -- stage 0 must find one. |

Global rules (from `C:\Users\Daniel Martinez\.claude\CLAUDE.md`): fail loud; no
fallbacks or defensive branches for cases that have not happened; prefer what upstream
has, then stdlib, then an installed dependency, then new code.

## 3. Decisions already made (do not relitigate)

1. **Separate repo, upstream as a library.** If a needed upstream module is not cleanly
   importable, fix it upstream in a PR to `surveycto_extractor_takeout` first; never
   vendor or copy. Known coupling: `cli/vardict.py:68`, `extractors/json_extractor.py:16`
   and `generators/load_survey_metadata.py:13` call `load_config()` at import time, which
   reads whatever `config.toml` is in the cwd. Give the functions we need explicit-path
   entry points upstream before importing them here.
2. **Output schemas match upstream** so the MCP server, `section_splitter` and
   `build_variable_graph` work unchanged. Keys that are Stata-specific
   (`stata_skip_logic`, `stata_constraint`) are emitted as `null`, a value they already
   take upstream. The vardict's `stata` block is replaced by a `parquet` block
   `{arrow_type, xml_type}`; the MCP server is taught to render either.
3. **PII is a "tread carefully" signal, not enforcement.** This tool is distributed to
   partners outside IPA; it reports evidence and never blocks. Rules in stage 6.
4. **Presidio runs only on actual submission data** -- never on the XML, the dictionary,
   choice labels, or column names. Column names are not evidence.
5. **`publishable` (`cleared_by_form`) is the main signal** and what makes the run fast.
   Publishable fields are never scanned.
6. Direct identifiers only in v1. Quasi-identifiers (DOB, village, age x sex) out of scope.
7. Presidio is a **required** dependency. A missing spaCy model for a configured
   language is an error, not a skip.

## 4. Stages (each: tests first, then code)

### Stage 0 -- Discovery and fixtures (needs Daniel + a live server)

Not code-first; produces the facts and fixtures every later test depends on.

1. **Find the XForm XML endpoint.** Candidate: OpenRosa `GET /formList` -> each form's
   `downloadUrl` (and `manifestUrl` for attached media). Verify against the server;
   record the exact URL shapes in `docs/api-endpoints.md`.
2. **Build an encrypted test form** (has `public_key`; the bundled sample has none, so it
   cannot be used here). Fictional data only. Must contain:
   - `publishable=yes` on: `text`, `integer`, `select_one`, `select_multiple`, `calculate`,
     and one field inside a repeat;
   - a non-publishable `text` field holding fictional names, one holding fictional phone
     numbers (stored as text);
   - one `geopoint`, one `pulldata()` calculate from an attached CSV, one `search()` select
     from a server dataset, one `calculate` that `concat()`s two text fields, one numeric
     `calculate` over integers;
   - a nested repeat.
   Submit ~10 fictional submissions.
3. **Record fixtures** into `tests/fixtures/api/`: the XML, the `/design` JSON, wide JSON
   **with** the key, wide JSON **without** the key, the attached CSV(s), the server
   dataset CSV. Strip auth headers; never commit the private key.
4. **Answer, in `docs/stage0-findings.md`:**
   - Does `publishable` survive into the compiled XML? Where (bind attribute? namespace?)
     If not: does `/design` JSON carry it? That picks the runtime source of the signal.
   - Publishable oracle = fields present in both pulls, after dropping
     `is_system_column` columns and mapping columns -> fields via `map_column`
     (repeats and select_multiple expand one field into many columns). Compare
     **presence of columns, not values** (a publishable field can be empty everywhere).
   - Does wide JSON include select_multiple per-choice binaries, or only the
     space-separated field? geopoint as one field or `-Latitude/-Longitude/...`?
   - What does an encrypted, non-publishable field look like without the key: absent,
     or present and empty?

### Stage 1 -- API client (`api.py`)

`pull_form_xml(form_id)`, `pull_design(form_id)`, `pull_wide_json(form_id, *, key_path=None, since=None)`,
`pull_dataset(dataset_id)`, `pull_form_files(form_id)`.

Tests (HTTP mocked from stage-0 recordings, no network):

- each function hits the URL recorded in `docs/api-endpoints.md`;
- `key_path` sends the key as multipart `private_key` and the bytes never appear in
  logs, exceptions or reprs;
- any non-2xx raises with status + URL (no retry, no swallow);
- credentials come from env (`SCTO_SERVER`, `SCTO_USER`, `SCTO_PASSWORD`, `SCTO_KEY_PATH`);
  a missing one raises naming the variable.
- One `@pytest.mark.live` test per function, skipped unless `SCTO_LIVE=1`.

Use `requests` (already what pysurveycto uses) unless stage 0 shows a reason not to.

### Stage 2 -- Contract + publishable

- `contract = parse_contract(xml)` from upstream.
- Add the publishable flag where stage 0 found it. If it is in the XML, that is an
  upstream change to `Node` (new field) + `parse_contract` -- do it upstream with tests
  there.
- **Contract test (the key one):** `publishable fields from the contract == oracle from
  the two recorded pulls`. This catches SurveyCTO changing its XML output later.
- Dataset discovery: the set of datasets to pull = every `Node.data_source.dataset`.
  Test: the test form's XML yields exactly the attached CSV + the server dataset;
  each resolves to either a form file (manifest) or a server dataset, and an
  unresolvable one raises.

### Stage 3 -- questions.json + sections from XML

Emit a list of question dicts with exactly the upstream keys:
`type, variable_name, question_text, choice_list, choices, relevance, group_relevances,
constraint, stata_constraint (null), stata_skip_logic (null), required, disabled,
calculation, choice_filter, appearance, references, group_path, question_order`.
Choice dicts: `value, label`.

- Labels from `<itext>`, the form's default language (from XML); keep other languages
  only if upstream's schema has a slot -- check, don't invent one.
- Inline choices from `Node.choice_items`; `search()`/file choices from the pulled
  dataset, keyed through `sanitize_choice_value` where upstream does.
- `references` = `${var}` tokens across relevance/calculation/constraint (upstream
  `_parse_refs`).
- Tests: on the test form, every non-system node appears once, in body order; schema
  parity with `sample/output/household_survey_questions.json` (same key set, same value
  types); `section_splitter` runs on the output unchanged.
- If upstream exposes `/design` JSON -> XLSForm rows cheaply, a parity test
  "XML-built == XLSForm-built questions.json" on the test form is worth more than any
  hand-written expectation. Add it if stage 0 shows `/design` is the XLSForm.

### Stage 4 -- Typed parquet

Cast the all-string wide JSON using the contract (column -> `map_column` -> node ->
`xml_type`/`control`):

| XML | Arrow |
|---|---|
| `int` | `int64` (nullable) |
| `decimal` | `float64` |
| `date` | `date32` |
| `dateTime` | `timestamp[ms, tz=UTC]` (check what SurveyCTO emits; record in findings) |
| `time` | `time64` |
| `select_one` | `string` (codes stay strings: `01` != `1`) |
| `select_multiple` | `string` (space-separated); per-choice binaries `int8` if present |
| `geopoint` etc. | per stage-0 findings |
| `string` / `calculate` | `string` |

- Empty string -> null. Anything else that fails to cast **raises** with column, row key
  and value. No coercion to null, no `errors="coerce"`.
- Sentinel codes (-99, -98, -88) in int fields cast as ints; flagging them is the
  dictionary's job.
- Columns `map_column` cannot resolve raise, except `is_system_column` ones, which get
  fixed types (`KEY` string, `SubmissionDate`/`CompletionDate` timestamp, ...).
- Store `xml_type`, node path and `repeat_path` as Arrow field metadata.
- Tests: every type above on the fixture; a bad value raises with the triple; round-trip
  read gives the declared schema; row count == submissions.

### Stage 5 -- Variable dictionary + graph

- Same top-level shape as upstream: `{dataset, summary, variables: {name: entry}}`.
  Entry keeps `variable_order, non_null_count, missing_rate, survey{...}`, replaces
  `stata{type}` with `parquet{arrow_type, xml_type}`, adds `pii{...}` (stage 6).
- Stats from parquet metadata (upstream `_read_parquet_column_stats`), sentinels via
  upstream `core/sentinels.py`.
- Graph via upstream `build_variable_graph(questions, vardict_json, graph_path)`,
  written as `<form>_variable_graph.json` next to the dictionary (the MCP server's
  naming convention).
- Tests: schema parity with `sample/output/household_survey_variable_dictionary.json`
  except the documented `stata`->`parquet` swap; graph edge types match upstream's set.

### Stage 6 -- PII classification

First matching rule wins:

| # | `status` | Rule | Presidio |
|---|---|---|---|
| 1 | `cleared_by_form` | node is publishable | never |
| 2 | `contains_pii` | type `geopoint`/`geotrace`/`geoshape`; attachment types (`image`, `audio`, `video`, `file`, signature, audio audit); device metadata (`deviceid`, `subscriberid`, `simserial`/`simid`, `phonenumber`/`devicephonenum`, `username`) | never |
| 3 | `whitelisted` | `int`/`decimal`; select with inline choices; `calculate` whose `references` are all `whitelisted` and which has no `pulldata()` | never |
| 4 | `may_contain_pii` or `no_hits` | everything else: `text`, `pulldata()` calculates, `search()`/file selects, calculates with any non-whitelisted input | on the column's values |

Block per variable:

```json
"pii": {
  "status": "may_contain_pii",
  "basis": ["type:text", "presidio:PERSON", "presidio:PHONE_NUMBER"],
  "presidio": {"entities": {"PERSON": 34, "PHONE_NUMBER": 2},
               "distinct_scanned": 120, "hit_keys": ["uuid:..."]}
}
```

- `basis` always says why (`publishable`, `type:geopoint`, `lineage:a,b`, ...).
- Presidio config (`[pii]` in TOML): `entities` default
  `PERSON, PHONE_NUMBER, EMAIL_ADDRESS, LOCATION` + a configurable ID regex recognizer;
  `score_threshold`; `max_distinct` per column; `seed`; `languages` default = the
  form's itext languages mapped to spaCy models.
- Scan distinct non-null values, sampled with the seed when over `max_distinct`.
- **Never store matched text**, only entity counts and the record keys of hit rows.
- Write `<form>_pii_review.parquet`: record key + every `contains_pii` and
  `may_contain_pii` column, for the user to verify.
- Tests:
  - each rule fires on its test-form field; first-match order holds (a publishable
    `text` is `cleared_by_form` and Presidio is **not called** -- assert with a spy);
  - lineage: integer-only calculate -> `whitelisted`; `concat(name1, name2)` -> scanned;
    `pulldata()` calc -> scanned;
  - fictional names / phones in the fixture -> `may_contain_pii` with the right entities;
    a numeric-looking non-PII text column -> `no_hits`;
  - same input + seed -> identical block (determinism);
  - no value from the data appears anywhere in the dictionary JSON (grep the output for
    every fixture name/phone);
  - missing spaCy model for a configured language raises.

### Stage 7 -- MCP (upstream PR)

In `surveycto_extractor_takeout`, `mcp_server/survey_server.py`:

- render the `pii` block in `lookup_variable`, `lookup_variables`,
  `get_variable_neighborhood`;
- render `parquet{arrow_type}` where it renders `stata{type}`;
- server instructions: "check `pii.status` before reading a variable's values; review
  `<form>_pii_review.parquet` for flagged columns";
- optional config switch `mcp.withhold_pii_values` (default **off**) that omits value-level
  output for `contains_pii` / `may_contain_pii` variables -- for teams that want to
  hold themselves to it (IPA does).
- Tests in upstream's `tests/test_survey_server.py` style.

### Stage 8 -- CLI + config

- `scto-pull` (stages 1-2: XML, JSON, datasets) and `scto-build` (stages 3-6), plus
  `scto-run` doing both. Config TOML compatible with upstream `config_loader` dataset
  entries (`questions_json`, `output_json`) so the MCP server picks outputs up unchanged;
  credentials only from env / `.env`.
- `.gitignore`: data dirs, `.env`, `*.pem`.
- End-to-end test on the recorded fixtures: `scto-build` from a fixtures dir produces all
  outputs, and the upstream MCP server loads them and answers `lookup_variable` on a
  publishable, a `contains_pii` and a `may_contain_pii` field.

## 5. Ground rules for the session

- No network in unit tests; live tests opt-in only (`SCTO_LIVE=1`).
- Fixtures are fictional. If a recorded pull contains anything that looks real, stop
  and tell Daniel; do not edit or commit it.
- Commit per green stage; conventional commits; PR to `main` per stage or pair of stages.
- Upstream changes go to `surveycto_extractor_takeout` as their own PRs, landed before the
  downstream stage that needs them.

## 6. Open questions to settle as they come up (ask, don't assume)

- Pulled server datasets / preload CSVs are actual data and often rosters. Do they get
  their own parquet + PII pass in v1, or only feed choice labels?
- `search()` choice labels drawn from a roster end up in questions.json and are served by
  `get_choice_list`. Should a select whose dataset column is flagged carry that into its
  own `pii.basis`?
- Incremental pulls (`since=` + merge on `KEY`, as pysurveycto-stata does) -- v1 or later?
- Repo name.
