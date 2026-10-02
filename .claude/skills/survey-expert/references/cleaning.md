# Cleaning SurveyCTO data in Python

The team's cleaning practice for this tool's outputs, in pandas/pyarrow. Each rule is
followed by its reason. Read this before writing or reviewing cleaning code.

## What the outputs already give you

- `<form>.parquet` is typed from the XForm: integers, decimals, dates and UTC
  timestamps are real types; select codes stay strings (`"01"` is not `"1"`).
- Empty and not asked are both null. Why a value is missing (skipped, DK, refused)
  comes from the eligibility mask and the sentinel codes (sections 1-2).
- Sentinel codes are **still coded**: `-99` is `-99` in the parquet. The dictionary
  counts them per column under `sentinels`; nothing has been recoded.
- select_multiple arrives as `<name>_<code>` binaries: `1` chosen, `0` when another
  choice of the same question was chosen in that row, null when the question was
  skipped. There is no space-separated column.
- `questions.json` holds each question's `relevance`, `group_relevances`,
  `constraint` and `choices` in `${name}` syntax: the instrument, in machine form.
- `just stats` writes `<form>_summary_stats.csv`: per column, how many rows were
  eligible under skip logic, answered, sentinel, missing while eligible, and answered
  while skipped.
- A column kept as text (`parquet.cast_fallback` in the dictionary) holds values that
  did not fit the form's type; look at why before converting it.

## 1. Sentinels (-99 Don't know, -88 Refused, -77 Not applicable, -66 Other, -55 Not in list)

1. **The instrument decides which codes are valid for each question.** Build the recode
   map per column from its `constraint` and `choices`, not one blanket map: a question
   whose choices allow `-99` and `-88` needs both recoded, and adding recodes for codes
   the form never allows creates false confidence. Comment columns with none
   ("choices 0/1, no sentinels").
2. **Tabulate, assert coverage, assert tolerance, then recode in one pass.** An unknown
   or missed code otherwise passes silently.
   ```python
   neg = s[s < 0]
   print(neg.value_counts())
   if not neg.isin(VALID[col]).all():
       raise ValueError(f"{col}: unknown negative codes {sorted(set(neg) - VALID[col])}")
   ```
3. **Derive missing-reason columns before recoding** (`<col>_mi` in
   `{asked, skipped, dk, rf, na}`), because after `-99 -> NA` the code is gone. Build
   them on the full sample, from the raw values and the eligibility mask.
4. **Recode before any arithmetic, division first.** `1 / -55 = -0.018` is a plausible
   value no later recode will find. If recode and arithmetic live in different
   modules, add a non-negative assertion on the derived column.
5. **Calculate fields leak sentinels.** A calculate does not inherit its inputs'
   constraints: `${length} * ${width}` with `-99` is a large negative. Recode the
   calculate column from its inputs (NA where any input is DK/RF). Never `abs()`.
6. **Formula artifacts are not DK.** A negative made by the formula itself is an error:
   set it missing with its own reason (`bad_calc`) and a flag.
7. **Assert non-negative quantities right after creating them**, before aggregating:
   `if (s.dropna() < 0).any(): raise ...`.
8. **Don't fold DK into "no" before the sentinel pass.**
   `np.where(s.isin([0, -99]), 0, ...)` makes DK disappear into "No" in tables. If the
   protocol really wants DK = 0, do it after, explicitly, with a comment.

## 2. Skip logic and structural missing

1. **Not asked and asked-but-DK are different and never share a code.** Keep the
   eligibility mask (or the reason column) next to the value. `just stats` evaluates
   every relevance per row; reproduce its mask in cleaning code when you need it.
2. **Assert the skip structure both ways:** missing where the skip applies, present
   where the question was asked. `answered_when_skipped` and `missing_when_eligible`
   in the stats CSV are those two counts.
3. **Resolve skip-logic violations against the gate-off denominator.** Count them,
   assert at most ~1%; if most expected follow-ups hold valid values, flip the gate on,
   otherwise enforce the gate. Comment the decision.
4. **Zero-by-logic only when the gate determines the value** ("no loans" means
   `loan_size = 0`). Recode sentinels first, then zero; for nested gates, zero when the
   top gate is off, then item-level where the item gate is off.
   ```python
   df.loc[df["has_loan"].eq(0), loan_cols] = 0   # a null gate fails eq(0): stays missing
   ```
   `gate != 1` would also zero rows whose gate is missing.
