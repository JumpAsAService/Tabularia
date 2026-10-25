"""The copy to an external S3 is BEST EFFORT: its error does not bring the run down.

The contract decided explicitly — «the run first, then the copy». The output parquet is
written already when the copy starts, and the gateway publishes the datasource reading
this same result: failing the task because a delivery downstream did not succeed would
undo valid work.

This is the first test that exercises `transform_data_task` directly, because the
`try/except` to check lives there and not in the writing function.
"""
from __future__ import annotations

import polars as pl
import pytest

from app.engine.base import ColumnInfo, DataSource, RunResult
from app.ingest.s3_destination import S3ConnectionSpec
from app.tasks.jobs import transform_data_task
from tests.conftest import BUCKET, upload_df
# the STATEFUL fake S3 client lives next to the destination's tests already: reusing it
# avoids duplicating its behavior (upload/list/delete) and seeing two copies diverge
from tests.test_s3_destination import RecordingClient


class _FakeEngine:
    """A minimal engine: transforms nothing, only declares what it wrote."""

    def __init__(self, key: str):
        self._key = key

    def run(self, source, operations, destination):
        return RunResult(
            destination=DataSource(bucket=BUCKET, key=self._key),
            rows_written=4,
            columns=[ColumnInfo(name="paese", dtype="String")],
        )


@pytest.fixture
def source(storage):
    df = pl.DataFrame({"paese": ["IT", "IT", "FR", "DE"], "vendite": [1.0, 2.0, 3.0, 4.0]})
    return upload_df(storage, df, "out/mirror_test.parquet")


@pytest.fixture
def task_pronto(monkeypatch, storage, source):
    """Hooks the fake engine and storage to the task, without network."""
    import app.tasks.jobs as jobs
    import app.utils as utils

    monkeypatch.setattr(jobs, "get_engine", lambda name=None: _FakeEngine(source.key))
    monkeypatch.setattr(utils, "get_storage_service", lambda: storage)
    return source


@pytest.fixture
def recorder(monkeypatch) -> RecordingClient:
    """No network: every S3ConnectionSpec returns the same fake client."""
    client = RecordingClient()
    monkeypatch.setattr(S3ConnectionSpec, "client", lambda self: client)
    return client


def _mirror(key: str) -> dict:
    return {
        "connection": {"endpoint_url": "http://minio:9000", "access_key": "k", "secret_key": "s"},
        "target": {"bucket": "lake", "key": key, "format": "parquet", "partition_by": []},
    }


def test_failed_copy_the_task_stays_successful(task_pronto, source):
    """An empty key → `write_output_to_s3` raises. The task must NOT propagate: it must end
    successfully and report the error in the result."""
    out = transform_data_task(
        bucket=BUCKET,
        input_key=source.key,
        operations=[],
        output_key=source.key,
        mirror=_mirror(""),  # an empty path: a clear error inside the writing
    )

    assert out["status"] == "success"  # the primary result holds
    assert out["rows_written"] == 4
    assert out["mirror"]["ok"] is False
    assert out["mirror"]["error"]  # the error is reported, not swallowed


def test_successful_copy_reported(task_pronto, source, recorder):
    out = transform_data_task(
        bucket=BUCKET,
        input_key=source.key,
        operations=[],
        output_key=source.key,
        mirror=_mirror("published/vendite/latest.parquet"),
    )

    assert out["status"] == "success"
    assert out["mirror"]["ok"] is True
    assert recorder.uploads == [("lake", "published/vendite/latest.parquet")]


def test_without_copy_no_key_in_the_result(task_pronto, source):
    out = transform_data_task(
        bucket=BUCKET, input_key=source.key, operations=[], output_key=source.key
    )
    assert out["status"] == "success" and "mirror" not in out
