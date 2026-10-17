"""The Output node «SFTP»: the gateway's part.

The flow's definition names a connection, a folder, a file-name template and a
format; the gateway resolves the connection (CONNECT, secrets still encrypted)
and adds what only it knows: the run's number for {run_id} and the flow's name
for {flow}. The run row exists BEFORE the task is queued, so its id can be in
the name; if the engine refuses, the row goes. At the end the run records what
was written.
"""
import json

import pytest
from fastapi import HTTPException
from sqlmodel import select

from app.core.crypto import encrypt_secret
from app.models import Connection, Run
from app.models.permission import Capability
from app.routes.runs import _friendly_error, _launch_flow_run, _reconcile
from app.schemas.models import RunCreate, RunDestinationSpec
from app.services.flow_resolver import _output_body
from tests.conftest import make_flow, make_permission, make_project, make_run, make_user

pytestmark = pytest.mark.anyio


def _conn(session, project_id, db_type="sftp", **kw):
    base = dict(name="drop", project_id=project_id, db_type=db_type, host="sftp.example.com", port=None,
                username="tab", password_encrypted=encrypt_secret("pw"), extra=json.dumps({"auth": "password"}),
                host_key_type="ssh-ed25519", host_key_fingerprint="SHA256:k")
    base.update(kw)
    c = Connection(**base)
    session.add(c)
    session.commit()
    session.refresh(c)
    return c


def _body(conn_id, **kw) -> RunCreate:
    dest = dict(type="sftp", connection_id=conn_id, folder="/out", file_name="vendite_{date}_{run_id}", format="csv")
    dest.update(kw)
    return RunCreate(bucket="data-prep", input_key="datasets/x.parquet", operations=[], destination=RunDestinationSpec(**dest))


# ── the node in the flow's definition ──────────────────────────────────────────
def test_the_output_node_becomes_an_sftp_destination():
    node = {"type": "output", "data": {"destType": "sftp", "connectionId": 4, "sftpFolder": " /out ",
                                       "sftpFileName": " report_{date} ", "sftpFormat": "xlsx"}}
    body = _output_body(node, ("data-prep", "datasets/in.parquet"), [], "data-prep")
    assert body["destination"] == {"type": "sftp", "connection_id": 4, "folder": "/out",
                                   "file_name": "report_{date}", "format": "xlsx"}
    assert "publish" not in body and "email" not in body


def test_the_format_defaults_to_csv():
    node = {"type": "output", "data": {"destType": "sftp", "connectionId": 4, "sftpFileName": "x"}}
    assert _output_body(node, ("b", "k"), [], "b")["destination"]["format"] == "csv"


# ── launching ──────────────────────────────────────────────────────────────────
async def test_the_engine_gets_the_connection_the_target_and_the_run_number(session, fake_engine):
    admin = make_user(session, email="a@x.it", is_superuser=True)
    p = make_project(session, name="p")
    flow = make_flow(session, name="Report mensile", project_id=p.id)
    conn = _conn(session, p.id)
    run = await _launch_flow_run(session, admin, flow, _body(conn.id))
    (sent,) = fake_engine.transforms
    dest = sent["destination"]
    assert dest["type"] == "sftp"
    assert dest["target"] == {"folder": "/out", "file_name": "vendite_{date}_{run_id}", "format": "csv",
                              "run_id": run.id, "flow": "Report mensile"}
    assert dest["connection"]["host_key_fingerprint"] == "SHA256:k" and "pw" not in json.dumps(sent)
    assert run.task_id == "t-transform-1"
    summary = json.loads(run.destination)
    assert summary == {"type": "sftp", "connection_id": conn.id, "host": "sftp.example.com:22", "folder": "/out",
                       "file_name": "vendite_{date}_{run_id}", "format": "csv"}


async def test_an_engine_that_refuses_leaves_no_run_behind(session, fake_engine):
    admin = make_user(session, email="a@x.it", is_superuser=True)
    p = make_project(session, name="p")
    flow = make_flow(session, name="f", project_id=p.id)
    conn = _conn(session, p.id)
    fake_engine.transform_status = 503
    with pytest.raises(HTTPException):
        await _launch_flow_run(session, admin, flow, _body(conn.id))
    assert session.exec(select(Run)).all() == []


async def test_it_needs_connect_on_the_connection(session, fake_engine):
    p = make_project(session, name="p")
    q = make_project(session, name="q")
    flow = make_flow(session, name="f", project_id=p.id)
    conn = _conn(session, q.id)
    u = make_user(session, email="u@x.it")
    make_permission(session, project_id=p.id, user_id=u.id, capability=Capability.RUN)
    make_permission(session, project_id=p.id, user_id=u.id, capability=Capability.VIEW)
    with pytest.raises(HTTPException) as e:
        await _launch_flow_run(session, u, flow, _body(conn.id))
    assert e.value.status_code == 403 and fake_engine.transforms == []


