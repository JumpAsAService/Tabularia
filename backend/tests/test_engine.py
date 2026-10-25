"""Test end-to-end del PolarsEngine coi fake: preview, run, cache, export."""
from __future__ import annotations

import os
import tempfile

import polars as pl
import pytest

from app.engine.base import DataSource
from app.engine.cache import HITS_KEY, plan_hashes
from app.engine.exceptions import EngineError, OperationError
from tests.conftest import BUCKET

FILTER_IT = [{"type": "filter", "params": {"column": "paese", "operator": "eq", "value": "IT"}}]


# ── Preview ──────────────────────────────────────────────────────────────────
def test_preview_returns_rows_columns_and_count(engine, sales):
    res = engine.preview(sales, FILTER_IT, limit=100)
    assert res.row_count == 2
    assert not res.truncated
    assert [c.name for c in res.columns] == ["paese", "vendite", "data"]
    assert all(r["paese"] == "IT" for r in res.rows)


def test_preview_cuts_at_the_limit_and_says_so(engine, sales):
    res = engine.preview(sales, [], limit=2)
    assert res.row_count == 2
    assert res.truncated


def test_preview_exposes_dtypes_as_strings(engine, sales):
    res = engine.preview(sales, [], limit=1)
    dtypes = {c.name: c.dtype for c in res.columns}
    assert dtypes["data"] == "Date"  # the frontend pattern-matches on it (calendars)
    assert dtypes["vendite"].startswith("Int")


def test_preview_with_a_broken_operation_says_node_and_position(engine, sales):
    ops = FILTER_IT + [{"type": "filter", "params": {"column": "non_esiste", "operator": "eq", "value": 1}}]
    with pytest.raises((OperationError, EngineError)):
        engine.preview(sales, ops, limit=10)


# ── Incremental cache ───────────────────────────────────────────────────────
def test_the_preview_leaves_the_parent_to_materialize(engine, sales, cache):
    from tests.conftest import drain_cache

    ops = FILTER_IT + [{"type": "limit", "params": {"n": 1}}]
    engine.preview(sales, ops, limit=10)
    parent_hash = plan_hashes(engine._source_id(sales), FILTER_IT)[-1]
    assert not cache.has(parent_hash)  # the preview does NOT wait for the copy
    assert drain_cache(engine) == 1  # the deferred task writes it
    assert cache.has(parent_hash)  # iterating on the last node's params now costs 1 op
    assert engine.take_pending() == []


def test_the_second_preview_reuses_the_cache(engine, sales, fredis):
    from tests.conftest import drain_cache

    ops = FILTER_IT + [{"type": "limit", "params": {"n": 1}}]
    engine.preview(sales, ops, limit=10)
    drain_cache(engine)
    hits_before = fredis.counters[HITS_KEY]
    engine.preview(sales, ops, limit=10)
    assert fredis.counters[HITS_KEY] > hits_before


def test_the_result_with_cache_is_identical_to_the_one_without(engine, sales):
    from tests.conftest import drain_cache

    ops = FILTER_IT + [{"type": "sort", "params": {"by": "vendite"}}]
    fresco = engine.preview(sales, ops, limit=10).rows
    drain_cache(engine)  # fills the cache
    from_cache = engine.preview(sales, ops, limit=10).rows  # starts from the cache
    assert fresco == from_cache


# ── Run ──────────────────────────────────────────────────────────────────────
def test_run_writes_the_output_and_reports_the_rows(engine, sales, storage):
    dest = DataSource(bucket=BUCKET, key="out/risultato.parquet")
    res = engine.run(sales, FILTER_IT, dest)
    assert res.rows_written == 2
    assert storage.exists(BUCKET, "out/risultato.parquet")


def test_run_caches_the_final_step_too(engine, sales, cache):
    dest = DataSource(bucket=BUCKET, key="out/risultato.parquet")
    engine.run(sales, FILTER_IT, dest)
    final_hash = plan_hashes(engine._source_id(sales), FILTER_IT)[-1]
    assert cache.has(final_hash)  # runs again and the leaf node's preview are immediate


# ── Export ───────────────────────────────────────────────────────────────────
def _tmp(suffix: str) -> str:
    fd, path = tempfile.mkstemp(suffix=suffix)
    os.close(fd)
    return path


def test_export_csv_writes_the_filtered_result(engine, sales):
    path = _tmp(".csv")
    try:
        engine.export(sales, FILTER_IT, fmt="csv", out_path=path)
        df = pl.read_csv(path)
        assert df.height == 2
        assert set(df["paese"]) == {"IT"}
    finally:
        os.unlink(path)


def test_export_xlsx_makes_a_valid_excel(engine, sales):
    path = _tmp(".xlsx")
    try:
        engine.export(sales, [], fmt="xlsx", out_path=path)
        df = pl.read_excel(path)
        assert df.height == 6
    finally:
        os.unlink(path)


def test_export_xlsx_over_the_row_cap_suggests_csv(engine, sales, monkeypatch):
    monkeypatch.setattr(type(engine), "XLSX_MAX_ROWS", 3)  # lowers the cap for the test
    path = _tmp(".xlsx")
    try:
        with pytest.raises(EngineError, match="CSV"):
            engine.export(sales, [], fmt="xlsx", out_path=path)
    finally:
        os.unlink(path)


def test_export_csv_over_the_row_cap_stops_and_suggests_an_output(engine, sales):
    # 6 rows: with a cap of 5 it is refused (the browser would gather it all in memory)
    path = _tmp(".csv")
    try:
        with pytest.raises(EngineError, match="Output node"):
            engine.export(sales, [], fmt="csv", out_path=path, max_rows=5)
    finally:
        os.unlink(path)


@pytest.mark.parametrize("max_rows, limit, rows", [(6, None, 6), (None, None, 6), (5, 3, 3), (5, 5, 5)])
def test_export_csv_at_or_under_the_cap_passes(engine, sales, max_rows, limit, rows):
    # at the exact cap it passes; without a cap everything passes; a limit under the cap is enough by itself
    path = _tmp(".csv")
    try:
        engine.export(sales, [], fmt="csv", out_path=path, limit=limit, max_rows=max_rows)
        assert pl.read_csv(path).height == rows
    finally:
        os.unlink(path)


def test_export_csv_counts_the_true_rows_even_with_line_breaks_in_values(engine, storage):
    # 3 rows, one with a line break inside the value: 3, not 4 — the cap of 3 holds
    from tests.conftest import upload_df

    src = upload_df(storage, pl.DataFrame({"note": ["una", "due\nrighe", "tre"]}), "datasets/note.parquet")
    path = _tmp(".csv")
    try:
        engine.export(src, [], fmt="csv", out_path=path, max_rows=3)
        assert pl.read_csv(path).height == 3
        with pytest.raises(EngineError):
            engine.export(src, [], fmt="csv", out_path=path, max_rows=2)
    finally:
        os.unlink(path)


def test_export_unknown_format_gives_a_clear_error(engine, sales):
    path = _tmp(".bin")
    try:
        with pytest.raises(EngineError, match="unsupported export format"):
            engine.export(sales, [], fmt="pdf", out_path=path)
    finally:
        os.unlink(path)


def test_export_respects_the_limit(engine, sales):
    path = _tmp(".csv")
    try:
        engine.export(sales, [], fmt="csv", out_path=path, limit=3)
        assert pl.read_csv(path).height == 3
    finally:
        os.unlink(path)
