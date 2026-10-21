"""A flow waits for each Output to really end before the next step.

There used to be a ten-minute cap counted from the launch, queue time included: with
many flows scheduled at the same minute and two worker slots, an output still queued
at ten minutes was recorded as an error and the sequence moved on, so the steps
downstream read data not written yet and the flow was closed (and could be started
again) while the output kept writing. The run's own life is bounded elsewhere
(_reconcile fails a run past ENGINE__RUN_STALE_TIMEOUT_SECONDS), so the flow just
waits.
"""
import json

import pytest

from app.models import Run
from app.services import orchestrator as orch
from tests.conftest import make_flow, make_project, make_user

pytestmark = pytest.mark.anyio


def _flow(session):
    definition = {
        "nodes": [
            {"id": "first", "type": "output", "data": {"destType": "datasource", "name": "first"}},
            {"id": "second", "type": "output", "data": {"destType": "datasource", "name": "second"}},
        ],
        "edges": [{"id": "s", "source": "first", "target": "second", "type": "sequence"}],
    }
    p = make_project(session, name="p")
    return make_flow(session, name="f", project_id=p.id, definition=json.dumps(definition))


async def test_the_next_output_waits_for_a_long_queue(session, monkeypatch):
    events: list[str] = []
    polls = {"first": 0}

    def fake_resolve(definition, node, resolve_ds, default_bucket, engine_mode, script_outputs=None):
        return {"bucket": "b", "input_key": f"{node['id']}.parquet", "operations": []}

    async def fake_launch(session, user, flow, body, **kw):
        name = body.input_key.split(".")[0]
        events.append(f"launch {name}")
        run = Run(flow_id=flow.id, status="PENDING", kind="flow", task_id=name,
                  input_key=body.input_key, output_bucket="b", output_key="")
        run.id = len(events)
        return run

    async def fake_reconcile(session, run):
        if run.task_id == "first":
            polls["first"] += 1
            # queued well past the old cap (200 polls), then it runs and succeeds
            run.status = "SUCCESS" if polls["first"] > 300 else "PENDING"
            if run.status == "SUCCESS":
                events.append("first done")
        else:
            run.status = "SUCCESS"
        return run

    monkeypatch.setattr(orch, "resolve_output_request", fake_resolve)
    monkeypatch.setattr(orch, "_launch_flow_run", fake_launch)
    monkeypatch.setattr(orch, "_reconcile", fake_reconcile)
    monkeypatch.setattr(orch, "OUTPUT_WAIT_INTERVAL_S", 0)

    user = make_user(session, email="a@x.local", is_superuser=True)
    errors = await orch.orchestrate(session, user, _flow(session))

    assert errors == []
    assert events == ["launch first", "first done", "launch second"]


async def test_an_output_that_fails_is_still_reported(session, monkeypatch):
    async def fake_launch(session, user, flow, body, **kw):
        run = Run(flow_id=flow.id, status="PENDING", kind="flow", task_id="t", input_key="", output_bucket="b", output_key="")
        run.id = 1
        return run

    async def fake_reconcile(session, run):
        run.status, run.error = "FAILURE", "the warehouse refused the insert"
        return run

    monkeypatch.setattr(orch, "resolve_output_request", lambda *a, **k: {"bucket": "b", "input_key": "k", "operations": []})
    monkeypatch.setattr(orch, "_launch_flow_run", fake_launch)
    monkeypatch.setattr(orch, "_reconcile", fake_reconcile)
    user = make_user(session, email="a@x.local", is_superuser=True)
    errors = await orch.orchestrate(session, user, _flow(session))
    assert errors and "the warehouse refused the insert" in errors[0]
