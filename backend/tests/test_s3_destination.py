"""Tests of the S3 destinations: the whole path with a fake storage and a recording boto3
client (no network), plus the pure validations."""
from __future__ import annotations

import polars as pl
import pytest

from app.ingest import s3_destination as s3d
from app.ingest.s3_destination import (
    S3ConnectionSpec,
    S3DestinationError,
    S3DestinationSpec,
    _clean_key,
    write_output_to_s3,
)
from tests.conftest import BUCKET, upload_df


class RecordingClient:
    """A STATEFUL fake S3 client: records the uploads and keeps the objects' state (to test
    the list/delete of the partitions' idempotent cleanup)."""

    def __init__(self):
        self.uploads: list[tuple[str, str]] = []  # cronologia (bucket, key)
        self.configs: list = []  # the transfer settings of each upload
        self.objects: dict[tuple[str, str], bool] = {}  # current state

    def upload_file(self, local, bucket, key, Config=None):
        self.uploads.append((bucket, key))
        self.configs.append(Config)
        self.objects[(bucket, key)] = True

    def get_paginator(self, op):
        outer = self

        class _Paginator:
            def paginate(self, Bucket, Prefix):
                yield {
                    "Contents": [
                        {"Key": k} for (b, k) in outer.objects if b == Bucket and k.startswith(Prefix)
                    ]
                }

        return _Paginator()

    def delete_objects(self, Bucket, Delete):
        for o in Delete["Objects"]:
            self.objects.pop((Bucket, o["Key"]), None)


@pytest.fixture
def recorder(monkeypatch) -> RecordingClient:
    client = RecordingClient()
    monkeypatch.setattr(S3ConnectionSpec, "client", lambda self: client)
    return client


@pytest.fixture
def source(storage):
    df = pl.DataFrame(
        {
            "paese": ["IT", "IT", "FR", "DE"],
            "anno": [2024, 2025, 2024, 2024],
            "vendite": [100.0, 300.0, 50.0, 250.0],
        }
    )
    return upload_df(storage, df, "out/run_test.parquet")


def _conn(**kw) -> S3ConnectionSpec:
    return S3ConnectionSpec(endpoint_url="http://minio:9000", access_key="k", secret_key="s", **kw)


# ── the whole path (fake client) ─────────────────────────────────────────
def test_single_parquet_one_exact_key(storage, source, recorder):
    out = write_output_to_s3(
        _conn(), S3DestinationSpec(bucket="cliente", key="exports/vendite.parquet"),
        BUCKET, source.key, storage=storage,
    )
    assert recorder.uploads == [("cliente", "exports/vendite.parquet")]
    assert out["rows_written"] == 4 and out["files"] == 1 and out["format"] == "parquet"
    # parts sized for storages that take at most 1,000 of them (see test_storage_parts.py)
    assert recorder.configs[0] is not None and recorder.configs[0].multipart_chunksize >= 8 * 1024**2


def test_single_csv_converted(storage, source, recorder):
    out = write_output_to_s3(
        _conn(), S3DestinationSpec(bucket="cliente", key="exports/vendite.csv", format="csv"),
        BUCKET, source.key, storage=storage,
    )
    assert recorder.uploads == [("cliente", "exports/vendite.csv")]
    assert out["format"] == "csv" and out["rows_written"] == 4


def test_hive_partitioned_keys_column_equals_value(storage, source, recorder):
    out = write_output_to_s3(
        _conn(),
        S3DestinationSpec(bucket="cliente", key="exports/vendite", partition_by=["paese"]),
        BUCKET, source.key, storage=storage,
    )
    keys = sorted(k for _, k in recorder.uploads)
    assert len(keys) == 3 and out["partitions"] == 3
    for prefix in ("exports/vendite/paese=DE/", "exports/vendite/paese=FR/", "exports/vendite/paese=IT/"):
        assert any(k.startswith(prefix) and k.endswith(".parquet") for k in keys)


def test_partitions_several_columns(storage, source, recorder):
    out = write_output_to_s3(
        _conn(),
        S3DestinationSpec(bucket="c", key="p", partition_by=["paese", "anno"]),
        BUCKET, source.key, storage=storage,
    )
    assert out["partitions"] == 4  # IT/2024, IT/2025, FR/2024, DE/2024
    assert any("paese=IT/anno=2025/" in k for _, k in recorder.uploads)


