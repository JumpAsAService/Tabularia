"""I kwarg opzionali NON si inviano se non valorizzati.

Celery serializza per nome: se l'API aggiornata manda un parametro che il worker
ancora vecchio non ha in firma, il task muore di TypeError PRIMA di iniziare —
ogni run fallito per tutta la finestra di un aggiornamento progressivo, e di un
rollback. È già successo tre volte in questo repo (`mirror`, poi `sort_keys` su
preview_task, poi `mirror`+`email`+`engine` insieme qui), ogni volta perché la
correzione aveva toccato una sola chiamata e non le sorelle.

Il test non guarda i nomi di oggi: prende la firma che il worker aveva PRIMA di
queste funzionalità e verifica che quanto la route invia ci si incastri. Un
kwarg nuovo inviato incondizionatamente lo fa fallire — che è esattamente
l'errore che si vedrebbe in produzione durante l'aggiornamento.
"""
from __future__ import annotations

import inspect
from typing import Any

import pytest

from app.api.models import TransformDataRequest
from app.api.routes import tasks as route


class _Spia:
    """Sta al posto del task: registra i kwarg invece di accodare davvero."""

    def __init__(self):
        self.kwargs: dict[str, Any] | None = None

    def delay(self, **kwargs):
        self.kwargs = kwargs
        return type("R", (), {"id": "task-finto"})()


def _worker_di_ieri(bucket, input_key, operations, output_key, destination=None):
    """La firma PRIMA di mirror/email/engine: il worker che sopravvive qualche
    minuto accanto all'API nuova durante un rolling update."""


def _invia(monkeypatch, **campi) -> dict:
    spia = _Spia()
    monkeypatch.setattr(route, "transform_data_task", spia)
    req = TransformDataRequest(
        bucket="b", input_key="in.parquet", output_key="out.parquet", operations=[], **campi
    )
    route.submit_transform_data_task(req)
    assert spia.kwargs is not None
    return spia.kwargs


def test_a_plain_request_still_binds_to_the_previous_worker_signature(monkeypatch):
    """Il caso del rolling update: nessuna funzionalità nuova in uso."""
    inviati = _invia(monkeypatch)
    inspect.signature(_worker_di_ieri).bind(**inviati)  # solleva se c'è un kwarg di troppo


def test_optional_kwargs_are_omitted_when_empty(monkeypatch):
    inviati = _invia(monkeypatch)
    assert set(inviati) == {"bucket", "input_key", "operations", "output_key"}, inviati


@pytest.mark.parametrize(
    "campo, valore",
    [
        ("email", {"connection": {}, "target": {}}),
        ("mirror", {"connection": {}, "target": {}}),
        ("engine", "polars"),
        ("destination", {"type": "s3"}),
    ],
)
def test_a_valued_option_is_sent(monkeypatch, campo, valore):
    """Omettere i vuoti non deve significare perdere quelli che servono."""
    inviati = _invia(monkeypatch, **{campo: valore})
    assert inviati[campo] == valore
