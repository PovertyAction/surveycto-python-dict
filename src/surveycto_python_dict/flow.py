"""How a project's forms chain through datasets: ``flow.json``.

The XML says which datasets a form reads (``pulldata()``, ``search()``), and the form
list which ones it attaches and which enumerator dataset its enumerator fields pick
from. Neither says which form publishes to a dataset, so that
is read off the columns: a form publishes a dataset when nearly every dataset column
(``NEAR``) is one of the form's fields, by the field's own name or by its wide column
(``score_2``). A field that copies a column of the dataset under the column's name
(``village = pulldata('households', 'village', ...)``) does not count: the form reads
that dataset. The columns no field explains are ``legacy_columns``: fields the form's
current version no longer has, or columns added on the server. A field published from
a repeat gives one dataset row per instance, under its own name. When several forms
cover a dataset, its readers drop out (a form that re-asks the fields it reads); two
left raise, and the config's ``[datasets.<id>] published_by`` settles it. A dataset no
form covers is external (uploaded, or published by a form outside the project);
``closest`` names the form covering most of its columns.

A form's stage is 1 when it reads nothing another project form publishes, else one
more than the latest stage it reads from; a form reading its own dataset doesn't count.

Each published column gets the origin of the field behind it: ``entered`` (a question
the enumerator answers, a ``search()`` pick included), ``metadata`` (a preload such as
the username), ``carried`` (a calculate that traces back to a ``pulldata()`` from
another dataset) or ``calculated`` (any other calculate).

``keys`` lists the columns that can identify an entity: each dataset's key and each
column a ``pulldata()`` matches on (a ``search()`` filter selects a set of rows, so it
identifies nothing), with the forms (and the table in each) and datasets that carry it. ``project`` holds what the user says the project is about:
a description and named entities, each on one or more columns (``describe_project``).
"""

import csv
import re
from collections import Counter
from pathlib import Path

from surveycto_python_dict.contract import DatasetSource, label_columns
from surveycto_python_dict.xml_contract import ENUMERATOR_COLUMNS, FormContract

_QUOTED = re.compile(r"'[^']*'|\"[^\"]*\"")

# The share of a dataset's columns a form's fields must cover to publish it.
NEAR = 0.9


def _header(path: Path) -> tuple[list[str], int]:
    """Return a CSV's columns and its number of data rows."""
    with open(path, encoding="utf-8-sig", newline="") as f:
        reader = csv.reader(f)
        columns = next(reader)
        return columns, sum(1 for _ in reader)


def _field(contract: FormContract, column: str) -> str | None:
    """Return the path of the field a dataset column publishes, if ``contract`` has one.

    A field's own name comes first: the wide-column reading of a repeat field's bare
    name (``plot_size``) would be the repeat ``plot`` with a key ``size``.
    """
    paths = [n.path for n in contract.nodes.values() if n.name == column]
    if len(paths) == 1:
        return paths[0]
    m = contract.map_column(column)
    if m["kind"] != "matched" or "string_key" in m or m["choice_code"] is not None:
        return None
    return m["node_path"]


def _copies(
    contract: FormContract, sources: dict[str, DatasetSource], dataset_id: str
) -> set[str]:
    """Return the paths of the fields that copy a column of server dataset
    ``dataset_id`` under the column's own name, through ``pulldata()``.
    """
    copies = set()
    for node in contract.nodes.values():
        source = node.data_source
        if not source or source["kind"] != "pulldata":
            continue
        read = sources[source["dataset"]]
        if (
            read.kind == "server_dataset"
            and read.ref == dataset_id
            and source["value"] in (f"'{node.name}'", f'"{node.name}"')
        ):
            copies.add(node.path)
    return copies


