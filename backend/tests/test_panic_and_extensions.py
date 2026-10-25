"""Two defects found by the dbt export oracle (2026-10-10), both upstream of the export:
the import from Postgres wrote the numerics as an opaque Arrow extension, and Polars'
streaming reader PANICked on a filter of that column; the panic (BaseException, not
Exception) made the worker quit."""
import tempfile

import polars as pl
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from polars.exceptions import PanicException

from app.engine.exceptions import EngineError
from app.ingest.db_source import _batch_without_extensions, _schema_without_extensions


def _opaque_batch():
    t = pa.opaque(pa.string_view(), "numeric", "PostgreSQL")
    amount = pa.ExtensionArray.from_storage(t, pa.array(["1.50", None, "100.00"], type=pa.string_view()))
    return pa.RecordBatch.from_arrays([pa.array([1, 2, 3], pa.int32()), amount], names=["id", "importo"])


def test_the_import_writes_numerics_as_numbers_and_the_schema_matches():
    """The user's choice (2026-10-10): a Postgres numeric is a number, not a text."""
    b = _opaque_batch()
    nb = _batch_without_extensions(b)
    assert nb.schema.field("importo").type == pa.float64() and nb.column(1).to_pylist() == [1.5, None, 100.0]
    assert _schema_without_extensions(b.schema) == nb.schema


def test_numerics_special_values_and_the_other_opaque_types():
    t = pa.opaque(pa.string(), "numeric", "PostgreSQL")
    special = pa.ExtensionArray.from_storage(t, pa.array(["NaN", "Infinity", "-Infinity", "12345678901234567890.123", "-0.01"]))
    other = pa.ExtensionArray.from_storage(pa.opaque(pa.string(), "interval", "PostgreSQL"), pa.array(["1 day"] * 5))
    nb = _batch_without_extensions(pa.RecordBatch.from_arrays([special, other], names=["n", "i"]))
    v = nb.column(0).to_pylist()
    assert v[0] != v[0] and v[1:3] == [float("inf"), float("-inf")] and v[3] == 1.2345678901234567e19 and v[4] == -0.01
    assert nb.schema.field("i").type == pa.string() and nb.column(1).to_pylist() == ["1 day"] * 5
    clean = pa.RecordBatch.from_arrays([pa.array([1])], names=["id"])
    assert _batch_without_extensions(clean) is clean and _schema_without_extensions(clean.schema) is clean.schema


def test_the_imports_parquet_filters_streaming():
    f = tempfile.mktemp(suffix=".parquet")
    pq.write_table(pa.Table.from_batches([_batch_without_extensions(_opaque_batch())]), f)
    lf = pl.scan_parquet(f)
    assert lf.filter(pl.col("importo").is_not_null()).collect(engine="streaming").height == 2
    assert lf.drop_nulls().collect(engine="streaming").height == 2
    assert lf.select(pl.col("importo").sum()).collect(engine="streaming").item() == 101.5   # it adds up


def test_a_polars_panic_becomes_a_task_error_and_does_not_kill_the_worker():
    from app.tasks.celery_app import _TaskThatSparesTheWorker, celery_app

    class Broken(_TaskThatSparesTheWorker):
        name = "test.broken"

        def run(self):
            raise PanicException("not yet implemented: arrow.opaque")

    t = Broken()
    t.bind(celery_app)
    with pytest.raises(EngineError, match="Internal error in the compute engine"):
        t()


def test_the_polars_engine_stops_the_panic_too():
    from app.engine import polars_engine

    assert PanicException in polars_engine._POLARS_ERRORS and Exception in polars_engine._POLARS_ERRORS
    assert "except Exception as e" not in open(polars_engine.__file__).read()
