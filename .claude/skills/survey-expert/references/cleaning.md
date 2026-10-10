# Cleaning SurveyCTO data in Python

The team's suggested cleaning practice for this tool's outputs, in pandas/pyarrow.
These are guidelines, not steps the tool runs: the people and agents cleaning the data
decide what applies to their study. Each suggestion is followed by its reason.

## How to write a cleaning script

Write for the person who reviews it, not for the computer.

- **A plain script, top to bottom,** read like a do-file: load, check, change, check,
  save. No helper functions or classes unless the same lines repeat many times.
- **Look before you write.** The MCP tools (or `scto-survey`), the summary stats and the
  data tell you the codes, the skip logic and what is missing. Write for what is there,
  not for cases that have not happened.
- **Name the columns explicitly,** and comment why a step is done; the code shows what.
- **Check with plain `assert`.** When a check could fail on future data, the comment says
  what to decide then.
- **Say what you found about missing values** before computing anything from them.
- **Readability over speed.**

A whole script in this style:

```python
"""Average skill rating (skill_avg).

Input:  data/out/<form>/<form>.parquet
Output: data/out/<form>/derived/skills.parquet

Four self-ratings on the same 0-3 scale, asked only when consent = 1.
"""

import pandas as pd

df = pd.read_parquet("data/out/<form>/<form>.parquet")

items = ["skill_read", "skill_write", "skill_speak", "skill_listen"]

# Only people who consented were asked.
consented = df["consent"] == 1
assert df.loc[~consented, items].isna().all().all()

# Missing ratings among the people who were asked.
print(df.loc[consented, items].isna().sum())

# Everyone who consented answered all four, so the average uses all four. If a rating
# is ever missing, decide whether the average uses the ratings that exist or stays
# missing for that person.
assert df.loc[consented, items].notna().all().all()
assert df.loc[consented, items].isin([0, 1, 2, 3]).all().all()

df["skill_avg"] = df[items].mean(axis=1)
print(df.loc[consented, "skill_avg"].describe())

df[["KEY", *items, "skill_avg"]].to_parquet(
    "data/out/<form>/derived/skills.parquet", index=False
)
```

## What the outputs already give you

- `<form>.parquet` is typed from the XForm: integers, decimals, dates and UTC
  timestamps are real types. A select_one is an integer when every choice code is a
  plain integer (`0`, `1`, `-99`), and text when any code has a leading zero or letters
  (`"01"`, `"yes"`), so no code changes meaning.
- Empty and not asked are both null. Why a value is missing (skipped, DK, refused)
  comes from the eligibility mask and the sentinel codes (sections 3-4).
- Sentinel codes are **still coded**: `-99` is `-99` in the parquet. The dictionary
  counts them per column under `sentinels`; nothing has been recoded.
- select_multiple arrives as `<name>_<code>` binaries: `1` chosen, `0` when another
  choice of the same question was chosen in that row, null when the question was
  skipped. There is no space-separated column. Where the question's list is read per
  submission (`choices_per_submission`), only codes someone picked have a column, and
  an unpicked code is null (section 6).
- `questions.json` holds each question's `relevance`, `group_relevances`,
  `constraint` and `choices` in `${name}` syntax: the instrument, in machine form.
- `just stats` writes `<form>_summary_stats.csv`: per column, how many rows were
  eligible under skip logic, answered, sentinel, missing while eligible, and answered
  while skipped. `<form>.stats` in the database has it typed; pandas reads the CSV
  with `low_memory=False`, since most of its text columns are empty.
- A column kept as text (`parquet.cast_fallback` in the dictionary) holds values that
  did not fit the form's type; look at why before converting it.

## 1. Fail loudly

Write what must be true of the data as checks that stop the run. A check that passes
silently, or a step that swallows an error, moves the problem downstream where it is
harder to find.

1. **Encode every expectation as a check, next to the step that relies on it:** value
   domains, unique keys, the skip structure, non-negative quantities, merge cardinality,
   row counts after a reshape.
