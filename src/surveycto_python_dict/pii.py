"""Per-variable PII signal: evidence for the people cleaning the data, never enforcement.

Each column gets ``{"status", "basis"[, "presidio"]}``; the first matching rule wins:

0. ``contains_pii`` / ``reviewed``: the column or its question is on the user's
   blacklist; ``whitelisted`` / ``reviewed``: on the whitelist.
1. ``cleared_by_form``: the question is ``publishable``. With ``trust_publishable =
   false`` this rule is skipped and ``publishable`` is only noted in the basis.
2. ``contains_pii``: device metadata columns; whatever the name, a field that
   preloads device or user metadata (``username``, ``deviceid``, ...), an enumerator
   field, a calculate that calls ``enumerator-id()``, ``enumerator-name()`` or
   ``username()``; and a calculate whose formula reads such a field, a geo or
   attachment field or a blacklisted one, directly or through other calculates (basis
   ``lineage:<field>``). This rule holds under ``no_personal_data`` too, except for
   geo and attachment fields; the whitelist outranks it. Geo and attachment fields
   themselves are ``contains_pii`` by their type (rule 3's ``static``), which
   ``no_personal_data`` overrides.
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

import random
import re
import warnings
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import pyarrow as pa

from surveycto_python_dict.contract import (
    DatasetTable,
    SubmissionValues,
    dataset_header,
    search_universe,
)
from surveycto_python_dict.survey import code_reasons, plain_label
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
# Fields that capture who or which device collected the submission, whatever their name.
_METADATA_TYPES = {
    "deviceid",
    "subscriberid",
    "simserial",
    "phonenumber",
    "username",
    "enumerator",  # stores the enumerator's "name (id)"
}
_METADATA_CALLS = re.compile(r"(?<![\w-])(enumerator-id|enumerator-name|username)\(\)")
_SELECT_TYPES = {"select_one", "select_multiple"}
_REF = re.compile(r"\$\{([^}]+)\}")
_CALL = re.compile(r"([A-Za-z][\w-]*)\s*\(")
# Functions that make a value without reading the form or the device.
_GENERATED_CALLS = {"once", "uuid", "now", "today", "random", "concat", "if"} | {
    # Pure functions: they reshape the value they are given.
    "substr",
    "int",
    "round",
    "string",
    "format-date-time",
    "date",
    "selected-at",
}
# The statuses that clear a calculate reading the field (lineage), and those that hold
# or may hold PII.
SAFE = {"cleared_by_form", "whitelisted"}
FLAGGED = {"contains_pii", "may_contain_pii"}
# Enough KEYs to find examples; the review workbook has every flagged row.
_MAX_HIT_KEYS = 5
# A search() list longer than this stays in its dataset: a filter on a field can leave
# every row of a large dataset as a possible choice, copied into each column.
MAX_INLINE_CHOICES = 50
_DETECTORS = {
    "spacy",
    "spacy_column",
    "structure",
    "zero_shot",
    "gliner2",
    "gliner2_onnx",
}
# The detectors that run a zero-shot model on each column.
_ZERO_SHOT = {"zero_shot", "gliner2", "gliner2_onnx"}
# The detectors that read each column with its context: the zero-shot models, and
# the configured spaCy models read the same way.
_COLUMNAR = _ZERO_SHOT | {"spacy_column"}
# zero_shot: the model, the labels it is asked for (its own words -> entity) and the
# score a hit needs.
_ZERO_SHOT_MODEL = "urchade/gliner_multi_pii-v1"
_ZERO_SHOT_LABELS = {
    "person": "PERSON",
    "address": "ADDRESS",
    "phone number": "PHONE_NUMBER",
    "email": "EMAIL_ADDRESS",
    "identity card number": "ID_NUMBER",
    "bank account number": "BANK_ACCOUNT",
}
_ZERO_SHOT_THRESHOLD = 0.5
# gliner2: a GLiNER2 model fine-tuned for PII, asked for its own labels.
_GLINER2_MODEL = "fastino/gliner2-privacy-filter-PII-multi"
_GLINER2_LABELS = {
    "person": "PERSON",
    "date_of_birth": "DATE_OF_BIRTH",
    "national_id_number": "ID_NUMBER",
    "government_id": "ID_NUMBER",
    "passport_number": "ID_NUMBER",
    "address": "ADDRESS",
    "street_address": "ADDRESS",
    "phone_number": "PHONE_NUMBER",
    "email": "EMAIL_ADDRESS",
    "bank_account": "BANK_ACCOUNT",
    "account_number": "BANK_ACCOUNT",
}
# gliner2_onnx: the multilingual GLiNER2 base model exported to ONNX, run without
# torch, asked in plain words.
_GLINER2_ONNX_MODEL = "lmo3/gliner2-multi-v1-onnx"
_GLINER2_ONNX_LABELS = {
    "person name": "PERSON",
    "date of birth": "DATE_OF_BIRTH",
    "national identity document number": "ID_NUMBER",
    "street address": "ADDRESS",
    "phone number": "PHONE_NUMBER",
    "email address": "EMAIL_ADDRESS",
    "bank account number": "BANK_ACCOUNT",
}
# Labels the model gives freely (its card: raise person's threshold) need more.
_GLINER2_MIN = {"PERSON": 0.8, "ADDRESS": 0.8, "DATE_OF_BIRTH": 0.9}
# A column's context and values per text: well inside the model's 384 tokens.
_CHUNK_CHARS = 600
# Distinct values the model reads from a column of short values.
_SHORT_SAMPLE = 100
# Free text: answers with a median of this many words or more.
_FREE_TEXT_WORDS = 5
# A value on at least this many rows, and this share of them, is a category.
_FREQUENT_ROWS = 10
_FREQUENT_SHARE = 0.02
# Entities written in digits: a hit needs this many in its value.
_NUMBERED = {"PHONE_NUMBER", "ID_NUMBER", "BANK_ACCOUNT"}
_MIN_DIGITS = 5


def _shaped(entity: str, value: str) -> bool:
    """Return whether ``value`` can hold ``entity``: a number needs its digits."""
    return entity not in _NUMBERED or sum(c.isdigit() for c in value) >= _MIN_DIGITS


def _free_text(distinct: list[str]) -> bool:
    """Return whether a column's distinct values are free text, not short values."""
    words = sorted(len(v.split()) for v in distinct)
    return bool(words) and words[len(words) // 2] >= _FREE_TEXT_WORDS


# spaCy's code for text in any language: the structure detector reads no language.
_ANY_LANGUAGE = "xx"
# A phone column, by shape: digit strings of 6 to 12 digits.
_PHONE_DIGITS = re.compile(r"\d{6,12}")
# Fewer distinct numbers than this give the shape tests too little to go on.
_MIN_PHONES = 10
# Operators' leading digits: this many 3-digit prefixes cover most of a phone column.
_PHONE_PREFIXES = 30
_PULLDATA = re.compile(r"pulldata\s*\(")
# Functions and operators that turn a value into a test of it.
_TESTS = re.compile(r"(?<![\w-])(empty|not|string-length|boolean)\s*\(\s*$")
_COMPARISON = re.compile(r"\s*(!=|=|<|>)")


def shows_values(status: str) -> bool:
    """Return whether an output an agent reads may show a column's values (range,
    quartiles, frequencies, codes) for its pii ``status``: every status but a flagged
    one. Counts, null rates and ``KEY``s are shown for every column.
    """
    return status not in FLAGGED


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
    # What scans the open columns; set per run by scripts/pii_compare.py while #108
    # compares detectors.
    detector: str = "spacy"

    def __post_init__(self):
        """Raise on an unknown detector or a name on both the whitelist and the
        blacklist.
        """
        if self.detector not in _DETECTORS:
            raise ValueError(
                f"detector {self.detector!r}: one of {', '.join(sorted(_DETECTORS))}"
            )
        both = set(self.whitelist) & set(self.blacklist)
        if both:
            raise ValueError(
                f"on both the whitelist and the blacklist: {', '.join(sorted(both))}"
            )


def build_analyzer(config: PiiConfig) -> "AnalyzerEngine":
    """Return a Presidio analyzer for the configured detector and ID patterns.

    ``spacy``: the configured languages' spaCy models and Presidio's recognizers.
    ``structure``: no NLP model; only emails and the ID patterns, while ``_block``
    finds phone columns by their shape.
    """
    # Imported here: spaCy and Presidio take a second to load, and only the build
    # scans with them.
    import spacy
    from presidio_analyzer import (
        AnalyzerEngine,
        Pattern,
        PatternRecognizer,
        RecognizerRegistry,
    )
    from presidio_analyzer.nlp_engine import NlpEngineProvider, NoOpNlpEngine
    from presidio_analyzer.predefined_recognizers import EmailRecognizer

    if config.detector in ("structure", *_ZERO_SHOT):
        nlp = NoOpNlpEngine(
            models=[{"lang_code": _ANY_LANGUAGE, "model_name": "no_op"}]
        )
        nlp.load()
        recognizers = [EmailRecognizer(supported_language=_ANY_LANGUAGE)] + [
            PatternRecognizer(
                supported_entity=name,
                supported_language=_ANY_LANGUAGE,
                patterns=[Pattern(name=name, regex=regex, score=1.0)],
            )
            for name, regex in config.id_patterns.items()
        ]
        if config.detector == "zero_shot":
            from presidio_analyzer.chunkers import CharacterBasedTextChunker
            from presidio_analyzer.predefined_recognizers import GLiNERRecognizer

            recognizers.append(
                GLiNERRecognizer(
                    supported_language=_ANY_LANGUAGE,
                    entity_mapping=_ZERO_SHOT_LABELS,
                    model_name=_ZERO_SHOT_MODEL,
                    threshold=_ZERO_SHOT_THRESHOLD,
                    map_location="cpu",
                    # Texts come chunked by column (_column_entities), each with
                    # its context: Presidio passes them whole.
                    text_chunker=CharacterBasedTextChunker(
                        chunk_size=2 * _CHUNK_CHARS, chunk_overlap=0
                    ),
                )
            )
        registry = RecognizerRegistry(recognizers, supported_languages=[_ANY_LANGUAGE])
        analyzer = AnalyzerEngine(
            nlp_engine=nlp, registry=registry, supported_languages=[_ANY_LANGUAGE]
        )
        if config.detector == "gliner2_onnx":
            from gliner2_onnx import GLiNER2ONNXRuntime

            # fp32: ONNX Runtime has no CPU kernels for fp16 matrix products.
            analyzer.zero_shot = GLiNER2ONNXRuntime.from_pretrained(
                _GLINER2_ONNX_MODEL, precision="fp32"
            )
        if config.detector == "gliner2":
            from gliner2 import AutoExtractor

            # Read in batches by _column_entities, beside Presidio's patterns.
            analyzer.zero_shot = AutoExtractor.from_pretrained(
                _GLINER2_MODEL, map_location="cpu"
            )
        return analyzer
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
        no_personal_data: bool = False,
        read_dates: bool = False,
    ):
        self.by_name = {q["variable_name"]: q for q in questions}
        self.whitelist = whitelist
        self.blacklist = blacklist
        self.trust_publishable = trust_publishable
        # In a project declared to hold no personal data, geo and attachment fields are
        # whitelisted, so they carry nothing.
        self.personal_types = set() if no_personal_data else _CONTAINS_TYPES
        # A zero-shot model reads date questions: its label says whether one is a
        # birth date.
        self.typed_safe = _TYPED_SAFE - {"date"} if read_dates else _TYPED_SAFE
        self._memo: dict[str, dict | None] = {}
        self._carried: dict[str, str | None] = {}

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
        if basis := self.personal(q["variable_name"]):
            return {"status": "contains_pii", "basis": [basis]}
        if qtype in _CONTAINS_TYPES:
            return {"status": "contains_pii", "basis": [f"type:{qtype}"]}
        if qtype in self.typed_safe:
            return {"status": "whitelisted", "basis": [f"type:{qtype}"]}
        if qtype in _SELECT_TYPES and "search(" not in (q["appearance"] or ""):
            return {"status": "whitelisted", "basis": ["choices:inline"]}
        # A pulldata() only tested for a match (empty(), not(), =) yields true or
        # false, not the dataset's value.
        holds = self._pulldata(q) and _holds_pulldata(q["calculation"])
        if qtype == "calculate" and not holds:
            refs = q["references"]
            # Reading no field and calling only functions that generate a value, it
            # holds nothing anyone entered: once(uuid(5)), today().
            calls = set(_CALL.findall(q["calculation"] or ""))
            if not refs and calls <= _GENERATED_CALLS:
                return {"status": "whitelisted", "basis": ["generated"]}
            if refs and all(r in self.by_name and self._safe(r) for r in refs):
                return {"status": "whitelisted", "basis": [f"lineage:{','.join(refs)}"]}
        return None

    def personal(self, name: str | None) -> str | None:
        """Return why question ``name`` captures collection metadata, else None: its
        type, or a function its calculation calls. A calculate reading such a field
        is not cleared by lineage, so it is scanned like any other text.
        """
        q = self.by_name.get(name)
        if q is None:
            return None
        if q["type"] in _METADATA_TYPES:
            return f"metadata:{q['type']}"
        calls = _METADATA_CALLS.search(q["calculation"] or "")
        if calls:
            return f"calls:{calls.group(1)}()"
        return None

    def carried(self, name: str | None) -> str | None:
        """Return a field a calculate's formula reads, directly or through other
        calculates, that is blacklisted, collection metadata or of a type that holds
        personal data (a geopoint, an image, ...), else None: ``selected-at(${gps},
        0)`` holds the latitude. A whitelisted field carries nothing. A
        ``pulldata()`` holds its dataset column, not its key: it carries the column
        when that is blacklisted.
        """
        q = self.by_name.get(name)
        if q is None or q["type"] != "calculate":
            return None
        source = q["data_source"]
        if source and source["kind"] == "pulldata":
            column = f"{source['dataset']}.{source['column']}"
            return column if column in self.blacklist else None
        if name not in self._carried:
            self._carried[name] = None  # a reference cycle carries nothing
            for ref in dict.fromkeys(_REF.findall(q["calculation"] or "")):
                if ref in self.whitelist:
                    continue
                found = (
                    ref
                    if ref in self.blacklist
                    or self.personal(ref)
                    or self.by_name.get(ref, {}).get("type") in self.personal_types
                    else self.carried(ref)
                )
                if found:
                    self._carried[name] = found
                    break
        return self._carried[name]

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
    for lang in analyzer.supported_languages:
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
    contract: FormContract,
    datasets: dict[str, Path],
    config: PiiConfig,
    analyzer,
    rows: list[dict],
) -> None:
    """Give each ``search()`` select its dataset choices when they hold no PII.

    All or nothing: a list over ``MAX_INLINE_CHOICES``, a blacklisted question, or any
    Presidio hit among the choices' values and labels withholds the whole list from
    the outputs (``node.choices_withheld`` says why) and the select keeps only its
    literal items. Withheld or not, ``node.choice_universe`` holds every choice, so
    the parquet's binaries are the same. Each records the value and label columns its
    item names (``node.choice_columns``). A whitelisted question gets its list
    unscanned. A filter on a field keeps the rows
    matching the values the field takes in ``rows`` (the submissions); a filter on
    anything but a plain field withholds the list.

    A blacklisted select with inline choices is withheld too (``reviewed``), its
    choices kept in ``node.choice_universe``: the blacklist says its list holds
    personal data, so the outputs refer to it instead of listing it.
    """
    submitted = SubmissionValues(contract, rows)
    tables: dict[str, DatasetTable] = {}
    for node in contract.nodes.values():
        source = node.data_source
        if not source or source["kind"] != "search":
            if node.choice_items and node.name in config.blacklist:
                node.choice_universe = node.choice_items
                node.choices_withheld = ["reviewed"]
                node.choice_items = []
            continue
        header = dataset_header(datasets[source["dataset"]])
        named = [i for i in node.choice_items if i["value"] in header]
        if named:
            node.choice_columns = {
                "value": named[0]["value"],
                "label": named[0]["label"],
            }
        literals = [i for i in node.choice_items if i not in named]
        fields = {
            column: submitted.of(source["match"][column])
            for column, value in source["filter"].items()
            if value is None
        }
        unread = [source["match"][c] for c, v in fields.items() if v is None]
        if unread:
            node.choices_withheld = [f"filtered by {e}" for e in unread]
            node.choice_items = literals
            continue
        dataset = source["dataset"]
        if dataset not in tables:
            tables[dataset] = DatasetTable(datasets[dataset])
        universe = search_universe(node, tables[dataset], fields)
        node.choice_universe = universe
        withheld = []
        if len(universe) > MAX_INLINE_CHOICES:
            withheld = [f"{len(universe)} choices"]
            node.choices_per_submission = True
        elif node.name in config.blacklist:
            withheld = ["reviewed"]
        elif node.name not in config.whitelist and not config.no_personal_data:
            texts = sorted({t for c in universe for t in (c["value"], c["label"]) if t})
            found = sorted(set().union(*_entities(texts, config, analyzer)))
            withheld = [f"presidio:{e}" for e in found]
        if withheld:
            node.choices_withheld = withheld
            node.choice_items = literals
        else:
            node.choice_items = universe


