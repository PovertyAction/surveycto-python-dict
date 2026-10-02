# surveycto-python-dict

Pulls a SurveyCTO form's compiled XML and wide JSON over the API and builds, from them,
`questions.json`, sections, typed parquet, a variable dictionary and graph, and a
per-variable PII signal.

## Use

```sh
just setup                           # uv sync, including the spaCy models
cp config.example.toml config.toml   # set form_id
just pull                            # form XML + submissions (wide JSON) -> <out_dir>/<form_id>/raw
just build                           # dictionaries, parquet, graph, PII review; rerun any time
just stats                           # <form>_summary_stats.csv over the rows where each question was asked
just codebook                        # <form>_codebook.xlsx for PIs: Variables + Summary stats sheets
just mcp                             # survey-expert MCP server over stdio
```

Each recipe takes another config as an argument (`just build path/to/config.toml`);
`just` lists them all.

Copy `.env.example` to `.env` and fill it in: `SCTO_SERVER` (the subdomain),
`SCTO_USER`, `SCTO_PASSWORD`, `SCTO_KEY_PATH` for encrypted forms, and
`SCTO_SENTINELS`, the study's non-response codes (`"-99=Don't know;-88=Refused;..."`),
which `just build` and `just stats` require. Never commit `.env`, the `.pem` key,
`config.toml` or `data/`.

Outputs in `<out_dir>/<form_id>/`:

| File | Content |
|---|---|
| `<form>_questions.json` | One entry per question, keyed by its XLSForm name |
| `sections/` | One slice of `questions.json` per group level |
| `<form>.parquet` | Submissions, typed from the XML |
| `<form>_variable_dictionary.json` | One entry per column: stats, types, sentinels, `pii` |
| `<form>_variable_graph.json` | Questions linked by calculation, relevance, constraint, repeat |
| `<form>_pii_review.parquet` | `KEY` plus every column flagged `contains_pii` / `may_contain_pii` |
| `<form>_summary_stats.csv` | `just stats`: per column, counts over the rows where it was asked (skip logic evaluated per row), sentinels apart, then mean/quartiles, frequencies or shares; flagged PII columns get counts only |
| `<form>_codebook.xlsx` | `just codebook`: for PIs. Variables (label, type, group, relevance, value labels, missing, sentinels, PII status) and Summary stats; no values of flagged PII columns |

The `pii` block is a signal for the people cleaning the data, not enforcement:
`cleared_by_form` (publishable), `whitelisted`, `contains_pii`, `may_contain_pii`
or `no_hits`, with the basis for each. Review flagged columns in
`<form>_pii_review.parquet` and add the ones you clear to `[pii] whitelist`.

## Survey-expert MCP server and skill

`just mcp` (`uv run scto-mcp --config config.toml`) serves one form's outputs over stdio to a local
agent: `lookup_variable(s)`, `search_questions`, `get_choice_list`, `get_gate_chain`,
`get_survey_info`, `get_variable_neighborhood`, `get_repeat_structure`. It reads only
the JSON outputs, never the parquet, and shows each column's `pii` status.

To connect it to Claude Code, copy `.mcp.json.example` to `.mcp.json` (git-ignored) and
point `--config` at your config. The `survey-expert` skill
(`.claude/skills/survey-expert/SKILL.md`) teaches an agent to read the outputs and to
check `pii.status` first; without MCP it uses the same answers from
`uv run scto-survey <var|search|choices|gates|neighborhood|repeats|info>`.

Design: `surveycto-api-extractor-PROMPT.md`; API notes: `docs/`.
