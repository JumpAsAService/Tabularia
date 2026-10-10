"""Due difetti trovati dall'oracolo dell'export dbt (2026-10-10), entrambi a monte
dell'export: l'import da Postgres scriveva i numeric come estensione Arrow opaca,
e il lettore in streaming di Polars andava in PANIC su un filtro di quella
colonna; il panic (BaseException, non Exception) faceva uscire il worker."""
import tempfile

import polars as pl
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from polars.exceptions import PanicException

from app.engine.exceptions import EngineError
from app.ingest.db_source import _batch_senza_estensioni, _schema_senza_estensioni


def _batch_opaco():
    t = pa.opaque(pa.string_view(), "numeric", "PostgreSQL")
    importo = pa.ExtensionArray.from_storage(t, pa.array(["1.50", None, "100.00"], type=pa.string_view()))
    return pa.RecordBatch.from_arrays([pa.array([1, 2, 3], pa.int32()), importo], names=["id", "importo"])


def test_l_import_scrive_i_numeric_come_numeri_e_lo_schema_combacia():
    """Scelta dell'utente (2026-10-10): un numeric di Postgres è un numero, non un testo."""
    b = _batch_opaco()
    nb = _batch_senza_estensioni(b)
    assert nb.schema.field("importo").type == pa.float64() and nb.column(1).to_pylist() == [1.5, None, 100.0]
    assert _schema_senza_estensioni(b.schema) == nb.schema


def test_i_valori_speciali_di_numeric_e_gli_altri_tipi_opachi():
    t = pa.opaque(pa.string(), "numeric", "PostgreSQL")
    speciali = pa.ExtensionArray.from_storage(t, pa.array(["NaN", "Infinity", "-Infinity", "12345678901234567890.123", "-0.01"]))
    altro = pa.ExtensionArray.from_storage(pa.opaque(pa.string(), "interval", "PostgreSQL"), pa.array(["1 day"] * 5))
    nb = _batch_senza_estensioni(pa.RecordBatch.from_arrays([speciali, altro], names=["n", "i"]))
    v = nb.column(0).to_pylist()
    assert v[0] != v[0] and v[1:3] == [float("inf"), float("-inf")] and v[3] == 1.2345678901234567e19 and v[4] == -0.01
    assert nb.schema.field("i").type == pa.string() and nb.column(1).to_pylist() == ["1 day"] * 5
    pulito = pa.RecordBatch.from_arrays([pa.array([1])], names=["id"])
    assert _batch_senza_estensioni(pulito) is pulito and _schema_senza_estensioni(pulito.schema) is pulito.schema


def test_il_parquet_dell_import_si_filtra_in_streaming():
    f = tempfile.mktemp(suffix=".parquet")
    pq.write_table(pa.Table.from_batches([_batch_senza_estensioni(_batch_opaco())]), f)
    lf = pl.scan_parquet(f)
    assert lf.filter(pl.col("importo").is_not_null()).collect(engine="streaming").height == 2
    assert lf.drop_nulls().collect(engine="streaming").height == 2
    assert lf.select(pl.col("importo").sum()).collect(engine="streaming").item() == 101.5   # si somma


def test_un_panic_di_polars_diventa_un_errore_del_task_e_non_uccide_il_worker():
    from app.tasks.celery_app import _TaskCheNonUccideIlWorker, celery_app

    class Rotto(_TaskCheNonUccideIlWorker):
        name = "test.rotto"

        def run(self):
            raise PanicException("not yet implemented: arrow.opaque")

    t = Rotto()
    t.bind(celery_app)
    with pytest.raises(EngineError, match="Errore interno del motore di calcolo"):
        t()


def test_il_motore_polars_ferma_anche_il_panic():
    from app.engine import polars_engine

    assert PanicException in polars_engine._ERRORI_POLARS and Exception in polars_engine._ERRORI_POLARS
    assert "except Exception as e" not in open(polars_engine.__file__).read()
