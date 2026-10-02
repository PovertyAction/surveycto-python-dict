"""Cast all-string wide JSON rows into a typed Arrow table through the form contract.

Empty strings and absent keys are null. A select_multiple comes as per-choice
binaries present only when chosen: within one field (and one repeat instance), an
unchosen binary is 0 when any sibling was chosen, else null; every choice gets its
column. Any value that does not cast raises ``CastError`` naming column and KEY; the
value itself is shown only for publishable fields and non-identifying system columns.
"""

import datetime as dt
import json
from dataclasses import dataclass

import pyarrow as pa

from surveycto_python_dict.xml_contract import (
    FormContract,
    Node,
    is_system_column,
    sanitize_choice_value,
)

_TIMESTAMP = pa.timestamp("ms", tz="UTC")
_SYSTEM_TIMESTAMPS = {"SubmissionDate", "CompletionDate", "starttime", "endtime"}
_SYSTEM_INTS = {"duration"}
# System columns whose values identify no one; cast errors may print them.
_SYSTEM_SHOWN = _SYSTEM_TIMESTAMPS | _SYSTEM_INTS | {"formdef_version", "review_status"}

_XML_TYPES = {
    "int": pa.int64(),
    "decimal": pa.float64(),
    "date": pa.date32(),
    "dateTime": _TIMESTAMP,
    "string": pa.string(),
    "select1": pa.string(),  # codes stay strings: "01" != "1"
    "select": pa.string(),
    "binary": pa.string(),  # attachment URL
}


class CastError(ValueError):
    """A wide-JSON value or column does not fit the form contract."""


@dataclass
class _Column:
    name: str
    arrow_type: pa.DataType
    shown: bool  # cast errors may print the value
    metadata: dict[str, str]
    sort_key: tuple
    binary_group: tuple | None = (
        None  # (node path, repeat iterations) of a choice binary
    )


def _parse_timestamp(value: str) -> dt.datetime:
    # The REST API returns UTC, e.g. "Mar 7, 2025 4:06:12 PM".
    return dt.datetime.strptime(value, "%b %d, %Y %I:%M:%S %p").replace(
        tzinfo=dt.timezone.utc
    )


def _parse_date(value: str) -> dt.date:
    return dt.datetime.strptime(value, "%b %d, %Y").date()


def _parse_binary(value: str) -> int:
    if value != "1":
        raise ValueError("a select_multiple choice binary is '1' when chosen")
    return 1


_PARSERS = {
    pa.int64(): int,
    pa.float64(): float,
    pa.date32(): _parse_date,
    _TIMESTAMP: _parse_timestamp,
    pa.string(): str,
    pa.int8(): _parse_binary,
}


def _node_metadata(node: Node) -> dict[str, str]:
    return {
        "xml_type": node.xml_type or "",
        "node_path": node.path,
        "repeat_path": json.dumps(node.repeat_path),
    }


def _choice_index(node: Node, code) -> int:
    values = [str(c["value"]) for c in node.choice_items]
    if str(code) not in values:
        raise CastError(
            f"node {node.path!r}: choice code {code!r} is not among its choices"
        )
    return values.index(str(code))


def _binary_name(node: Node, code: str, iterations: tuple[int, ...]) -> str:
    return f"{node.name}_{sanitize_choice_value(code)}" + "".join(
        f"_{i}" for i in iterations
    )


def _columns(names: list[str], contract: FormContract) -> list[_Column]:
    """Resolve every column, adding the binaries of choices nobody picked."""
    order = {path: i for i, path in enumerate(contract.nodes)}
    columns: dict[str, _Column] = {}
    multi_instances: dict[str, set[tuple[int, ...]]] = {}
    for position, name in enumerate(names):
        if is_system_column(name):
            arrow_type = (
                _TIMESTAMP
                if name in _SYSTEM_TIMESTAMPS
                else pa.int64()
                if name in _SYSTEM_INTS
                else pa.string()
            )
            rank = -1 if name == "KEY" else position
            columns[name] = _Column(
                name, arrow_type, name in _SYSTEM_SHOWN, {"system": "true"}, (0, rank)
            )
            continue
        m = contract.map_column(name)
        if m["kind"] != "matched":
            raise CastError(
                f"column {name!r} maps to no field in form {contract.formid!r}"
            )
        node = contract.nodes[m["node_path"]]
        if not node.is_leaf:
            raise CastError(f"column {name!r} maps to group {node.path!r}, not a field")
        iterations = tuple(i for _, i in m["repeat_iterations"])
        if node.is_select_multiple and m["choice_code"] is not None:
            multi_instances.setdefault(node.path, set()).add(iterations)
            continue
        if node.xml_type not in _XML_TYPES:
            raise CastError(
                f"column {name!r}: no Arrow type for xml_type {node.xml_type!r}"
            )
        columns[name] = _Column(
            name,
            _XML_TYPES[node.xml_type],
            node.publishable,
            _node_metadata(node),
            (1, order[node.path], iterations, 0),
        )

    for path, instances in multi_instances.items():
        node = contract.nodes[path]
        if not node.repeat_path:
            instances = {()}
        for iterations in instances:
            for item in node.choice_items:
                name = _binary_name(node, item["value"], iterations)
                m = contract.map_column(name)
                if m["kind"] != "matched" or m["node_path"] != path:
                    raise CastError(
                        f"choice binary {name!r} does not map back to {path!r}"
                    )
                columns[name] = _Column(
                    name,
                    pa.int8(),
                    node.publishable,
                    {**_node_metadata(node), "choice_value": str(item["value"])},
                    (1, order[path], iterations, _choice_index(node, item["value"])),
                    binary_group=(path, iterations),
                )
    return sorted(columns.values(), key=lambda c: c.sort_key)


def cast_wide(rows: list[dict], contract: FormContract) -> pa.Table:
    """Return the submissions as a typed table, system columns first, then form order."""
    names = list(dict.fromkeys(name for row in rows for name in row))
    columns = _columns(names, contract)
    groups: dict[tuple, list[str]] = {}
    for c in columns:
        if c.binary_group is not None:
            groups.setdefault(c.binary_group, []).append(c.name)

    arrays, fields = [], []
    for c in columns:
        parse = _PARSERS[c.arrow_type]
        values = []
        for row in rows:
            raw = row.get(c.name, "")
            if raw == "":
                answered = c.binary_group is not None and any(
                    row.get(sibling, "") != "" for sibling in groups[c.binary_group]
                )
                values.append(0 if answered else None)
                continue
            try:
                values.append(parse(raw))
            except ValueError as e:
                # The parser's own message repeats the value, so it is shown only
                # when the value is.
                detail = f"{raw!r} ({e})" if c.shown else "<redacted>"
                raise CastError(
                    f"column {c.name!r}, KEY {row['KEY']!r}: cannot cast {detail} to {c.arrow_type}"
                ) from None
        arrays.append(pa.array(values, type=c.arrow_type))
        fields.append(pa.field(c.name, c.arrow_type, metadata=c.metadata))
    return pa.Table.from_arrays(arrays, schema=pa.schema(fields))
