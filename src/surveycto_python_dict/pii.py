"""Per-variable PII signal: evidence for the people cleaning the data, never enforcement.

Each column gets ``{"status", "basis"[, "presidio"]}``; the first matching rule wins:

0. ``contains_pii`` / ``reviewed``: the column or its question is on the user's
   blacklist; ``whitelisted`` / ``reviewed``: on the whitelist.
1. ``cleared_by_form``: the question is ``publishable``. With ``trust_publishable =
   false`` this rule is skipped and ``publishable`` is only noted in the basis.
2. ``contains_pii``: device metadata columns; geo and attachment question types.
3. ``whitelisted``: system columns; integer, decimal, date, datetime and time
   questions; selects with inline choices; calculates whose every reference is
   cleared or whitelisted and that do not ``pulldata()``.
4. ``may_contain_pii`` / ``no_hits``: everything else, scanned with Presidio on its
   distinct non-null values (sampled with ``seed`` above ``max_distinct``). With
   ``no_personal_data`` the project declares it holds none: every column past the
   device and system columns is ``whitelisted`` / ``project``, and Presidio never runs.

Presidio runs on submission values, on ``search()`` choice lists before they enter
any output (``screen_search_choices``), and on the datasets the form reads
(``dataset_codebook``). Blocks hold entity counts, the number of hit
rows and up to five of their KEYs, never matched text.
"""

import csv
import random
import warnings
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import pyarrow as pa

from surveycto_python_dict.contract import DatasetTable, search_universe
from surveycto_python_dict.xml_contract import FormContract

if TYPE_CHECKING:
    from presidio_analyzer import AnalyzerEngine

_DEVICE_COLUMNS = {
    "deviceid",
    "subscriberid",
    "simid",
    "simserial",
    "devicephonenum",
    "phonenumber",
    "username",
}
_CONTAINS_TYPES = {
    "geopoint",
    "geotrace",
    "geoshape",
    "image",
    "audio",
    "video",
    "file",
}
_TYPED_SAFE = {"integer", "decimal", "date", "datetime", "time"}
_SELECT_TYPES = {"select_one", "select_multiple"}
# The statuses whose values may be read, and those that hold or may hold PII.
SAFE = {"cleared_by_form", "whitelisted"}
FLAGGED = {"contains_pii", "may_contain_pii"}
# Enough KEYs to find examples; the review parquet has every flagged row.
_MAX_HIT_KEYS = 5
# A search() list longer than this stays in its dataset: a filter on a field can leave
# every row of a large dataset as a possible choice, copied into each column.
MAX_INLINE_CHOICES = 1000


class MissingSpacyModel(RuntimeError):
    """A configured language's spaCy model is not installed."""


@dataclass(frozen=True)
class PiiConfig:
    """``[pii]`` settings."""

    languages: dict[str, str] = field(
        default_factory=lambda: {"es": "es_core_news_lg", "en": "en_core_web_lg"}
    )
    entities: tuple[str, ...] = ("PERSON", "PHONE_NUMBER", "EMAIL_ADDRESS", "LOCATION")
    # Extra entity name -> regex, e.g. {"NATIONAL_ID": r"\b\d{8,10}\b"}.
    id_patterns: dict[str, str] = field(default_factory=dict)
    # Presidio scores bare phone numbers 0.4.
    score_threshold: float = 0.4
    max_distinct: int = 1000
    seed: int = 0
    # Column or question names the user reviewed and cleared.
    whitelist: tuple[str, ...] = ()
    # Column or question names the user reviewed as personal data; above publishable.
    blacklist: tuple[str, ...] = ()
    # False: publishable clears nothing; the column goes through the other rules.
    trust_publishable: bool = True
    # True: the project holds no personal data (study metadata, say); every column
    # past the blacklist, publishable and device rules is whitelisted, unscanned.
    no_personal_data: bool = False

    def __post_init__(self):
        """Raise when a name is on both the whitelist and the blacklist."""
        both = set(self.whitelist) & set(self.blacklist)
        if both:
            raise ValueError(
                f"on both the whitelist and the blacklist: {', '.join(sorted(both))}"
            )


