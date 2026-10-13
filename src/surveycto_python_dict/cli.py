"""``scto-pull``, ``scto-build`` and ``scto-run``.

Configuration is a TOML file (``--config``, default ``config.toml``)::

    forms = ["listing", "baseline"]
    server_datasets = ["households"]  # pulled besides the ones the forms read
    out_dir = "data/out/my_project"  # relative to the config file

    [pii]  # every key optional; see PiiConfig
    whitelist = ["village_name"]
    blacklist = ["respondent_id"]

Credentials and the study's sentinel codes come from the environment or ``.env``:
``SCTO_SERVER``, ``SCTO_USER``, ``SCTO_PASSWORD``, ``SCTO_KEY_PATH`` for encrypted forms,
and ``SCTO_SENTINELS`` (``"-99=Don't know;-88=Refused"``) for building.

Layout under ``out_dir``: ``raw/`` holds the form list, the server's dataset list
(``datasets.json``) and every server dataset pulled (``raw/datasets/<id>.csv``), each
once for the whole project; ``<form>/raw/`` holds a form's XML, its submissions and the
CSV files attached to it (``files/``); ``<form>/`` holds its built outputs; and
``flow.json`` says how the forms chain through datasets.
"""

import argparse
import datetime as dt
import json
import os
import tomllib
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from dataclasses import dataclass, fields
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
from dotenv import load_dotenv

from surveycto_python_dict.api import Client, MissingEnvVar, env_key_path
from surveycto_python_dict.cast import cast_wide
from surveycto_python_dict.codebook import write_pii_review
from surveycto_python_dict.contract import (
    DatasetSource,
    attach_enumerators,
    form_entry,
    resolve_datasets,
)
from surveycto_python_dict.dictionary import build_dictionary, build_graph, write_json
from surveycto_python_dict.flow import build_flow, describe_project
from surveycto_python_dict.pii import (
    PiiConfig,
    attach,
    build_analyzer,
    classify,
    dataset_codebook,
    screen_search_choices,
    warn_unknown_names,
)
from surveycto_python_dict.questions import (
    build_questions,
    search_choices,
    write_sections,
)
from surveycto_python_dict.stats import reached
from surveycto_python_dict.survey import render_mermaid
from surveycto_python_dict.xml_contract import FormContract, parse_contract

SEARCH_CHOICES = pa.schema(
    [(c, pa.string()) for c in ("question", "value", "label", "when", "withheld")]
)

_TOP_KEYS = {
    "forms",
    "server_datasets",
    "out_dir",
    "pii",
    "datasets",
    "project",
    "entities",
    "submissions",
}


@dataclass(frozen=True)
class Config:
    """A parsed config file."""

    forms: tuple[str, ...]
    # Server datasets pulled besides the ones the forms read.
    server_datasets: tuple[str, ...]
    out_dir: Path
    pii: PiiConfig
    # [datasets.<name>] key: the columns that identify a dataset row.
    dataset_keys: dict[str, list[str]]
    # [datasets.<name>] published_by: the form that publishes it, where the columns
    # don't tell.
    dataset_publishers: dict[str, str]
    # [submissions] exclude: an SQL condition on the parquet's columns for rows the
    # project does not use (tests, say), left out of the counts and stats.
    exclude: str | None
    # [project] description: what the project is about, in the user's words.
    description: str | None
    # [entities.<name>]: key (the columns that identify it, together), description,
    # parent.
    entities: dict[str, dict]

    @property
    def project(self) -> str:
        """Return the project's name: its ``out_dir`` folder."""
        return self.out_dir.resolve().name

    @property
    def raw_dir(self) -> Path:
        """Return where the project-wide pulls go: form list and server datasets."""
        return self.out_dir / "raw"

    def form_dir(self, form: str) -> Path:
        """Return where a form's built outputs go."""
        return self.out_dir / form

    def form_raw(self, form: str) -> Path:
        """Return where a form's XML, submissions and attached files go."""
        return self.form_dir(form) / "raw"


def _check_keys(section: str, got: set[str], allowed: set[str]) -> None:
    unknown = sorted(got - allowed)
    if unknown:
        raise ValueError(f"unknown key(s) in {section}: {', '.join(unknown)}")


