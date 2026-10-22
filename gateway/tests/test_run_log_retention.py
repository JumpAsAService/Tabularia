"""How long runs keep their logs (services/run_log_retention.py, ORCHESTRATOR__LOG_RETENTION_DAYS).

Fixed here: past the window a run loses its logs — a Script or Container node's output,
the engine's traceback — and keeps everything else (outcome, short error, attempts,
exit code), saying the log expired; recent runs keep theirs; a second pass changes
nothing; 0 keeps every log.
"""
import json
from datetime import datetime, timedelta, timezone

from sqlmodel import select

from app.models import Run
from app.services import run_log_retention

NOW = datetime(2026, 10, 22, 12, 0)


def _run(session, kind, days_ago, *, log="", error_detail=None):
    at = NOW - timedelta(days=days_ago)
    info = {"node_id": "n1", "label": "Step", "attempts": 2, "exit_code": 1, "log": log}
    r = Run(kind=kind, task_id="", status="FAILURE", input_key="", output_bucket="", output_key="",
            started_at=at, finished_at=at, error="it ended with exit code 1", error_detail=error_detail,
            script=json.dumps(info) if kind in ("script", "container") else None)
    session.add(r)
    session.commit()
    return r.id


def test_old_runs_lose_their_logs_and_keep_the_rest(session):
    old_script = _run(session, "script", 45, log="Traceback: boom\n" * 100)
    old_container = _run(session, "container", 31, log="hello\n")
    silent = _run(session, "container", 50, log="")
    old_flow = _run(session, "flow", 60, error_detail="Traceback (most recent call last): ...")
    recent = _run(session, "container", 5, log="still here\n", error_detail="detail")
    assert run_log_retention.svuota(session, 30, ora=NOW) == 4
    assert json.loads(session.get(Run, silent).script)["log_expired"] is False  # it printed nothing: it still says so
    session.expire_all()
    for run_id in (old_script, old_container):
        run = session.get(Run, run_id)
        info = json.loads(run.script)
        assert info["log"] == "" and info["log_expired"] is True
        assert info["attempts"] == 2 and info["exit_code"] == 1 and run.error == "it ended with exit code 1"
    assert session.get(Run, old_flow).error_detail is None and session.get(Run, old_flow).error
    fresh = session.get(Run, recent)
    assert json.loads(fresh.script)["log"] == "still here\n" and fresh.error_detail == "detail"
    assert run_log_retention.svuota(session, 30, ora=NOW) == 0  # nothing left to empty


def test_zero_keeps_every_log(session):
    _run(session, "script", 400, log="ancient\n", error_detail="x")
    assert run_log_retention.svuota(session, 0, ora=NOW) == 0
    run = session.exec(select(Run)).one()
    assert json.loads(run.script)["log"] == "ancient\n" and run.error_detail == "x"


def test_a_run_still_going_keeps_its_log(session):
    r = Run(kind="container", task_id="", status="STARTED", input_key="", output_bucket="", output_key="",
            started_at=NOW - timedelta(days=90), script=json.dumps({"log": "running for long\n"}))
    session.add(r)
    session.commit()
    assert run_log_retention.svuota(session, 30, ora=NOW) == 0


def test_the_editor_says_the_log_of_the_latest_run_expired(session):
    from app.services import script_runs
    from tests.conftest import make_flow, make_project

    flow = make_flow(session, name="f", project_id=make_project(session, name="p").id)
    run_id = _run(session, "script", 45, log="old output\n")
    run = session.get(Run, run_id)
    run.flow_id = flow.id
    session.add(run)
    session.commit()
    run_log_retention.svuota(session, 30, ora=NOW)
    last = script_runs.latest_outputs(session, flow.id, ["n1"])["n1"]["last"]
    assert last["log"] == "" and last["log_expired"] is True
