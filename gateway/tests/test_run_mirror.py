"""Copia dell'output su S3 esterno, IN AGGIUNTA alla datasource pubblicata.

La copia è BEST-EFFORT per scelta esplicita: la datasource è il risultato
primario, la copia è una consegna a valle (data engineering che legge il
prefisso concordato). Un suo fallimento — credenziali scadute, bucket pieno,
rete — non deve annullare un run riuscito né impedire la pubblicazione.

Qui si verifica la parte che vive nel gateway:
  1. il nodo Output di tipo "datasource" emette `mirror` ACCANTO a `publish`
     (non al posto suo: `destination` resta libero, così il percorso S3 in cui
     la scrittura È il risultato continua a fallire duramente);
  2. l'esito riportato dal worker viene registrato sul run — altrimenti un
     fallimento resterebbe solo nei log, invisibile in cronologia.
"""
import json

import pytest

from app.routes.runs import _reconcile
from app.services.flow_resolver import _output_body
from tests.conftest import make_run

pytestmark = pytest.mark.anyio


def _node(**data) -> dict:
    base = {"type": "output", "data": {"destType": "datasource", "name": "vendite", "projectId": 1}}
    base["data"].update(data)
    return base


# ── risoluzione del nodo Output ──────────────────────────────────────────────
def test_mirror_convive_con_publish():
    """La copia si AGGIUNGE alla datasource: entrambi i campi nel corpo-run."""
    body = _output_body(
        _node(mirrorEnabled=True, mirrorConnectionId=7, mirrorBucket="lake",
              mirrorKey="published/vendite/latest.parquet"),
        ("data-prep", "datasets/in.parquet"), [], "data-prep",
    )
    assert body["publish"]["name"] == "vendite"  # il risultato primario resta
    # nessun `format`: la copia è sempre parquet, la scelta non esiste
    assert body["mirror"] == {
        "connection_id": 7,
        "bucket": "lake",
        "key": "published/vendite/latest.parquet",
    }
    # `destination` NON è usato: là un errore sarebbe fatale, qui non deve esserlo
    assert "destination" not in body


def test_mirror_assente_se_non_richiesto():
    body = _output_body(_node(), ("data-prep", "datasets/in.parquet"), [], "data-prep")
    assert "mirror" not in body and body["publish"]["name"] == "vendite"


def test_mirror_ignorato_senza_connessione():
    """Spunta attiva ma connessione non scelta: si ignora invece di lanciare un
    run che fallirebbe la validazione a valle."""
    body = _output_body(
        _node(mirrorEnabled=True, mirrorKey="published/x.parquet"),
        ("data-prep", "datasets/in.parquet"), [], "data-prep",
    )
    assert "mirror" not in body


def test_mirror_non_tocca_gli_altri_tipi_di_output():
    """Su un Output di tipo s3 la scrittura È il risultato: nessun mirror, e la
    destinazione resta quella di sempre (fallimento vincolante)."""
    body = _output_body(
        {"type": "output", "data": {"destType": "s3", "connectionId": 3, "s3Key": "out/x.parquet",
                                    "mirrorEnabled": True, "mirrorConnectionId": 9}},
        ("data-prep", "datasets/in.parquet"), [], "data-prep",
    )
    assert body["destination"]["type"] == "s3"
    assert "mirror" not in body and "publish" not in body


# ── registrazione dell'esito sul run ─────────────────────────────────────────
async def test_copia_fallita_non_fa_fallire_il_run(session, fake_engine):
    """Il cuore del requisito: prima il run, poi la copia. Se la copia fallisce
    il run resta SUCCESS e l'errore è leggibile in cronologia."""
    run = make_run(session, kind="flow", status="STARTED", task_id="t-mirror-ko", flow_id=1)
    fake_engine.set_task(
        "t-mirror-ko", "SUCCESS",
        result={
            "rows_written": 42,
            "mirror": {"ok": False, "bucket": "lake", "key": "published/v.parquet",
                       "error": "EndpointConnectionError: impossibile raggiungere l'endpoint"},
        },
    )

    await _reconcile(session, run)
    session.refresh(run)

    assert run.status == "SUCCESS"  # il risultato primario NON è compromesso
    assert run.rows_written == 42
    esito = json.loads(run.mirror)
    assert esito["ok"] is False
    assert "EndpointConnectionError" in esito["error"]


async def test_copia_riuscita_registrata(session, fake_engine):
    run = make_run(session, kind="flow", status="STARTED", task_id="t-mirror-ok", flow_id=1)
    fake_engine.set_task(
        "t-mirror-ok", "SUCCESS",
        result={"rows_written": 7,
                "mirror": {"ok": True, "bucket": "lake", "key": "published/v.parquet", "files": 1}},
    )

    await _reconcile(session, run)
    session.refresh(run)

    assert run.status == "SUCCESS"
    assert json.loads(run.mirror)["ok"] is True


async def test_run_senza_copia_lascia_il_campo_vuoto(session, fake_engine):
    """Nessuna copia richiesta: la colonna resta NULL, non un JSON finto."""
    run = make_run(session, kind="flow", status="STARTED", task_id="t-no-mirror", flow_id=1)
    fake_engine.set_task("t-no-mirror", "SUCCESS", result={"rows_written": 3})

    await _reconcile(session, run)
    session.refresh(run)

    assert run.status == "SUCCESS" and run.mirror is None