def test_partitioned_idempotent_removes_stale_files(storage, source, recorder):
    """A file of an EARLIER run under the prefix (a partition gone, or another file count)
    must be removed: no duplicate rows when writing again."""
    # a leftover of an earlier run under the same prefix
    recorder.objects[("c", "p/paese=ZZ/anno=1999/00000000.parquet")] = True
    write_output_to_s3(
        _conn(),
        S3DestinationSpec(bucket="c", key="p", partition_by=["paese", "anno"]),
        BUCKET, source.key, storage=storage,
    )
    remaining = {k for (b, k) in recorder.objects if b == "c"}
    assert "p/paese=ZZ/anno=1999/00000000.parquet" not in remaining  # the stale one removed
    assert any("paese=IT/anno=2025/" in k for k in remaining)  # the new ones there


def test_default_bucket_from_the_connection(storage, source, recorder):
    write_output_to_s3(
        _conn(bucket="default-bucket"), S3DestinationSpec(key="x.parquet"),
        BUCKET, source.key, storage=storage,
    )
    assert recorder.uploads[0][0] == "default-bucket"


def test_no_bucket_clear_error(storage, source, recorder):
    with pytest.raises(S3DestinationError, match="bucket"):
        write_output_to_s3(_conn(), S3DestinationSpec(key="x.parquet"), BUCKET, source.key, storage=storage)


def test_partition_column_that_does_not_exist(storage, source, recorder):
    with pytest.raises(S3DestinationError, match="not in the output"):
        write_output_to_s3(
            _conn(), S3DestinationSpec(bucket="c", key="p", partition_by=["boh"]),
            BUCKET, source.key, storage=storage,
        )


def test_too_many_partitions_refused(storage, source, recorder, monkeypatch):
    monkeypatch.setattr(s3d, "MAX_PARTITIONS", 2)
    with pytest.raises(S3DestinationError, match="partitions"):
        write_output_to_s3(
            _conn(), S3DestinationSpec(bucket="c", key="p", partition_by=["paese"]),
            BUCKET, source.key, storage=storage,
        )


# ── validazioni pure ─────────────────────────────────────────────────────────
def test_clean_key_normalizza_slash():
    assert _clean_key("/exports/vendite.parquet/") == "exports/vendite.parquet"


def test_clean_key_empty_or_traversal_refused():
    with pytest.raises(S3DestinationError):
        _clean_key("  ")
    with pytest.raises(S3DestinationError):
        _clean_key("a/../b")
    with pytest.raises(S3DestinationError):
        _clean_key("a//b")


def test_clear_secret_development_only():
    conn = S3ConnectionSpec(access_key="k", secret_key="chiaro")
    assert conn.resolve_secret() == "chiaro"


# ── placeholders in the key ───────────────────────────────────────────────────
def test_the_key_resolves_its_placeholders_in_the_time_zone(storage, source, recorder):
    from datetime import datetime, timezone

    out = write_output_to_s3(
        _conn(), S3DestinationSpec(bucket="c", key="exports/{today:yyyy/MM}/{flow}_{date}_{run_id}.parquet",
                                   timezone="Asia/Tokyo", run_id=9, flow="Vendite mese"),
        BUCKET, source.key, storage=storage, now=datetime(2026, 10, 17, 22, 30, tzinfo=timezone.utc),
    )
    assert recorder.uploads == [("c", "exports/2026/10/Vendite_mese_2026-10-18_9.parquet")]
    assert out["key"] == "exports/2026/10/Vendite_mese_2026-10-18_9.parquet"


def test_a_key_without_placeholders_is_written_as_before(storage, source, recorder):
    write_output_to_s3(_conn(), S3DestinationSpec(bucket="c", key="a/b.parquet"), BUCKET, source.key, storage=storage)
    assert recorder.uploads == [("c", "a/b.parquet")]


@pytest.mark.parametrize("key", ["x/{flow}/../y.csv", "x/{week}.csv", "x/{today"])
def test_a_bad_key_template_is_refused_before_writing(storage, source, recorder, key):
    with pytest.raises(S3DestinationError):
        write_output_to_s3(_conn(), S3DestinationSpec(bucket="c", key=key, format="csv", flow="f"), BUCKET,
                           source.key, storage=storage)
    assert recorder.uploads == []


def test_a_templated_key_over_1024_bytes_is_refused(storage, source, recorder):
    with pytest.raises(S3DestinationError, match="1024"):
        write_output_to_s3(_conn(), S3DestinationSpec(bucket="c", key="/".join(["d" * 200] * 6) + "/{date}.parquet"),
                           BUCKET, source.key, storage=storage)
    assert recorder.uploads == []


def test_a_long_key_without_placeholders_is_sent_as_before(storage, source, recorder):
    key = "/".join(["d" * 200] * 6) + "/v.parquet"
    write_output_to_s3(_conn(), S3DestinationSpec(bucket="c", key=key), BUCKET, source.key, storage=storage)
    assert recorder.uploads == [("c", key)]
