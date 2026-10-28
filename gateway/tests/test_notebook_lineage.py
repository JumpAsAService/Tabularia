"""A notebook's session as the lineage sees it, end to end through the routes: the pod
starts (START), reads with `tabularia.read` (RUNNING at the first read of each
datasource, recorded for the Lineage page), stops (COMPLETE with every datasource it
read); a pod that cannot start is a FAIL. The cluster, the pod and the engine are the
fakes of test_notebook_runtime.py."""
import json

import pytest
from openlineage.client import OpenLineageClient
from openlineage.client.transport import Transport
from sqlmodel import select

from app.core.config import get_settings
from app.models import NotebookRead
from app.services import openlineage as ol
from tests.conftest import make_datasource
from tests.test_notebook_runtime import (  # noqa: F401 — the fixtures, used by name
    _pod_client, _ready, _setup, _token, app, cluster, engine, pod,
)
from tests.test_notebooks import CSRF


class _Capture(Transport):
    kind = "capture"

    def __init__(self):
        self.events = []

    def emit(self, event):
        from openlineage.client.serde import Serde

        self.events.append(json.loads(Serde.to_json(event)))


@pytest.fixture
def capture(monkeypatch):
    t = _Capture()
    ol.set_client(OpenLineageClient(transport=t))
    monkeypatch.setattr(get_settings().openlineage, "url", "http://collector:5000")
    monkeypatch.setattr(get_settings().openlineage, "namespace", "tabularia")
    yield t
    ol.wait_idle()
    ol.set_client(None)


def test_an_editors_session_starts_reads_and_ends_as_one_run(app, session, cluster, engine, pod, capture):
    _, _, _, cleo, _, folder, nid = _setup(app, session)
    sales = make_datasource(session, name="sales", project_id=folder, key="datasets/sales.parquet").id
    cleo.post(f"/notebooks/{nid}/sessions", json={"mode": "edit"}, headers=CSRF)
    cluster.pod = "ready"
    _ready(cleo, nid)
    bearer = {"Authorization": f"Bearer {_token(cluster)}"}
    pod_client = _pod_client(app)
    for _ in range(3):  # a notebook re-runs its cells: one event, one row
        assert pod_client.post("/notebook-data/read", json={"datasource": "sales"}, headers=bearer).status_code == 200
    assert cleo.delete(f"/notebooks/{nid}/sessions/edit", headers=CSRF).status_code == 204
    ol.wait_idle()

    assert [e["eventType"] for e in capture.events] == ["START", "RUNNING", "COMPLETE"]
    assert len({e["run"]["runId"] for e in capture.events}) == 1
    assert {e["job"]["name"] for e in capture.events} == {"Analyses/Sales app (notebook)"}
    assert [i["name"] for i in capture.events[1]["inputs"]] == ["/Analyses/sales"]
    assert [i["name"] for i in capture.events[2]["inputs"]] == ["/Analyses/sales"]
    assert capture.events[0]["run"]["facets"]["tabularia"]["mode"] == "edit"
    (read,) = session.exec(select(NotebookRead)).all()
    assert (read.notebook_id, read.datasource_id, read.mode) == (nid, sales, "edit")


def test_a_session_that_cannot_start_is_a_failed_run(app, session, cluster, capture):
    _, ana, _, _, _, _, nid = _setup(app, session)
    ana.post(f"/notebooks/{nid}/sessions", json={"mode": "app"}, headers=CSRF)
    cluster.pod = "ended"
    cluster.log = "Traceback (most recent call last):\nModuleNotFoundError: No module named 'marimo'\n"
    _ready(ana, nid)
    ol.wait_idle()
    assert [e["eventType"] for e in capture.events] == ["START", "FAIL"]
    assert "marimo is not in the image" in capture.events[1]["run"]["facets"]["errorMessage"]["message"]


# ── found in the bug hunt of 2026-10-28 ──────────────────────────────────────
def test_a_notebook_deleted_while_its_pod_runs_ends_its_run(app, session, cluster, engine, pod, capture):
    # told before the notebook goes: once deleted it has no name, and the run stayed open
    # in the catalog for ever
    _, ana, _, _, _, _, nid = _setup(app, session)
    ana.post(f"/notebooks/{nid}/sessions", json={"mode": "app"}, headers=CSRF)
    cluster.pod = "ready"
    _ready(ana, nid)
    assert ana.delete(f"/notebooks/{nid}", headers=CSRF).status_code == 204
    ol.wait_idle()
    assert [e["eventType"] for e in capture.events] == ["START", "COMPLETE"]
    assert len({e["run"]["runId"] for e in capture.events}) == 1


def test_a_session_stopped_or_failed_twice_at_once_ends_its_run_once(app, session, db_engine, cluster, engine, pod, capture):
    # a person and the reaper, or two replicas, get to the same session together: each
    # with the row as it read it; the catalog hears the end once
    import asyncio

    from sqlmodel import Session

    from app.models import NotebookSession
    from app.services import notebook_runtime as runtime

    _, ana, _, _, _, _, nid = _setup(app, session)
    ana.post(f"/notebooks/{nid}/sessions", json={"mode": "app"}, headers=CSRF)
    cluster.pod = "ready"
    _ready(ana, nid)
    mine = select(NotebookSession).where(NotebookSession.notebook_id == nid)
    with Session(db_engine) as other:
        first, second = session.exec(mine).one(), other.exec(mine).one()
        asyncio.run(runtime.stop(session, first, actor=None, reason="stopped"))
        asyncio.run(runtime.stop(other, second, actor=None, reason="idle"))
    ol.wait_idle()
    assert [e["eventType"] for e in capture.events] == ["START", "COMPLETE"]

    capture.events.clear()
    cluster.pod = "pending"
    ana.post(f"/notebooks/{nid}/sessions", json={"mode": "app"}, headers=CSRF)
    with Session(db_engine) as other:
        first, second = session.exec(mine).one(), other.exec(mine).one()
        asyncio.run(runtime._fail(session, first, "no node could take it"))
        asyncio.run(runtime._fail(other, second, "it did not start within 300 s"))
    ol.wait_idle()
    assert [e["eventType"] for e in capture.events] == ["START", "FAIL"]