def build_analyzer(config: PiiConfig) -> "AnalyzerEngine":
    """Return a Presidio analyzer for the configured languages and ID patterns."""
    # Imported here: spaCy and Presidio take a second to load, and only the build
    # scans with them.
    import spacy
    from presidio_analyzer import AnalyzerEngine, Pattern, PatternRecognizer
    from presidio_analyzer.nlp_engine import NlpEngineProvider

    for lang, model in config.languages.items():
        if not spacy.util.is_package(model):
            raise MissingSpacyModel(
                f"spaCy model {model!r} for language {lang!r} is not installed"
            )
    nlp = NlpEngineProvider(
        nlp_configuration={
            "nlp_engine_name": "spacy",
            "models": [
                {"lang_code": k, "model_name": v} for k, v in config.languages.items()
            ],
        }
    ).create_engine()
    analyzer = AnalyzerEngine(
        nlp_engine=nlp, supported_languages=list(config.languages)
    )
    for name, regex in config.id_patterns.items():
        for lang in config.languages:
            analyzer.registry.add_recognizer(
                PatternRecognizer(
                    supported_entity=name,
                    supported_language=lang,
                    patterns=[Pattern(name=name, regex=regex, score=1.0)],
                )
            )
    return analyzer


class _Rules:
    """Question-level rules, memoised so calculate lineage resolves once."""

    def __init__(
        self,
        questions: list[dict],
        whitelist: set[str],
        blacklist: set[str],
        trust_publishable: bool,
    ):
        self.by_name = {q["variable_name"]: q for q in questions}
        self.whitelist = whitelist
        self.blacklist = blacklist
        self.trust_publishable = trust_publishable
        self._memo: dict[str, dict | None] = {}

    def static(self, name: str) -> dict | None:
        """Return the question's block when a rule decides it without data, else None."""
        if name not in self._memo:
            self._memo[name] = None  # a reference cycle resolves to "scan"
            self._memo[name] = self._decide(self.by_name[name])
        return self._memo[name]

    def _decide(self, q: dict) -> dict | None:
        qtype = q["type"]
        if q["variable_name"] in self.blacklist:
            return {"status": "contains_pii", "basis": ["reviewed"]}
        if q["variable_name"] in self.whitelist:
            return {"status": "whitelisted", "basis": ["reviewed"]}
        if q["publishable"] and self.trust_publishable:
            return {"status": "cleared_by_form", "basis": ["publishable"]}
        if qtype in _CONTAINS_TYPES:
            return {"status": "contains_pii", "basis": [f"type:{qtype}"]}
        if qtype in _TYPED_SAFE:
            return {"status": "whitelisted", "basis": [f"type:{qtype}"]}
        if qtype in _SELECT_TYPES and "search(" not in (q["appearance"] or ""):
            return {"status": "whitelisted", "basis": ["choices:inline"]}
        if qtype == "calculate" and not self._pulldata(q) and q["references"]:
            refs = q["references"]
            if all(r in self.by_name and self._safe(r) for r in refs):
                return {"status": "whitelisted", "basis": [f"lineage:{','.join(refs)}"]}
        return None

    def _safe(self, name: str) -> bool:
        block = self.static(name)
        return block is not None and block["status"] in SAFE

    @staticmethod
    def _pulldata(q: dict) -> bool:
        return "pulldata(" in (q["calculation"] or "")

    def scan_basis(self, name: str | None) -> list[str]:
        if name is None:
            return ["no_question"]
        q = self.by_name[name]
        return [f"type:{q['type']}"] + (["pulldata"] if self._pulldata(q) else [])


