"""`stopOnFailure` on the email output MUST stop the sequence.

The first test of `orchestrate`: the function had none, and it is why the defect got
through. The `raise` sat INSIDE the try whose `except Exception` is six lines below,
so the stop was collected as any error and the nodes downstream ran all the same —
taking for done a notification never sent, which is exactly what the option promises
to avoid.
"""
from __future__ import annotations

import json

import pytest

from app.models import Run
from app.services import orchestrator as orch
from tests.conftest import make_flow, make_project, make_user

pytestmark = pytest.mark.anyio


def _flow(session, *, stop_on_failure=None):
    """Two outputs in sequence: first the email, then a datasource."""
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
    # `definition` on the model is the canvas's serialized JSON, not a dict
    return make_flow(session, name="f", project_id=p.id, definition=json.dumps(definition))


def _replace(monkeypatch, launched: list[str]):
    """The email fails, everything else succeeds. Records WHO is launched."""

    def fake_resolve(definition, node, resolve_ds, default_bucket, engine_mode, script_outputs=None):
        launched.append(node["data"]["destType"])
        return {"bucket": "b", "input_key": "k.parquet", "operations": []}

    async def fake_launch(session, user, flow, body, **kw):
        kind = launched[-1]
        run = Run(flow_id=flow.id, status="RUNNING", kind="flow")
        run.id = len(launched)
        run.status = "FAILED" if kind == "email" else "SUCCESS"
        run.error = "SMTP non raggiungibile" if kind == "email" else None
        return run

    async def fake_wait(session, run):
        return run

    monkeypatch.setattr(orch, "resolve_output_request", fake_resolve)
    monkeypatch.setattr(orch, "_launch_flow_run", fake_launch)
    monkeypatch.setattr(orch, "_wait_run", fake_wait)


async def test_a_failed_email_stops_the_sequence(session, monkeypatch):
    """The heart of the finding: the node downstream must NOT run."""
    launched: list[str] = []
    _replace(monkeypatch, launched)
    user_ = make_user(session, email="a@x.local", is_superuser=True)
    flow = _flow(session)  # stopOnFailure missing = on (default)

    with pytest.raises(orch.StopSequence):
        await orch.orchestrate(session, user_, flow)

    assert launched == ["email"], f"il nodo a valle è stato eseguito comunque: {launched}"


async def test_the_abort_is_not_swallowed_as_a_plain_error(session, monkeypatch):
    """Stopping is not enough: it must GET OUT, not end up in the list of errors (that is
    where it was collected, letting the sequence go on)."""
    launched: list[str] = []
    _replace(monkeypatch, launched)
    user_ = make_user(session, email="b@x.local", is_superuser=True)

    with pytest.raises(orch.StopSequence) as e:
        await orch.orchestrate(session, user_, _flow(session))
    assert "email" in str(e.value).lower()


async def test_without_the_option_the_sequence_goes_on(session, monkeypatch):
    """The difference is WANTED and holds only for the email with the option on: turning it
    off, the error is collected and the nodes downstream run."""
    launched: list[str] = []
    _replace(monkeypatch, launched)
    user_ = make_user(session, email="c@x.local", is_superuser=True)

    errors = await orch.orchestrate(session, user_, _flow(session, stop_on_failure=False))

    assert launched == ["email", "datasource"], launched
    assert any("email" in x or "FAILED" in x for x in errors), errors


async def test_an_email_refused_at_launch_stops_the_sequence_too(session, monkeypatch):
    """Refused BEFORE its run exists (a subject whose placeholder the engine rejects,
    a recipient outside the allowed domains): the same as a failed send — the steps
    after it must not run."""
    from fastapi import HTTPException

    launched: list[str] = []
    _replace(monkeypatch, launched)

    async def refuse(session, user, flow, body, **kw):
        if launched[-1] == "email":
            raise HTTPException(status_code=422, detail="subject: unknown placeholder {week}")
        run = Run(flow_id=flow.id, status="SUCCESS", kind="flow")
        run.id = 99
        return run

    monkeypatch.setattr(orch, "_launch_flow_run", refuse)
    user_ = make_user(session, email="d@x.local", is_superuser=True)
    with pytest.raises(orch.StopSequence) as e:
        await orch.orchestrate(session, user_, _flow(session))
    assert launched == ["email"] and "{week}" in str(e.value)


async def test_an_email_refused_at_launch_without_the_option_is_collected(session, monkeypatch):
    from fastapi import HTTPException

    launched: list[str] = []
    _replace(monkeypatch, launched)

    async def refuse(session, user, flow, body, **kw):
        if launched[-1] == "email":
            raise HTTPException(status_code=422, detail="bad subject")
        run = Run(flow_id=flow.id, status="SUCCESS", kind="flow")
        run.id = 99
        return run

    monkeypatch.setattr(orch, "_launch_flow_run", refuse)
    user_ = make_user(session, email="e@x.local", is_superuser=True)
    errors = await orch.orchestrate(session, user_, _flow(session, stop_on_failure=False))
    assert launched == ["email", "datasource"] and any("bad subject" in x for x in errors), errors