@pytest.mark.parametrize("kw, db_type, status", [
    ({}, "postgresql", 422),                      # not an SFTP connection
    ({"file_name": "  "}, "sftp", 422),           # no name
    ({"file_name": "a/b.csv"}, "sftp", 422),      # a folder in the name
    ({"folder": "out/../../etc"}, "sftp", 422),   # going up
])
async def test_what_is_refused_before_the_engine(session, fake_engine, kw, db_type, status):
    admin = make_user(session, email="a@x.it", is_superuser=True)
    p = make_project(session, name="p")
    flow = make_flow(session, name="f", project_id=p.id)
    conn = _conn(session, p.id, db_type=db_type, extra="{}" if db_type != "sftp" else json.dumps({"auth": "password"}))
    with pytest.raises(HTTPException) as e:
        await _launch_flow_run(session, admin, flow, _body(conn.id, **kw))
    assert e.value.status_code == status and fake_engine.transforms == []


async def test_a_database_output_refuses_an_sftp_connection(session, fake_engine):
    admin = make_user(session, email="a@x.it", is_superuser=True)
    p = make_project(session, name="p")
    flow = make_flow(session, name="f", project_id=p.id)
    conn = _conn(session, p.id)
    body = RunCreate(bucket="data-prep", input_key="datasets/x.parquet", operations=[],
                     destination=RunDestinationSpec(type="database", connection_id=conn.id, table="t"))
    with pytest.raises(HTTPException) as e:
        await _launch_flow_run(session, admin, flow, body)
    assert e.value.status_code == 422


async def test_an_s3_output_is_still_parquet_or_csv(session, fake_engine):
    admin = make_user(session, email="a@x.it", is_superuser=True)
    p = make_project(session, name="p")
    flow = make_flow(session, name="f", project_id=p.id)
    s3 = _conn(session, p.id, db_type="s3", name="lake", database="bucket", extra="{}")
    body = RunCreate(bucket="data-prep", input_key="datasets/x.parquet", operations=[],
                     destination=RunDestinationSpec(type="s3", connection_id=s3.id, key="k.xlsx", format="xlsx"))
    with pytest.raises(HTTPException) as e:
        await _launch_flow_run(session, admin, flow, body)
    assert e.value.status_code == 422


# ── at the end ─────────────────────────────────────────────────────────────────
async def test_the_run_records_the_file_it_wrote(session, fake_engine):
    summary = {"type": "sftp", "connection_id": 1, "host": "h:22", "folder": "/out", "file_name": "v_{date}", "format": "csv"}
    run = make_run(session, kind="flow", flow_id=1, status="STARTED", task_id="t-sftp", destination=json.dumps(summary))
    fake_engine.set_task("t-sftp", "SUCCESS", result={
        "rows_written": 2, "destination": {"host": "h:22", "path": "/out/v_2026-10-17.csv", "bytes": 40, "rows": 2, "format": "csv"}})
    run = await _reconcile(session, run)
    assert run.status == "SUCCESS"
    d = json.loads(run.destination)
    assert d["path"] == "/out/v_2026-10-17.csv" and d["bytes"] == 40 and d["file_name"] == "v_{date}"


def test_sftp_messages_are_not_replaced_by_the_out_of_memory_one():
    msg = "SFTP: authentication rejected by h:22 for user 'u': check the password"
    assert _friendly_error(msg, "Traceback … MemoryError") == msg


def test_the_lineage_graph_shows_the_file_an_sftp_output_writes(session):
    from app.services.lineage import build_full_graph

    admin = make_user(session, email="a@x.it", is_superuser=True)
    p = make_project(session, name="p")
    conn = _conn(session, p.id)
    definition = {"nodes": [{"id": "o", "type": "output", "data": {"destType": "sftp", "connectionId": conn.id,
                                                                    "sftpFolder": "/out", "sftpFileName": "v_{date}",
                                                                    "sftpFormat": "xlsx"}}], "edges": []}
    flow = make_flow(session, name="f", project_id=p.id, definition=json.dumps(definition))
    g = build_full_graph(session, admin)
    (sink,) = [n for n in g.nodes.values() if n["type"] == "sftp_sink"]
    assert sink["label"] == "v_{date}" and sink["meta"]["folder"] == "/out" and sink["meta"]["format"] == "xlsx"
    assert sink["meta"]["connection_name"] == "drop" and not sink["restricted"]
    assert {"source": f"flow:{flow.id}", "target": sink["id"], "kind": "write"} in g.edges


async def test_a_placeholder_run_whose_task_never_left_is_closed_clearly(session, fake_engine):
    """The gateway died between writing the run (for {run_id}) and queueing its task:
    the run must not stay PENDING, nor read as a timeout of a task that never existed."""
    from datetime import datetime, timedelta, timezone

    old = datetime.now(timezone.utc) - timedelta(minutes=10)
    run = make_run(session, kind="flow", flow_id=1, status="PENDING", task_id="pending-abc", started_at=old)
    run = await _reconcile(session, run)
    assert run.status == "FAILURE" and "never queued" in run.error
    young = make_run(session, kind="flow", flow_id=1, status="PENDING", task_id="pending-def")
    assert (await _reconcile(session, young)).status == "PENDING"  # still being launched
    assert not any(r.startswith("pending-") for r in fake_engine.revoked)
