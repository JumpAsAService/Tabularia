"""`stopOnFailure` sull'output email DEVE fermare la sequenza.

Il primo test di `orchestrate`: la funzione non ne aveva nessuno, ed è il motivo
per cui il difetto è passato. Il `raise` stava DENTRO il try il cui
`except Exception` è sei righe sotto, quindi l'interruzione veniva raccolta come
un errore qualsiasi e i nodi a valle giravano lo stesso — dando per avvenuta una
notifica mai partita, che è esattamente ciò che l'opzione promette di evitare.
"""
from __future__ import annotations

import json

import pytest

from app.models import Run
from app.services import orchestrator as orch
from tests.conftest import make_flow, make_project, make_user

pytestmark = pytest.mark.anyio


def _flusso(session, *, stop_on_failure=None):
    """Due output in sequenza: prima l'email, poi una datasource."""
    email_data = {"destType": "email", "name": "avviso"}
    if stop_on_failure is not None:
        email_data["stopOnFailure"] = stop_on_failure
    definition = {
        "nodes": [
            {"id": "n_email", "type": "output", "data": email_data},
            {"id": "n_dopo", "type": "output", "data": {"destType": "datasource", "name": "a_valle"}},
        ],
        "edges": [],
    }
    p = make_project(session, name="p")
    # `definition` sul modello e' il JSON serializzato del canvas, non un dict
    return make_flow(session, name="f", project_id=p.id, definition=json.dumps(definition))


def _sostituisci(monkeypatch, lanciati: list[str]):
    """L'email fallisce, tutto il resto riesce. Registra CHI viene lanciato."""

    def finto_resolve(definition, node, resolve_ds, default_bucket, engine_mode):
        lanciati.append(node["data"]["destType"])
        return {"bucket": "b", "input_key": "k.parquet", "operations": []}

    async def finto_launch(session, user, flow, body, **kw):
        tipo = lanciati[-1]
        run = Run(flow_id=flow.id, status="RUNNING", kind="flow")
        run.id = len(lanciati)
        run.status = "FAILED" if tipo == "email" else "SUCCESS"
        run.error = "SMTP non raggiungibile" if tipo == "email" else None
        return run

    async def finto_wait(session, run):
        return run

    monkeypatch.setattr(orch, "resolve_output_request", finto_resolve)
    monkeypatch.setattr(orch, "_launch_flow_run", finto_launch)
    monkeypatch.setattr(orch, "_wait_run", finto_wait)


async def test_a_failed_email_stops_the_sequence(session, monkeypatch):
    """Il cuore del rilievo: il nodo a valle NON deve essere eseguito."""
    lanciati: list[str] = []
    _sostituisci(monkeypatch, lanciati)
    utente = make_user(session, email="a@x.local", is_superuser=True)
    flusso = _flusso(session)  # stopOnFailure assente = attivo (default)

    with pytest.raises(orch.StopSequence):
        await orch.orchestrate(session, utente, flusso)

    assert lanciati == ["email"], f"il nodo a valle è stato eseguito comunque: {lanciati}"


async def test_the_abort_is_not_swallowed_as_a_plain_error(session, monkeypatch):
    """Non basta che si fermi: deve USCIRE, non finire nella lista degli errori
    (era lì che veniva raccolta, facendo proseguire la sequenza)."""
    lanciati: list[str] = []
    _sostituisci(monkeypatch, lanciati)
    utente = make_user(session, email="b@x.local", is_superuser=True)

    with pytest.raises(orch.StopSequence) as e:
        await orch.orchestrate(session, utente, _flusso(session))
    assert "email" in str(e.value).lower()


async def test_without_the_option_the_sequence_goes_on(session, monkeypatch):
    """La differenza è VOLUTA e vale solo per l'email con l'opzione attiva:
    disattivandola, l'errore si raccoglie e i nodi a valle girano."""
    lanciati: list[str] = []
    _sostituisci(monkeypatch, lanciati)
    utente = make_user(session, email="c@x.local", is_superuser=True)

    errori = await orch.orchestrate(session, utente, _flusso(session, stop_on_failure=False))

    assert lanciati == ["email", "datasource"], lanciati
    assert any("email" in x or "FAILED" in x for x in errori), errori
