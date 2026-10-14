"""The routes the browser polls while something runs (a run, a flow's runs, a
datasource's refreshes) ask the engine for the state of live runs. They used to wait
for it, up to 30 s, with the request's transaction open: a pooled connection held
for nothing, and with enough polls at once the next checkout froze the replica
(review of 2026-10-14; the proxy routes had already been fixed the same way). Now
the connection goes back before the wait, and the reads and permission checks run in
a worker thread, not on the event loop. A background loop's session keeps its
transaction: it may hold locks it still needs."""
import threading

import httpx
import pytest
from sqlmodel import Session

from app.models import Run
from app.models.permission import Capability
from app.routes import datasources as datasource_routes
from app.routes import runs as run_routes
from app.services import permissions
from tests.conftest import make_datasource, make_flow, make_permission, make_project, make_user

pytestmark = pytest.mark.anyio


class _Calls(list):
    watched: dict


@pytest.fixture
def engine_watch(session, monkeypatch):
    """A fake engine that notes, at each call, whether the gateway's session still
    had a transaction open; every task is still running."""
    seen = _Calls()
    watched = {"session": session}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.path, watched["session"].in_transaction()))
        return httpx.Response(200, json={"status": "STARTED"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://engine")
    monkeypatch.setattr("app.core.engine_client._client", client)
    seen.watched = watched  # type: ignore[attr-defined]
    return seen


@pytest.fixture
def scene(session):
    project = make_project(session, name="Sales")
    reader = make_user(session, email="reader@x.local")
    make_permission(session, user_id=reader.id, project_id=project.id, capability=Capability.VIEW)
    flow = make_flow(session, name="report", project_id=project.id)
    ds = make_datasource(session, name="orders", project_id=project.id, kind="database")
    flow_run = Run(kind="flow", flow_id=flow.id, task_id="t-flow", status="STARTED", input_key="", output_bucket="b", output_key="")
    ingest = Run(kind="ingest", datasource_id=ds.id, task_id="t-ingest", status="STARTED", input_key="", output_bucket="b", output_key="")
    session.add(flow_run)
    session.add(ingest)
    session.commit()
    ids = dict(flow=flow.id, ds=ds.id, flow_run=flow_run.id, ingest=ingest.id)
    reader.id  # noqa: B018 — loaded, as the request's authentication leaves it
    session.info[permissions.REQUEST_SESSION] = True  # as get_session marks every request's session
    return reader, ids


async def test_no_poll_holds_a_connection_while_the_engine_answers(session, engine_watch, scene):
    reader, ids = scene
    out = await run_routes.get_run(ids["flow_run"], user=reader, session=session)
    assert out.status == "STARTED"
    await run_routes.list_runs(ids["flow"], user=reader, session=session)
    await datasource_routes.list_datasource_runs(ids["ds"], user=reader, session=session)
    assert engine_watch == [("/tasks/t-flow", False), ("/tasks/t-flow", False), ("/tasks/t-ingest", False)]


async def test_reads_and_checks_run_in_a_worker_thread(session, engine_watch, scene, monkeypatch):
    reader, ids = scene
    threads: list[bool] = []
    real = permissions.has_capability

    def spy(*args, **kwargs):
        threads.append(threading.current_thread() is threading.main_thread())
        return real(*args, **kwargs)

    monkeypatch.setattr(permissions, "has_capability", spy)
    await run_routes.get_run(ids["flow_run"], user=reader, session=session)
    await run_routes.list_runs(ids["flow"], user=reader, session=session)
    await datasource_routes.list_datasource_runs(ids["ds"], user=reader, session=session)
    assert threads and not any(threads)  # never on the event loop's thread


async def test_a_background_session_keeps_its_transaction(db_engine, engine_watch, scene):
    _, ids = scene
    with Session(db_engine) as background:  # as the orchestrator and the scheduler open theirs
        engine_watch.watched["session"] = background
        run = background.get(Run, ids["flow_run"])
        await run_routes._reconcile(background, run)
    assert engine_watch == [("/tasks/t-flow", True)]


async def test_pending_changes_are_not_committed_early(session, engine_watch, scene):
    reader, ids = scene
    run = session.get(Run, ids["flow_run"])
    run.error = "not decided yet"  # something the caller has not committed
    await run_routes._reconcile(session, run)
    assert engine_watch == [("/tasks/t-flow", True)]
    session.rollback()
    assert session.get(Run, ids["flow_run"]).error is None
