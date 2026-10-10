"""Local survey-expert MCP server over a project's built outputs (``scto-mcp``)."""

import argparse
from pathlib import Path

from mcp.server.mcpserver import MCPServer

from surveycto_python_dict.cli import load_config
from surveycto_python_dict.survey import SurveyStore, search_forms, trace_lineage

_INSTRUCTIONS = (
    "Answers questions about one SurveyCTO project: what it is about and its entities "
    "(get_project, start there), its forms ({forms}), how they chain through datasets "
    "(get_flow), and each form's questions, skip logic, choices, "
    "columns and their relationships. Form tools take the form's id. Every column "
    "carries pii.status: contains_pii and may_contain_pii columns hold, or may hold, "
    "personal data. Use them like any column in cleaning scripts (join, clean, "
    "deduplicate, keep); handle them carefully by never printing or quoting their "
    "values, only counts and KEYs. The people cleaning the data verify the flags in "
    "<form>_pii_review.xlsx."
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
    def lookup_variable(form: str, name: str, columns: bool = False) -> str:
        """Describe a question or column: text, logic, choices, column stats and pii.

        More than 20 columns (a repeat, a long choice list) are summarised; ``columns``
        lists each.
        """
        return store(form).lookup_variable(name, columns)

    @server.tool()
    def lookup_variables(form: str, names: list[str], columns: bool = False) -> str:
        """Describe several questions or columns of a form at once, each question's
        columns summarised; ``columns`` lists each.
        """
        return store(form).lookup_variables(names, columns)

    @server.tool()
    def search_questions(
        query: str, form: str | None = None, max_results: int = 20
    ) -> str:
        """Find questions by words in their name or text (TF-IDF ranked), or by their
        exact name; in every form, or in ``form``.
        """
        if form is None:
            return search_forms(stores, query, max_results)
        return store(form).search_questions(query, max_results)

    @server.tool()
    def get_group(form: str, group: str) -> str:
        """List a group's questions in form order, with their type and condition."""
        return store(form).get_group(group)

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
        """List calculation, gating and constraint links around a question, and the
        repeat it is in.
        """
        return store(form).get_variable_neighborhood(name, depth)

    @server.tool()
    def get_repeat_structure(form: str, repeat: str | None = None) -> str:
        """Show a form's repeat groups, their nesting, counts and first members; with
        ``repeat``, that repeat's members in full.
        """
        return store(form).get_repeat_structure(repeat)

    @server.tool(name="trace_lineage")
    def lineage(form: str, name: str) -> str:
        """Trace where a field's value comes from, hop by hop across forms: a
        calculate to the fields it reads, a pulldata() or search() to the field
        another form publishes the dataset column from, back to the questions
        someone answered.
        """
        return trace_lineage(stores, form, name)

    @server.tool()
    def get_datasets(
        form: str, dataset: str | None = None, columns: bool = False
    ) -> str:
        """Describe the datasets a form reads and how their rows join to the form.

        Long lists are summarised: ``dataset`` gives one dataset's joins in full,
        ``columns`` lists each column.
        """
        return store(form).get_datasets(dataset, columns)

    @server.tool()
    def get_project() -> str:
        """Say what the project is about: the user's description, the entities they
        named (each with its key column and where it appears), and candidate keys.
        """
        return project.get_project()

    @server.tool()
    def get_flow(dataset: str | None = None, columns: bool = False) -> str:
        """Show how the project's forms chain through datasets: stages, publishers, readers.

        With ``dataset``, list its columns: the field each is published from, with its
        origin, and the fields that read it, across forms. A wide dataset is
        summarised one line per field; ``columns`` lists each column.
        """
        return project.get_flow(dataset, columns)

    @server.tool()
    def get_structure_db(schema: str | None = None, table: str | None = None) -> str:
        """Map <project>.duckdb: a summary of its schemas, one schema, or one table.

        ``schema`` is a form's id (its tables, the datasets it joins and its joins to
        other forms), ``dataset`` or ``main``; ``table`` lists one table's columns.
        """
        return project.get_structure_db(schema, table)

    return server


def main(argv: list[str] | None = None) -> None:
    """Entry point for ``scto-mcp``: serve the project of ``--config`` over stdio."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=Path("config.toml"))
    cfg = load_config(parser.parse_args(argv).config)
    make_server(
        {form: SurveyStore(cfg.form_dir(form), form) for form in cfg.forms}
    ).run()
