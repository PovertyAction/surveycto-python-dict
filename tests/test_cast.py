"""Stage 4: wide JSON cast to a typed Arrow table through the contract."""

import datetime as dt

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from surveycto_python_dict.cast import CastError, cast_wide
from surveycto_python_dict.xml_contract import parse_contract

_XML = """<?xml version="1.0"?>
<h:html xmlns="http://www.w3.org/2002/xforms" xmlns:h="http://www.w3.org/1999/xhtml"
        xmlns:jr="http://openrosa.org/javarosa">
  <h:head><model>
    <instance><farm id="farm" version="3">
      <starttime/><endtime/><deviceid/>
      <owner/><plots/><area/><visit/><crop/><tools/><photo/>
      <plot jr:template=""><plot_crop/><plot_size/></plot>
      <meta><instanceID/></meta>
    </farm></instance>
    <bind nodeset="/farm/starttime" type="dateTime" jr:preload="timestamp" jr:preloadParams="start"/>
    <bind nodeset="/farm/endtime" type="dateTime" jr:preload="timestamp" jr:preloadParams="end"/>
    <bind nodeset="/farm/deviceid" type="string" jr:preload="property" jr:preloadParams="deviceid"/>
    <bind nodeset="/farm/owner" type="string"/>
    <bind nodeset="/farm/plots" type="int" publishable="true()"/>
    <bind nodeset="/farm/area" type="decimal"/>
    <bind nodeset="/farm/visit" type="date"/>
    <bind nodeset="/farm/crop" type="select1"/>
    <bind nodeset="/farm/tools" type="select"/>
    <bind nodeset="/farm/photo" type="binary"/>
    <bind nodeset="/farm/plot/plot_crop" type="select"/>
    <bind nodeset="/farm/plot/plot_size" type="decimal"/>
    <bind nodeset="/farm/meta/instanceID" type="string" calculate="concat('uuid:', uuid())"/>
  </model></h:head>
  <h:body>
    <input ref="/farm/owner"><label>Owner</label></input>
    <input ref="/farm/plots"><label>Plots</label></input>
    <input ref="/farm/area"><label>Area</label></input>
    <input ref="/farm/visit"><label>Visit</label></input>
    <select1 ref="/farm/crop"><label>Crop</label>
      <item><label>Maize</label><value>01</value></item><item><label>Rice</label><value>02</value></item>
    </select1>
    <select ref="/farm/tools"><label>Tools</label>
      <item><label>Hoe</label><value>1</value></item><item><label>Plough</label><value>2</value></item>
      <item><label>Other</label><value>77</value></item>
    </select>
    <upload ref="/farm/photo" mediatype="image/*"><label>Photo</label></upload>
    <group ref="/farm/plot"><label>Plot</label><repeat nodeset="/farm/plot" jr:count=" /farm/plots ">
      <select ref="/farm/plot/plot_crop"><label>Plot crop</label>
        <item><label>Maize</label><value>1</value></item><item><label>Rice</label><value>2</value></item>
      </select>
      <input ref="/farm/plot/plot_size"><label>Plot size</label></input>
    </repeat></group>
  </h:body>
</h:html>
"""

# Shapes as the API returns them: all strings, keys sparse per row, select_multiple
# only as per-choice binaries present when chosen, timestamps in UTC.
_ROWS = [
    {
        "SubmissionDate": "Mar 7, 2025 4:06:12 PM",
        "starttime": "Mar 7, 2025 3:58:01 PM",
        "endtime": "Mar 7, 2025 4:05:59 PM",
        "deviceid": "(web)",
        "duration": "478",
        "owner": "Ana Fictional",
        "plots": "2",
        "area": "1.5",
        "visit": "Mar 6, 2025",
        "crop": "01",
        "tools_2": "1",
        "photo": "https://example.surveycto.com/view/submission-attachment/x.jpg",
        "plot_crop_1_1": "1",
        "plot_size_1": "0.75",
        "plot_size_2": "",
        "formdef_version": "2503071606",
        "KEY": "uuid:aaa",
    },
    {
        "SubmissionDate": "Mar 12, 2025 11:00:00 AM",
        "starttime": "Mar 12, 2025 10:50:00 AM",
        "endtime": "Mar 12, 2025 10:59:00 AM",
        "deviceid": "(web)",
        "duration": "540",
        "owner": "",
        "plots": "-99",
        "area": "",
        "crop": "02",
        "plot_crop_2_2": "1",
        "formdef_version": "2503071606",
        "KEY": "uuid:bbb",
    },
]


