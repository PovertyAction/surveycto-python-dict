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


def test_a_choice_filter_shows_with_each_choices_filter_values(store):
    crop = store.questions["crop"]
    crop["choices"] = [
        dict(c, filter_columns={"season": "dry"}) for c in crop["choices"]
    ]
    crop["choice_filter"] = {
        "expression": "season = 'dry'",
        "per_submission": False,
        "items": len(crop["choices"]) + 1,
    }
    passed = len(crop["choices"])
    line = (
        f"Choice filter: season = 'dry' (applied: {passed} of {passed + 1} "
        "choices pass)"
    )
    assert line in store.lookup_variable("crop")
    out = store.get_choice_list("crop").splitlines()
    assert out[1] == f"  {line}"
    assert out[2].endswith("  [season = dry]")
    # A filter that reads the form leaves each submission part of the list.
    crop["choice_filter"]["per_submission"] = True
    assert "(per submission: each sees the choices it passes)" in (
        store.get_choice_list("crop")
    )
    # A filter no choice passes still lists as a select.
    crop["choices"] = []
    assert store.get_choice_list("crop").splitlines()[0] == "crop (select_one):"


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
        "get_group",
        "get_choice_list",
        "get_gate_chain",
        "get_survey_info",
        "get_variable_neighborhood",
        "get_repeat_structure",
        "get_datasets",
        "get_flow",
        "get_project",
        "get_structure_db",
        "trace_lineage",
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
        choices=[{"value": "-88", "label": "Other"}],
        choice_columns={"value": "crop_id", "label": "crop_name"},
        choices_withheld=["presidio:PERSON"],
    )
    out = store.get_choice_list("crop")
    assert "  The form's own choices: -88 = Other" in out
    assert "choices not listed (presidio:PERSON)" in out
    assert "value column `crop_id`, label column `crop_name`" in out
    assert "crop_id = crop_name" not in out


def test_withheld_choices_say_how_to_decode_them_and_what_was_picked(
    store, monkeypatch
):
    store.questions["crop"] = dict(
        store.questions["crop"],
        choices=[],
        choice_columns={"value": "crop_id", "label": "crop_name"},
        choices_withheld=["presidio:PERSON"],
        data_source={
            "kind": "search",
            "dataset": "crops",
            "match": [{"column": "zone", "value": "${area}"}],
        },
    )
    monkeypatch.setattr(store, "read", lambda dataset, path: ({"id": "crops"}, {}))
    store.dataset_columns = [
        {"dataset": "crops", "column": "crop_id", "pii": {"status": "whitelisted"}},
        {
            "dataset": "crops",
            "column": "crop_name",
            "pii": {"status": "may_contain_pii"},
        },
    ]
    store.summary["withheld_choices"] = {
        "crop": {"n_codes_picked": 3, "codes_not_in_list": ["9"]}
    }
    lines = store.get_choice_list("crop").splitlines()
    assert lines[1:] == [
        "  The rest: in out.duckdb dataset.crops; value column crop_id (whitelisted), "
        "label column crop_name (may_contain_pii)",
        '  A script decodes them: SELECT crop_id, crop_name FROM dataset."crops" '
        "WHERE zone = <the submission's ${area}>",
        "  Every choice, with its filter values: SELECT value, label, filter_values FROM "
        "farm.choices WHERE question = 'crop'",
        "  In the data: 3 codes picked; 1 not in the list: 9",
    ]
    # A value column Presidio found nothing in shows its codes too.
    store.dataset_columns[0]["pii"]["status"] = "no_hits"
    assert store.get_choice_list("crop").splitlines()[-1] == (
        "  In the data: 3 codes picked; 1 not in the list: 9"
    )
    # Codes a flagged value column holds are counted, not shown.
    store.dataset_columns[0]["pii"]["status"] = "may_contain_pii"
    assert store.get_choice_list("crop").splitlines()[-1] == (
        "  In the data: 3 codes picked; 1 not in the list (codes not shown: crop_id is "
        "may_contain_pii)"
    )
    store.summary["withheld_choices"] = {}
    assert store.get_choice_list("crop").splitlines()[-1] == (
        "  In the data: no submission picked a code"
    )


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
    assert "    prev_round.phone (unique: one dataset row per match) =" in out
    assert "      ${phone} (in submissions): prev <- owner" in out
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


