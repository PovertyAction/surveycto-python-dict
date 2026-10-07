"""Stage 7: the survey store behind the MCP server, and the server's tool list."""

import asyncio

import pytest
from farm_form import SENTINELS, write_pull

from surveycto_python_dict.cli import build, load_config
from surveycto_python_dict.survey import SurveyStore


@pytest.fixture
def store(tmp_path, analyzer):
    config = tmp_path / "config.toml"
    config.write_text('forms = ["farm"]\nout_dir = "out"\n', encoding="utf-8")
    cfg = load_config(config)
    write_pull(
        cfg, "phone,owner\n3000000000,Fictional Owner\n3000000001,Fictional Owner\n"
    )
    build(cfg, SENTINELS, analyzer)
    return SurveyStore(cfg.form_dir("farm"), "farm")


def test_lookup_question_shows_text_logic_columns_and_pii(store):
    out = store.lookup_variable("area")
    assert "area (decimal)" in out
    assert "Area" in out
    assert "Relevance: ${plots} > 0" in out
    assert "Constraint: . >= 0" in out
    assert "whitelisted (type:decimal)" in out


def test_lookup_flagged_variable_warns(store):
    out = store.lookup_variable("owner")
    assert "may_contain_pii (type:text, presidio:PERSON)" in out
    assert "farm_pii_review.xlsx" in out


def test_lookup_by_column_resolves_its_question(store):
    out = store.lookup_variable("plot_crop_2_1")
    assert "plot_crop (select_multiple)" in out
    assert "plot_crop_2_1" in out


def test_lookup_unknown_suggests(store):
    out = store.lookup_variable("ownr")
    assert "No variable or question named 'ownr'" in out
    assert "owner" in out


def test_lookup_never_prints_flagged_values(store):
    out = store.lookup_variables(["owner", "phone", "tag", "prev"])
    for value in ("Ana Maria Gomez Restrepo", "3104567890", "Jorge Perez"):
        assert value not in out


def test_lookup_variables_batches(store):
    out = store.lookup_variables(["area", "plots"])
    assert "area (decimal)" in out and "plots (integer)" in out


def test_search_ranks_by_name_and_text(store):
    out = store.search_questions("plot size")
    assert out.splitlines()[0].startswith("plot_size")


def test_choice_list(store):
    out = store.get_choice_list("crop")
    assert "01 = Maize" in out and "02 = Rice" in out
    assert "not a select" in store.get_choice_list("owner")


def test_gate_chain_includes_group_and_upstream_gates(store):
    out = store.get_gate_chain("plot_size")
    assert "${plots} > 0" in out
    assert "plots" in out


def test_neighborhood(store):
    out = store.get_variable_neighborhood("per_plot")
    assert "area -> per_plot (calculates_from)" in out
    assert "plots -> per_plot (calculates_from)" in out


def test_repeat_structure(store):
    out = store.get_repeat_structure()
    assert "plot" in out and "count: ${plots}" in out
    assert "plot_crop, plot_size" in out


def test_survey_info(store):
    out = store.get_survey_info()
    assert "farm" in out
    assert "submissions: 2" in out
    assert "may_contain_pii" in out


def test_server_registers_the_tools(store):
    from surveycto_python_dict.mcp_server import make_server

    server = make_server({"farm": store})
    names = {t.name for t in asyncio.run(server.list_tools())}
    assert names == {
        "lookup_variable",
        "lookup_variables",
        "search_questions",
        "get_choice_list",
        "get_gate_chain",
        "get_survey_info",
        "get_variable_neighborhood",
        "get_repeat_structure",
        "get_datasets",
        "get_flow",
        "get_project",
        "get_structure_db",
    }
    assert "pii.status" in server.instructions


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["var", "area"], "area (decimal)"),
        (["var", "area", "plots"], "plots (integer)"),
        (["search", "plot", "size"], "plot_size"),
        (["choices", "crop"], "01 = Maize"),
        (["gates", "plot_size"], "${plots} > 0"),
        (["neighborhood", "per_plot", "--depth", "2"], "calculates_from"),
        (["repeats"], "count: ${plots}"),
        (["info"], "submissions: 2"),
    ],
)
def test_survey_cli(store, capsys, argv, expected):
    from surveycto_python_dict.survey import main

    main(argv, store=store)
    assert expected in capsys.readouterr().out


def test_survey_cli_from_a_directory_detects_the_form(store, capsys):
    from surveycto_python_dict.survey import main

    main(["--dir", str(store.form_dir), "info"])
    assert "submissions: 2" in capsys.readouterr().out


def test_survey_cli_flow_reads_the_project_flow(store, capsys):
    from surveycto_python_dict.survey import main

    main(["--dir", str(store.form_dir), "flow"])
    out = capsys.readouterr().out
    assert "Stage 1:\n  farm" in out
    assert "farm/prev_round.csv: 2 rows, key -; attached file; read by farm" in out