class _Scans:
    """Columns to scan, collected first so Presidio reads every distinct text once.

    Columns share many values ("0", "No", a date), so a form's columns are scanned
    together: ``add`` registers a column, ``run`` analyzes the union of their
    sampled distinct values, and ``block`` gives each column its result.
    """

    def __init__(self, config: PiiConfig):
        self.config = config
        self.columns: dict = {}
        self.found: dict[str, set[str]] = {}
        # zero_shot: each column's hits, by value.
        self.column_found: dict = {}

    def add(
        self,
        name,
        column: pa.ChunkedArray,
        keys: list,
        basis: list[str],
        context: str,
    ) -> None:
        """Register a column; ``context`` (its question label, or its name) leads
        every text the zero-shot model reads from it.
        """
        values = [None if v is None else str(v) for v in column.to_pylist()]
        distinct = sorted({v for v in values if v is not None})
        cap = self.config.max_distinct
        # The model decides a column of short values (names, places, codes) from a
        # sample of them; free text is read whole, since one answer may name someone.
        if self.config.detector in _COLUMNAR and not _free_text(distinct):
            cap = min(cap, _SHORT_SAMPLE)
        if len(distinct) > cap:
            distinct = sorted(random.Random(self.config.seed).sample(distinct, cap))
        self.columns[name] = (values, distinct, keys, basis, context)

    def run(self, analyzer) -> None:
        if self.config.detector in _COLUMNAR:
            for name, (_, distinct, _, _, context) in self.columns.items():
                self.column_found[name] = _column_entities(
                    context, distinct, self.config, analyzer
                )
            return
        texts = sorted(
            {t for _, distinct, _, _, _ in self.columns.values() for t in distinct}
            - set(self.found)
        )
        if texts:
            self.found.update(zip(texts, _entities(texts, self.config, analyzer)))

    def block(self, name) -> dict:
        values, distinct, keys, basis, _ = self.columns[name]
        if self.config.detector == "spacy":
            return _block(values, distinct, keys, basis, self.found)
        if name in self.column_found:
            found = _rare_hits(self.column_found[name], values, distinct)
        else:
            found = self.found
        return _block(values, distinct, keys, basis, found, _phone_shape(values))


