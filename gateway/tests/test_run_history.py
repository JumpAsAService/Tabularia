"""The run history is asked of the database instead of being loaded (review of
2026-10-14): the Flows page's calendar is counted in SQL, each flow's latest run and
a datasource's refreshes come from indexes, and the "importing…" flag reads only
live refreshes. On SQLite here, and on a real Postgres when one is configured: the
calendar's local day is written differently there, and an index is useful only if
the query really reads it."""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text
from sqlmodel import Session, select

from app.models import Flow, Run
from app.models.run import LIVE_INGEST
from app.routes.datasources import _refreshing_ids
from app.routes.flows import _with_owner_names
from app.routes.runs import runs_activity
from app.services import run_history
from app.services.permissions import REQUEST_SESSION
from tests.conftest import make_datasource, make_flow, make_project, make_user
from tests.test_migrazioni import database_temporaneo  # noqa: F401 — a fixture
from app.db import migra

NOW = datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)


def _run(session, **kw):
    kw.setdefault("kind", "flow")
    kw.setdefault("status", "SUCCESS")
    kw.setdefault("task_id", "t")
    kw.setdefault("input_key", "")
    kw.setdefault("output_bucket", "b")
    kw.setdefault("output_key", "")
    session.add(Run(**kw))


def _scene(session):
    admin = make_user(session, email="admin@x.local", is_superuser=True)
    folder = make_project(session, name="Sales")
    busy = make_flow(session, name="busy", project_id=folder.id)
    idle = make_flow(session, name="idle", project_id=folder.id)
    ds = make_datasource(session, name="orders", kind="database", project_id=folder.id)
    # three days ago at 23:30 UTC: 01:30 of the next day in UTC+2
    late = (NOW - timedelta(days=3)).replace(hour=23, minute=30, second=0)
    for minutes, status in ((-300, "SUCCESS"), (-200, "FAILURE"), (-100, "SUCCESS")):
        _run(session, flow_id=busy.id, status=status, started_at=NOW + timedelta(minutes=minutes))
    _run(session, kind="orchestration", flow_id=busy.id, started_at=NOW + timedelta(minutes=5))  # not a flow run
    _run(session, flow_id=busy.id, status="FAILURE", trigger_type="schedule", started_at=late)
    _run(session, kind="ingest", datasource_id=ds.id, status="STARTED", started_at=NOW)
    _run(session, kind="ingest", datasource_id=ds.id, status="SUCCESS", started_at=NOW - timedelta(hours=1))
    session.commit()
    return admin, busy, idle, ds, late


def _check(session):
    admin, busy, idle, ds, late = _scene(session)
    busy_id, idle_id = busy.id, idle.id

    assert run_history.latest_flow_runs(session, [busy_id, idle_id]) == {
        busy_id: ("SUCCESS", NOW - timedelta(minutes=100))}
    listed = {f.name: f for f in _with_owner_names(session, [busy, idle])}
    assert listed["busy"].last_run_status == "SUCCESS" and listed["idle"].last_run_status is None

    # the late run falls on the next local day in UTC+2 (getTimezoneOffset() = -120)
    utc = runs_activity(days=30, day=None, tz_offset=0, user=admin, session=session)
    plus2 = runs_activity(days=30, day=None, tz_offset=-120, user=admin, session=session)
    assert sum(b.total for b in utc.buckets) == sum(b.total for b in plus2.buckets) == 7
    utc_day, local_day = late.strftime("%Y-%m-%d"), (late + timedelta(hours=2)).strftime("%Y-%m-%d")
    assert next(b for b in utc.buckets if b.key == utc_day).scheduled == 1
    late_cell = next(b for b in plus2.buckets if b.key == local_day)
    assert (late_cell.total, late_cell.scheduled, late_cell.failure, late_cell.manual) == (1, 1, 1, 0)
    assert all(b.manual == b.total - b.scheduled for b in plus2.buckets)
    hours = runs_activity(days=30, day=local_day, tz_offset=-120, user=admin, session=session)
    assert hours.granularity == "hour" and [(b.key, b.total) for b in hours.buckets] == [("01", 1)]

    assert _refreshing_ids(session, [ds]) == {ds.id}