def _entities(texts: list[str], config: PiiConfig, analyzer) -> list[set[str]]:
    """Return the entity types Presidio finds in each text.

    spaCy reads the texts in batches, several times faster than one call per text,
    and Presidio's recognizers then run on each text's parse.
    """
    wanted = [*config.entities, *config.id_patterns]
    found: list[set[str]] = [set() for _ in texts]
    for lang in config.languages:
        parsed = analyzer.nlp_engine.process_batch(texts, lang, batch_size=256)
        for hits, (text, artifacts) in zip(found, parsed, strict=True):
            hits.update(
                r.entity_type
                for r in analyzer.analyze(
                    text=text,
                    language=lang,
                    entities=wanted,
                    score_threshold=config.score_threshold,
                    nlp_artifacts=artifacts,
                )
            )
    return found


def screen_search_choices(
    contract: FormContract, datasets: dict[str, Path], config: PiiConfig, analyzer
) -> None:
    """Give each ``search()`` select its dataset choices when they hold no PII.

    All or nothing: a blacklisted question, a list over ``MAX_INLINE_CHOICES``, or any
    Presidio hit among the choices' values and labels withholds the whole list
    (``node.choices_withheld`` says why) and the select keeps the items naming the
    dataset's columns. A whitelisted question gets its list unscanned.
    """
    tables: dict[str, DatasetTable] = {}
    for node in contract.nodes.values():
        source = node.data_source
        if not source or source["kind"] != "search":
            continue
        if node.name in config.blacklist:
            node.choices_withheld = ["reviewed"]
            continue
        dataset = source["dataset"]
        if dataset not in tables:
            tables[dataset] = DatasetTable(datasets[dataset])
        universe = search_universe(node, tables[dataset])
        if len(universe) > MAX_INLINE_CHOICES:
            node.choices_withheld = [f"{len(universe)} choices"]
            continue
        if node.name not in config.whitelist and not config.no_personal_data:
            texts = sorted({t for c in universe for t in (c["value"], c["label"]) if t})
            found = sorted(set().union(*_entities(texts, config, analyzer)))
            if found:
                node.choices_withheld = [f"presidio:{e}" for e in found]
                continue
        node.choice_items = universe


def _scan(
    column: pa.ChunkedArray,
    keys: list[str],
    basis: list[str],
    config: PiiConfig,
    analyzer,
) -> dict:
    values = [None if v is None else str(v) for v in column.to_pylist()]
    distinct = sorted({v for v in values if v is not None})
    if len(distinct) > config.max_distinct:
        distinct = sorted(
            random.Random(config.seed).sample(distinct, config.max_distinct)
        )
    found: dict[str, set[str]] = {}
    for value, types in zip(
        distinct, _entities(distinct, config, analyzer), strict=True
    ):
        for t in types:
            found.setdefault(t, set()).add(value)
    hits = set().union(*found.values()) if found else set()
    hit_keys = sorted({k for k, v in zip(keys, values, strict=True) if v in hits})
    return {
        "status": "may_contain_pii" if found else "no_hits",
        "basis": basis + [f"presidio:{t}" for t in sorted(found)],
        "presidio": {
            "entities": {t: len(found[t]) for t in sorted(found)},
            "distinct_scanned": len(distinct),
            "hit_rows": len(hit_keys),
            "hit_keys": hit_keys[:_MAX_HIT_KEYS],
        },
    }