def _origins(contract: FormContract) -> dict[str, tuple[str, set[str]]]:
    """Return each field's origin and, when carried, the datasets it comes from."""
    path_re = re.compile(rf"/{re.escape(contract.formid)}/([\w.-]+(?:/[\w.-]+)*)")
    origins: dict[str, tuple[str, set[str]]] = {}

    def origin(path: str, visiting: frozenset) -> tuple[str, set[str]]:
        if path in origins:
            return origins[path]
        node = contract.nodes[path]
        source = node.data_source
        if node.preload:
            found = ("metadata", set())
        elif source and source["kind"] == "pulldata":
            found = ("carried", {source["dataset"]})
        elif node.calculate:
            refs = {
                r
                for r in path_re.findall(node.calculate)
                if r in contract.nodes and r != path and r not in visiting
            }
            inputs = [origin(r, visiting | {path}) for r in sorted(refs)]
            carried = set().union(*(f for k, f in inputs if k == "carried"))
            found = ("carried", carried) if carried else ("calculated", set())
        else:
            found = ("entered", set())
        origins[path] = found
        return found

    for path in contract.nodes:
        origin(path, frozenset())
    return origins


def _reads(contract: FormContract, name: str, columns: list[str]) -> list[dict]:
    """Return each field of ``contract`` that reads dataset ``name``, with its columns.

    ``missing`` lists the columns it names that the dataset lacks; ``computed`` is a
    ``pulldata()`` column given as an expression (``concat('score_', index())``);
    ``choices`` are the columns a ``search()`` select takes its values and labels from,
    a filter column among them. ``value`` is the column whose value the field stores:
    the column a ``pulldata()`` returns, or the value column a ``search()`` item names
    (its labels are only shown); None for a computed ``pulldata()`` column and for an
    enumerator field, which stores two columns as "name (id)".
    """
    fields = []
    for node in contract.nodes.values():
        source = node.data_source
        if not source or source["dataset"] != name:
            continue
        computed, choices, stored = None, [], None
        if source["kind"] == "enumerator":
            match, used = [], list(ENUMERATOR_COLUMNS)
        elif source["kind"] == "pulldata":
            match = [source["key"]]
            value = source["value"]
            if _QUOTED.fullmatch(value):
                stored = value[1:-1]
                used = [stored, source["key"]]
            else:
                computed, used = value, [source["key"]]
        else:
            match = list(source["filter"])
            named = [i for i in node.choice_items if i["value"] in columns]
            stored = named[0]["value"] if named else None
            items = [
                v
                for i in node.choice_items
                for v in (i["value"], *label_columns(i["label"]))
            ]
            choices = list(dict.fromkeys(c for c in items if c in columns))
            used = [*source["filter"], *choices]
        used = list(dict.fromkeys(used))
        fields.append(
            {
                "field": node.path,
                "via": source["kind"],
                "columns": used,
                "match": match,
                "missing": [c for c in used if c not in columns],
                "computed": computed,
                "choices": choices,
                "value": stored,
            }
        )
    return fields


def _where(
    contracts: dict[str, FormContract], datasets: list[dict], column: str
) -> dict:
    """Return the forms (with the table in each) and datasets that carry ``column``."""
    forms = {}
    for form, contract in contracts.items():
        tables = sorted(
            {
                n.repeat_path[-1] if n.repeat_path else "submissions"
                for n in contract.nodes.values()
                if n.name == column
            }
        )
        if tables:
            forms[form] = tables
    return {
        "forms": forms,
        "datasets": [d["id"] for d in datasets if column in d["columns"]],
    }


def _keys(contracts: dict[str, FormContract], datasets: list[dict], reads) -> list:
    """Return the columns that can identify an entity, the most widely carried first."""
    key_of: dict[str, list[str]] = {}
    for d in datasets:
        if d["key"]:
            key_of.setdefault(d["key"], []).append(d["id"])
    # A pulldata() key finds one row; a search() filter selects a set of rows, so its
    # columns identify nothing.
    matched: dict[str, int] = {}
    for r in reads:
        for f in r["fields"]:
            if f["via"] != "pulldata":
                continue
            for column in f["match"]:
                matched[column] = matched.get(column, 0) + 1
    keys = [
        {
            "column": column,
            "key_of": key_of.get(column, []),
            "matched_by": matched.get(column, 0),
            **_where(contracts, datasets, column),
        }
        for column in sorted(set(key_of) | set(matched))
    ]
    return sorted(keys, key=lambda k: -(len(k["forms"]) + len(k["datasets"])))