5. **Never fill zeros from missingness.** `fillna(0)` erases "doesn't own" vs refused.
6. **Decide conditional vs unconditional on purpose.** A follow-up asked only past a
   gate and never zero-filled gives a conditional mean where readers expect a rate.
   Zero-fill from the gate when the consumer wants the unconditional rate; keep it
   missing and say "among X" in labels and captions when the conditional population is
   the point. Watch for non-null counts far below N and binary means near 1.
7. **Scope row totals to the eligible rows.** `df[cols].sum(axis=1)` returns 0 for an
   all-null row: mask with `.where(eligible)`.
8. **Translate relevance with XPath semantics.** A comparison with an empty field is
   false, but its negation is true: in pandas `~(df.age > 18)` and `df.x != 1` include
   missing rows. `selected(${x}, '2')` is `x == "2"` for select_one and `x_2 == 1` for
   select_multiple. Stripping an untranslatable clause from a top-level `and` only
   broadens the condition; inside `not(...)` it narrows it, so drop the whole `not`.
9. **Cross-check related variables:** gate "no" implies frequency 0; more detail rows
   than the headline count raises the count; platform totals match your own within a
   tolerance; treatment arm and strata agree across modules.
10. **Form versions:** when a module was added or removed mid-fieldwork, keep a coverage
    flag from `formdef_version` and document how rows without it are handled.

## 3. Recoding and other-specify

1. **Classify other-specify text by priority:** lowercase and trim; one regex per
   category; ambiguous beats specific; blank, unmatched or ambiguous is missing; text
   describing absence ("has no house") maps to the substantive category.
   ```python
   t = s.str.lower().str.strip()
   conds = [t.str.contains(AMBIGUOUS, na=False), t.str.contains(IMPROVED, na=False)]
   out = np.select(conds, [np.nan, 1], default=np.nan)   # first match wins
   ```
   Without `na=False`, `str.contains` returns NaN for null input.
2. **Keep a QC table** of every distinct trimmed text, its mapping and count, and
   review it when new data arrives. It prints free text: only where PII may be read.
3. **Priority order depends on the tool:** sequential `.loc` assignments are last-wins
   (lowest priority first); `np.select` is first-wins; `a.combine_first(b)` keeps `a`.
4. **Ordinal categories:** prefer mutually exclusive conditions and assert each row
   falls in exactly one: `(np.column_stack(conds).sum(1) == 1).all()`.
5. **One-off corrections by signature, not ID:** match on a value signature, assert the
   match count, apply, comment. Five or more go in an overrides CSV (old, new, source,
   rationale) applied by a helper that asserts and logs rows changed; a correction that
   changes 0 rows on every run is dead.
6. **Choice codes can change between form versions:** assert the code domain against
   the current `choices` after every pull; never hard-code "code N means X" across
   rounds.
7. **Named constants for magic numbers** (`WEEKS_PER_MONTH = 4.3`), defined once.

## 4. select_multiple

1. **The binaries are ready:** null already means not answered and 0 unchosen, so do not
   rebuild them from strings. If you derive a binary from another source, match tokens,
   not substrings (`2` must not match `12`), and mask `get_dummies` output back to null
   for null rows.
2. **`count-selected` is the row sum of the question's binaries**, null where the
   question was skipped: `df[cols].sum(axis=1, min_count=1)`.
3. **Invert "none of the above" with equality:** `has_any = (none == 0)` plus
   `.where(none.notna())`, not `none - 1`.
4. **Check exclusive-option guards:** when a constraint and a downstream relevance list
   different exclusive codes (typically `-66` Other), "other + a real option" can switch
   a whole repeat off. Read the exclusive set from the constraint.
5. **Write a frequency table for every new binary**, as a baseline.

## 5. Repeats and reshape

1. **Layout:** fields in a repeat end in `_<n>` per instance (`_<n>_<m>` nested); each
   repeat has a `<repeat>_count` question. Instance `n` is the n-th entry, **not** a
   choice code: map positions to codes through the routing variable after reshaping.
2. **Reshape safely:** assert unique keys before, the key and row count after.
   `pivot_table` silently averages duplicates; `wide_to_long` and `pivot` raise.
3. **Padding slots:** reshaping long creates a slot per possible instance; exclude
   slots beyond the reported count from missing checks.
