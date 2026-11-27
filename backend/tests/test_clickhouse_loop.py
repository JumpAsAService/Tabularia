"""The loop step (`foreach`) on the external ClickHouse.

Each iteration is a statement of its own, written as one parquet under a prefix of the chain's and
read back together by column name. The statements are checked here without a server; that the server
reads parts with different columns as one table by name was checked on a 25.3 service (the facts are
in `ClickHouseContext.loop_parts`).
"""
import datetime as dt
from types import SimpleNamespace

import pytest

from app.core.config import ClickHouseExternalSettings
from app.engine.chdb_ops import get_chdb_operation
from app.engine.clickhouse_engine import (
    LOOP_PARTS_MAX_AGE_SECONDS, LOOP_PREFIX, ClickHouseContext, sweep_loop_parts,
)
from app.engine.exceptions import EngineError


class _Client:
    def __init__(self, rows=None):
        self.commands, self.queries, self._rows = [], [], rows or []

    def command(self, sql, settings=None):
        self.commands.append((sql, dict(settings or {})))

    def query(self, sql, settings=None):
        self.queries.append(sql)
        described = sql.startswith("DESCRIBE")
        return SimpleNamespace(result_rows=[("area", "String"), ("soglia", "Int64")] if described else self._rows)


class _Storage:
    def __init__(self):
        self.deleted = []

    def delete_objects(self, bucket, keys):
        self.deleted.append((bucket, list(keys)))


def _ctx(transport="s3", rows=None):
    cfg = ClickHouseExternalSettings(host="clickhouse.example", transport=transport, s3_endpoint="https://s3.example",
                                     s3_named_collection="keys")
    cache = SimpleNamespace(bucket="data")
    return ClickHouseContext(client=_Client(rows), storage=_Storage(), cfg=cfg, tmp=[], cache=cache)


BODY = [{"type": "filter", "params": {"column": "area", "operator": "eq", "value": "{{area}}"}}]


def _foreach(ctx, params):
    return get_chdb_operation("foreach")("SELECT * FROM src", params, ctx)


def test_each_iteration_is_a_statement_of_its_own_and_they_are_read_back_by_name():
    ctx = _ctx()
    sql = _foreach(ctx, {"items": [{"area": "Nord"}, {"area": "Sud"}, {"area": "Isole"}], "body": BODY})
    writes = ctx.client.commands
    assert len(writes) == 3 and all(w[0].startswith("INSERT INTO FUNCTION s3(") for w in writes)
    # one parquet each, under one prefix of this chain's, in the iterations' order
    prefix = ctx.loops[0][1]
    assert prefix.startswith(LOOP_PREFIX + "/") and ctx.loops[0] == ("data", prefix, 3)
    for i, (stmt, settings) in enumerate(writes):
        assert f"/data/{prefix}/{i:06d}.parquet" in stmt and settings["s3_truncate_on_insert"] == 1
    assert "`area` = 'Nord'" in writes[0][0] and "`area` = 'Isole'" in writes[2][0]
    # read with a glob, and by name: without the union the first part's columns would be everyone's
    assert f"/data/{prefix}/*.parquet" in sql and sql.startswith("SELECT * FROM s3(")
    assert ctx.settings["schema_inference_mode"] == "union"
    # no iteration is inside the statement that reads them: none runs twice
    assert "`area` = " not in sql


def test_one_iteration_needs_no_part():
    ctx = _ctx()
    sql = _foreach(ctx, {"items": [{"area": "Nord"}], "body": BODY})
    assert ctx.client.commands == [] and "`area` = 'Nord'" in sql and not ctx.loops


def test_the_keys_of_an_iteration_can_travel_as_columns():
    ctx = _ctx()
    _foreach(ctx, {"items": [{"area": "Nord"}, {"area": "Sud"}], "body": BODY, "add_keys_as_columns": True})
    assert "'Nord' AS `area`" in ctx.client.commands[0][0] and "'Sud' AS `area`" in ctx.client.commands[1][0]


