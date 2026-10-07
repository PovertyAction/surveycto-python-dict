# Task runner (https://just.systems). Run `just` to list recipes.
# Every recipe takes the config file as an optional argument: `just build data/config.toml`.

set windows-shell := ["powershell.exe", "-NoLogo", "-Command"]

config := "config.toml"

[doc("List recipes (default)")]
default:
    @just --list

[doc("Install the environment, including the spaCy models")]
setup:
    uv sync

[doc("Download every form's XML and submissions (wide JSON) and every server dataset into <out_dir>")]
pull cfg=config:
    uv run scto-pull --config {{ cfg }}

[doc("Build each form's questions.json, sections, parquet, dictionary, graph, PII review and dataset codebook, then flow.json")]
build cfg=config:
    uv run scto-build --config {{ cfg }}

[doc("Every output from the raw pull: build, stats, codebook, duckdb. Rerun after editing [pii] lists")]
outputs cfg=config: (build cfg) (stats cfg) (codebook cfg) (duckdb cfg)

[doc("Pull, then every output")]
run cfg=config: (pull cfg) (outputs cfg)

[doc("Write <form>_summary_stats.csv: stats per column over the rows where it was asked, sentinels apart")]
stats cfg=config:
    uv run scto-stats --config {{ cfg }}

[doc("Write <form>_codebook.xlsx for PIs: a Variables sheet and a Summary stats sheet")]
codebook cfg=config:
    uv run scto-codebook --config {{ cfg }}

[doc("Write <form>.duckdb (submissions, repeats long, datasets, each with *_safe; descriptions as tables) and its map <form>_db.json / .sql")]
duckdb cfg=config: (stats cfg)
    uv run scto-duckdb --config {{ cfg }}

[doc("Serve a form's built outputs to a local agent over stdio (survey-expert MCP); `just mcp <cfg> --form <form>` when the config has several")]
mcp cfg=config *args:
    uv run scto-mcp --config {{ cfg }} {{ args }}

[doc("Ask about the form from the shell, e.g. `just survey var age`, `just survey db`; another form: `just config=data/x.toml survey info`")]
survey *args:
    uv run scto-survey --config {{ config }} {{ args }}

[doc("Run the offline test suite")]
test:
    uv run pytest -q

[doc("Lint and check formatting")]
lint:
    uv run ruff check src tests scripts
    uv run ruff format --check src tests scripts

[doc("Copy survey.py into the survey-expert skill as its standalone search_survey.py")]
sync-skill:
    uv run python -c "import shutil; shutil.copyfile('src/surveycto_python_dict/survey.py', '.claude/skills/survey-expert/search_survey.py')"
