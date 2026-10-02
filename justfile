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

[doc("Download the form XML and the submissions (wide JSON) into <out_dir>/<form_id>/raw")]
pull cfg=config:
    uv run scto-pull --config {{ cfg }}

[doc("Build questions.json, sections, parquet, dictionary, graph and PII review from the raw pull; safe to rerun")]
build cfg=config:
    uv run scto-build --config {{ cfg }}

[doc("Pull, then build")]
run cfg=config: (pull cfg) (build cfg)

[doc("Serve the built outputs to a local agent over stdio (survey-expert MCP)")]
mcp cfg=config:
    uv run scto-mcp --config {{ cfg }}

[doc("Query the built outputs from the shell, e.g. `just survey var age`")]
survey *args:
    uv run scto-survey --config {{ config }} {{ args }}

[doc("Run the offline test suite")]
test:
    uv run pytest -q

[doc("Lint and check formatting")]
lint:
    uv run ruff check src tests scripts
    uv run ruff format --check src tests scripts
