"""Test delle destinazioni S3: percorso completo con storage finto e client
boto3 registrato (niente rete), più le validazioni pure."""
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
    """Client S3 finto: registra gli upload_file(local, bucket, key)."""

    def __init__(self):
        self.uploads: list[tuple[str, str]] = []  # (bucket, key)

    def upload_file(self, local, bucket, key):
        self.uploads.append((bucket, key))


@pytest.fixture
def recorder(monkeypatch) -> RecordingClient:
    client = RecordingClient()
    monkeypatch.setattr(S3ConnectionSpec, "client", lambda self: client)
    return client


@pytest.fixture
def sorgente(storage):
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


# ── percorso completo (client finto) ─────────────────────────────────────────
def test_parquet_singolo_una_chiave_esatta(storage, sorgente, recorder):
    out = write_output_to_s3(
        _conn(), S3DestinationSpec(bucket="cliente", key="exports/vendite.parquet"),
        BUCKET, sorgente.key, storage=storage,
    )
    assert recorder.uploads == [("cliente", "exports/vendite.parquet")]
    assert out["rows_written"] == 4 and out["files"] == 1 and out["format"] == "parquet"


def test_csv_singolo_convertito(storage, sorgente, recorder):
    out = write_output_to_s3(
        _conn(), S3DestinationSpec(bucket="cliente", key="exports/vendite.csv", format="csv"),
        BUCKET, sorgente.key, storage=storage,
    )
    assert recorder.uploads == [("cliente", "exports/vendite.csv")]
    assert out["format"] == "csv" and out["rows_written"] == 4


def test_partizionato_hive_chiavi_colonna_uguale_valore(storage, sorgente, recorder):
    out = write_output_to_s3(
        _conn(),
        S3DestinationSpec(bucket="cliente", key="exports/vendite", partition_by=["paese"]),
        BUCKET, sorgente.key, storage=storage,
    )
    keys = sorted(k for _, k in recorder.uploads)
    assert len(keys) == 3 and out["partitions"] == 3
    for prefix in ("exports/vendite/paese=DE/", "exports/vendite/paese=FR/", "exports/vendite/paese=IT/"):
        assert any(k.startswith(prefix) and k.endswith(".parquet") for k in keys)


def test_partizioni_multiple_colonne(storage, sorgente, recorder):
    out = write_output_to_s3(
        _conn(),
        S3DestinationSpec(bucket="c", key="p", partition_by=["paese", "anno"]),
        BUCKET, sorgente.key, storage=storage,
    )
    assert out["partitions"] == 4  # IT/2024, IT/2025, FR/2024, DE/2024
    assert any("paese=IT/anno=2025/" in k for _, k in recorder.uploads)


def test_bucket_default_dalla_connessione(storage, sorgente, recorder):
    write_output_to_s3(
        _conn(bucket="default-bucket"), S3DestinationSpec(key="x.parquet"),
        BUCKET, sorgente.key, storage=storage,
    )
    assert recorder.uploads[0][0] == "default-bucket"


def test_nessun_bucket_errore_parlante(storage, sorgente, recorder):
    with pytest.raises(S3DestinationError, match="bucket"):
        write_output_to_s3(_conn(), S3DestinationSpec(key="x.parquet"), BUCKET, sorgente.key, storage=storage)


def test_colonna_partizione_inesistente(storage, sorgente, recorder):
    with pytest.raises(S3DestinationError, match="inesistenti"):
        write_output_to_s3(
            _conn(), S3DestinationSpec(bucket="c", key="p", partition_by=["boh"]),
            BUCKET, sorgente.key, storage=storage,
        )


def test_troppe_partizioni_rifiutate(storage, sorgente, recorder, monkeypatch):
    monkeypatch.setattr(s3d, "MAX_PARTITIONS", 2)
    with pytest.raises(S3DestinationError, match="partizioni"):
        write_output_to_s3(
            _conn(), S3DestinationSpec(bucket="c", key="p", partition_by=["paese"]),
            BUCKET, sorgente.key, storage=storage,
        )


# ── validazioni pure ─────────────────────────────────────────────────────────
def test_clean_key_normalizza_slash():
    assert _clean_key("/exports/vendite.parquet/") == "exports/vendite.parquet"


def test_clean_key_vuota_o_traversal_rifiutata():
    with pytest.raises(S3DestinationError):
        _clean_key("  ")
    with pytest.raises(S3DestinationError):
        _clean_key("a/../b")
    with pytest.raises(S3DestinationError):
        _clean_key("a//b")


def test_secret_in_chiaro_solo_sviluppo():
    conn = S3ConnectionSpec(access_key="k", secret_key="chiaro")
    assert conn.resolve_secret() == "chiaro"