def test_survey_cli_config_with_several_forms_needs_form(tmp_path):
    from surveycto_python_dict.survey import main

    config = tmp_path / "config.toml"
    config.write_text('forms = ["farm", "visit"]\nout_dir = "out"\n', encoding="utf-8")
    with pytest.raises(ValueError, match="--form"):
        main(["--config", str(config), "info"])
    with pytest.raises(ValueError, match="nope"):
        main(["--config", str(config), "--form", "nope", "info"])


def test_survey_cli_directory_without_outputs_raises(tmp_path):
    from surveycto_python_dict.survey import main

    with pytest.raises(FileNotFoundError, match="_questions.json"):
        main(["--dir", str(tmp_path), "info"])


def test_skill_script_is_the_survey_module():
    """The skill ships survey.py as a standalone script; `just sync-skill` refreshes it."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    module = root / "src" / "surveycto_python_dict" / "survey.py"
    script = root / ".claude" / "skills" / "survey-expert" / "search_survey.py"
    assert script.read_bytes() == module.read_bytes()


def test_survey_module_imports_only_the_standard_library():
    import ast
    import sys
    from pathlib import Path

    source = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "surveycto_python_dict"
        / "survey.py"
    )
    tree = ast.parse(source.read_text(encoding="utf-8"))
    imported = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        node.module.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }
    assert imported <= sys.stdlib_module_names


def test_lookup_shows_a_binary_column_its_own_values(store):
    out = store.lookup_variable("tools")
    line = next(r for r in out.splitlines() if r.strip().startswith("tools_77"))
    assert "1 = Other, 0 = not chosen" in line


def test_lookup_suggestions_are_listed_once(store):
    out = store.lookup_variable("ownerr")
    assert (
        out.count("owner") == 2
    )  # once in the message's suggestions, once in "ownerr"


def test_tokens_keep_accented_words_and_drop_html():
    from surveycto_python_dict.survey import _tokens

    assert _tokens("Nivel de café <b>tostado</b>") == ["nivel", "de", "café", "tostado"]


def test_search_shows_short_plain_text(store):
    from surveycto_python_dict.survey import _short

    assert _short("<p>" + "word " * 50 + "</p>").endswith("...")
    assert len(_short("word " * 50)) == 100


def test_withheld_choices_name_the_dataset_columns_not_choices(store):
    store.questions["crop"] = dict(
        store.questions["crop"],
        appearance="search('crops')",
        choices=[{"value": "crop_id", "label": "crop_name"}],
        choices_withheld=["presidio:PERSON"],
    )
    out = store.get_choice_list("crop")
    assert "choices withheld (presidio:PERSON)" in out
    assert "value column `crop_id`, label column `crop_name`" in out
    assert "crop_id = crop_name" not in out


def test_lookup_shows_where_a_value_comes_from(store):
    out = store.lookup_variable("prev")
    assert (
        "Data source: pulldata: prev_round.owner where prev_round.phone = ${phone}"
        in out
    )


def test_datasets_describe_the_join_to_the_form(store):
    out = store.get_datasets()
    assert out.startswith("prev_round: 2 rows, 2 columns")
    assert "In out.duckdb: dataset.farm/prev_round.csv (every column, plus row)" in out
    assert (
        "prev_round.phone = ${phone} (in submissions); "
        "unique in the dataset: one dataset row per match"
    ) in out
    assert "prev reads owner" in out
    assert "owner  non-null 2/2, distinct 1" in out
    assert "Fictional Owner" not in out


def test_many_columns_are_summarised_and_listed_on_request(store, monkeypatch):
    from surveycto_python_dict import survey

    monkeypatch.setattr(survey, "_LISTED", 1)
    out = store.lookup_variable("plot_crop")
    assert "Columns: 4, plot_crop_1_1 ... " in out
    assert "lookup_variable('plot_crop', columns=True)" in out
    assert "plot_crop_2_2  " not in out
    assert "plot_crop_2_2" in store.lookup_variable("plot_crop", columns=True)


def test_neighborhood_names_the_repeat_instead_of_its_members(store):
    out = store.get_variable_neighborhood("plot_size")
    assert out.startswith("plot_size is in repeat plot with 2 questions")
    assert "repeat_sibling" not in out


def test_repeat_structure_lists_members_and_drills_into_one(store, monkeypatch):
    from surveycto_python_dict import survey

    monkeypatch.setattr(survey, "_LISTED", 1)
    out = store.get_repeat_structure()
    assert "members (2): plot_crop, ... 1 more; get_repeat_structure('plot')" in out
    assert "plot_size" in store.get_repeat_structure("plot")


def test_project_commands_need_no_form(store, capsys):
    from surveycto_python_dict.survey import main

    config = store.form_dir.parent.parent / "two.toml"
    config.write_text('forms = ["farm", "visit"]\nout_dir = "out"\n', encoding="utf-8")
    main(["--config", str(config), "flow"])
    assert "Stage 1:\n  farm" in capsys.readouterr().out
