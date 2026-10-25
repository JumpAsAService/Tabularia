"""The optional ORDER BY of the import from a database (build_query).

The parquet comes out sorted by the chosen keys → sorted row groups → pruning
downstream. No keys = the query as before.
"""
import pytest

from app.ingest.db_source import DbConnectionSpec, DbSourceError, DbSourceSpec, build_query


def _conn(db_type="postgresql"):
    return DbConnectionSpec(db_type=db_type, host="h", database="d")


def test_no_keys_leaves_the_query_unchanged():
    q = build_query(_conn(), DbSourceSpec(mode="table", ref="ordini"))
    assert q == 'SELECT * FROM "ordini"'


def test_table_mode_appends_order_by():
    q = build_query(_conn(), DbSourceSpec(mode="table", ref="ordini", sort_keys=["id", "data"]))
    assert q == 'SELECT * FROM "ordini" ORDER BY "id", "data"'


def test_sql_mode_orders_the_wrapped_result():
    q = build_query(_conn(), DbSourceSpec(mode="sql", ref="SELECT * FROM v", sort_keys=["id"]))
    assert q == 'SELECT * FROM (SELECT * FROM v) AS _q ORDER BY "id"'


def test_identifier_quoting_is_per_dialect():
    q = build_query(_conn("mysql"), DbSourceSpec(mode="table", ref="ordini", sort_keys=["id"]))
    assert q == "SELECT * FROM `ordini` ORDER BY `id`"


def test_blank_keys_are_dropped():
    q = build_query(_conn(), DbSourceSpec(mode="table", ref="ordini", sort_keys=["  ", "id", ""]))
    assert q == 'SELECT * FROM "ordini" ORDER BY "id"'


def test_a_key_containing_the_quote_char_is_refused():
    # no injection through a column name: the table name's defense
    with pytest.raises(DbSourceError):
        build_query(_conn(), DbSourceSpec(mode="table", ref="ordini", sort_keys=['id" ; DROP']))
