"""Local survey-expert MCP server over one form's built outputs (``scto-mcp``)."""

import argparse
from pathlib import Path

from mcp.server.mcpserver import MCPServer

from surveycto_python_dict.cli import load_config
from surveycto_python_dict.survey import SurveyStore

_INSTRUCTIONS = (
    "Answers questions about one SurveyCTO form: its questions, skip logic, choices, "
    "columns and their relationships. Every column carries pii.status. Check "
    "pii.status before reading a variable's values: contains_pii and may_contain_pii "
    "columns hold, or may hold, personal data; their rows are listed in {review} for "
    "the people cleaning the data to verify."
)


def make_server(store: SurveyStore) -> MCPServer:
    """Return an MCP server whose tools answer from ``store``."""
    server = MCPServer(
        name="survey-expert",
        instructions=_INSTRUCTIONS.format(review=store.review_file),
    )

    @server.tool()
    def lookup_variable(name: str) -> str:
        """Describe a question or column: text, logic, choices, column stats and pii."""
        return store.lookup_variable(name)

    @server.tool()
    def lookup_variables(names: list[str]) -> str:
        """Describe several questions or columns at once."""
        return store.lookup_variables(names)

    @server.tool()
    def search_questions(query: str, max_results: int = 20) -> str:
        """Find questions by words in their name or text (TF-IDF ranked)."""
        return store.search_questions(query, max_results)

    @server.tool()
    def get_choice_list(name: str) -> str:
        """List a select question's choices (its choice list is named after it)."""
        return store.get_choice_list(name)

    @server.tool()
    def get_gate_chain(name: str) -> str:
        """Show the relevance conditions gating a question, and what gates those."""
        return store.get_gate_chain(name)

    @server.tool()
    def get_survey_info() -> str:
        """Summarise the form: submissions, columns, questions, pii counts."""
        return store.get_survey_info()

    @server.tool()
    def get_variable_neighborhood(name: str, depth: int = 1) -> str:
        """List calculation, gating, constraint and repeat links around a question."""
        return store.get_variable_neighborhood(name, depth)

    @server.tool()
    def get_repeat_structure() -> str:
        """Show the repeat groups, their nesting, counts and members."""
        return store.get_repeat_structure()

    @server.tool()
    def get_structure_db() -> str:
        """Map <form>.duckdb: each table and view, what one row is, its key and joins."""
        return store.get_structure_db()

    @server.tool()
    def get_datasets() -> str:
        """Describe the datasets the form reads and how their rows join to the form."""
        return store.get_datasets()

    return server


def main(argv: list[str] | None = None) -> None:
    """Entry point for ``scto-mcp``: serve the form named in ``--config`` over stdio."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=Path("config.toml"))
    cfg = load_config(parser.parse_args(argv).config)
    make_server(SurveyStore(cfg.form_dir, cfg.form_id)).run()