def _rare_hits(found: dict[str, set[str]], values: list, distinct: list[str]):
    """Keep a zero-shot hit only on a value few rows share, unless the column is free
    text: a value on many rows ("Savings", a role) is a category the column's context
    made look personal, not someone's data.
    """
    if _free_text(distinct):
        return found
    rows = Counter(v for v in values if v is not None)
    frequent = max(_FREQUENT_ROWS, _FREQUENT_SHARE * sum(rows.values()))
    # Numbers that run in sequence are IDs the survey assigned, not identity papers.
    numbers = [v for v in rows if v.isdigit()]
    assigned = len(numbers) >= _MIN_PHONES and _assigned(numbers)
    return {
        v: set() if rows[v] >= frequent else hits - {"ID_NUMBER"} if assigned else hits
        for v, hits in found.items()
    }


def _context(q: dict | None, column: str) -> str:
    """Return what tells the model what a column holds: its question's label; for a
    calculate, which has none, the dataset column its ``pulldata()`` reads or its own
    name.
    """
    if q is None:
        return column
    source = q["data_source"]
    if q["type"] == "calculate":
        return source["column"] if source and source.get("column") else column
    return plain_label(q["question_text"] or "") or column


def _column_entities(
    context: str, distinct: list[str], config: PiiConfig, analyzer
) -> dict[str, set[str]]:
    """Return the entity types the zero-shot model finds in each of a column's values.

    The model reads the column, not single cells: each text is the column's context
    followed by as many of its values as fit in ``_CHUNK_CHARS``, so it judges each
    value next to the question and to other values of the same column. A hit counts
    for the value it falls in; one in the context counts for none.
    """
    if config.detector in ("gliner2", "gliner2_onnx"):
        wanted = ["EMAIL_ADDRESS", *config.id_patterns]
    elif config.detector == "spacy_column":
        wanted = [*config.entities, *config.id_patterns]
    else:
        wanted = [*_ZERO_SHOT_LABELS.values(), "EMAIL_ADDRESS", *config.id_patterns]
    found: dict[str, set[str]] = {v: set() for v in distinct}
    chunk: list[str] = []
    chunks = []
    for value in distinct:
        if (
            chunk
            and len(context) + sum(len(v) + 3 for v in [*chunk, value]) > _CHUNK_CHARS
        ):
            chunks.append(chunk)
            chunk = []
        chunk.append(value)
    if chunk:
        chunks.append(chunk)
    texts, spans = [], []
    for chunk in chunks:
        text, chunk_spans = f"{context}: ", []
        for value in chunk:
            chunk_spans.append((len(text), len(text) + len(value), value))
            text += value + " | "
        texts.append(text)
        spans.append(chunk_spans)

    def hit(chunk_spans, start, end, entity):
        for s, e, value in chunk_spans:
            if start < e and end > s and _shaped(entity, value):
                found[value].add(entity)

    for text, chunk_spans in zip(texts, spans, strict=True):
        for language in analyzer.supported_languages:
            for r in analyzer.analyze(
                text=text,
                language=language,
                entities=wanted,
                score_threshold=config.score_threshold,
            ):
                hit(chunk_spans, r.start, r.end, r.entity_type)
    if config.detector == "gliner2" and texts:
        results = analyzer.zero_shot.batch_extract_entities(
            texts,
            list(_GLINER2_LABELS),
            batch_size=8,
            threshold=_ZERO_SHOT_THRESHOLD,
            include_spans=True,
            include_confidence=True,
        )
        for result, chunk_spans in zip(results, spans, strict=True):
            for label, entities in result["entities"].items():
                entity = _GLINER2_LABELS[label]
                for e in entities:
                    if e["confidence"] >= _GLINER2_MIN.get(entity, 0):
                        hit(chunk_spans, e["start"], e["end"], entity)
    if config.detector == "gliner2_onnx":
        for text, chunk_spans in zip(texts, spans, strict=True):
            for e in analyzer.zero_shot.extract_entities(
                text, list(_GLINER2_ONNX_LABELS), threshold=_ZERO_SHOT_THRESHOLD
            ):
                entity = _GLINER2_ONNX_LABELS[e.label]
                if e.score >= _GLINER2_MIN.get(entity, 0):
                    hit(chunk_spans, e.start, e.end, entity)
    return found