2. **Use plain `assert`,** with a message when the condition does not say it:
   ```python
   assert not df["hhid"].duplicated().any(), "hhid is not unique"
   assert (df["age"].dropna() >= 0).all(), "age has negative values"
   ```
2. **Tolerances are shares over the relevant denominator** (gate-eligible rows for
   sub-questions): about 1% by default, 1.5-2% for DK on hard questions, 5-10% for known
   instrument problems, above 10% only documented, above 25% the column may be
   unusable. Print a warning even inside tolerance and comment the rationale.
   ```python
   age = df.loc[asked, "age"]
   print("age above 100:", (age > 100).sum(), "of", len(age))
   assert (age > 100).mean() <= 0.01   # up to 1%: typos the form's constraint let through
   ```
3. **When a check fails, inspect before relaxing it:** list the contradicting rows,
   check the instrument (`questions.json`), then fix the code or the check, and say
   which in a comment.
4. **Let errors surface:** no `errors="coerce"`, `errors="ignore"`, `if col in df`,
   bare `except` or `try`/`except` that carries on. A schema change or a bad value
   should stop the run, not turn into missing data.

## 2. SurveyCTO submissions

1. **Form versions:** each row carries `formdef_version`, and the outputs are built
   from the current XML. Submissions from older versions may have had other relevance,
   choices or fields; they are the usual source of `answered_when_skipped` in the
   stats and of "choice code not among the form's choices" text columns. Tabulate the
   versions, and decide per change how older rows are handled.
2. **`KEY` identifies the submission, not the unit:** a household, person or firm
   visited twice has two `KEY`s. The unit is the study's own ID field; use `KEY` only to
   point at a submission.
3. **Duplicate respondents:** check the study ID for repeats and resolve them with a
   written rule (for example the latest completed submission), logging how many rows
   each branch drops.
4. **Test and pilot submissions:** drop them by a documented cut (before the launch
   date, or test enumerators or devices) and log the count.
5. **Timing:** check `starttime <= endtime <= CompletionDate <= SubmissionDate` and
   that `duration` is plausible; flag outliers for review rather than dropping them.
6. **`review_status`** is generated by SurveyCTO and says little about the data unless
   the team reviews submissions in the console; do not filter or assert on it.

## 3. Sentinels (the study's codes in `SCTO_SENTINELS`, e.g. -99 Don't know, -88 Refused)

