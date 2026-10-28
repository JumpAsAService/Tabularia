"""An import's progress, for whoever watches it: the phases every import reports
(databases of every type, SharePoint), the task that writes them to the result backend
at most every two seconds, and the engine that returns them.

The database path is one for every driver (ingest_db_to_parquet): here a fake driver on
SQLite exercises it, as test_ingest_row_groups."""
import pytest

from app.api.routes import tasks as tasks_route
from app.ingest import db_source
from app.ingest import sharepoint_source as sp
from app.ingest.db_source import DbConnectionSpec, DbSourceSpec, _dbapi_batches, ingest_db_to_parquet
from app.tasks import jobs
from tests.fake_graph import FakeGraph
from tests.fakes import FakeStorage
from tests.test_ingest_row_groups import _cursor
from tests.test_sharepoint_source import TREE, _client, _conn


def test_the_database_import_says_waiting_rows_and_saving(monkeypatch):
    rows = [(i, f"n{i}") for i in range(25)]
    monkeypatch.setitem(db_source._DRIVERS, "postgresql",
                        lambda conn, query: _dbapi_batches(_cursor(rows), batch_rows=10))
    seen = []
    ingest_db_to_parquet(DbConnectionSpec(db_type="postgresql", host="h", database="d"), DbSourceSpec(mode="table", ref="t"),
                         bucket="b", key="k.parquet", storage=FakeStorage(), progress=lambda f, **c: seen.append((f, c)))
    assert seen == [("waiting", {}), ("reading", {"rows": 10}), ("reading", {"rows": 20}), ("reading", {"rows": 25}),
                     ("saving", {"rows": 25})]


def test_the_sharepoint_import_counts_the_files(monkeypatch):
    seen = []
    sp.ingest_sharepoint_to_parquet(_conn(), sp.SharePointSourceSpec(path="Budget/2026/*.xlsx", sheet="Dati"), "b", "k.parquet",
                                    FakeStorage(), client=_client(FakeGraph(TREE)), progress=lambda f, **c: seen.append((f, c)))
    # feb.xlsx (1 row) then gen.xlsx (2 rows): the rows are those read BEFORE the file
    assert seen == [("files", {"file": 1, "files": 2, "rows": 0}), ("files", {"file": 2, "files": 2, "rows": 1}),
                     ("saving", {"rows": 3})]


class _FakeTask:
    def __init__(self):
        self.request = type("R", (), {"id": "t-1", "hostname": "celery@w1"})()
        self.states = []

    def update_state(self, state, meta):
        self.states.append((state, meta))


def test_the_task_writes_at_most_every_two_seconds_but_always_at_a_phase_change(monkeypatch):
    import celery

    task = _FakeTask()
    monkeypatch.setattr(celery, "current_task", task)
    clock = iter([0.0, 0.5, 1.0, 2.5, 2.6, 2.7])
    monkeypatch.setattr(jobs._time_mod, "monotonic", lambda: next(clock))
    advances = jobs._Progress()
    for phase, rows in (("reading", 1), ("reading", 2), ("reading", 3), ("reading", 4), ("reading", 5), ("saving", 5)):
        advances(phase, rows=rows)
    avanzamenti = [(state, {k: v for k, v in meta.items() if k not in ("pid", "hostname", "since")})
                   for state, meta in task.states]
    assert avanzamenti == [("STARTED", {"phase": "reading", "rows": 1}), ("STARTED", {"phase": "reading", "rows": 4}),
                           ("STARTED", {"phase": "saving", "rows": 5})]
    # the worker and the start stay in every meta: the engine tells a lost task by them
    assert all(meta["hostname"] == "celery@w1" and meta["since"] > 0 for _, meta in task.states)


def test_outside_a_worker_it_does_nothing_and_a_fault_does_not_stop_the_import(monkeypatch):
    import celery

    monkeypatch.setattr(celery, "current_task", None)
    jobs._Progress()("reading", rows=1)  # no task: nothing to write, no errors

    class _Broken(_FakeTask):
        def update_state(self, state, meta):
            raise ConnectionError("valkey giù")

    monkeypatch.setattr(celery, "current_task", _Broken())
    jobs._Progress()("reading", rows=1)


@pytest.mark.parametrize("info, expected", [
    ({"phase": "reading", "rows": 1200, "pid": 7}, {"phase": "reading", "rows": 1200}),
    ({"phase": "files", "file": 2, "files": 5, "rows": 0}, {"phase": "files", "file": 2, "files": 5, "rows": 0}),
    ({"pid": 7, "hostname": "worker@x"}, None),  # Celery's STARTED: no progress
])
def test_the_engine_returns_the_progress(monkeypatch, info, expected):
    class _Result:
        def __init__(self, task_id, app=None):
            self.status, self.info = "STARTED", info

    monkeypatch.setattr(tasks_route, "AsyncResult", _Result)
    assert tasks_route.get_task_status("t-1").progress == expected


def test_a_task_whose_worker_died_is_failed(monkeypatch, isolated_panel_settings):
    """A worker OOMKilled or whose pod went leaves its tasks STARTED in the result backend:
    the engine says they failed, and why, instead of «running» for an hour."""
    from app.tasks import worker_pool

    info = {"pid": 9, "hostname": "celery@gone", "since": 100.0, "phase": "reading", "rows": 3}

    class _Result:
        def __init__(self, task_id, app=None):
            self.status, self.state, self.info = "STARTED", "STARTED", info

    monkeypatch.setattr(tasks_route, "AsyncResult", _Result)
    isolated_panel_settings.set(worker_pool.KEY_PREFIX + "celery@gone", '{"started_at": 200.0}')  # started again since
    out = tasks_route.get_task_status("t-1")
    assert out.status == "FAILURE" and out.error == worker_pool.LOST and out.progress is None
    monkeypatch.setattr(tasks_route.exports, "ensure_owner", lambda task_id, owner: None)
    assert tasks_route.export_status("t-1") == {"status": "failed", "code": 503, "detail": worker_pool.LOST}
    isolated_panel_settings.set(worker_pool.KEY_PREFIX + "celery@gone", '{"started_at": 50.0}')  # the same life: running
    out = tasks_route.get_task_status("t-1")
    assert out.status == "STARTED" and out.progress == {"phase": "reading", "rows": 3}