def _phone_shape(values: list) -> set[str]:
    """Return a column's phone numbers when its values have their shape, else none.

    Most distinct values are strings of 6 to 12 digits, mostly of one length; they
    are close to one per row; a few leading 3-digit prefixes (the operators') cover
    most of them; and they are spread out, not runs of nearby numbers like the IDs a
    survey assigns.
    """
    present = [v for v in values if v is not None]
    numbers = sorted({v for v in present if _PHONE_DIGITS.fullmatch(v)})
    if len(numbers) < _MIN_PHONES or len(numbers) < 0.9 * len(set(present)):
        return set()
    length, same = Counter(map(len, numbers)).most_common(1)[0]
    rows = sum(1 for v in present if _PHONE_DIGITS.fullmatch(v))
    prefixes = Counter(v[:3] for v in numbers).most_common(_PHONE_PREFIXES)
    if (
        same < 0.8 * len(numbers)
        or len(numbers) < 0.5 * rows
        or sum(n for _, n in prefixes) < 0.8 * len(numbers)
        or _assigned(numbers)
    ):
        return set()
    return set(numbers)


def _assigned(numbers: list[str]) -> bool:
    """Return whether digit strings run in sequence, like the IDs a survey assigns:
    most of them sit within 10 of the next one up.
    """
    length = Counter(map(len, numbers)).most_common(1)[0][0]
    ints = sorted(int(v) for v in numbers if len(v) == length)
    nearby = sum(b - a <= 10 for a, b in zip(ints, ints[1:]))
    return nearby > 0.2 * (len(ints) - 1)


