# Task runner (https://just.systems). Run `just` to list recipes.
# Every recipe takes the config file as an optional argument: `just build data/config.toml`.

set windows-shell := ["powershell.exe", "-NoLogo", "-Command"]

config := "config.toml"

[doc("List recipes (default)")]
default:
    @just --list

[doc("Install the environment, including the spaCy models, and the pre-commit hooks")]
setup:
    uv sync
    uv run pre-commit install

[doc("Run the pre-commit hooks (ruff, codespell, markdownlint, ...) on every file")]
lint:
    uv run pre-commit run --all-files

[doc("Download every form's XML and submissions (wide JSON) and every server dataset into <out_dir>")]
pull cfg=config:
    uv run scto-pull --config {{ cfg }}

[doc("Rewrite flow.json and flow.md alone, e.g. after editing [project] or [entities]")]
flow cfg=config:
    uv run scto-flow --config {{ cfg }}

[doc("Build each form's questions.json, sections, parquet, dictionary, graph, PII review and dataset codebook, then flow.json")]
build cfg=config:
    uv run scto-build --config {{ cfg }}

[doc("Every output from the raw pull: build, the stats, then codebook and duckdb at once. Rerun after editing [pii] lists")]
outputs cfg=config: (build cfg)
    uv run scto-outputs --config {{ cfg }}

[doc("Pull, then every output")]
run cfg=config: (pull cfg) (outputs cfg)

[doc("Write <form>_summary_stats.csv: stats per column over the rows where it was asked, sentinels apart")]
stats cfg=config:
    uv run scto-stats --config {{ cfg }}

[doc("Write <form>_codebook.xlsx for PIs (Variables and Summary stats sheets) and <form>_summary_stats.csv from the same stats")]
codebook cfg=config:
    uv run scto-codebook --config {{ cfg }}

[doc("Write <project>.duckdb (a schema per form with submissions, repeats long and descriptions; datasets once; project tables) and its map <project>_db.json / .sql")]
duckdb cfg=config:
    uv run scto-duckdb --config {{ cfg }}

[doc("Serve the project's built outputs to a local agent over stdio (survey-expert MCP)")]
mcp cfg=config:
    uv run scto-mcp --config {{ cfg }}

[doc("Ask about the project from the shell, e.g. `just survey var age`, `just survey db`; a form: `just survey --form <id> info`; another project: `just config=data/x.toml survey info`")]
survey *args:
    uv run scto-survey --config {{ config }} {{ args }}

[doc("Run the offline test suite")]
test:
    uv run pytest -q

[doc("Copy survey.py into the survey-expert skill as its standalone search_survey.py")]
sync-skill:
    uv run python -c "import shutil; shutil.copyfile('src/surveycto_python_dict/survey.py', '.claude/skills/survey-expert/search_survey.py')"
