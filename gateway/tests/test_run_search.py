"""Errore arricchito (traceback) e ricerca globale delle esecuzioni.

- `_reconcile` salva sia il messaggio breve (`error`) sia il traceback completo
  (`error_detail`) dell'engine.
- `search_runs` cerca i run nei progetti LEGGIBILI dell'utente, con filtro per
  stato e ricerca testuale sul motivo dell'errore.
"""
import pytest
from sqlmodel import select

from datetime import datetime, timedelta, timezone

from app.models import Run
from app.routes.runs import _reconcile, runs_activity, search_runs
from tests.conftest import (
    make_datasource,
    make_flow,
    make_permission,
    make_project,
    make_run,
    make_user,
)

pytestmark = pytest.mark.anyio


# ── error_detail (traceback) ────────────────────────────────────────────────
async def test_reconcile_failure_captures_short_and_detailed_error(session, fake_engine):
    run = make_run(session, kind="flow", status="STARTED", task_id="t-err", flow_id=1)
    fake_engine.set_task(
        "t-err", "FAILURE",
        error="ColumnNotFoundError: 'vendite'",
        error_detail="Traceback (most recent call last):\n  ...\nColumnNotFoundError: 'vendite'",
    )
    await _reconcile(session, run)
    session.refresh(run)
    assert run.status == "FAILURE"
    assert run.error == "ColumnNotFoundError: 'vendite'"
    assert "Traceback" in (run.error_detail or "")


async def test_reconcile_oom_translated_to_friendly_message(session, fake_engine):
    # worker ucciso dal limite di memoria (OOM): il messaggio grezzo SIGKILL/
    # WorkerLostError va tradotto in qualcosa di chiaro, col traceback preservato
    run = make_run(session, kind="flow", status="STARTED", task_id="t-oom", flow_id=1)
    fake_engine.set_task(
        "t-oom", "FAILURE",
        error="WorkerLostError('Worker exited prematurely: signal 9 (SIGKILL) Job: 3.')",
        error_detail="Traceback ...\nbilliard.exceptions.WorkerLostError: signal 9 (SIGKILL)",
    )
    await _reconcile(session, run)
    session.refresh(run)
    assert run.status == "FAILURE"
    assert "memoria" in run.error.lower()  # messaggio chiaro, non il SIGKILL grezzo
    assert "SIGKILL" in (run.error_detail or "")  # dettaglio tecnico preservato


async def test_reconcile_success_leaves_error_detail_empty(session, fake_engine):
    run = make_run(session, kind="flow", status="STARTED", task_id="t-ok",
                   publish_name="x", publish_project_id=1)
    fake_engine.set_task("t-ok", "SUCCESS", result={"rows_written": 1, "columns": []})
    await _reconcile(session, run)
    session.refresh(run)
    assert run.status == "SUCCESS"
    assert run.error_detail is None


# ── ricerca globale con RBAC ────────────────────────────────────────────────
def _mk_scenario(session):
    """Utente con VIEW solo su p1; run falliti in p1 e p2."""
    user = make_user(session, email="viewer@x.local")
    p1 = make_project(session, name="p1")
    p2 = make_project(session, name="p2")
    make_permission(session, user_id=user.id, project_id=p1.id, capability="view")
    f1 = make_flow(session, name="flow-uno", project_id=p1.id)
    f2 = make_flow(session, name="flow-due", project_id=p2.id)
    r1 = make_run(session, kind="flow", status="FAILURE", flow_id=f1.id, task_id="a",
                  error="ColumnNotFoundError: vendite")
    r2 = make_run(session, kind="flow", status="FAILURE", flow_id=f2.id, task_id="b",
                  error="SchemaError: tipi incompatibili")
    return user, p1, p2, f1, f2, r1, r2


async def test_search_scopes_to_readable_projects(session, fake_engine):
    user, p1, p2, f1, f2, r1, r2 = _mk_scenario(session)
    res = search_runs(status=None, q=None, limit=50, offset=0, user=user, session=session)
    assert {x.flow_name for x in res.items} == {"flow-uno"}  # solo il leggibile, non p2
    assert res.total == 1


