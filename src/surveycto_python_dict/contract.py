"""A form's form-list entry and the datasets its XML references.

The contract itself is ``xml_contract.parse_contract``; ``Node.publishable`` carries the
SurveyCTO ``publishable`` bind attribute.
"""

import csv
import json
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

from surveycto_python_dict.xml_contract import FormContract, Node


class UnresolvedDataset(LookupError):
    """The XML references a dataset that is neither a server dataset nor a form file."""


@dataclass(frozen=True)
class FormEntry:
    """One form's ``/formList`` entry: its server datasets and attached files."""

    form_id: str
    version: str
    dataset_ids: set[str]
    filenames: set[str]


@dataclass(frozen=True)
class DatasetSource:
    """Where a referenced dataset is pulled from: ``pull_dataset`` or ``pull_form_files``."""

    kind: str  # "server_dataset" | "form_file"
    ref: str  # dataset id | attached filename


def _ln(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def form_entry(form_list_xml: bytes, form_id: str) -> FormEntry:
    """Return ``form_id``'s entry from the ``/formList`` XML.

    SurveyCTO puts ``datasets`` and ``files`` in the entry as JSON text
    (``{"datasets": [{"id": ...}]}``, ``{"files": [{"filename": ...}]}``);
    ``datasets`` is empty when the form uses none.
    """
    for xform in ET.fromstring(form_list_xml).iter():
        if _ln(xform.tag) != "xform":
            continue
        fields = {_ln(c.tag): (c.text or "").strip() for c in xform}
        if fields["formID"] != form_id:
            continue
        datasets = (
            json.loads(fields["datasets"])["datasets"] if fields["datasets"] else []
        )
        files = json.loads(fields["files"])["files"]
        return FormEntry(
            form_id=form_id,
            version=fields["version"],
            dataset_ids={d["id"] for d in datasets},
            filenames={f["filename"] for f in files},
        )
    raise KeyError(f"form {form_id!r} is not in the form list")


def search_universe(node: Node, csv_path: Path) -> list[dict]:
    """Return a ``search()`` select's choices from its dataset, one per value.

    The select's one ``<item>`` whose value is a dataset column names the value and
    label columns; any other item is a literal choice, kept after the dataset's. A
    literal ``matches`` filter keeps the matching rows; a filter on a field keeps
    every row, since it is decided per submission.
    """
    with open(csv_path, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
    (columns,) = [i for i in node.choice_items if i["value"] in reader.fieldnames]
    literals = [i for i in node.choice_items if i is not columns]
    source = node.data_source
    static = {c: v for c, v in source["filter"].items() if v is not None}
    if static and source["mode"] != "matches":
        raise ValueError(
            f"{node.path}: search() mode {source['mode']!r} with a literal filter"
        )
    choices: dict[str, str] = {}
    for row in rows:
        if all(row[c] == v for c, v in static.items()):
            choices.setdefault(row[columns["value"]], row[columns["label"]])
    return [{"value": v, "label": label} for v, label in choices.items()] + literals


def resolve_datasets(
    contract: FormContract, entry: FormEntry
) -> dict[str, DatasetSource]:
    """Map every dataset named in a ``pulldata()``/``search()`` to where it is pulled from.

    The compiled XML lowercases a dataset name while the attached file keeps its case
    (``search('prices')`` reads ``Prices.csv``), so files match case-insensitively.
    """
    names = {n.data_source["dataset"] for n in contract.nodes.values() if n.data_source}
    resolved = {}
    for name in sorted(names):
        files = [f for f in entry.filenames if f.lower() == f"{name}.csv".lower()]
        if name in entry.dataset_ids:
            resolved[name] = DatasetSource("server_dataset", name)
        elif len(files) == 1:
            resolved[name] = DatasetSource("form_file", files[0])
        else:
            raise UnresolvedDataset(
                f"dataset {name!r} is referenced by form {entry.form_id!r} but is neither "
                f"a server dataset nor an attached file ({name}.csv)"
            )
    return resolved