def _columns(key: str | list[str]) -> list[str]:
    """Return a config key, one column or a list, as a list."""
    return [key] if isinstance(key, str) else list(key)


def load_config(path: Path) -> Config:
    """Parse ``path``; unknown keys raise so a typo never silently falls back."""
    data = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    _check_keys("config", set(data), _TOP_KEYS)
    pii = data.get("pii", {})
    _check_keys("[pii]", set(pii), {f.name for f in fields(PiiConfig)})
    if "entities" in pii:
        pii["entities"] = tuple(pii["entities"])
    for key in ("whitelist", "blacklist"):
        if key in pii:
            pii[key] = tuple(pii[key])
    datasets = data.get("datasets", {})
    for name, section in datasets.items():
        _check_keys(f"[datasets.{name}]", set(section), {"key", "published_by"})
    project = data.get("project", {})
    _check_keys("[project]", set(project), {"description"})
    submissions = data.get("submissions", {})
    _check_keys("[submissions]", set(submissions), {"exclude"})
    entities = data.get("entities", {})
    for name, section in entities.items():
        _check_keys(
            f"[entities.{name}]", set(section), {"key", "description", "parent"}
        )
        section["key"] = _columns(section["key"])
    if not data["forms"]:
        raise ValueError("forms is empty: name at least one form")
    # <project>.duckdb holds a schema per form: a form named like the database makes
    # every <form>.<table> ambiguous to DuckDB.
    project_name = (Path(path).parent / data["out_dir"]).resolve().name
    if project_name in data["forms"]:
        raise ValueError(
            f"out_dir's folder {project_name!r} is also a form's id; name the project "
            f"folder differently (e.g. {project_name}_project)"
        )
    return Config(
        forms=tuple(data["forms"]),
        server_datasets=tuple(data.get("server_datasets", ())),
        out_dir=Path(path).parent / data["out_dir"],
        pii=PiiConfig(**pii),
        dataset_keys={
            name: _columns(section["key"])
            for name, section in datasets.items()
            if "key" in section
        },
        dataset_publishers={
            name: section["published_by"]
            for name, section in datasets.items()
            if "published_by" in section
        },
        exclude=submissions.get("exclude"),
        description=project.get("description"),
        entities=entities,
    )


def env_sentinels() -> dict[int | dt.date, str]:
    """Return ``SCTO_SENTINELS`` and ``SCTO_DATE_SENTINELS`` as ``{code: meaning}``.

    The study's non-response codes, as ``code=meaning`` pairs separated by ``;``:
    ``SCTO_SENTINELS="-99=Don't know;-88=Refused;-77=Not applicable"``. Dates the
    team types into a date question to code the same answers go, optionally, in
    ``SCTO_DATE_SENTINELS="1900-01-01=Don't know"``.
    """
    raw = os.environ.get("SCTO_SENTINELS", "").strip().strip(";")
    if not raw:
        raise MissingEnvVar(
            "environment variable SCTO_SENTINELS is not set "
            '(e.g. SCTO_SENTINELS="-99=Don\'t know;-88=Refused")'
        )
    table = {}
    for pair in raw.split(";"):
        code, sep, meaning = pair.partition("=")
        if not sep or not code.strip() or not meaning.strip():
            raise ValueError(f"SCTO_SENTINELS: {pair.strip()!r} is not code=meaning")
        table[int(code.strip())] = meaning.strip()
    dates = os.environ.get("SCTO_DATE_SENTINELS", "").strip().strip(";")
    for pair in dates.split(";") if dates else []:
        code, sep, meaning = pair.partition("=")
        if not sep or not meaning.strip():
            raise ValueError(
                f"SCTO_DATE_SENTINELS: {pair.strip()!r} is not date=meaning"
            )
        table[dt.date.fromisoformat(code.strip())] = meaning.strip()
    return table


