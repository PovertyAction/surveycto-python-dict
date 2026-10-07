"""How a project's forms chain through datasets: ``flow.json``.

The XML says which datasets a form reads (``pulldata()``, ``search()``), and the form
list which ones it attaches. Neither says which form publishes to a dataset, so that
is read off the columns: a form publishes a dataset when every dataset column is one
of the form's fields, by the field's own name or by its wide column (``score_2``). A
field published from a repeat gives one dataset row per instance, under its own name.
Two forms that both cover a dataset raise; a dataset no form covers is external
(uploaded, or published by a form outside the project).

A form's stage is 1 when it reads nothing another project form publishes, else one
more than the latest stage it reads from; a form reading its own dataset doesn't count.
"""

import csv
from pathlib import Path

from surveycto_python_dict.contract import DatasetSource
from surveycto_python_dict.xml_contract import FormContract


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


def _reads(contract: FormContract, name: str, columns: list[str]) -> list[dict]:
    """Return each field of ``contract`` that reads dataset ``name``, with its columns."""
    fields = []
    for node in contract.nodes.values():
        source = node.data_source
        if not source or source["dataset"] != name:
            continue
        if source["kind"] == "pulldata":
            used = [source["value"].strip("'\""), source["key"]]
        else:
            items = [v for i in node.choice_items for v in (i["value"], i["label"])]
            used = [*source["filter"], *(c for c in items if c in columns)]
        fields.append(
            {
                "field": node.path,
                "via": source["kind"],
                "columns": list(dict.fromkeys(used)),
            }
        )
    return fields


def build_flow(
    forms: dict[str, tuple[FormContract, dict[str, DatasetSource]]],
    server: list[dict],
    listed: tuple[str, ...],
    datasets_dir: Path,
    files_dirs: dict[str, Path],
) -> dict:
    """Return the project's forms, datasets and the edges between them.

    ``forms`` maps each form to its contract and resolved sources; ``server`` is the
    server's dataset list; ``listed`` are the datasets the config names. Server datasets
    are read from ``datasets_dir/<id>.csv``, attached files from ``files_dirs[form]``.
    """
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

    for node in datasets.values():
        if node["kind"] != "server_dataset":
            continue
        covering = {
            form: {c: _field(contract, c) for c in node["columns"]}
            for form, (contract, _) in forms.items()
        }
        publishers = [f for f, m in covering.items() if all(m.values())]
        if len(publishers) > 1:
            raise ValueError(
                f"dataset {node['id']!r}: every column is a field of each of "
                f"{', '.join(publishers)}; which one publishes it cannot be told"
            )
        if publishers:
            node["published_by"] = publishers[0]
            node["publish_map"] = covering[publishers[0]]

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
                f"forms publish to each other in a cycle: {' -> '.join(path)}"
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
    }
