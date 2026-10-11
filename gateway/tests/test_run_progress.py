"""L'avanzamento dei run in corso verso il browser: la fase che l'engine riporta
(o in coda / in esecuzione quando non la dice), i conteggi, e i secondi contati
dal gateway — dall'inizio dell'esecuzione, non dal lancio. Vive nella sola
richiesta: niente colonne, niente scritture."""
from datetime import datetime, timedelta, timezone

import pytest

from app.routes.datasources import list_datasource_runs
from app.routes.runs import _reconcile, run_out
from tests.conftest import make_datasource, make_run, make_user

pytestmark = pytest.mark.anyio


def _fa(secondi: int) -> datetime:
    return datetime.now(timezone.utc) - timedelta(seconds=secondi)


async def test_in_coda_conta_dal_lancio(session, fake_engine):
    run = make_run(session, kind="ingest", status="PENDING", task_id="t-coda", started_at=_fa(40))
    fake_engine.set_task("t-coda", "PENDING")
    p = run_out(session, await _reconcile(session, run)).progress
    assert p.phase == "queued" and 39 <= p.seconds <= 42


async def test_in_esecuzione_porta_fase_e_righe_e_conta_dall_inizio_vero(session, fake_engine):
    # 5 minuti in coda, 2 in esecuzione: il tempo mostrato è quello dell'esecuzione
    run = make_run(session, kind="ingest", status="STARTED", task_id="t-legge",
                   started_at=_fa(420), engine_started_at=_fa(120))
    fake_engine.task_states["t-legge"] = {"status": "STARTED", "progress": {"phase": "reading", "rows": 1_250_000}}
    p = run_out(session, await _reconcile(session, run)).progress
    assert (p.phase, p.rows) == ("reading", 1_250_000) and 119 <= p.seconds <= 122


async def test_un_engine_che_non_dice_la_fase_e_un_run_in_esecuzione(session, fake_engine):
    run = make_run(session, kind="ingest", status="STARTED", task_id="t-vecchio", engine_started_at=_fa(5))
    fake_engine.set_task("t-vecchio", "STARTED")  # engine di prima: nessun `progress`
    p = run_out(session, await _reconcile(session, run)).progress
    assert p.phase == "running" and p.rows is None


async def test_un_avanzamento_strano_non_rompe_la_risposta(session, fake_engine):
    run = make_run(session, kind="ingest", status="STARTED", task_id="t-strano", engine_started_at=_fa(5))
    fake_engine.task_states["t-strano"] = {"status": "STARTED", "progress": {"phase": "reading", "rows": "tante"}}
    out = run_out(session, await _reconcile(session, run))
    assert out.status == "STARTED" and out.progress is None


async def test_un_run_finito_non_ha_avanzamento(session, fake_engine):
    run = make_run(session, kind="ingest", status="SUCCESS", task_id="t-fatto")
    assert run_out(session, run).progress is None


async def test_la_cronologia_della_datasource_lo_restituisce(session, fake_engine):
    admin = make_user(session, email="a@x.local", is_superuser=True)
    ds = make_datasource(session, name="ordini", kind="database")
    make_run(session, kind="ingest", status="STARTED", task_id="t-ds", datasource_id=ds.id, engine_started_at=_fa(3))
    fake_engine.task_states["t-ds"] = {"status": "STARTED", "progress": {"phase": "files", "file": 2, "files": 5, "rows": 900}}
    [r] = await list_datasource_runs(ds.id, user=admin, session=session)
    assert r.progress.model_dump(exclude={"seconds"}) == {"phase": "files", "rows": 900, "file": 2, "files": 5}