@pytest.fixture
def contract(tmp_path):
    path = tmp_path / "farm.xml"
    path.write_text(_XML, encoding="utf-8")
    return parse_contract(path)


@pytest.fixture
def table(contract):
    return cast_wide(_ROWS, contract)


def _col(table, name):
    return table.column(name).to_pylist()


def test_row_count(table):
    assert table.num_rows == len(_ROWS)


def test_types(table):
    types = {f.name: f.type for f in table.schema}
    utc_ms = pa.timestamp("ms", tz="UTC")
    assert types["SubmissionDate"] == utc_ms
    assert types["starttime"] == utc_ms
    assert types["duration"] == pa.int64()
    assert types["deviceid"] == pa.string()
    assert types["KEY"] == pa.string()
    assert types["formdef_version"] == pa.string()
    assert types["owner"] == pa.string()
    assert types["plots"] == pa.int64()
    assert types["area"] == pa.float64()
    assert types["visit"] == pa.date32()
    assert types["crop"] == pa.string()
    assert types["tools_2"] == pa.int8()
    assert types["photo"] == pa.string()
    assert types["plot_size_1"] == pa.float64()


def test_values(table):
    assert _col(table, "SubmissionDate")[0] == dt.datetime(
        2025, 3, 7, 16, 6, 12, tzinfo=dt.timezone.utc
    )
    assert _col(table, "visit") == [dt.date(2025, 3, 6), None]
    assert _col(table, "plots") == [2, -99]  # sentinels stay ints
    assert _col(table, "crop") == ["01", "02"]  # codes stay strings
    assert _col(table, "duration") == [478, 540]


def test_empty_and_absent_are_null(table):
    assert _col(table, "owner") == ["Ana Fictional", None]
    assert _col(table, "area") == [1.5, None]
    assert _col(table, "plot_size_2") == [None, None]


def test_unchosen_binary_is_zero_when_a_sibling_is_chosen(table):
    # Row 0 chose tools 2; row 1 answered no tools option at all -> null.
    assert _col(table, "tools_2") == [1, None]


def test_binary_columns_for_every_choice_exist(table):
    """A choice nobody picked still gets its column, 0 where the question was answered."""
    assert _col(table, "tools_1") == [0, None]
    assert _col(table, "tools_77") == [0, None]


def test_binary_zero_fill_is_per_repeat_instance(table):
    assert _col(table, "plot_crop_1_1") == [1, None]
    assert _col(table, "plot_crop_2_1") == [0, None]
    assert _col(table, "plot_crop_1_2") == [None, 0]
    assert _col(table, "plot_crop_2_2") == [None, 1]


def test_column_order_system_then_form_order(table):
    names = table.schema.names
    assert names[0] == "KEY"
    assert (
        names.index("owner")
        < names.index("plots")
        < names.index("tools_1")
        < names.index("tools_2")
    )
    assert names.index("tools_2") < names.index("tools_77") < names.index("photo")


def test_field_metadata(table):
    meta = table.schema.field("plot_size_1").metadata
    assert meta[b"xml_type"] == b"decimal"
    assert meta[b"node_path"] == b"plot/plot_size"
    assert meta[b"repeat_path"] == b'["plot"]'
    assert table.schema.field("KEY").metadata[b"system"] == b"true"


def test_parquet_round_trip_keeps_schema(table, tmp_path):
    out = tmp_path / "farm.parquet"
    pq.write_table(table, out)
    assert pq.read_schema(out).equals(table.schema, check_metadata=True)


def test_bad_value_raises_with_column_and_key_value_redacted(contract):
    rows = [dict(_ROWS[0], area="about two")]
    with pytest.raises(CastError) as err:
        cast_wide(rows, contract)
    message = str(err.value)
    assert "area" in message and "uuid:aaa" in message
    assert "about two" not in message
    assert "<redacted>" in message


def test_bad_value_in_publishable_field_is_shown(contract):
    rows = [dict(_ROWS[0], plots="two")]
    with pytest.raises(CastError, match="'two'"):
        cast_wide(rows, contract)


def test_binary_value_other_than_one_raises(contract):
    rows = [dict(_ROWS[0], tools_2="yes")]
    with pytest.raises(CastError, match="tools_2"):
        cast_wide(rows, contract)


def test_unmapped_column_raises(contract):
    rows = [dict(_ROWS[0], mystery="x")]
    with pytest.raises(CastError, match="mystery"):
        cast_wide(rows, contract)
