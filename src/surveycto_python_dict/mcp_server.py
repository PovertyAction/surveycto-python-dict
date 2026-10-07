"""Local survey-expert MCP server over a project's built outputs (``scto-mcp``)."""

import argparse
from pathlib import Path

from mcp.server.mcpserver import MCPServer

from surveycto_python_dict.cli import load_config
from surveycto_python_dict.survey import SurveyStore

_INSTRUCTIONS = (
    "Answers questions about one SurveyCTO project: its forms ({forms}), how they chain "
    "through datasets (get_flow), and each form's questions, skip logic, choices, "
    "columns and their relationships. Form tools take the form's id. Every column "
    "carries pii.status. Check pii.status before reading a variable's values: "
    "contains_pii and may_contain_pii columns hold, or may hold, personal data; their "
    "rows are listed in <form>_pii_review.xlsx for the people cleaning the data to "
    "verify."
)


def make_server(stores: dict[str, SurveyStore]) -> MCPServer:
    """Return an MCP server whose tools answer from each form's ``stores`` entry."""
    server = MCPServer(
        name="survey-expert",
        instructions=_INSTRUCTIONS.format(forms=", ".join(stores)),
    )

    def store(form: str) -> SurveyStore:
        if form not in stores:
            raise ValueError(f"{form!r} is not a form of the project: {list(stores)}")
        return stores[form]

    # Every store answers the project-wide tools from the same files.
    project = next(iter(stores.values()))

    @server.tool()
    def lookup_variable(form: str, name: str) -> str:
        """Describe a question or column: text, logic, choices, column stats and pii."""
        return store(form).lookup_variable(name)

    @server.tool()
    def lookup_variables(form: str, names: list[str]) -> str:
        """Describe several questions or columns of a form at once."""
        return store(form).lookup_variables(names)

    @server.tool()
    def search_questions(form: str, query: str, max_results: int = 20) -> str:
        """Find a form's questions by words in their name or text (TF-IDF ranked)."""
        return store(form).search_questions(query, max_results)

    @server.tool()
    def get_choice_list(form: str, name: str) -> str:
        """List a select question's choices (its choice list is named after it)."""
        return store(form).get_choice_list(name)

    @server.tool()
    def get_gate_chain(form: str, name: str) -> str:
        """Show the relevance conditions gating a question, and what gates those."""
        return store(form).get_gate_chain(name)

    @server.tool()
    def get_survey_info(form: str) -> str:
        """Summarise a form: submissions, columns, questions, pii counts."""
        return store(form).get_survey_info()

    @server.tool()
    def get_variable_neighborhood(form: str, name: str, depth: int = 1) -> str:
        """List calculation, gating, constraint and repeat links around a question."""
        return store(form).get_variable_neighborhood(name, depth)

    @server.tool()
    def get_repeat_structure(form: str) -> str:
        """Show a form's repeat groups, their nesting, counts and members."""
        return store(form).get_repeat_structure()

    @server.tool()
    def get_datasets(form: str) -> str:
        """Describe the datasets a form reads and how their rows join to the form."""
        return store(form).get_datasets()

    @server.tool()
    def get_flow(dataset: str | None = None) -> str:
        """Show how the project's forms chain through datasets: stages, publishers, readers.

        With ``dataset``, list its columns: the field each is published from, with its
        origin, and the fields that read it, across forms.
        """
        return project.get_flow(dataset)

    @server.tool()
    def get_structure_db(schema: str | None = None) -> str:
        """Map <project>.duckdb: a summary of its schemas, or one schema in full.

        ``schema`` is a form's id (its tables, the datasets it joins and its joins to
        other forms), ``dataset`` or ``main``.
        """
        return project.get_structure_db(schema)

    return server


def main(argv: list[str] | None = None) -> None:
    """Entry point for ``scto-mcp``: serve the project of ``--config`` over stdio."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=Path("config.toml"))
    cfg = load_config(parser.parse_args(argv).config)
    make_server(
        {form: SurveyStore(cfg.form_dir(form), form) for form in cfg.forms}
    ).run()