4. **Carry DK/RF flags through reshape and aggregation:** flag on the source columns,
   then `groupby(...).agg(any)`; after arithmetic DK and not-asked both look null.
5. **The roster head is the relationship code, not slot 1;** assert one head per
   household.
6. **Merges:** `validate="one_to_one"` / `"many_to_one"` and `indicator=True` with
   asserts on the counts; select columns before merging; keep IDs as strings in every
   file. A merge on non-unique keys explodes rows silently.

## 6. Indicators and aggregation

1. **Strict propagation by default:** any missing component makes the total missing:
   `df[cols].sum(axis=1, skipna=False)`. Default `.sum()` treats null as 0 and gives 0
   for an all-null row; `min_count=1` is missing only when all are. Comment which rule
   each aggregate uses.
2. **Sparse components:** when components are mostly skipped by design, zero-fill the
   structural missings from the gates first, then flag DK/RF on the sources; a plain
   `isna().any(axis=1)` would make almost everyone missing.
3. **Group sums hide missing:** `groupby().sum()` skips nulls and gives 0 for an
   all-null group; pyarrow's `pc.sum` gives null. Carry an `any missing` column and
   mask afterwards.
4. **Indicators stay missing when the parent is:** `(s == 1).astype(int)` turns null
   into 0. Use `(s == 1).astype("Int8").where(s.notna())`.
5. **Impute with medians by arm x stratum** among valid values, merged
   `many_to_one`, and record that the value is imputed.
6. **Check after aggregating:** one row per unit, components sum to the total, ranges
   plausible.
7. **Record lineage:** pandas has no variable labels. Keep a codebook keyed by column
   (or Arrow field metadata) saying source, imputation and suffix meaning (`_imp`,
   `_sh`), and test it survives the write.
8. **Winsorize last,** after sample restrictions, and keep the unwinsorized file.
9. **Money:** deflate to the reporting year first, convert currency at reporting time,
   with rates from one table.

## 7. Verification and reproducibility

1. **Raise, don't assert, in pipelines:** `python -O` strips `assert`.
2. **Two phases:** validate raw invariants (domains, gate-to-detail counts, skip
   structure against the instrument), then transform with tolerance checks.
3. **Tolerances are shares over the relevant denominator** (gate-eligible rows for
   sub-questions): about 1% by default, 1.5-2% for DK on hard questions, 5-10% for known
   instrument problems, above 10% only documented, above 25% the column may be
   unusable. Print a warning even inside tolerance; comment the rationale.
4. **When an assertion fails,** list the contradicting rows, check the instrument, then
   fix the code or the assertion.
5. **Log what each step changes:** pandas never reports "N changes made", so log
   `mask.sum()`; end each module with `describe(include="all")` and missing counts.
6. **Compare outputs per column, aligned on KEY:** `NaN != NaN` is true, so count
   `(a != b) & ~(a.isna() & b.isna())`, and judge floats by maximum difference
   (`np.isclose`), not a file hash. Take a fresh "before" snapshot for each change.
7. **Randomness:** pass one `np.random.default_rng(seed)` around; never the global seed;
   the lockfile pins numpy.

## 8. Pipeline structure

1. **One module per domain,** around 300 lines; a docstring with inputs, outputs and
   dependencies.
2. **Treat the schema as fixed:** no `errors="ignore"`, `errors="coerce"`,
   `if col in df` or bare `except`; let a schema change fail loudly.
3. **No wildcard column lists:** `df.filter(like=...)` matches too much; use an anchored
   regex (`^asset_amount_\d{1,2}$`) or the column list from the dictionary.
4. **Drop helper columns** and assert the output columns equal the expected list before
   writing.

## 9. PII

1. **Check `pii.status` before reading any column's values;** `contains_pii` and
   `may_contain_pii` are for the people cleaning the data, through
   `<form>_pii_review.parquet`.
2. **Presidio over-flags** (codes read as names): it proposes, a person decides, and the
   decision goes in `[pii] whitelist`.
3. **Free text:** drop or review before sharing; keep derived scores. Do not drop every
   other-specify column; only the flagged ones are PII.
4. **Scan what you deliver,** derived columns included, and make the drop repeatable.
5. **Tokenize IDs with an HMAC crosswalk** (`hmac.new(key, id.encode(), hashlib.sha256)`,
   base32): deterministic for joins, irreversible without the key. Key and crosswalk
   never go in the repo. One-off corrections keyed by signature survive tokenization.
