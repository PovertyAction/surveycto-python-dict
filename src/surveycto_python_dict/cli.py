"""``scto-pull``, ``scto-build`` and ``scto-run``.

Configuration is a TOML file (``--config``, default ``config.toml``)::

    form_id = "my_form"
    out_dir = "data/out"          # relative to the config file

    [pii]                         # every key optional; see PiiConfig
    whitelist = ["village_name"]

    [sentinels]                   # replaces the default code -> meaning table
    "-99" = "Don't know"

Credentials come from the environment or ``.env``: ``SCTO_SERVER``, ``SCTO_USER``,
``SCTO_PASSWORD``, and ``SCTO_KEY_PATH`` for encrypted forms. Raw pulls go to
``<out_dir>/<form_id>/raw/``; built outputs to ``<out_dir>/<form_id>/``.
"""

import argparse
import json
import tomllib
from collections import Counter
from dataclasses import dataclass, fields
from pathlib import Path

import pyarrow.parquet as pq
from dotenv import load_dotenv

from surveycto_python_dict.api import Client, env_key_path
from surveycto_python_dict.cast import cast_wide
from surveycto_python_dict.contract import form_entry, resolve_datasets
from surveycto_python_dict.dictionary import (
    DEFAULT_SENTINELS,
    build_dictionary,
    build_graph,
    write_json,
)
from surveycto_python_dict.pii import (
    PiiConfig,
    attach,
    build_analyzer,
    classify,
    review_table,
)
from surveycto_python_dict.questions import build_questions, write_sections
from surveycto_python_dict.xml_contract import parse_contract

_TOP_KEYS = {"form_id", "out_dir", "pii", "sentinels"}


@dataclass(frozen=True)
class Config:
    """A parsed config file."""

    form_id: str
    out_dir: Path
    pii: PiiConfig
    sentinels: dict[int, str]

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
    if "whitelist" in pii:
        pii["whitelist"] = tuple(pii["whitelist"])
    sentinels = data.get("sentinels")
    return Config(
        form_id=data["form_id"],
        out_dir=Path(path).parent / data["out_dir"],
        pii=PiiConfig(**pii),
        sentinels=(
            {int(code): meaning for code, meaning in sentinels.items()}
            if sentinels is not None
            else DEFAULT_SENTINELS
        ),
    )


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
    attached = (
        client.pull_form_files(cfg.form_id)
        if any(s.kind == "form_file" for s in sources.values())
        else {}
    )
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


def build(cfg: Config, analyzer=None) -> dict[str, Path]:
    """Build every output from the raw dir; return the written paths by name."""
    raw, out, form = cfg.raw_dir, cfg.form_dir, cfg.form_id
    contract = parse_contract(raw / "form.xml")
    questions = build_questions(raw / "form.xml")
    rows = json.loads((raw / "wide.json").read_text(encoding="utf-8"))
    table = cast_wide(rows, contract)
    vardict = build_dictionary(table, questions, contract, cfg.sentinels)
    blocks = classify(
        table, vardict, questions, cfg.pii, analyzer or build_analyzer(cfg.pii)
    )
    attach(vardict, blocks)

    paths = {
        "questions": out / f"{form}_questions.json",
        "sections": out / "sections",
        "parquet": out / f"{form}.parquet",
        "dictionary": out / f"{form}_variable_dictionary.json",
        "graph": out / f"{form}_variable_graph.json",
        "pii_review": out / f"{form}_pii_review.parquet",
    }
    write_json(questions, paths["questions"])
    write_sections(questions, paths["sections"])
    pq.write_table(table, paths["parquet"])
    write_json(vardict, paths["dictionary"])
    write_json(build_graph(questions, vardict), paths["graph"])
    pq.write_table(review_table(table, blocks), paths["pii_review"])

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
    build(_config_arg(argv))


def run_main(argv: list[str] | None = None) -> None:
    """Entry point for ``scto-run``: pull, then build."""
    load_dotenv()
    cfg = _config_arg(argv)
    pull(cfg, Client.from_env())
    build(cfg)