def _contract(cfg: Config, form: str) -> FormContract:
    """Parse ``form``'s pulled XML, its enumerator fields reading the dataset the
    form list names.
    """
    contract = parse_contract(cfg.form_raw(form) / "form.xml")
    form_list = (cfg.raw_dir / "formList.xml").read_bytes()
    attach_enumerators(contract, form_entry(form_list, form))
    return contract


def _sources(
    cfg: Config, form: str, contract: FormContract
) -> dict[str, DatasetSource]:
    """Resolve the datasets ``form`` reads against the pulled form and dataset lists."""
    form_list = (cfg.raw_dir / "formList.xml").read_bytes()
    server = json.loads((cfg.raw_dir / "datasets.json").read_text(encoding="utf-8"))
    return resolve_datasets(
        contract, form_entry(form_list, form), {d["id"] for d in server}
    )


def form_datasets(cfg: Config, form: str) -> dict[str, Path]:
    """Return the CSV of each dataset ``form`` reads, by the name the form uses."""
    contract = _contract(cfg, form)
    return {
        name: (
            cfg.raw_dir / "datasets" / f"{s.ref}.csv"
            if s.kind == "server_dataset"
            else cfg.form_raw(form) / "files" / s.ref
        )
        for name, s in _sources(cfg, form, contract).items()
    }


def dataset_csvs(cfg: Config, flow: dict) -> dict[str, Path]:
    """Return the CSV of each ``flow.json`` dataset: a server dataset or a form's file."""
    csvs = {}
    for d in flow["datasets"]:
        if d["kind"] == "server_dataset":
            csvs[d["id"]] = cfg.raw_dir / "datasets" / f"{d['id']}.csv"
        else:
            form, filename = d["id"].split("/", 1)
            csvs[d["id"]] = cfg.form_raw(form) / "files" / filename
    return csvs


def pull(cfg: Config, client: Client) -> Path:
    """Pull every form's XML, submissions and attached files, and the server datasets.

    The server datasets are the config's ``server_datasets`` plus every one a form
    reads, each pulled once into ``raw/datasets/``.
    """
    raw = cfg.raw_dir
    (raw / "datasets").mkdir(parents=True, exist_ok=True)
    (raw / "formList.xml").write_bytes(client.pull_form_list())
    server = client.list_datasets()
    (raw / "datasets.json").write_text(json.dumps(server, indent=1), encoding="utf-8")
    unknown = sorted(set(cfg.server_datasets) - {d["id"] for d in server})
    if unknown:
        raise ValueError(f"server_datasets not on the server: {', '.join(unknown)}")

    wanted = set(cfg.server_datasets)
    for form in cfg.forms:
        form_raw = cfg.form_raw(form)
        form_raw.mkdir(parents=True, exist_ok=True)
        (form_raw / "form.xml").write_bytes(client.pull_form_xml(form))
        contract = _contract(cfg, form)
        sources = _sources(cfg, form, contract)
        wanted |= {s.ref for s in sources.values() if s.kind == "server_dataset"}
        form_files = [s.ref for s in sources.values() if s.kind == "form_file"]
        if form_files:
            (form_raw / "files").mkdir(exist_ok=True)
            for name, data in client.pull_form_files(form, form_files).items():
                (form_raw / "files" / name).write_bytes(data)
        for name, s in sources.items():
            if not s.attached:
                print(f"[pull] {form} reads {name} ({s.ref}) but does not attach it")

        key = env_key_path() if contract.encrypted else None
        rows = client.pull_wide_json(form, key_path=key)
        (form_raw / "wide.json").write_text(
            json.dumps(rows, ensure_ascii=False), encoding="utf-8"
        )
        columns = len({c for r in rows for c in r})
        print(
            f"[pull] {form} v{contract.formdef_version}: {len(rows)} submissions, "
            f"{columns} columns, reads {len(sources)} datasets, "
            f"{'encrypted' if contract.encrypted else 'unencrypted'} -> {form_raw}"
        )

    for dataset in sorted(wanted):
        (raw / "datasets" / f"{dataset}.csv").write_text(
            client.pull_dataset(dataset), encoding="utf-8"
        )
    print(f"[pull] {len(wanted)} server datasets -> {raw / 'datasets'}")
    return raw