def test_on_sqlite(session):
    session.info[REQUEST_SESSION] = True
    _check(session)


def test_on_postgres(database_temporaneo):
    migra.migra(database_temporaneo)
    with Session(database_temporaneo) as session:
        session.info[REQUEST_SESSION] = True
        _check(session)


HISTORY = """
SET session_replication_role = replica;  -- bulk rows without their parents
INSERT INTO runs (kind, flow_id, datasource_id, task_id, status, input_key, output_bucket, output_key,
                  started_at, trigger_type, publish_description, publish_overwrite, publish_sort_keys)
SELECT CASE WHEN i % 3 = 0 THEN 'ingest' WHEN i % 7 = 0 THEN 'orchestration' ELSE 'flow' END,
       CASE WHEN i % 3 = 0 THEN NULL ELSE i % 50 END,
       CASE WHEN i % 3 = 0 THEN i % 40 END,
       't' || i, CASE WHEN i % 997 = 0 THEN 'STARTED' WHEN i % 11 = 0 THEN 'FAILURE' ELSE 'SUCCESS' END,
       '', 'b', '', now() - (i || ' minutes')::interval * 10, 'schedule', '', false, '[]'
FROM generate_series(1, 50000) AS i;
SET session_replication_role = origin;
ANALYZE runs;
"""


@pytest.fixture
def history(database_temporaneo):
    """A year of runs (50,000), as the planner sees them after ANALYZE."""
    migra.migra(database_temporaneo)
    with database_temporaneo.begin() as c:
        for statement in filter(str.strip, HISTORY.split(";")):
            c.execute(text(statement))
    return database_temporaneo


def _plan(engine, query) -> str:
    with engine.connect() as c:
        return "\n".join(r[0] for r in c.execute(text(f"EXPLAIN {query}")))


@pytest.mark.parametrize("query, index", [
    ("SELECT * FROM runs WHERE datasource_id = 7 AND kind = 'ingest' ORDER BY started_at DESC LIMIT 50",
     "ix_runs_datasource_id_started_at"),
    ("SELECT * FROM runs ORDER BY started_at DESC LIMIT 50", "ix_runs_started_at"),
], ids=["a datasource's refreshes", "the history page"])
def test_postgres_reads_the_history_from_its_indexes(history, query, index):
    plan = _plan(history, query)
    assert index in plan, plan


def test_postgres_finds_each_flow_s_latest_run_without_reading_its_history(history):
    with Session(history) as session:  # the flows the runs point at, without their folder
        session.exec(text("SET session_replication_role = replica"))
        session.add_all([Flow(id=i, name=f"f{i}", project_id=1) for i in range(1, 50)])
        session.commit()
    stmt = run_history.latest_flow_runs_query(list(range(1, 50)))
    plan = _plan(history, stmt.compile(history, compile_kwargs={"literal_binds": True}))
    assert "Index Scan Backward using ix_runs_flow_id_started_at" in plan and "Limit" in plan, plan
    with Session(history) as session:
        latest = run_history.latest_flow_runs(session, [1, 2])
    assert set(latest) == {1, 2} and all(status in ("SUCCESS", "FAILURE", "STARTED") for status, _ in latest.values())


def test_postgres_reads_live_refreshes_from_the_partial_index(history):
    """The partial index serves only a query that writes its condition the same way:
    this is the statement `_refreshing_ids` sends, for a page of 40 datasources."""
    stmt = select(Run.datasource_id).where(Run.datasource_id.in_(list(range(40))), text(LIVE_INGEST))
    plan = _plan(history, stmt.compile(history, compile_kwargs={"literal_binds": True}))
    assert "ix_runs_live_ingest" in plan, plan