def _block(
    values: list,
    distinct: list[str],
    keys: list,
    basis: list[str],
    entities: dict,
    phones: set[str] = frozenset(),
) -> dict:
    found: dict[str, set[str]] = {}
    for value in distinct:
        for t in entities[value]:
            found.setdefault(t, set()).add(value)
    hits = set().union(*found.values(), phones)
    hit_keys = sorted({k for k, v in zip(keys, values, strict=True) if v in hits})
    return {
        "status": "may_contain_pii" if found or phones else "no_hits",
        "basis": basis
        + [f"presidio:{t}" for t in sorted(found)]
        + (["shape:phone"] if phones else []),
        "presidio": {
            "entities": {t: len(found[t]) for t in sorted(found)},
            "distinct_scanned": len(distinct),
            "hit_rows": len(hit_keys),
            "hit_keys": hit_keys[:_MAX_HIT_KEYS],
        },
    }


def classify(
    table: pa.Table,
    vardict: dict,
    questions: list[dict],
    config: PiiConfig,
    analyzer,
    dataset_pii: dict[str, dict],
) -> dict[str, dict]:
    """Return the PII block for every column of ``table``.

    ``dataset_pii`` holds each ``dataset.column`` block of the datasets the form reads
    (``dataset_codebook``); a ``pulldata()`` calculate takes a flag from its column.
    """
    variables = vardict["variables"]
    question_names = {q["variable_name"] for q in questions}
    whitelist, blacklist = set(config.whitelist), set(config.blacklist)
    # The blacklist keeps its dataset columns (dataset.column): a pulldata() of one
    # carries it.
    rules = _Rules(
        questions,
        whitelist & question_names,
        blacklist,
        config.trust_publishable,
        config.no_personal_data,
        config.detector in _COLUMNAR,
    )
    keys = table.column("KEY").to_pylist()

    blocks = {}
    scans = _Scans(config)
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
        elif basis := rules.personal(question):
            block = {"status": "contains_pii", "basis": [basis]}
        elif carried := rules.carried(question):
            block = {"status": "contains_pii", "basis": [f"lineage:{carried}"]}
        elif entry.get("system"):
            block = {"status": "whitelisted", "basis": ["system"]}
        elif config.no_personal_data:
            block = {"status": "whitelisted", "basis": ["project"]}
        else:
            block = rules.static(question) if question is not None else None
            held = _held_column(q, dataset_pii) if config.detector != "spacy" else None
            if block is None and held:
                # Its values are the dataset column's, already scanned.
                status = dataset_pii[held]["status"]
                block = {
                    "status": status if status in FLAGGED else "no_hits",
                    "basis": [*rules.scan_basis(question), f"lineage:{held}"],
                }
            elif block is None:
                scans.add(
                    column,
                    table.column(column),
                    keys,
                    rules.scan_basis(question),
                    _context(q, column),
                )
                continue
            if q is not None and q["publishable"]:
                block = {**block, "basis": [*block["basis"], "publishable"]}
        blocks[column] = block
    scans.run(analyzer)
    for column in scans.columns:
        block = scans.block(column)
        if (q := rules.by_name.get(variables[column]["question"])) and q["publishable"]:
            block = {**block, "basis": [*block["basis"], "publishable"]}
        blocks[column] = block
    if config.detector != "spacy":
        _inherit(blocks, variables, rules, dataset_pii)
    return {column: blocks[column] for column in table.schema.names}


