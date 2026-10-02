"""A form's form-list entry and the datasets its XML references.

The contract itself is upstream ``parse_contract``; ``Node.publishable`` carries the
SurveyCTO ``publishable`` bind attribute.
"""

import json
import xml.etree.ElementTree as ET
from dataclasses import dataclass

from surveycto_extractor.parsers.xml_contract import FormContract


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


def resolve_datasets(
    contract: FormContract, entry: FormEntry
) -> dict[str, DatasetSource]:
    """Map every dataset named in a ``pulldata()``/``search()`` to where it is pulled from."""
    names = {n.data_source["dataset"] for n in contract.nodes.values() if n.data_source}
    resolved = {}
    for name in sorted(names):
        if name in entry.dataset_ids:
            resolved[name] = DatasetSource("server_dataset", name)
        elif f"{name}.csv" in entry.filenames:
            resolved[name] = DatasetSource("form_file", f"{name}.csv")
        else:
            raise UnresolvedDataset(
                f"dataset {name!r} is referenced by form {entry.form_id!r} but is neither "
                f"a server dataset nor an attached file ({name}.csv)"
            )
    return resolved