def test_a_long_choice_list_is_cut_or_given_by_its_pattern(store, monkeypatch):
    from surveycto_python_dict import survey

    monkeypatch.setattr(survey, "_LISTED", 2)
    q = store.questions["crop"]
    q["choices"] = [{"value": str(n), "label": f"${{combo{n}}}"} for n in (1, 2, 3)]
    assert (
        "Choices: 3, 1 ... 3, each labelled ${combo<n>} (<n>: its value); "
        "get_choice_list('crop') lists each"
    ) in store.lookup_variable("crop")
    q["choices"] = [{"value": v, "label": v.upper()} for v in ("a", "b", "c")]
    assert (
        "Choices: a = A; b = B; ... 1 more; get_choice_list('crop') lists each"
        in store.lookup_variable("crop")
    )


def test_several_lookups_summarise_each_question_columns(store):
    out = store.lookup_variables(["plot_crop", "plot_size"])
    assert "Columns: 4, plot_crop_1_1 ... " in out
    assert "plot_crop_2_2  " not in out
    assert "plot_crop_2_2  " in store.lookup_variable("plot_crop")


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


def test_repeat_structure_says_what_sets_its_instances(store):
    store.questions["plot_size"]["calculation"] = "concat('plot-', index())"
    out = store.get_repeat_structure("plot")
    assert "  count reads plots (integer) Plots" in out
    assert "  set per instance (calculates that read index()):" in out
    assert "    plot_size = concat('plot-', index())" in out


def test_a_default_is_told_apart_from_a_calculation(store):
    q = store.questions["plot_size"]
    q.update(calculation="concat('plot-', index())", calculation_is_default=True)
    assert (
        "Default (computed; the user can change it): concat('plot-', index())"
        in store.lookup_variable("plot_size")
    )
    out = store.get_repeat_structure("plot")
    assert "  set per instance (defaults that read index(); the user can" in out
    assert "calculates that read index()" not in out


def test_lookup_shows_appearance_and_no_formula_as_text(store):
    store.questions["area"]["appearance"] = "month-year"
    assert "Appearance: month-year" in store.lookup_variable("area")
    q = store.questions["plot_size"]
    q.update(type="calculate", calculation="1 + 1", question_text="1 + 1")
    out = store.lookup_variable("plot_size")
    assert "Text:" not in out
    assert "Calculation: 1 + 1" in out


def test_reached_choices_count_only_filtered_lists(store):
    store.questions["crop"]["choices"] = [
        {"value": "1", "label": "A", "when": {"list": "x"}},
        {"value": "2", "label": "B", "when": {"list": "y"}},
        {"value": "-66", "label": "Other"},
    ]
    assert (
        "Choices: 3 in 2 lists, by the dataset rows the submissions reach "
        "(list = x; list = y), plus 1 in every list (-66); get_choice_list shows them"
    ) in store.lookup_variable("crop")


def test_project_commands_need_no_form(store, capsys):
    from surveycto_python_dict.survey import main

    config = store.form_dir.parent.parent / "two.toml"
    config.write_text('forms = ["farm", "visit"]\nout_dir = "out"\n', encoding="utf-8")
    main(["--config", str(config), "flow"])
    assert "Stage 1:\n  farm" in capsys.readouterr().out


def test_survey_info_lists_questions_with_no_column(tmp_path, analyzer):
    from farm_form import ROWS

    config = tmp_path / "config.toml"
    config.write_text('forms = ["farm"]\nout_dir = "out"\n', encoding="utf-8")
    cfg = load_config(config)
    # Nobody answered `visit`: the export has no column for it.
    write_pull(cfg, rows=[{k: v for k, v in r.items() if k != "visit"} for r in ROWS])
    build(cfg, SENTINELS, analyzer)
    store = SurveyStore(cfg.form_dir("farm"), "farm")
    assert store.summary["questions_without_columns"] == ["visit"]
    assert (
        "Columns: none in the data; reached in 2 of 2 submissions, never answered"
        in store.lookup_variable("visit")
    )
    assert "no submission holds a value: 1 (visit)" in store.get_survey_info()


def test_group_lists_its_questions_in_order(store):
    out = store.get_group("plot")
    assert out.splitlines()[0] == "plot: 2 questions, asked if ${plots} > 0"
    assert out.index("plot_crop") < out.index("plot_size")
    # A subgroup's condition shows once, where its questions start.
    size = store.questions["plot_size"]
    size["group_path"] = [*size["group_path"], "measured"]
    size["gates"] = [
        *size["gates"],
        {"group": "measured", "relevance": "${plot_crop} != ''", "repeat_depth": 1},
    ]
    lines = store.get_group("plot").splitlines()
    assert lines[2:4] == [
        "  -- measured: if ${plot_crop} != ''",
        "  plot_size (decimal) [measured]",
    ]
    # A nested group says which groups it sits in and when they are asked.
    assert store.get_group("measured").splitlines()[:2] == [
        "measured: 1 questions, asked if ${plot_crop} != ''",
        "  inside plot, asked if ${plots} > 0",
    ]
    assert store.get_group("plt").startswith("No group named 'plt'. Close: plot")