def _holds_pulldata(calculation: str) -> bool:
    """Return whether a ``pulldata()`` result can be the calculation's value: the call
    itself, or a branch of ``coalesce()`` or ``if()``. A result passed to ``empty()``,
    ``not()``, ``string-length()`` or a comparison only yields a test.
    """
    for call in _PULLDATA.finditer(calculation):
        depth, end = 0, call.end() - 1
        for end in range(call.end() - 1, len(calculation)):
            depth += {"(": 1, ")": -1}.get(calculation[end], 0)
            if depth == 0:
                break
        tested = _TESTS.search(calculation[: call.start()])
        compared = _COMPARISON.match(calculation[end + 1 :])
        if not tested and not compared:
            return True
    return False


def _held_column(q: dict | None, dataset_pii: dict) -> str | None:
    """Return the ``dataset.column`` whose value calculate ``q`` holds through its
    ``pulldata()``, when that column was classified, else None.
    """
    if q is None or q["type"] != "calculate":
        return None
    source = q["data_source"]
    if not source or source["kind"] != "pulldata":
        return None
    held = f"{source['dataset']}.{source['column']}"
    if held not in dataset_pii or not _holds_pulldata(q["calculation"]):
        return None
    return held


def _inherit(
    blocks: dict[str, dict], variables: dict, rules: _Rules, dataset_pii: dict
) -> None:
    """Flag each ``no_hits`` calculate that holds a flagged value: the dataset column
    its ``pulldata()`` reads, or a field its formula reads, directly or through other
    calculates (basis ``lineage:<name>``).

    A ``pulldata()`` returns the column's value, and a form that reads and publishes
    the same dataset stores it back, so the field, the column and every
    ``pulldata()`` of it are one value with one status. Lineage only adds flags.
    """
    columns: dict[str, list[str]] = {}
    for column in blocks:
        columns.setdefault(variables[column]["question"], []).append(column)

    def flagged(name: str) -> bool:
        return any(blocks[c]["status"] in FLAGGED for c in columns.get(name, []))

    changed = True
    while changed:
        changed = False
        for name, cols in columns.items():
            q = rules.by_name.get(name)
            if q is None or q["type"] != "calculate":
                continue
            source = q["data_source"]
            if source and source["kind"] == "pulldata":
                if not _holds_pulldata(q["calculation"]):
                    continue
                held = f"{source['dataset']}.{source['column']}"
                # A column the dataset lacks makes pulldata() return "": it holds
                # nothing from the dataset.
                if held not in dataset_pii:
                    continue
                status = dataset_pii[held]["status"]
            else:
                held = next(
                    (r for r in _REF.findall(q["calculation"] or "") if flagged(r)),
                    None,
                )
                status = "may_contain_pii"
            if held is None or status not in FLAGGED:
                continue
            for c in cols:
                if blocks[c]["status"] == "no_hits":
                    blocks[c] = {
                        **blocks[c],
                        "status": status,
                        "basis": [*blocks[c]["basis"], f"lineage:{held}"],
                    }
                    changed = True


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
    tables = {}
    for dataset, csv_path in sorted(datasets.items()):
        table = DatasetTable(csv_path)
        tables[dataset] = (csv_path, table.header, table.rows)
    # Every column not yet scanned for another form, scanned together.
    scans = _Scans(config)
    for dataset, (csv_path, header, records) in tables.items():
        for column in header:
            name = f"{dataset}.{column}"
            if (
                name in blacklist
                or name in whitelist
                or config.no_personal_data
                or (csv_path, column) in scanned
            ):
                continue
            values = [r[column] or None for r in records]
            scans.add(
                (csv_path, column),
                pa.chunked_array([pa.array(values, pa.string())]),
                list(range(1, len(records) + 1)),
                ["dataset"],
                column,
            )
    scans.run(analyzer)
    for key in scans.columns:
        scanned[key] = scans.block(key)

    rows = []
    for dataset, (csv_path, header, records) in tables.items():
        for column in header:
            name = f"{dataset}.{column}"
            values = [r[column] or None for r in records]
            if name in blacklist:
                block = {"status": "contains_pii", "basis": ["reviewed"]}
            elif name in whitelist:
                block = {"status": "whitelisted", "basis": ["reviewed"]}
            elif config.no_personal_data:
                block = {"status": "whitelisted", "basis": ["project"]}
            else:
                block = scanned[csv_path, column]
            read_by = [
                q["variable_name"]
                for q in questions
                if q["data_source"]
                and q["data_source"]["dataset"] == dataset
                and column
                in [q["data_source"].get("column")]
                + q["data_source"].get("columns", [])
                + [m["column"] for m in q["data_source"]["match"]]
            ]
            lengths = [len(v) for v in values if v is not None]
            rows.append(
                {
                    "dataset": dataset,
                    "column": column,
                    "rows": len(records),
                    "non_null": sum(v is not None for v in values),
                    "distinct": len({v for v in values if v is not None}),
                    # Many values at exactly the longest length can mean a limit cut
                    # them when the dataset was written.
                    "max_length": max(lengths, default=0),
                    "at_max_length": lengths.count(max(lengths, default=0))
                    if lengths
                    else 0,
                    "read_by": read_by,
                    "pii": block,
                }
            )
    return rows