1. **The instrument decides which codes are valid for each question.** Look up each
   question's `constraint` and `choices` (the MCP lookup shows both) and recode exactly
   those codes: a question whose choices allow `-99` and `-88` needs both recoded, and
   recodes for codes the form never allows create false confidence. Comment columns with none
   ("choices 0/1, no sentinels"). For a number, `sentinels_allowed` in the dictionary
   (the lookup's "Non-response codes its constraint accepts") lists the codes its
   constraint accepts; `sentinels` counts the codes in the data, whatever the question
   allows.
2. **Tabulate, assert coverage, assert tolerance, then recode in one pass.** An unknown
   or missed code otherwise passes silently.
   ```python
   print(df.loc[df["income"] < 0, "income"].value_counts())
   assert df.loc[df["income"] < 0, "income"].isin([-99, -88]).all()
   ```
3. **Suggested: move the codes to `<var>_dk`, then set them missing in `<var>`.** Where a column
   holds a sentinel code that is not part of its normal distribution, create the
   auxiliary column `<var>_dk` holding that code (`-99`, `-88`, ...) on those rows and
   null elsewhere, and convert the code to missing in the main column. The main column
   then holds only real values, and `<var>_dk` keeps which non-response it was.
   ```python
   dk = df["income"].isin([-99, -88])
   df["income_dk"] = df["income"].where(dk)   # -99 or -88 where it was a code
   df.loc[dk, "income"] = None                # income now holds real values only
   ```
   Do it before any arithmetic, and on the full sample.
4. **Recode before any arithmetic, division first.** `1 / -55 = -0.018` is a plausible
   value no later recode will find. If recode and arithmetic live in different
   modules, add a non-negative assertion on the derived column.
5. **Calculate fields leak sentinels.** A calculate does not inherit its inputs'
   constraints: `${length} * ${width}` with `-99` is a large negative. Recode the
   calculate column from its inputs (missing where any input has a `_dk` code). Never
   `abs()`.
6. **Formula artifacts are not DK.** A negative made by the formula itself is an error:
   set it missing with its own reason (`bad_calc`) and a flag.
7. **Assert non-negative quantities right after creating them**, before aggregating:
   `assert (df["area"].dropna() >= 0).all()`.
8. **Don't fold DK into "no" before the sentinel pass.**
   `np.where(s.isin([0, -99]), 0, ...)` makes DK disappear into "No" in tables. If the
   protocol really wants DK = 0, do it after, explicitly, with a comment.

## 4. Skip logic and structural missing

To learn what a gate opens or what a calculate reads, start with
`get_variable_neighborhood` on it (the survey-expert skill, "How a form works").

1. **Not asked and asked-but-DK are different and never share a code.** A null in
   `<var>` with a code in `<var>_dk` was asked and not answered; a null where the
   eligibility mask is false was skipped. `just stats` evaluates every relevance per
   row; reproduce its mask in cleaning code when you need it.
2. **Check the skip structure both ways:** missing where the skip applies, present
   where the question was asked. `answered_when_skipped` and `missing_when_eligible` in
   the stats CSV are those two counts. SurveyCTO clears the answer to a question that is
   not relevant, so on its data `answered_when_skipped` should be 0, and
   `missing_when_eligible` is 0 for required questions (`required` in the stats CSV).
   Calculates and optional questions can be empty by design.
3. **Trace a violation to its source before changing data.** A non-zero count points
   to a submission from another form version or an edit on the server, not to the
   respondent: look at the rows' `formdef_version` and review corrections, fix the
   cause, and document it.
4. **Zero-by-logic is an analysis choice, made in a derived column.** A skipped
   question is null by design and correct as collected. When a table needs the
   unconditional value and the gate determines it ("no loans" means `loan_size = 0`),
   fill a new column from the gate and keep the original. Recode sentinels first; for
   nested gates, zero where the top gate is off, then item-level where the item gate is
   off.
   ```python
   df["loan_size_all"] = df["loan_size"]
   df.loc[df["has_loan"] == 0, "loan_size_all"] = 0   # no loan: size 0
   # Rows where has_loan is missing stay missing; `!= 1` would zero them too.
   ```
5. **Never fill zeros from missingness.** `fillna(0)` erases "doesn't own" vs refused.
6. **Choose conditional or unconditional on purpose.** A follow-up asked only past a
   gate gives a conditional mean ("among borrowers"). Use the zero-filled column when
   readers expect a rate over everyone; keep the original and say "among X" in labels
   and captions when the conditional population is the point. Watch for non-null
   counts far below N and binary means near 1.
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
## 5. Recoding and other-specify

1. **Classify other-specify text by priority:** lowercase and trim; one regex per
   category; ambiguous beats specific; blank, unmatched or ambiguous is missing; text
   describing absence ("has no house") maps to the substantive category.
   ```python
   text = df["water_osp"].str.lower().str.strip()
   df["water_osp_class"] = None
   df.loc[text.str.contains("well|borehole", na=False), "water_osp_class"] = "improved"
   df.loc[text.str.contains("mixed|several", na=False), "water_osp_class"] = None  # ambiguous wins
   ```
   Later lines overwrite earlier ones, so the highest priority goes last. Without
   `na=False`, `str.contains` returns NaN for empty text.
2. **Keep a QC table** of every distinct trimmed text, its mapping and count, and
   review it when new data arrives. It prints free text: only where PII may be read.
3. **Priority order:** with one `.loc` line per category, later lines overwrite earlier
   ones, so write the lowest priority first and the highest last, and say so in a
   comment.
4. **Ordinal categories:** prefer mutually exclusive conditions and assert each row
   falls in exactly one: `assert (low.astype(int) + mid.astype(int) + high.astype(int) == 1).all()`.
5. **A `search()` select's labels** are the form's own choices plus its list's dataset
   rows (`choice_columns` names the value and label columns, `data_source` the
   filter). Where a dataset row's label is empty, keep the form's label.
5. **One-off corrections by signature, not ID:** match on a value signature, assert the
   match count, apply, comment. Five or more go in an overrides CSV (old, new, source,
   rationale), applied by the script with an assert on the rows changed; a correction
   that changes 0 rows on every run is dead.
6. **Choice codes can change between form versions:** assert the code domain against
   the current `choices` after every pull; never hard-code "code N means X" across
   rounds.
7. **Named constants for magic numbers** (`WEEKS_PER_MONTH = 4.3`), defined once.

## 6. select_multiple

1. **The binaries are ready:** null already means not answered and 0 unchosen, so do not
   rebuild them from strings. The exception is a list read per submission
   (`choices_per_submission`): an unpicked code there is null too, so fill 0 where the
   question was answered, which is where any of its binaries is non-null:
   `answered = df[cols].notna().any(axis=1)`, then
   `df.loc[answered, cols] = df.loc[answered, cols].fillna(0)`. If you derive a binary from another source, match tokens,
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

## 7. Repeats and reshape

1. **Layout:** fields in a repeat end in `_<n>` per instance (`_<n>_<m>` nested); each
   repeat has a `<repeat>_count` question. Instance `n` is the n-th entry, **not** a
   choice code: map positions to codes through the routing variable after reshaping.
2. **Suggested: work each repeat at its own level, from its long view.** `just duckdb`
   writes `<form>.<repeat>` in the project database: one row
   per instance with any value, keyed by
   `KEY` and one `<repeat>_index` per repeat from the outermost down, each field
   under its name without the instance suffix. A household roster becomes one row per
   member. Clean and aggregate at that level, then merge the results back to the
   submission table on `KEY`.
   ```python
   con = duckdb.connect("data/out/<project>/<project>.duckdb", read_only=True)
   roster = con.sql(
       "SELECT KEY, roster_index AS member, member_name, member_age "
       "FROM baseline.roster"
   ).df()
   assert not roster.duplicated(["KEY", "member"]).any()

   # The view keeps every instance with a value, so compare with the declared count:
   # an instance past it was filled and then dropped from the count.
   counts = con.sql("SELECT KEY, roster_count FROM baseline.submissions").df()
   ```
3. **Merge back with the identifiers and a declared cardinality:**
   ```python
   per_hh = roster.groupby("KEY").agg(n_members=("member", "size"))
   df = df.merge(per_hh, on="KEY", how="left", validate="one_to_one")
   ```
4. **Reshape safely:** assert unique keys before, the key and row count after.
   `pivot_table` silently averages duplicates; `wide_to_long` and `pivot` raise.
5. **Padding slots:** the wide table has a slot for every instance any submission
   used; drop slots with no values, and exclude slots beyond the reported count from
   missing checks.
6. **Carry the `_dk` columns through reshape and aggregation:** reshape them with
   their values and count them per unit (`roster.groupby("KEY")["income_dk"].count()`);
   after arithmetic a DK and a skip both look null.
7. **The roster head is the relationship code, not slot 1;** assert one head per
   household.
8. **Merges:** `validate="one_to_one"` / `"many_to_one"` and `indicator=True` with
   asserts on the counts; select columns before merging; keep IDs as strings in every
   file. A merge on non-unique keys explodes rows silently.

## 8. Indicators and aggregation

1. **Strict propagation by default:** any missing component makes the total missing:
   `df[cols].sum(axis=1, skipna=False)`. Default `.sum()` treats null as 0 and gives 0
   for an all-null row; `min_count=1` is missing only when all are. Comment which rule
   each aggregate uses.
2. **Sparse components:** when components are mostly skipped by design, zero-fill the
   structural missing values from the gates first, then use the sources' `_dk`
   columns; a plain `isna().any(axis=1)` would make almost everyone missing.
3. **Group sums hide missing:** `groupby().sum()` skips nulls and gives 0 for an
   all-null group. Count the missing values per group alongside the sum and decide what
   the total means when some are missing.
4. **Indicators stay missing when the parent is:** `(df["land"] == 1).astype(int)`
   turns a missing `land` into 0.
   ```python
   df["owns_land"] = (df["land"] == 1).astype(float)
   df.loc[df["land"].isna(), "owns_land"] = None
   ```
5. **Check after aggregating:** one row per unit, components sum to the total, ranges
   plausible.
6. **Record lineage:** say in the script's docstring and comments where each derived
   column comes from and how it is built.

## 9. Verification and reproducibility

1. **Two phases:** validate raw invariants (domains, gate-to-detail counts, skip
   structure against the instrument), then transform with tolerance checks.
2. **Log what each step changes:** pandas never reports "N changes made", so log
   `mask.sum()`; end each module with missing counts, and `describe()` of the
   columns whose values you may see.
3. **Compare outputs per column, aligned on KEY:** `NaN != NaN` is true, so count
   `(a != b) & ~(a.isna() & b.isna())`, and judge floats by maximum difference
   (`np.isclose`), not a file hash. Take a fresh "before" snapshot for each change.
4. **Randomness:** pass one `np.random.default_rng(seed)` around; never the global seed;
   the lockfile pins numpy.
5. **Measure coverage on the delivered data.** A target field counts as filled when the
   output has values, not when a mapping to a question exists. List the fields that
   are mapped but empty, each with its reason: never answered, its gate never true, or
   a question only an older form version had.
6. **Where several people code the same unit,** list their disagreements per field
   rather than keeping one answer silently.

## 10. Pipeline structure

1. **Start from `<form>.parquet` and write your own files.** The parquet typed from
   the XML is the source: read it, never overwrite it. Cleaning writes separate parquet
   files (for example `<form>_clean.parquet`, or one file per level such as a roster),
   so the source stays a fixed point to rebuild from and compare against; `just build`
   regenerates it from the raw pull at any time.
2. **Change data only through scripts, committed and tracked in the study's
   repository.** No edits by hand in a spreadsheet or a notebook cell that is not kept:
   every recode, correction and derived column lives in a script under version
   control, so rerunning it from the source reproduces the cleaned files and the
   history shows who changed what and why. The data stays out of git; the scripts go
   in.
3. **One module per domain,** around 300 lines; a docstring with inputs, outputs and
   dependencies.
4. **No wildcard column lists:** `df.filter(like=...)` matches too much; use an anchored
   regex (`^asset_amount_\d{1,2}$`) or an explicit list.
5. **Drop helper columns** and assert the output columns equal the expected list before
   writing.
6. **One grain per field:** each output field lives in one table, at that table's grain
   (a study, an arm, a wave). Assert the table's key is unique before writing.

## 11. PII

1. **A flag means handle carefully, never drop or avoid.** Scripts use every column:
   join on it, clean it, deduplicate on it, keep it. For a `contains_pii`,
   `may_contain_pii` or `no_hits` column, print counts, null rates, distinct counts and
   the `KEY`s a check flags, never its values, `head()`, `value_counts()` or
   `describe()`. The people cleaning the data verify the flags in
   `<form>_pii_review.xlsx`.
2. **Presidio over-flags** (codes read as names): it proposes, a person decides, and the
   decision goes in `[pii] whitelist`, or `[pii] blacklist` for columns that are
   personal even when the form marks them publishable.
3. **Sharing is decided at delivery, by the team:** free text is reviewed before it
   leaves the project, derived scores stay, and only the flagged other-specify columns
   are PII.
4. **Scan what you deliver,** derived columns included, and make the delivery step
   repeatable.
5. **Tokenize IDs with an HMAC crosswalk** (`hmac.new(key, id.encode(), hashlib.sha256)`,
   base32): deterministic for joins, irreversible without the key. Key and crosswalk
   never go in the repo. One-off corrections keyed by signature survive tokenization.