def classify(
    table: pa.Table, vardict: dict, questions: list[dict], config: PiiConfig, analyzer
) -> dict[str, dict]:
    """Return the PII block for every column of ``table``."""
    variables = vardict["variables"]
    question_names = {q["variable_name"] for q in questions}
    whitelist, blacklist = set(config.whitelist), set(config.blacklist)
    rules = _Rules(
        questions,
        whitelist & question_names,
        blacklist & question_names,
        config.trust_publishable,
    )
    keys = table.column("KEY").to_pylist()

    blocks = {}
    for column in table.schema.names:
        entry = variables[column]
        question = entry["question"]
        q = rules.by_name.get(question)
        if column in blacklist or question in rules.blacklist:
            block = {"status": "contains_pii", "basis": ["reviewed"]}
        elif column in whitelist or question in rules.whitelist:
            block = {"status": "whitelisted", "basis": ["reviewed"]}
        elif q is not None and q["publishable"] and config.trust_publishable:
            block = {"status": "cleared_by_form", "basis": ["publishable"]}
        elif column in _DEVICE_COLUMNS and entry.get("system"):
            block = {"status": "contains_pii", "basis": [f"device:{column}"]}
        elif entry.get("system"):
            block = {"status": "whitelisted", "basis": ["system"]}
        elif config.no_personal_data:
            block = {"status": "whitelisted", "basis": ["project"]}
        else:
            block = rules.static(question) if question is not None else None
            if block is None:
                block = _scan(
                    table.column(column),
                    keys,
                    rules.scan_basis(question),
                    config,
                    analyzer,
                )
            if q is not None and q["publishable"]:
                block = {**block, "basis": [*block["basis"], "publishable"]}
        blocks[column] = block
    return blocks


def dataset_codebook(
    datasets: dict[str, Path],
    questions: list[dict],
    config: PiiConfig,
    analyzer,
    scanned: dict[tuple[Path, str], dict],
) -> list[dict]:
    """Return one row per column of each dataset the form reads, with its pii block.

    A dataset has no XML, so a column gets only its counts, the questions that read
    it, and a status: ``dataset.column`` on the blacklist or whitelist (``reviewed``),
    else Presidio on its values. Hits are counted by CSV row (1 = first data row).
    ``scanned`` keeps each Presidio result by CSV and column, so forms that read the
    same dataset scan it once.
    """
    whitelist, blacklist = set(config.whitelist), set(config.blacklist)
    rows = []
    for dataset, csv_path in sorted(datasets.items()):
        with open(csv_path, encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            records = list(reader)
        numbers = list(range(1, len(records) + 1))
        for column in reader.fieldnames:
            name = f"{dataset}.{column}"
            values = [r[column] or None for r in records]
            if name in blacklist:
                block = {"status": "contains_pii", "basis": ["reviewed"]}
            elif name in whitelist:
                block = {"status": "whitelisted", "basis": ["reviewed"]}
            elif config.no_personal_data:
                block = {"status": "whitelisted", "basis": ["project"]}
            else:
                if (csv_path, column) not in scanned:
                    scanned[csv_path, column] = _scan(
                        pa.chunked_array([pa.array(values, pa.string())]),
                        numbers,
                        ["dataset"],
                        config,
                        analyzer,
                    )
                block = scanned[csv_path, column]
            read_by = [
                q["variable_name"]
                for q in questions
                if q["data_source"]
                and q["data_source"]["dataset"] == dataset
                and column
                in [q["data_source"].get("column")]
                + [m["column"] for m in q["data_source"]["match"]]
            ]
            rows.append(
                {
                    "dataset": dataset,
                    "column": column,
                    "rows": len(records),
                    "non_null": sum(v is not None for v in values),
                    "distinct": len({v for v in values if v is not None}),
                    "read_by": read_by,
                    "pii": block,
                }
            )
    return rows


def warn_unknown_names(config: PiiConfig, known: set[str]) -> None:
    """Warn for each whitelist or blacklist name that no form or dataset of the project has."""
    for name in sorted((set(config.whitelist) | set(config.blacklist)) - known):
        warnings.warn(f"`{name}` cannot be found", UserWarning, stacklevel=2)


def attach(vardict: dict, blocks: dict[str, dict]) -> None:
    """Add each column's block to the dictionary and a status count to its summary."""
    for column, block in blocks.items():
        vardict["variables"][column]["pii"] = block
    vardict["summary"]["pii"] = dict(Counter(b["status"] for b in blocks.values()))