def test_the_iterations_come_from_a_driver_too():
    ctx = _ctx(rows=[["Nord", 10], ["Sud", 20]])
    body = BODY + [{"type": "filter", "params": {"column": "n", "operator": "gt", "value": "{{soglia}}"}}]
    _foreach(ctx, {"driver": {"bucket": "data", "key": "drv.parquet"}, "body": body})
    assert len(ctx.client.commands) == 2
    # a placeholder that is the whole value keeps the driver's type: a number stays a number
    assert "`n` > 10" in ctx.client.commands[0][0] and "`n` > 20" in ctx.client.commands[1][0]


def test_the_parts_are_removed_when_the_chain_is_done():
    ctx = _ctx()
    _foreach(ctx, {"items": [{"area": "Nord"}, {"area": "Sud"}], "body": BODY})
    prefix = ctx.loops[0][1]
    ctx.cleanup()
    assert ctx.storage.deleted == [("data", [f"{prefix}/000000.parquet", f"{prefix}/000001.parquet"])]
    assert ctx.loops == []


def test_an_iteration_that_fails_says_which():
    ctx = _ctx()

    def refuse(sql, settings=None):
        raise RuntimeError("Code: 241. DB::Exception: Query memory limit exceeded")

    ctx.client.command = refuse
    with pytest.raises(EngineError, match=r"iteration 1/2"):
        _foreach(ctx, {"items": [{"area": "Nord"}, {"area": "Sud"}], "body": BODY})
    # what was booked is still removed
    ctx.cleanup()
    assert ctx.storage.deleted and ctx.storage.deleted[0][1][0].endswith("000000.parquet")


def test_the_limits_and_the_safety_are_the_other_engines():
    ctx = _ctx()
    with pytest.raises(EngineError, match="no iterations"):
        _foreach(ctx, {"items": [], "body": BODY})
    with pytest.raises(EngineError, match="body"):  # an empty body is a body not given
        _foreach(ctx, {"items": [{"a": 1}], "body": []})
    with pytest.raises(EngineError, match="over the limit"):
        _foreach(ctx, {"items": [{"a": i} for i in range(1001)], "body": BODY})
    # a source in the body cannot take its path from the data
    bad = [{"type": "join", "params": {"how": "inner", "on": ["k"], "right": {"bucket": "data", "key": "{{area}}.parquet"}}}]
    with pytest.raises(EngineError):
        _foreach(ctx, {"items": [{"area": "x"}], "body": bad})
    assert ctx.client.commands == []


def test_where_the_server_cannot_keep_the_parts_the_step_is_not_supported():
    with pytest.raises(EngineError, match="not supported"):
        _foreach(_ctx(transport="push"), {"items": [{"area": "Nord"}, {"area": "Sud"}], "body": BODY})


def test_parts_left_by_a_worker_that_died_are_swept_by_age():
    now = 1_800_000_000.0
    old = dt.datetime.fromtimestamp(now - LOOP_PARTS_MAX_AGE_SECONDS - 60, dt.timezone.utc)
    new = dt.datetime.fromtimestamp(now - 60, dt.timezone.utc)
    pages = [{"Contents": [{"Key": f"{LOOP_PREFIX}/a/000000.parquet", "LastModified": old},
                           {"Key": f"{LOOP_PREFIX}/b/000000.parquet", "LastModified": new}]}]
    seen = {}

    class _Paginator:
        def paginate(self, **kw):
            seen.update(kw)
            return pages

    storage = _Storage()
    storage.client = SimpleNamespace(get_paginator=lambda name: _Paginator())
    assert sweep_loop_parts(storage, "data", now=now) == 1
    # only under the loops' prefix, and only what no running chain can still be reading
    assert seen == {"Bucket": "data", "Prefix": LOOP_PREFIX + "/"}
    assert storage.deleted == [("data", [f"{LOOP_PREFIX}/a/000000.parquet"])]