def build_form(
    cfg: Config,
    form: str,
    sentinels: dict[int, str],
    analyzer,
    scanned: dict[tuple[Path, str], dict],
) -> tuple[dict[str, Path], set[str]]:
    """Build one form's outputs from its raw pull.

    Return the written paths by name and the names the ``[pii]`` lists can use: each
    question, column and ``dataset.column``.
    """
    raw, out = cfg.form_raw(form), cfg.form_dir(form)
    contract = _contract(cfg, form)
    datasets = form_datasets(cfg, form)
    rows = json.loads((raw / "wide.json").read_text(encoding="utf-8"))
    screen_search_choices(contract, datasets, cfg.pii, analyzer, rows)
    questions = build_questions(contract)
    table = cast_wide(rows, contract)
    vardict = build_dictionary(table, questions, contract, sentinels, cfg.exclude)
    blocks = classify(table, vardict, questions, cfg.pii, analyzer)
    codebook = dataset_codebook(datasets, questions, cfg.pii, analyzer, scanned)
    attach(vardict, blocks, questions, codebook)

    paths = {
        "questions": out / f"{form}_questions.json",
        "sections": out / "sections",
        "parquet": out / f"{form}.parquet",
        "dictionary": out / f"{form}_variable_dictionary.json",
        "graph": out / f"{form}_variable_graph.json",
        "pii_review": out / f"{form}_pii_review.xlsx",
        "dataset_codebook": out / f"{form}_dataset_codebook.json",
        "search_choices": out / f"{form}_search_choices.parquet",
    }
    # pyarrow writes the parquet outside the GIL while the JSON is written here.
    with ThreadPoolExecutor(1) as background:
        parquet = background.submit(pq.write_table, table, paths["parquet"])
        write_json(questions, paths["questions"])
        write_sections(questions, paths["sections"])
        # The two largest outputs, read by programs: compact is a third smaller and
        # several times faster to write.
        write_json(vardict, paths["dictionary"], indent=None)
        write_json(
            build_graph(questions, vardict, reached(table, questions, vardict)),
            paths["graph"],
            indent=None,
        )
        write_pii_review(table, vardict, paths["pii_review"])
        write_json(codebook, paths["dataset_codebook"])
        pq.write_table(
            pa.Table.from_pylist(search_choices(contract), schema=SEARCH_CHOICES),
            paths["search_choices"],
        )
        parquet.result()

    fallbacks = vardict["summary"]["n_cast_fallback"]
    print(
        f"[build] {form}: {len(questions)} questions, {table.num_rows} rows, "
        f"{table.num_columns} columns, {fallbacks} kept as text; "
        f"pii {dict(Counter(b['status'] for b in blocks.values()))} -> {out}"
    )
    names = {q["variable_name"] for q in questions} | set(vardict["variables"])
    names |= {f"{c['dataset']}.{c['column']}" for c in codebook}
    return paths, names


def write_flow(cfg: Config) -> Path:
    """Write ``flow.json``, how the project's forms chain through datasets, and ``flow.md``."""
    forms = {}
    for form in cfg.forms:
        contract = _contract(cfg, form)
        forms[form] = (contract, _sources(cfg, form, contract))
    server = json.loads((cfg.raw_dir / "datasets.json").read_text(encoding="utf-8"))
    flow = build_flow(
        forms,
        server,
        cfg.server_datasets,
        cfg.raw_dir / "datasets",
        {form: cfg.form_raw(form) / "files" for form in cfg.forms},
        cfg.dataset_publishers,
    )
    flow["project"] = describe_project(
        flow,
        {form: contract for form, (contract, _) in forms.items()},
        cfg.description,
        cfg.entities,
    )
    path = cfg.out_dir / "flow.json"
    write_json(flow, path)
    (cfg.out_dir / "flow.md").write_text(
        "# How the forms chain\n\n"
        "Green: filled from the publishing form's answers. Orange: carried from another "
        "dataset. Yellow: both. Grey: no project form fills it. A dashed read is not "
        "attached to the form on the server.\n\n"
        f"```mermaid\n{render_mermaid(flow)}\n```\n",
        encoding="utf-8",
        newline="\n",
    )
    print(
        "[flow] "
        + "; ".join(f"{f['form_id']} stage {f['stage']}" for f in flow["forms"])
        + f" -> {path}"
    )
    return path


