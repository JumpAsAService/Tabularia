"""Integrità dei run: verità dello stato, ordine degli snapshot, orchestrazioni orfane.

Regressione per i tre rilievi ALTA dell'audit 2026-09-13 (docs/audit/):

- **C1** un run dichiarato scaduto veniva marcato FAILURE senza fermare il task,
  che continuava a girare e a SCRIVERE: su un Output in append il rilancio
  dell'utente accodava le stesse righe due volte. In più il cronometro partiva
  dalla nascita della riga, quindi contava l'attesa in CODA e poteva dichiarare
  scaduto un run appena partito.
- **C2** lo scambio dello snapshot era «vince chi finisce»: un refresh lento
  partito PRIMA ma finito DOPO rimpiazzava dati più freschi e ne programmava la
  cancellazione.
- **C3** le orchestrazioni restavano STARTED per sempre dopo un riavvio del
  gateway (girano come task asyncio nel processo, non come task Celery, e
  `_reconcile` le salta).
"""
from datetime import datetime, timedelta, timezone

import pytest
from sqlmodel import select

from app.models import Datasource, PendingBlobDeletion, Run
from app.routes.runs import (
    _finalize_ingest,
    _publish_datasource,
    _reconcile,
    snapshot_key,
)
from app.services.objects import MANAGED_PREFIXES
from app.services.orchestrator import (
    ORCHESTRATION_INTERRUPTED,
    close_interrupted_orchestrations,
)
from tests.conftest import make_datasource, make_run

pytestmark = pytest.mark.anyio


def _pending_keys(session) -> list[str]:
    return [r.key for r in session.exec(select(PendingBlobDeletion)).all()]


def _ago(seconds: int) -> datetime:
    return datetime.now(timezone.utc) - timedelta(seconds=seconds)


# ── C1: lo stato dice la verità ─────────────────────────────────────────────
async def test_stale_run_is_revoked_not_just_marked_failed(session, fake_engine, monkeypatch):
    """Il cuore del rilievo: marcare FAILURE senza revocare lascia un task che
    scrive di nascosto. Ora il task viene fermato davvero."""
    import app.routes.runs as runs_mod

    monkeypatch.setattr(runs_mod, "STALE_AFTER_SECONDS", 60)
    run = make_run(
        session, kind="flow", status="STARTED", task_id="t-stale", flow_id=1,
        started_at=_ago(600), engine_started_at=_ago(600),
    )
    fake_engine.set_task("t-stale", "STARTED")  # l'engine lo dà ancora in esecuzione

    await _reconcile(session, run)
    session.refresh(run)
    assert run.status == "FAILURE"
    assert fake_engine.revoked == ["t-stale"]  # fermato sull'engine, non solo in cronologia
    assert "interrotto" in (run.error or "")


async def test_queue_wait_is_not_counted_as_execution_time(session, fake_engine, monkeypatch):
    """Un run rimasto a lungo in CODA e appena partito NON è scaduto: il
    cronometro parte dall'esecuzione reale, non dalla nascita della riga."""
    import app.routes.runs as runs_mod

    monkeypatch.setattr(runs_mod, "STALE_AFTER_SECONDS", 60)
    run = make_run(
        session, kind="flow", status="STARTED", task_id="t-queued", flow_id=1,
        started_at=_ago(3600),      # in coda da un'ora…
        engine_started_at=_ago(5),  # …ma in esecuzione da 5 secondi
    )
    fake_engine.set_task("t-queued", "STARTED")

    await _reconcile(session, run)
    session.refresh(run)
    assert run.status == "STARTED"  # niente falso fallimento
    assert fake_engine.revoked == []


async def test_execution_start_recorded_on_first_started_observation(session, fake_engine):
    run = make_run(session, kind="flow", status="PENDING", task_id="t-start", flow_id=1)
    assert run.engine_started_at is None
    fake_engine.set_task("t-start", "STARTED")

    await _reconcile(session, run)
    session.refresh(run)
    assert run.status == "STARTED"
    assert run.engine_started_at is not None  # baseline del cronometro


async def test_completed_run_is_never_revoked(session, fake_engine):
    run = make_run(session, kind="flow", status="STARTED", task_id="t-done", flow_id=1)
    fake_engine.set_task("t-done", "SUCCESS", result={"rows_written": 1, "columns": []})

    await _reconcile(session, run)
    session.refresh(run)
    assert run.status == "SUCCESS"
    assert fake_engine.revoked == []


# ── C2: vince chi ha letto per ultimo, non chi finisce per ultimo ───────────
async def test_refresh_started_earlier_does_not_clobber_fresher_snapshot(session, fake_engine):
    """Scenario esatto del rilievo, end-to-end: A parte prima, B parte dopo e
    finisce prima. Quando A finisce NON deve riportare indietro la datasource."""
    ds = make_datasource(session, name="ordini", kind="database", key="datasets/1/v0.parquet")
    run_a = make_run(session, kind="ingest", status="STARTED", task_id="t-a",
                     datasource_id=ds.id, output_key="datasets/1/a.parquet")
    run_b = make_run(session, kind="ingest", status="STARTED", task_id="t-b",
                     datasource_id=ds.id, output_key="datasets/1/b.parquet")
    assert run_a.id < run_b.id  # A ha letto la sorgente PRIMA
    ok = {"bucket": "data-prep", "rows_written": 1, "columns": []}
    fake_engine.set_task("t-a", "SUCCESS", result=ok)
    fake_engine.set_task("t-b", "SUCCESS", result=ok)

    await _reconcile(session, run_b)  # B finisce per primo
    await _reconcile(session, run_a)  # A finisce dopo, ma è più vecchio

    session.refresh(ds)
    assert ds.key == "datasets/1/b.parquet"       # resta il dato più fresco
    assert ds.snapshot_run_id == run_b.id
    assert "datasets/1/a.parquet" in _pending_keys(session)  # il parquet stantio è orfano
    assert "datasets/1/b.parquet" not in _pending_keys(session)  # il fresco NON è cancellato