def test_search_finds_an_exact_name_first_and_across_forms(store):
    from surveycto_python_dict.survey import search_forms

    assert store.search_questions("per_plot").splitlines()[0].startswith("per_plot")
    out = search_forms({"farm": store}, "plot size")
    assert out.splitlines()[0].startswith("farm.plot_size")
    assert search_forms({"farm": store}, "zzz") == "No question of farm matches 'zzz'."


def test_datasets_summarise_columns_and_list_them_on_request(store, monkeypatch):
    from surveycto_python_dict import survey

    monkeypatch.setattr(survey, "_LISTED", 1)
    out = store.get_datasets()
    assert (
        "  Columns (2 in 2 stems; get_datasets('farm', 'prev_round', columns=True)"
        in out
    )
    assert "  Columns:" in store.get_datasets("prev_round", columns=True)


def test_search_matches_name_parts_and_word_starts_and_drops_one_letters(store):
    from surveycto_python_dict.survey import _name_tokens

    assert _name_tokens("refugeePop_v2") == ["refugee", "pop", "v2", "refugeepop_v2"]
    store.questions["per_plot"]["question_text"] = "Area per plot, in hectares"
    # "hectare" starts "hectares"; "a" alone of one letter is dropped.
    assert store.search_questions("a hectare").splitlines()[0].startswith("per_plot")


def test_search_weighs_rare_words_and_finds_choice_labels(store):
    names = [n for n in store.questions if n != "per_plot"][:3]
    for n in names:
        store.questions[n]["question_text"] = f"Study note {n}"
    store.questions["per_plot"].update(
        type="calculate", calculation="${plots}", question_text="Informal sector"
    )
    # "sector" is rarer than "study", so the one calculate holding it comes first.
    assert store.search_questions("study sector").splitlines()[0].startswith("per_plot")
    assert store.search_questions("maize").splitlines()[0].startswith("crop")


def test_search_across_forms_weighs_words_over_every_form(store):
    import copy

    from surveycto_python_dict.survey import search_forms

    # "study" is rare in one form and common in the other; "sector" is rarer overall.
    first = next(iter(store.questions))
    store.questions[first]["question_text"] = "Study design"
    other = copy.copy(store)
    other.questions = copy.deepcopy(store.questions)
    names = list(other.questions)
    for n in names[:6]:
        other.questions[n]["question_text"] = f"Study note {n}"
    other.questions[names[6]]["question_text"] = "Informal sector"
    out = search_forms({"farm": store, "other": other}, "study sector")
    assert out.splitlines()[0].startswith(f"other.{names[6]}")


def test_search_skips_formulas_and_repeat_count_text_and_weighs_hints_half(store):
    q = store.questions["per_plot"]
    q.update(type="calculate", calculation="${area} * 2", question_text="${area} * 2")
    assert "per_plot" not in store.search_questions("area")
    assert "plot_count" not in store.search_questions("iterations group")
    store.questions["owner"]["hint"] = "The crop owner"
    hits = [line.split()[0] for line in store.search_questions("crop").splitlines()]
    assert hits[-1] == "owner"
    assert {"crop", "plot_crop"} <= set(hits[:-1])


def test_search_ranks_answered_questions_first_and_names_unmatched_words(store):
    from surveycto_python_dict.survey import search_forms

    lines = store.search_questions("plot zzz").splitlines()
    assert lines[0] == "No question holds: zzz; ranked on the rest."
    types = [store.questions[line.split()[0]]["type"] for line in lines[1:]]
    computed = [t in ("calculate", "repeat_count") for t in types]
    # per_plot (a calculate) and plot_count follow every question someone answers.
    assert any(computed) and computed == sorted(computed)
    assert search_forms({"farm": store}, "plot zzz").splitlines()[0] == lines[0]


def test_search_tags_flagged_columns(store):
    assert "[may_contain_pii]" in store.search_line("owner") or "[contains_pii]" in (
        store.search_line("owner")
    )
    assert "[" not in store.search_line("plots")


def test_neighborhood_collapses_a_group_gate(store):
    out = store.get_variable_neighborhood("plots")
    assert "plots gates group plot (2 questions)" in out
    assert "(group_gated_by)" not in out


def test_survey_shows_values_as_pii_does():
    """survey.py keeps its own copy of the flagged statuses: it ships standalone."""
    from surveycto_python_dict import pii, survey

    assert survey._FLAGGED == pii.FLAGGED
    statuses = [*pii.FLAGGED, "no_hits", "cleared_by_form", "whitelisted"]
    assert [survey._shows_values(s) for s in statuses] == [
        pii.shows_values(s) for s in statuses
    ]