def warn_unknown_names(config: PiiConfig, known: set[str]) -> None:
    """Warn for each whitelist or blacklist name that no form or dataset of the project has."""
    for name in sorted((set(config.whitelist) | set(config.blacklist)) - known):
        warnings.warn(f"`{name}` cannot be found", UserWarning, stacklevel=2)


def attach(
    vardict: dict, blocks: dict[str, dict], questions: list[dict], codebook: list[dict]
) -> None:
    """Add each column's block to the dictionary and a status count to its summary.

    A column whose values are not shown (``shows_values``) loses its range
    (``data_min``, ``data_max``). The codes a withheld list lacks are only counted
    when its codes are not shown (``survey.code_reasons``): its list is withheld for
    what it holds, or a column of its select or its dataset's value column is flagged.
    """
    variables = vardict["variables"]
    for column, block in blocks.items():
        variables[column]["pii"] = block
        if not shows_values(block["status"]):
            for key in ("data_min", "data_max"):
                variables[column].pop(key, None)
    vardict["summary"]["pii"] = dict(Counter(b["status"] for b in blocks.values()))
    by_name = {q["variable_name"]: q for q in questions}
    for name, picked in vardict["summary"]["withheld_choices"].items():
        if code_reasons(by_name[name], variables, codebook):
            del picked["codes_not_in_list"]