async def test_finalize_ingest_accepts_newer_run_and_records_baseline(session, fake_engine):
    ds = make_datasource(session, name="live", kind="database", key="datasets/1/old.parquet")
    older = make_run(session, kind="ingest", datasource_id=ds.id, task_id="t-old")
    ds.snapshot_run_id = older.id
    session.add(ds)
    session.commit()
    newer = make_run(session, kind="ingest", datasource_id=ds.id, task_id="t-new",
                     output_key="datasets/1/new.parquet")

    _finalize_ingest(session, newer, {"bucket": "data-prep", "rows_written": 5, "columns": []})
    session.commit()
    session.refresh(ds)
    assert ds.key == "datasets/1/new.parquet"
    assert ds.snapshot_run_id == newer.id


async def test_finalize_ingest_without_baseline_is_accepted(session, fake_engine):
    """Le datasource create prima di questo campo non hanno baseline: si accetta
    (altrimenti il primo refresh dopo l'aggiornamento sarebbe rifiutato)."""
    ds = make_datasource(session, name="legacy", kind="database", key="datasets/old.parquet")
    assert ds.snapshot_run_id is None
    run = make_run(session, kind="ingest", datasource_id=ds.id, output_key="datasets/2/x.parquet")

    _finalize_ingest(session, run, {"bucket": "data-prep", "rows_written": 2, "columns": []})
    session.commit()
    session.refresh(ds)
    assert ds.key == "datasets/2/x.parquet" and ds.snapshot_run_id == run.id


async def test_publish_overwrite_rejects_older_run(session, fake_engine):
    existing = make_datasource(session, name="vendite", project_id=1, kind="flow",
                               key="datasets/3/fresh.parquet")
    older = make_run(session, kind="flow", status="SUCCESS", task_id="t-o1",
                     publish_overwrite=True, publish_name="vendite", publish_project_id=1,
                     output_key="datasets/3/stale.parquet")
    newer = make_run(session, kind="flow", status="SUCCESS", task_id="t-o2")
    existing.snapshot_run_id = newer.id  # una pubblicazione più recente è già passata
    session.add(existing)
    session.commit()

    _publish_datasource(session, older, {"rows_written": 9, "columns": []})
    session.commit()
    session.refresh(existing)
    assert existing.key == "datasets/3/fresh.parquet"  # non sovrascritta dal più vecchio
    assert "datasets/3/stale.parquet" in _pending_keys(session)


async def test_publish_creation_records_baseline(session, fake_engine):
    run = make_run(session, kind="flow", status="SUCCESS", task_id="t-crea",
                   publish_name="nuova", publish_project_id=1, output_key="datasets/new/x.parquet")
    _publish_datasource(session, run, {"rows_written": 3, "columns": []})
    session.commit()
    ds = session.exec(select(Datasource).where(Datasource.name == "nuova")).first()
    assert ds is not None and ds.snapshot_run_id == run.id


# ── Layout delle chiavi (operatività) ───────────────────────────────────────
def test_snapshot_key_is_foldered_and_timestamped():
    key = snapshot_key(42)
    assert key.startswith("datasets/42/") and key.endswith(".parquet")
    # il pinning del data plane accetta solo i prefissi gestiti: la sottocartella
    # non deve farne uscire la chiave
    assert key.startswith(MANAGED_PREFIXES)


def test_snapshot_key_without_datasource_uses_neutral_folder():
    """Alla PRIMA pubblicazione la datasource non esiste ancora: nessun id."""
    assert snapshot_key(None).startswith("datasets/new/")


def test_snapshot_keys_do_not_collide_within_the_same_second():
    keys = {snapshot_key(1) for _ in range(50)}
    assert len(keys) == 50  # il suffisso casuale evita la collisione


# ── C3: orchestrazioni orfane chiuse all'avvio ──────────────────────────────
@pytest.fixture
def orch_db(db_engine, monkeypatch):
    """`close_interrupted_orchestrations` apre una sessione propria (gira fuori da
    una richiesta): la si punta al DB di test."""
    import app.services.orchestrator as orch_mod

    monkeypatch.setattr(orch_mod, "engine", db_engine)
    return db_engine


async def test_startup_closes_orphaned_orchestrations(session, orch_db):
    stuck = make_run(session, kind="orchestration", status="STARTED", task_id="", flow_id=1)
    session.commit()

    assert close_interrupted_orchestrations() == 1

    session.expire_all()
    reloaded = session.get(Run, stuck.id)
    assert reloaded.status == "FAILURE"
    assert reloaded.finished_at is not None
    assert reloaded.error == ORCHESTRATION_INTERRUPTED


async def test_startup_leaves_finished_and_engine_backed_runs_alone(session, orch_db):
    done = make_run(session, kind="orchestration", status="SUCCESS", task_id="", flow_id=1)
    # un run di flusso ha un task sull'engine: lo riconcilia `_reconcile`, non l'avvio
    flow_run = make_run(session, kind="flow", status="STARTED", task_id="t-live", flow_id=1)
    session.commit()

    close_interrupted_orchestrations()

    session.expire_all()
    assert session.get(Run, done.id).status == "SUCCESS"
    assert session.get(Run, flow_run.id).status == "STARTED"


async def test_startup_close_is_idempotent(session, orch_db):
    make_run(session, kind="orchestration", status="STARTED", task_id="", flow_id=1)
    session.commit()

    assert close_interrupted_orchestrations() == 1
    assert close_interrupted_orchestrations() == 0  # un riavvio successivo non ha nulla da fare
