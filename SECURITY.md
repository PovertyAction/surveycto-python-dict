# Security

The tool pulls a SurveyCTO project's forms, submissions and server datasets, which
usually hold personal data. Treat everything it downloads or writes as you would the
submissions themselves.

## Never commit

| File or path | Why |
|---|---|
| `.env` | Server name, user, password, key path and sentinel codes |
| `*.pem` | The private key that decrypts encrypted forms |
| `config.toml` | Names the project's forms and datasets |
| `.mcp.json` | Points an agent at a project's outputs |
| `<out_dir>` (`data/` by default) | Raw pulls and every output, PII review files included |

`.gitignore` blocks all of these in this repository. Copy the same entries into a
project that uses the tool, and check `git status` before each commit.

## Agents and personal data

A column flagged `contains_pii` or `may_contain_pii` keeps its values in the parquet
and the database. The MCP server, the survey-expert skill, the codebook and the stats
show such a column's counts only, so an agent working through them never sees its
values. Keep agents away from `<form>_pii_review.xlsx`, the raw pulls and direct reads
of flagged columns.

## Reporting a vulnerability

Report a security issue through GitHub's private vulnerability reporting on this
repository, or write to <researchsupport@poverty-action.org>. Do not open a public
issue for it.
