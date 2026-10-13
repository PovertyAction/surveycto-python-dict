# Contributing

The tool serves any SurveyCTO project on its way to cleaning. A change belongs here
when any form could need it; a gap in one project's flags goes in that project's
`[pii] whitelist` or `blacklist`.

## Ground rules

- **Nothing from a real form enters git:** no submissions, XML, dataset rows, form
  IDs, field names, labels or counts, in code, tests, docs or commit messages. The
  repository is public.
- **Tests are offline and synthetic.** Fixtures are invented XForms and wide-JSON rows
  shaped like the API's (see `tests/farm_form.py`); a fresh clone with no `.env` and no
  `data/` passes the whole suite.
- **The server is the source of truth.** The tool reads the compiled form XML, the
  wide JSON and the server datasets; it reads no XLSForm.
- **Fail loud.** Raise on a case the code does not handle instead of guessing: a silent
  wrong answer costs more than a crash.
- **Report, do not enforce.** The outputs describe the data; the people cleaning it
  decide. Sentinels are counted, never recoded, and a PII flag changes what is shown,
  never what is kept.

## Workflow

1. Branch from `main` (`fix/...`, `feat/...`, `docs/...`).
2. `just setup` once, then make the change with a test that fails without it.
3. `just test` and `just lint` pass.
4. Open a pull request using the template. CI runs the tests on Python 3.11 and 3.12
   and the pre-commit hooks; a code owner reviews it.

Commit subjects follow `type(scope): what changed`, for example
`fix(pii): a calculate carries the flag of a field it reads`.

## Checking against a live project

Field tests run the tool on a real project you have access to (`just run`), with
outputs in a git-ignored `out_dir`. Report what you find as an issue described in
general terms ("a select_multiple inside a repeat"), never with the form's names or
values.