def describe_project(
    flow: dict,
    contracts: dict[str, FormContract],
    description: str | None,
    entities: dict[str, dict],
) -> dict:
    """Return the ``project`` section: the user's description and their entities.

    Each entity names the columns that identify it, together; a column no form or
    dataset carries raises, and so do columns no form or dataset carries together and
    a parent that is not another entity. ``forms`` and ``datasets`` are where the
    columns are carried together, with each form's tables.
    """
    described = []
    for name, entity in entities.items():
        wheres = []
        for column in entity["key"]:
            where = _where(contracts, flow["datasets"], column)
            if not where["forms"] and not where["datasets"]:
                raise ValueError(
                    f"entity {name!r}: no form or dataset has a column {column!r}"
                )
            wheres.append(where)
        forms = set.intersection(*(set(w["forms"]) for w in wheres))
        where = {
            "forms": {
                form: sorted({t for w in wheres for t in w["forms"][form]})
                for form in wheres[0]["forms"]
                if form in forms
            },
            "datasets": [
                d
                for d in wheres[0]["datasets"]
                if all(d in w["datasets"] for w in wheres)
            ],
        }
        if not where["forms"] and not where["datasets"]:
            raise ValueError(
                f"entity {name!r}: no form or dataset has all of "
                f"{', '.join(entity['key'])}"
            )
        parent = entity.get("parent")
        if parent is not None and parent not in entities:
            raise ValueError(f"entity {name!r}: parent {parent!r} is not an entity")
        described.append(
            {
                "name": name,
                "key": entity["key"],
                "description": entity["description"],
                "parent": parent,
                **where,
            }
        )
    return {"description": description, "entities": described}


def _publisher(node: dict, share: dict[str, int], named: str | None) -> str | None:
    """Return the form that publishes dataset ``node``, or None when none does.

    ``share`` counts the dataset columns that are fields of each form; ``named`` is
    the config's ``[datasets.<id>] published_by``. Without it, the forms covering at
    least ``NEAR`` of the columns are candidates; when several are, the dataset's
    readers drop out (a form that re-asks the fields it reads), unless that leaves
    none. The candidate covering the most columns publishes; a tie raises.
    """
    dataset = node["id"]
    if named is not None:
        if named not in share:
            raise ValueError(f"[datasets.{dataset}] published_by: no form {named!r}")
        if not share[named]:
            raise ValueError(
                f"[datasets.{dataset}] published_by: no column is a field of {named}"
            )
        return named
    n = len(node["columns"])
    near = [f for f in share if share[f] >= NEAR * n]
    if len(near) > 1:
        near = [f for f in near if f not in node["read_by"]] or near
    if not near:
        return None
    top = max(share[f] for f in near)
    best = [f for f in near if share[f] == top]
    if len(best) > 1:
        raise ValueError(
            f"dataset {dataset!r}: {top} of its {n} columns are fields of each of "
            f"{', '.join(best)}; name the publisher in [datasets.{dataset}] published_by"
        )
    return best[0]