async def test_search_superuser_sees_all(session, fake_engine):
    _mk_scenario(session)
    admin = make_user(session, email="admin@x.local", is_superuser=True)
    res = search_runs(status=None, q=None, limit=50, offset=0, user=admin, session=session)
    assert {x.flow_name for x in res.items} == {"flow-uno", "flow-due"}


async def test_search_filters_by_status(session, fake_engine):
    user, p1, *_ = _mk_scenario(session)
    make_run(session, kind="flow", status="SUCCESS", flow_id=make_flow(session, project_id=p1.id).id, task_id="c")
    res = search_runs(status="FAILURE", q=None, limit=50, offset=0, user=user, session=session)
    assert res.items and all(x.status == "FAILURE" for x in res.items)


async def test_search_filters_by_query_text(session, fake_engine):
    user, *_ = _mk_scenario(session)
    res = search_runs(status=None, q="ColumnNotFound", offset=0, user=user, session=session, limit=50)
    assert res.total == 1 and "ColumnNotFound" in res.items[0].error


async def test_search_paginates_with_total(session, fake_engine):
    # 5 run falliti in un progetto leggibile: la pagina è limitata ma total è il totale
    admin = make_user(session, email="pager@x.local", is_superuser=True)
    f = make_flow(session, name="f", project_id=1)
    for i in range(5):
        make_run(session, kind="flow", status="FAILURE", flow_id=f.id, task_id=f"p{i}", error=f"err {i}")
    page1 = search_runs(status="FAILURE", q=None, limit=2, offset=0, user=admin, session=session)
    page3 = search_runs(status="FAILURE", q=None, limit=2, offset=4, user=admin, session=session)
    assert page1.total == 5 and len(page1.items) == 2
    assert len(page3.items) == 1  # ultima pagina


async def test_search_includes_flow_and_source_names(session, fake_engine):
    admin = make_user(session, email="a2@x.local", is_superuser=True)
    ds = make_datasource(session, name="ordini", kind="database")
    make_run(session, kind="ingest", status="FAILURE", datasource_id=ds.id, task_id="d", error="boom")
    res = search_runs(status="FAILURE", q=None, limit=50, offset=0, user=admin, session=session)
    ingest = next(x for x in res.items if x.kind == "ingest")
    assert ingest.source_name == "ordini"


# ── chi ha avviato il run (nome utente) e origine (manuale/schedule) ─────────
async def test_search_resolves_launcher_name(session, fake_engine):
    admin = make_user(session, email="admin@x.local", is_superuser=True)
    launcher = make_user(session, email="mario@x.local", full_name="Mario Rossi")
    f = make_flow(session, name="f", project_id=1)
    make_run(session, kind="flow", status="FAILURE", flow_id=f.id, task_id="x",
             error="boom", launched_by=launcher.id)
    res = search_runs(status="FAILURE", q=None, limit=50, offset=0, user=admin, session=session)
    assert res.items[0].launched_by_name == "Mario Rossi"


async def test_search_launcher_name_falls_back_to_email(session, fake_engine):
    admin = make_user(session, email="admin@x.local", is_superuser=True)
    launcher = make_user(session, email="noname@x.local")  # full_name vuoto
    f = make_flow(session, name="f", project_id=1)
    make_run(session, kind="flow", status="FAILURE", flow_id=f.id, task_id="x",
             error="boom", launched_by=launcher.id)
    res = search_runs(status="FAILURE", q=None, limit=50, offset=0, user=admin, session=session)
    assert res.items[0].launched_by_name == "noname@x.local"


async def test_search_exposes_trigger_type(session, fake_engine):
    admin = make_user(session, email="admin@x.local", is_superuser=True)
    f = make_flow(session, name="f", project_id=1)
    make_run(session, kind="orchestration", status="SUCCESS", flow_id=f.id, task_id="m",
             trigger_type="manual")
    make_run(session, kind="orchestration", status="SUCCESS", flow_id=f.id, task_id="s",
             trigger_type="schedule")
    res = search_runs(status=None, q=None, limit=50, offset=0, user=admin, session=session)
    by_task = {(x.flow_name, x.trigger_type) for x in res.items}
    assert ("f", "manual") in by_task and ("f", "schedule") in by_task


