"""Oracle as a source: the query sent to the server and the Arrow types.

Oracle's rules, not the other dialects': an identifier without quotes reads in UPPERCASE
(as in SQL Developer), in quotes it stays as it is; a table alias wants no AS nor may
start with «_». The test against a real Oracle is in test_oracle_live.py."""
import pyarrow as pa
import pytest

from app.ingest import db_source as ds
from app.ingest.db_source import DbConnectionSpec, DbSourceError, DbSourceSpec, build_query


def _conn(schema=""):
    return DbConnectionSpec(db_type="oracle", host="h", database="FREEPDB1", db_schema=schema)


@pytest.mark.parametrize("ref, schema, expected", [
    ("employees", "", 'SELECT * FROM "EMPLOYEES"'),
    ("hr.employees", "", 'SELECT * FROM "HR"."EMPLOYEES"'),
    ("Employees", "hr", 'SELECT * FROM "HR"."EMPLOYEES"'),
    ('HR."conti 2024"', "", 'SELECT * FROM "HR"."conti 2024"'),
])
def test_table_with_oracles_rules(ref, schema, expected):
    assert build_query(_conn(schema), DbSourceSpec(mode="table", ref=ref)) == expected


def test_query_wrapped_without_as_and_sorted_outside():
    q = build_query(_conn(), DbSourceSpec(mode="sql", ref="SELECT * FROM v;", sort_keys=["id", '"Data"']))
    assert q == 'SELECT * FROM (SELECT * FROM v) q ORDER BY "ID", "Data"'


@pytest.mark.parametrize("ref", ['hr."a"b"', 'hr.a"b', '""'])
def test_misplaced_quotes_refused(ref):
    with pytest.raises(DbSourceError):
        build_query(_conn(), DbSourceSpec(mode="table", ref=ref))


def test_our_order_by_is_detached_to_put_it_outside_the_wrapper():
    q = build_query(_conn(), DbSourceSpec(mode="sql", ref="SELECT a FROM t ORDER BY a", sort_keys=["a"]))
    m = ds._ORDER_BY_FINALE.match(q)
    assert m.group(1) == "SELECT * FROM (SELECT a FROM t ORDER BY a) q" and m.group(2) == '"A"'
    # without keys the user's ORDER BY stays in parentheses and is not touched
    assert ds._ORDER_BY_FINALE.match(build_query(_conn(), DbSourceSpec(mode="sql", ref="SELECT a FROM t ORDER BY a"))) is None


class _Type:
    def __init__(self, name):
        self.name = name


@pytest.mark.parametrize("kind, precision, scala, expected", [
    ("DB_TYPE_NUMBER", 10, 0, pa.int64()),
    ("DB_TYPE_NUMBER", 18, 0, pa.int64()),
    ("DB_TYPE_NUMBER", 19, 0, pa.float64()),     # beyond 18 digits: Float64, as the driver
    ("DB_TYPE_NUMBER", 0, -127, pa.float64()),   # NUMBER without a precision: Float64 (the choice of 2026-10-11)
    ("DB_TYPE_NUMBER", 10, 2, pa.float64()),
    ("DB_TYPE_DATE", None, None, pa.timestamp("us")),
    ("DB_TYPE_TIMESTAMP_TZ", None, None, pa.timestamp("us")),
    ("DB_TYPE_VARCHAR", None, None, pa.string()),
    ("DB_TYPE_CLOB", None, None, pa.large_string()),
    ("DB_TYPE_BLOB", None, None, pa.large_binary()),
    ("DB_TYPE_BOOLEAN", None, None, pa.bool_()),
])
def test_arrow_type_from_the_description_for_an_empty_result(kind, precision, scala, expected):
    assert ds._oracle_arrow_type(("C", _Type(kind), None, None, precision, scala, True)) == expected


def test_dates_and_timestamps_in_microseconds_nanoseconds_stay():
    t = pa.table({
        "giorno": pa.array([0], pa.timestamp("s")), "ts3": pa.array([0], pa.timestamp("ms")),
        "ts9": pa.array([0], pa.timestamp("ns")), "n": pa.array([1], pa.int64()),
    })
    assert [f.type for f in ds._oracle_normalize(t).schema] == [pa.timestamp("us"), pa.timestamp("us"), pa.timestamp("ns"), pa.int64()]


def test_port_and_table_listing():
    assert ds.DEFAULT_PORTS["oracle"] == 1521 and "oracle_maintained = 'N'" in ds._TABLES_QUERY["oracle"]