def build_flow(
    forms: dict[str, tuple[FormContract, dict[str, DatasetSource]]],
    server: list[dict],
    listed: tuple[str, ...],
    datasets_dir: Path,
    files_dirs: dict[str, Path],
    published_by: dict[str, str] | None = None,
) -> dict:
    """Return the project's forms, datasets and the edges between them.

    ``forms`` maps each form to its contract and resolved sources; ``server`` is the
    server's dataset list; ``listed`` are the datasets the config names. Server datasets
    are read from ``datasets_dir/<id>.csv``, attached files from ``files_dirs[form]``.
    ``published_by`` names a dataset's publisher where the columns don't tell.
    """
    published_by = published_by or {}
    meta = {d["id"]: d for d in server}
    datasets: dict[str, dict] = {}
    reads = []

    def server_node(dataset_id: str) -> dict:
        if dataset_id not in datasets:
            columns, rows = _header(datasets_dir / f"{dataset_id}.csv")
            m = meta[dataset_id]
            datasets[dataset_id] = {
                "id": dataset_id,
                "kind": "server_dataset",
                "title": m["title"],
                "key": m["uniqueRecordField"],
                "rows": rows,
                "columns": columns,
                "published_by": None,
                "publish_map": {},
                "origins": {},
                "origin_counts": {},
                "legacy_columns": [],
                "closest": None,
                "read_by": [],
            }
        return datasets[dataset_id]

    for dataset_id in listed:
        server_node(dataset_id)
    for form, (contract, sources) in forms.items():
        for name, source in sources.items():
            if source.kind == "server_dataset":
                node = server_node(source.ref)
            else:
                columns, rows = _header(files_dirs[form] / source.ref)
                node = datasets.setdefault(
                    f"{form}/{source.ref}",
                    {
                        "id": f"{form}/{source.ref}",
                        "kind": "form_file",
                        "title": source.ref,
                        "key": None,
                        "rows": rows,
                        "columns": columns,
                        "published_by": None,
                        "publish_map": {},
                        "origins": {},
                        "origin_counts": {},
                        "legacy_columns": [],
                        "closest": None,
                        "read_by": [],
                    },
                )
            node["read_by"].append(form)
            reads.append(
                {
                    "form": form,
                    "dataset": node["id"],
                    "name_in_form": name,
                    "attached": source.attached,
                    "fields": _reads(contract, name, node["columns"]),
                }
            )

    origin_of = {form: _origins(contract) for form, (contract, _) in forms.items()}
    for node in datasets.values():
        if node["kind"] != "server_dataset":
            continue
        covering = {
            form: {c: _field(contract, c) for c in node["columns"]}
            for form, (contract, _) in forms.items()
        }
        # A copy of the dataset's own column reads it, so a form preloading the
        # dataset does not cover it.
        copies = {
            form: _copies(contract, sources, node["id"])
            for form, (contract, sources) in forms.items()
        }
        share = {
            f: sum(1 for p in m.values() if p and p not in copies[f])
            for f, m in covering.items()
        }
        form = _publisher(node, share, published_by.get(node["id"]))
        if form is None:
            closest = max(share, key=share.get, default=None)
            if closest is not None and share[closest]:
                node["closest"] = {"form": closest, "columns": share[closest]}
            continue
        mapped = {c: p for c, p in covering[form].items() if p}
        node["published_by"] = form
        node["publish_map"] = mapped
        node["legacy_columns"] = [c for c in node["columns"] if c not in mapped]
        origins = origin_of[form]
        node["origins"] = {
            column: {"kind": origins[path][0], "from": sorted(origins[path][1])}
            for column, path in mapped.items()
        }
        node["origin_counts"] = dict(
            Counter(o["kind"] for o in node["origins"].values())
        )

    unknown = sorted(
        set(published_by)
        - {d["id"] for d in datasets.values() if d["kind"] == "server_dataset"}
    )
    if unknown:
        raise ValueError(
            f"[datasets.<id>] published_by: no server dataset {', '.join(unknown)} "
            "is read or listed"
        )
    publisher = {d["id"]: d["published_by"] for d in datasets.values()}
    upstream = {
        form: sorted(
            {
                publisher[r["dataset"]]
                for r in reads
                if r["form"] == form and publisher[r["dataset"]] not in (None, form)
            }
        )
        for form in forms
    }
    stages: dict[str, int] = {}

    def stage(form: str, path: tuple[str, ...]) -> int:
        if form in path:
            raise ValueError(
                "forms publish to each other in a cycle: "
                + " -> ".join(path[path.index(form) :] + (form,))
            )
        if form not in stages:
            stages[form] = 1 + max(
                (stage(u, (*path, form)) for u in upstream[form]), default=0
            )
        return stages[form]

    return {
        "forms": [
            {
                "form_id": form,
                "stage": stage(form, ()),
                "reads_from": upstream[form],
                "publishes": [
                    d["id"] for d in datasets.values() if d["published_by"] == form
                ],
            }
            for form in forms
        ],
        "datasets": list(datasets.values()),
        "reads": reads,
        "keys": _keys(
            {form: contract for form, (contract, _) in forms.items()},
            list(datasets.values()),
            reads,
        ),
    }
