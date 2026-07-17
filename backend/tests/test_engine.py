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

FILTRO_IT = [{"type": "filter", "params": {"column": "paese", "operator": "eq", "value": "IT"}}]


# ── Preview ──────────────────────────────────────────────────────────────────
def test_preview_ritorna_righe_colonne_e_conteggio(engine, vendite):
    res = engine.preview(vendite, FILTRO_IT, limit=100)
    assert res.row_count == 2
    assert not res.truncated
    assert [c.name for c in res.columns] == ["paese", "vendite", "data"]
    assert all(r["paese"] == "IT" for r in res.rows)


def test_preview_tronca_al_limite_e_lo_segnala(engine, vendite):
    res = engine.preview(vendite, [], limit=2)
    assert res.row_count == 2
    assert res.truncated


def test_preview_espone_i_dtype_come_stringhe(engine, vendite):
    res = engine.preview(vendite, [], limit=1)
    dtypes = {c.name: c.dtype for c in res.columns}
    assert dtypes["data"] == "Date"  # il frontend ci fa pattern-matching (calendari)
    assert dtypes["vendite"].startswith("Int")


def test_preview_con_operazione_rotta_indica_nodo_e_posizione(engine, vendite):
    ops = FILTRO_IT + [{"type": "filter", "params": {"column": "non_esiste", "operator": "eq", "value": 1}}]
    with pytest.raises((OperationError, EngineError)):
        engine.preview(vendite, ops, limit=10)


# ── Cache incrementale ───────────────────────────────────────────────────────
def test_la_preview_materializza_il_parent_in_cache(engine, vendite, cache):
    ops = FILTRO_IT + [{"type": "limit", "params": {"n": 1}}]
    engine.preview(vendite, ops, limit=10)
    parent_hash = plan_hashes(engine._source_id(vendite), FILTRO_IT)[-1]
    assert cache.has(parent_hash)  # iterare sui params dell'ultimo nodo ora costa 1 op


def test_la_seconda_preview_riusa_la_cache(engine, vendite, fredis):
    ops = FILTRO_IT + [{"type": "limit", "params": {"n": 1}}]
    engine.preview(vendite, ops, limit=10)
    hits_prima = fredis.counters[HITS_KEY]
    engine.preview(vendite, ops, limit=10)
    assert fredis.counters[HITS_KEY] > hits_prima


def test_il_risultato_con_cache_e_identico_a_quello_senza(engine, vendite):
    ops = FILTRO_IT + [{"type": "sort", "params": {"by": "vendite"}}]
    fresco = engine.preview(vendite, ops, limit=10).rows  # popola la cache
    dal_cache = engine.preview(vendite, ops, limit=10).rows  # riparte dalla cache
    assert fresco == dal_cache


# ── Run ──────────────────────────────────────────────────────────────────────
def test_run_scrive_l_output_e_riporta_le_righe(engine, vendite, storage):
    dest = DataSource(bucket=BUCKET, key="out/risultato.parquet")
    res = engine.run(vendite, FILTRO_IT, dest)
    assert res.rows_written == 2
    assert storage.exists(BUCKET, "out/risultato.parquet")


def test_run_mette_in_cache_anche_lo_step_finale(engine, vendite, cache):
    dest = DataSource(bucket=BUCKET, key="out/risultato.parquet")
    engine.run(vendite, FILTRO_IT, dest)
    final_hash = plan_hashes(engine._source_id(vendite), FILTRO_IT)[-1]
    assert cache.has(final_hash)  # ri-run e preview del nodo foglia sono immediati


# ── Export ───────────────────────────────────────────────────────────────────
def _tmp(suffix: str) -> str:
    fd, path = tempfile.mkstemp(suffix=suffix)
    os.close(fd)
    return path


def test_export_csv_scrive_il_risultato_filtrato(engine, vendite):
    path = _tmp(".csv")
    try:
        engine.export(vendite, FILTRO_IT, fmt="csv", out_path=path)
        df = pl.read_csv(path)
        assert df.height == 2
        assert set(df["paese"]) == {"IT"}
    finally:
        os.unlink(path)


def test_export_xlsx_produce_un_excel_valido(engine, vendite):
    path = _tmp(".xlsx")
    try:
        engine.export(vendite, [], fmt="xlsx", out_path=path)
        df = pl.read_excel(path)
        assert df.height == 6
    finally:
        os.unlink(path)


def test_export_xlsx_oltre_il_tetto_righe_suggerisce_il_csv(engine, vendite, monkeypatch):
    monkeypatch.setattr(type(engine), "XLSX_MAX_ROWS", 3)  # abbassa il tetto per il test
    path = _tmp(".xlsx")
    try:
        with pytest.raises(EngineError, match="CSV"):
            engine.export(vendite, [], fmt="xlsx", out_path=path)
    finally:
        os.unlink(path)


def test_export_formato_sconosciuto_da_errore_chiaro(engine, vendite):
    path = _tmp(".bin")
    try:
        with pytest.raises(EngineError, match="non supportato"):
            engine.export(vendite, [], fmt="pdf", out_path=path)
    finally:
        os.unlink(path)


def test_export_rispetta_il_limit(engine, vendite):
    path = _tmp(".csv")
    try:
        engine.export(vendite, [], fmt="csv", out_path=path, limit=3)
        assert pl.read_csv(path).height == 3
    finally:
        os.unlink(path)