async def test_run_trigger_type_defaults_to_manual(session):
    r = make_run(session, kind="flow", status="STARTED", task_id="def")
    session.refresh(r)
    assert r.trigger_type == "manual"


# ── calendar plot (attività per giorno / drill-down orario) ─────────────────
def _now_utc():
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def test_activity_daily_buckets_count_by_status_and_origin(session):
    admin = make_user(session, email="admin@x.local", is_superuser=True)
    f = make_flow(session, name="f", project_id=1)
    now = _now_utc()
    make_run(session, kind="orchestration", status="SUCCESS", flow_id=f.id, task_id="a",
             trigger_type="manual", started_at=now)
    make_run(session, kind="orchestration", status="FAILURE", flow_id=f.id, task_id="b",
             trigger_type="schedule", started_at=now)
    res = runs_activity(days=30, day=None, tz_offset=0, user=admin, session=session)
    assert res.granularity == "day"
    today = now.strftime("%Y-%m-%d")
    bucket = next(b for b in res.buckets if b.key == today)
    assert bucket.total == 2
    assert bucket.success == 1 and bucket.failure == 1
    assert bucket.scheduled == 1 and bucket.manual == 1


async def test_activity_hour_drilldown(session):
    admin = make_user(session, email="admin@x.local", is_superuser=True)
    f = make_flow(session, name="f", project_id=1)
    now = _now_utc()
    make_run(session, kind="orchestration", status="SUCCESS", flow_id=f.id, task_id="h",
             trigger_type="schedule", started_at=now)
    day = now.strftime("%Y-%m-%d")
    res = runs_activity(days=30, day=day, tz_offset=0, user=admin, session=session)
    assert res.granularity == "hour"
    assert res.from_key == "00" and res.to_key == "23"
    bucket = next(b for b in res.buckets if b.key == f"{now.hour:02d}")
    assert bucket.total == 1 and bucket.scheduled == 1


async def test_activity_scoped_to_readable_projects(session):
    user, p1, p2, f1, f2, r1, r2 = _mk_scenario(session)  # user vede solo p1
    res = runs_activity(days=30, day=None, tz_offset=0, user=user, session=session)
    # r1 (p1, leggibile) contato; r2 (p2, non leggibile) escluso
    assert sum(b.total for b in res.buckets) == 1


async def test_activity_counts_only_high_level_runs(session):
    """Un'orchestrazione + i suoi figli output: il calendario conta 1 (solo
    l'orchestrazione), non i doppioni figli."""
    admin = make_user(session, email="admin@x.local", is_superuser=True)
    f = make_flow(session, name="f", project_id=1)
    now = _now_utc()
    orch = make_run(session, kind="orchestration", status="SUCCESS", flow_id=f.id,
                    task_id="orch", started_at=now)
    # due output figli della stessa orchestrazione (doppioni, da NON contare)
    make_run(session, kind="flow", status="SUCCESS", flow_id=f.id, task_id="c1",
             started_at=now, parent_run_id=orch.id)
    make_run(session, kind="flow", status="SUCCESS", flow_id=f.id, task_id="c2",
             started_at=now, parent_run_id=orch.id)
    # un run diretto dell'editor (alto livello, parent None → contato)
    make_run(session, kind="flow", status="SUCCESS", flow_id=f.id, task_id="direct",
             started_at=now, parent_run_id=None)
    res = runs_activity(days=30, day=None, tz_offset=0, user=admin, session=session)
    assert sum(b.total for b in res.buckets) == 2  # orchestrazione + run diretto


async def test_activity_window_excludes_old_runs(session):
    admin = make_user(session, email="admin@x.local", is_superuser=True)
    f = make_flow(session, name="f", project_id=1)
    old = _now_utc() - timedelta(days=100)
    make_run(session, kind="orchestration", status="SUCCESS", flow_id=f.id, task_id="old",
             started_at=old)
    res = runs_activity(days=30, day=None, tz_offset=0, user=admin, session=session)
    assert sum(b.total for b in res.buckets) == 0  # fuori dalla finestra di 30 giorni
