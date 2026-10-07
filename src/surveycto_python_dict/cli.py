"""``scto-pull``, ``scto-build`` and ``scto-run``.

Configuration is a TOML file (``--config``, default ``config.toml``)::

    form_id = "my_form"
    out_dir = "data/out"  # relative to the config file

    [pii]  # every key optional; see PiiConfig
    whitelist = ["village_name"]
    blacklist = ["respondent_id"]

Credentials and the study's sentinel codes come from the environment or ``.env``:
``SCTO_SERVER``, ``SCTO_USER``, ``SCTO_PASSWORD``, ``SCTO_KEY_PATH`` for encrypted forms,
and ``SCTO_SENTINELS`` (``"-99=Don't know;-88=Refused"``) for building. Raw pulls go to
``<out_dir>/<form_id>/raw/``; built outputs to ``<out_dir>/<form_id>/``.
"""

import argparse
import json
import os
import tomllib
from collections import Counter
from dataclasses import dataclass, fields
from pathlib import Path

import pyarrow.parquet as pq
from dotenv import load_dotenv

from surveycto_python_dict.api import Client, MissingEnvVar, env_key_path
from surveycto_python_dict.cast import cast_wide
from surveycto_python_dict.codebook import write_pii_review
from surveycto_python_dict.contract import form_entry, resolve_datasets
from surveycto_python_dict.dictionary import build_dictionary, build_graph, write_json
from surveycto_python_dict.pii import (
    PiiConfig,
    attach,
    build_analyzer,
    classify,
    dataset_codebook,
    screen_search_choices,
)
from surveycto_python_dict.questions import build_questions, write_sections
from surveycto_python_dict.xml_contract import parse_contract

_TOP_KEYS = {"form_id", "out_dir", "pii", "datasets"}


@dataclass(frozen=True)
class Config:
    """A parsed config file."""

    form_id: str
    out_dir: Path
    pii: PiiConfig
    # [datasets.<name>] key: the columns that identify a dataset row.
    dataset_keys: dict[str, list[str]]

    @property
    def form_dir(self) -> Path:
        """Return where built outputs go."""
        return self.out_dir / self.form_id

    @property
    def raw_dir(self) -> Path:
        """Return where raw pulls go."""
        return self.form_dir / "raw"


def _check_keys(section: str, got: set[str], allowed: set[str]) -> None:
    unknown = sorted(got - allowed)
    if unknown:
        raise ValueError(f"unknown key(s) in {section}: {', '.join(unknown)}")


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
        _check_keys(f"[datasets.{name}]", set(section), {"key"})
    return Config(
        form_id=data["form_id"],
        out_dir=Path(path).parent / data["out_dir"],
        pii=PiiConfig(**pii),
        dataset_keys={name: section["key"] for name, section in datasets.items()},
    )


def env_sentinels() -> dict[int, str]:
    """Return ``SCTO_SENTINELS`` as ``{code: meaning}``.

    The study's non-response codes, as ``code=meaning`` pairs separated by ``;``:
    ``SCTO_SENTINELS="-99=Don't know;-88=Refused;-77=Not applicable"``.
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
    return table


def pull(cfg: Config, client: Client) -> Path:
    """Pull the form list, XML, referenced datasets and wide JSON into the raw dir."""
    raw = cfg.raw_dir
    raw.mkdir(parents=True, exist_ok=True)
    form_list = client.pull_form_list()
    (raw / "formList.xml").write_bytes(form_list)
    (raw / "form.xml").write_bytes(client.pull_form_xml(cfg.form_id))
    contract = parse_contract(raw / "form.xml")

    sources = resolve_datasets(contract, form_entry(form_list, cfg.form_id))
    if sources:
        (raw / "datasets").mkdir(exist_ok=True)
    form_files = [s.ref for s in sources.values() if s.kind == "form_file"]
    attached = client.pull_form_files(cfg.form_id, form_files) if form_files else {}
    for name, source in sources.items():
        out = raw / "datasets" / f"{name}.csv"
        if source.kind == "server_dataset":
            out.write_text(client.pull_dataset(source.ref), encoding="utf-8")
        else:
            out.write_bytes(attached[source.ref])

    key = env_key_path() if contract.encrypted else None
    rows = client.pull_wide_json(cfg.form_id, key_path=key)
    (raw / "wide.json").write_text(
        json.dumps(rows, ensure_ascii=False), encoding="utf-8"
    )
    columns = len({c for r in rows for c in r})
    print(
        f"[pull] {cfg.form_id} v{contract.formdef_version}: {len(rows)} submissions, "
        f"{columns} columns, {len(sources)} datasets, "
        f"{'encrypted' if contract.encrypted else 'unencrypted'} -> {raw}"
    )
    return raw


def build(cfg: Config, sentinels: dict[int, str], analyzer=None) -> dict[str, Path]:
    """Build every output from the raw dir; return the written paths by name."""
    raw, out, form = cfg.raw_dir, cfg.form_dir, cfg.form_id
    analyzer = analyzer or build_analyzer(cfg.pii)
    contract = parse_contract(raw / "form.xml")
    screen_search_choices(contract, raw / "datasets", cfg.pii, analyzer)
    questions = build_questions(contract)
    rows = json.loads((raw / "wide.json").read_text(encoding="utf-8"))
    table = cast_wide(rows, contract)
    vardict = build_dictionary(table, questions, contract, sentinels)
    blocks = classify(table, vardict, questions, cfg.pii, analyzer)
    attach(vardict, blocks)

    paths = {
        "questions": out / f"{form}_questions.json",
        "sections": out / "sections",
        "parquet": out / f"{form}.parquet",
        "dictionary": out / f"{form}_variable_dictionary.json",
        "graph": out / f"{form}_variable_graph.json",
        "pii_review": out / f"{form}_pii_review.xlsx",
        "dataset_codebook": out / f"{form}_dataset_codebook.json",
    }
    write_json(questions, paths["questions"])
    write_sections(questions, paths["sections"])
    pq.write_table(table, paths["parquet"])
    write_json(vardict, paths["dictionary"])
    write_json(build_graph(questions, vardict), paths["graph"])
    write_pii_review(table, vardict, paths["pii_review"])
    codebook = dataset_codebook(raw / "datasets", questions, cfg.pii, analyzer)
    write_json(codebook, paths["dataset_codebook"])

    fallbacks = vardict["summary"]["n_cast_fallback"]
    print(
        f"[build] {form}: {len(questions)} questions, {table.num_rows} rows, "
        f"{table.num_columns} columns, {fallbacks} kept as text; "
        f"pii {dict(Counter(b['status'] for b in blocks.values()))} -> {out}"
    )
    return paths


def _config_arg(argv: list[str] | None) -> Config:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=Path("config.toml"))
    return load_config(parser.parse_args(argv).config)


def pull_main(argv: list[str] | None = None) -> None:
    """Entry point for ``scto-pull``."""
    load_dotenv()
    pull(_config_arg(argv), Client.from_env())


def build_main(argv: list[str] | None = None) -> None:
    """Entry point for ``scto-build``."""
    load_dotenv()
    build(_config_arg(argv), env_sentinels())


def run_main(argv: list[str] | None = None) -> None:
    """Entry point for ``scto-run``: pull, then build."""
    load_dotenv()
    cfg = _config_arg(argv)
    sentinels = env_sentinels()
    pull(cfg, Client.from_env())
    build(cfg, sentinels)