def build(
    cfg: Config, sentinels: dict[int, str], analyzer=None
) -> dict[str, dict[str, Path]]:
    """Build every form's outputs and ``flow.json``; return each form's paths."""
    if analyzer is None and not cfg.pii.no_personal_data:
        analyzer = build_analyzer(cfg.pii)
    scanned: dict[tuple[Path, str], dict] = {}
    paths, known = {}, set()
    for form in cfg.forms:
        paths[form], names = build_form(cfg, form, sentinels, analyzer, scanned)
        known |= names
    warn_unknown_names(cfg.pii, known)
    write_flow(cfg)
    return paths


def config_arg(argv: list[str] | None) -> Config:
    """Return the config named by ``--config`` (default ``config.toml``)."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=Path("config.toml"))
    return load_config(parser.parse_args(argv).config)


def pull_main(argv: list[str] | None = None) -> None:
    """Entry point for ``scto-pull``."""
    load_dotenv()
    pull(config_arg(argv), Client.from_env())


def build_main(argv: list[str] | None = None) -> None:
    """Entry point for ``scto-build``."""
    load_dotenv()
    build(config_arg(argv), env_sentinels())


def flow_main(argv: list[str] | None = None) -> None:
    """Entry point for ``scto-flow``: rewrite ``flow.json`` and ``flow.md`` alone."""
    write_flow(config_arg(argv))


def outputs_main(argv: list[str] | None = None) -> None:
    """Entry point for ``scto-outputs``: the stats, then the codebooks and the database
    at once, since each reads the stats and writes only its own files.
    """
    import sys

    from surveycto_python_dict import codebook, database
    from surveycto_python_dict.stats import write_form_stats

    argv = sys.argv[1:] if argv is None else argv
    load_dotenv()
    cfg = config_arg(argv)
    sentinels = env_sentinels()
    stats = {f: write_form_stats(cfg.form_dir(f), f, sentinels) for f in cfg.forms}
    with ProcessPoolExecutor(1) as other:
        db = other.submit(database.main, argv)
        for form in cfg.forms:
            path = codebook.write_codebook(cfg.form_dir(form), form, stats[form])
            print(f"[codebook] {form} -> {path}")
        db.result()


def run_main(argv: list[str] | None = None) -> None:
    """Entry point for ``scto-run``: pull, then build."""
    load_dotenv()
    cfg = config_arg(argv)
    sentinels = env_sentinels()
    pull(cfg, Client.from_env())
    build(cfg, sentinels)


def _skill_source() -> Path:
    """Return the survey-expert skill's folder: in the package when installed from the
    wheel, in the repository's ``.claude/skills`` in a clone.
    """
    package = Path(__file__).parent
    for folder in (
        package / "skill",
        package.parents[1] / ".claude/skills/survey-expert",
    ):
        if (folder / "SKILL.md").exists():
            return folder
    raise FileNotFoundError(f"no survey-expert skill next to {package}")


def install_skill(target: Path) -> Path:
    """Write the survey-expert skill into ``target``: SKILL.md, its references, and
    ``search_survey.py`` from this package's ``survey.py``, so the copy matches the
    installed tool.
    """
    import shutil

    source = _skill_source()
    target.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source / "SKILL.md", target / "SKILL.md")
    shutil.copytree(source / "references", target / "references", dirs_exist_ok=True)
    shutil.copyfile(Path(__file__).parent / "survey.py", target / "search_survey.py")
    return target


def skill_main(argv: list[str] | None = None) -> None:
    """Entry point for ``scto-skill``: write the survey-expert skill into a project."""
    parser = argparse.ArgumentParser(prog="scto-skill")
    parser.add_argument(
        "target",
        nargs="?",
        type=Path,
        default=Path(".claude/skills/survey-expert"),
        help="where to write it (default: .claude/skills/survey-expert)",
    )
    target = install_skill(parser.parse_args(argv).target)
    print(f"[skill] survey-expert -> {target}")
