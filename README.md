# surveycto-python-dict

Pulls a SurveyCTO form's compiled XML and wide JSON over the API and builds, from them,
`questions.json`, sections, typed parquet, a variable dictionary and graph, and a
per-variable PII signal.

## Use

```sh
uv sync
cp config.example.toml config.toml   # set form_id
uv run scto-run                      # scto-pull, then scto-build
```

Credentials come from `.env`: `SCTO_SERVER` (the subdomain), `SCTO_USER`,
`SCTO_PASSWORD`, and `SCTO_KEY_PATH` for encrypted forms. Never commit `.env`, the
`.pem` key, `config.toml` or `data/`.

Outputs in `<out_dir>/<form_id>/`:

| File | Content |
|---|---|
| `<form>_questions.json` | One entry per question, keyed by its XLSForm name |
| `sections/` | One slice of `questions.json` per group level |
| `<form>.parquet` | Submissions, typed from the XML |
| `<form>_variable_dictionary.json` | One entry per column: stats, types, sentinels, `pii` |
| `<form>_variable_graph.json` | Questions linked by calculation, relevance, constraint, repeat |
| `<form>_pii_review.parquet` | `KEY` plus every column flagged `contains_pii` / `may_contain_pii` |

The `pii` block is a signal for the people cleaning the data, not enforcement:
`cleared_by_form` (publishable), `whitelisted`, `contains_pii`, `may_contain_pii`
or `no_hits`, with the basis for each. Review flagged columns in
`<form>_pii_review.parquet` and add the ones you clear to `[pii] whitelist`.

## Survey-expert MCP server

`uv run scto-mcp --config config.toml` serves one form's outputs over stdio to a local
agent: `lookup_variable(s)`, `search_questions`, `get_choice_list`, `get_gate_chain`,
`get_survey_info`, `get_variable_neighborhood`, `get_repeat_structure`. It reads only
the JSON outputs, never the parquet, and shows each column's `pii` status.

Design: `surveycto-api-extractor-PROMPT.md`; API notes: `docs/`.
