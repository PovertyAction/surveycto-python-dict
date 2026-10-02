"""Per-variable PII signal: evidence for the people cleaning the data, never enforcement.

Each column gets ``{"status", "basis"[, "presidio"]}``; the first matching rule wins:

0. ``whitelisted`` / ``reviewed``: the column or its question is on the user's whitelist.
1. ``cleared_by_form``: the question is ``publishable``.
2. ``contains_pii``: device metadata columns; geo and attachment question types.
3. ``whitelisted``: system columns; integer, decimal, date, datetime and time
   questions; selects with inline choices; calculates whose every reference is
   cleared or whitelisted and that do not ``pulldata()``.
4. ``may_contain_pii`` / ``no_hits``: everything else, scanned with Presidio on its
   distinct non-null values (sampled with ``seed`` above ``max_distinct``).

Presidio runs only on submission values. Blocks hold entity counts, the number of hit
rows and up to five of their KEYs, never matched text.
"""

import random
import warnings
from collections import Counter
from dataclasses import dataclass, field

import pyarrow as pa
import spacy
from presidio_analyzer import AnalyzerEngine, Pattern, PatternRecognizer
from presidio_analyzer.nlp_engine import NlpEngineProvider

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
_SAFE = {"cleared_by_form", "whitelisted"}
_FLAGGED = {"contains_pii", "may_contain_pii"}
# Enough KEYs to find examples; the review parquet has every flagged row.
_MAX_HIT_KEYS = 5


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


def build_analyzer(config: PiiConfig) -> AnalyzerEngine:
    """Return a Presidio analyzer for the configured languages and ID patterns."""
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

    def __init__(self, questions: list[dict], whitelist: set[str]):
        self.by_name = {q["variable_name"]: q for q in questions}
        self.whitelist = whitelist
        self._memo: dict[str, dict | None] = {}

    def static(self, name: str) -> dict | None:
        """Return the question's block when a rule decides it without data, else None."""
        if name not in self._memo:
            self._memo[name] = None  # a reference cycle resolves to "scan"
            self._memo[name] = self._decide(self.by_name[name])
        return self._memo[name]

    def _decide(self, q: dict) -> dict | None:
        qtype = q["type"]
        if q["variable_name"] in self.whitelist:
            return {"status": "whitelisted", "basis": ["reviewed"]}
        if q["publishable"]:
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
        return block is not None and block["status"] in _SAFE

    @staticmethod
    def _pulldata(q: dict) -> bool:
        return "pulldata(" in (q["calculation"] or "")

    def scan_basis(self, name: str | None) -> list[str]:
        if name is None:
            return ["no_question"]
        q = self.by_name[name]
        return [f"type:{q['type']}"] + (["pulldata"] if self._pulldata(q) else [])


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
    entities = (*config.entities, *config.id_patterns)
    found: dict[str, set[str]] = {}
    for value in distinct:
        types = {
            r.entity_type
            for lang in config.languages
            for r in analyzer.analyze(
                text=value,
                language=lang,
                entities=list(entities),
                score_threshold=config.score_threshold,
            )
        }
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
    whitelist = set(config.whitelist)
    for name in sorted(whitelist - question_names - set(variables)):
        warnings.warn(f"variable `{name}` cannot be found", UserWarning, stacklevel=2)
    rules = _Rules(questions, whitelist & question_names)
    keys = table.column("KEY").to_pylist()

    blocks = {}
    for column in table.schema.names:
        entry = variables[column]
        question = entry["question"]
        q = rules.by_name.get(question)
        if column in whitelist or question in rules.whitelist:
            block = {"status": "whitelisted", "basis": ["reviewed"]}
        elif q is not None and q["publishable"]:
            block = {"status": "cleared_by_form", "basis": ["publishable"]}
        elif column in _DEVICE_COLUMNS and entry.get("system"):
            block = {"status": "contains_pii", "basis": [f"device:{column}"]}
        elif entry.get("system"):
            block = {"status": "whitelisted", "basis": ["system"]}
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
        blocks[column] = block
    return blocks


def attach(vardict: dict, blocks: dict[str, dict]) -> None:
    """Add each column's block to the dictionary and a status count to its summary."""
    for column, block in blocks.items():
        vardict["variables"][column]["pii"] = block
    vardict["summary"]["pii"] = dict(Counter(b["status"] for b in blocks.values()))


def review_table(table: pa.Table, blocks: dict[str, dict]) -> pa.Table:
    """Return KEY plus every ``contains_pii`` / ``may_contain_pii`` column."""
    flagged = [c for c in table.schema.names if blocks[c]["status"] in _FLAGGED]
    return table.select(["KEY", *flagged])
