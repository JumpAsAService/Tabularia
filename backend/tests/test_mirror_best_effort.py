"""La copia su S3 esterno è BEST-EFFORT: un suo errore non abbatte il run.

È il contratto deciso esplicitamente — «prima il run, poi la copia». Il parquet
di output è già scritto quando la copia parte, e la datasource verrà pubblicata
dal gateway leggendo questo stesso risultato: far fallire il task perché una
consegna a valle non è riuscita annullerebbe un lavoro valido.

Questo è il primo test che esercita `transform_data_task` direttamente (finora
nessuno lo faceva), perché il `try/except` da verificare vive lì e non nella
funzione di scrittura.
"""
from __future__ import annotations

import polars as pl
import pytest

from app.engine.base import ColumnInfo, DataSource, RunResult
from app.ingest.s3_destination import S3ConnectionSpec
from app.tasks.jobs import transform_data_task
from tests.conftest import BUCKET, upload_df
# il client S3 finto STATEFUL vive già accanto ai test della destinazione:
# riusarlo evita di duplicarne il comportamento (upload/list/delete) e di
# vederne divergere le due copie
from tests.test_s3_destination import RecordingClient


class _EngineFinto:
    """Engine minimo: non trasforma nulla, dichiara solo cosa ha scritto."""

    def __init__(self, key: str):
        self._key = key

    def run(self, source, operations, destination):
        return RunResult(
            destination=DataSource(bucket=BUCKET, key=self._key),
            rows_written=4,
            columns=[ColumnInfo(name="paese", dtype="String")],
        )


@pytest.fixture
def sorgente(storage):
    df = pl.DataFrame({"paese": ["IT", "IT", "FR", "DE"], "vendite": [1.0, 2.0, 3.0, 4.0]})
    return upload_df(storage, df, "out/mirror_test.parquet")


@pytest.fixture
def task_pronto(monkeypatch, storage, sorgente):
    """Aggancia engine e storage finti al task, senza rete."""
    import app.tasks.jobs as jobs
    import app.utils as utils

    monkeypatch.setattr(jobs, "get_engine", lambda name=None: _EngineFinto(sorgente.key))
    monkeypatch.setattr(utils, "get_storage_service", lambda: storage)
    return sorgente


@pytest.fixture
def recorder(monkeypatch) -> RecordingClient:
    """Niente rete: ogni S3ConnectionSpec restituisce lo stesso client finto."""
    client = RecordingClient()
    monkeypatch.setattr(S3ConnectionSpec, "client", lambda self: client)
    return client


def _mirror(key: str) -> dict:
    return {
        "connection": {"endpoint_url": "http://minio:9000", "access_key": "k", "secret_key": "s"},
        "target": {"bucket": "lake", "key": key, "format": "parquet", "partition_by": []},
    }


def test_copia_fallita_il_task_resta_riuscito(task_pronto, sorgente):
    """Chiave vuota → `write_output_to_s3` solleva. Il task NON deve propagare:
    deve concludersi con successo e riportare l'errore nel risultato."""
    out = transform_data_task(
        bucket=BUCKET,
        input_key=sorgente.key,
        operations=[],
        output_key=sorgente.key,
        mirror=_mirror(""),  # percorso vuoto: errore parlante dentro la scrittura
    )

    assert out["status"] == "success"  # il risultato primario regge
    assert out["rows_written"] == 4
    assert out["mirror"]["ok"] is False
    assert out["mirror"]["error"]  # l'errore è riportato, non ingoiato


def test_copia_riuscita_riportata(task_pronto, sorgente, recorder):
    out = transform_data_task(
        bucket=BUCKET,
        input_key=sorgente.key,
        operations=[],
        output_key=sorgente.key,
        mirror=_mirror("published/vendite/latest.parquet"),
    )

    assert out["status"] == "success"
    assert out["mirror"]["ok"] is True
    assert recorder.uploads == [("lake", "published/vendite/latest.parquet")]


def test_senza_copia_nessuna_chiave_nel_risultato(task_pronto, sorgente):
    out = transform_data_task(
        bucket=BUCKET, input_key=sorgente.key, operations=[], output_key=sorgente.key
    )
    assert out["status"] == "success" and "mirror" not in out
