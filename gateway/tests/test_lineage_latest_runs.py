"""Lineage: each flow's latest run and last success come from two aggregate queries
instead of loading every run (seconds and hundreds of megabytes with a year of
history), and a connection can be the center of the graph."""
from datetime import datetime, timedelta

import pytest
from fastapi import HTTPException

from app.models import Connection, Run
from app.models.permission import Capability
from app.routes.lineage import get_lineage
from app.services import lineage as lineage_service
from tests.conftest import make_datasource, make_flow, make_permission, make_project, make_user

T0 = datetime(2026, 10, 1, 8, 0)


def _run(s, flow_id, status, minutes, kind="flow"):
    s.add(Run(kind=kind, flow_id=flow_id, task_id="", status=status, input_key="", output_bucket="b", output_key="",
              started_at=T0 + timedelta(minutes=minutes), finished_at=T0 + timedelta(minutes=minutes + 1)))


def test_each_flow_shows_its_latest_run(session):
    admin = make_user(session, email="a@x.local", is_superuser=True)
    p = make_project(session, name="p")
    busy = make_flow(session, name="busy", project_id=p.id)
    idle = make_flow(session, name="idle", project_id=p.id)
    for i, status in enumerate(["SUCCESS", "FAILURE", "SUCCESS", "FAILURE"]):
        _run(session, busy.id, status, i * 10)
    _run(session, busy.id, "SUCCESS", 99, kind="orchestration")  # not an output run: not counted
    session.commit()

    g = lineage_service.build_full_graph(session, admin)
    meta = g.nodes[f"flow:{busy.id}"]["meta"]
    assert meta["last_run_status"] == "FAILURE" and meta["last_run_at"].startswith("2026-10-01T08:30")
    assert g.nodes[f"flow:{idle.id}"]["meta"]["never_run"] is True


def test_a_connection_can_be_the_center(session):
    p = make_project(session, name="p")
    reader = make_user(session, email="r@x.local")
    make_permission(session, user_id=reader.id, project_id=p.id, capability=Capability.VIEW)
    conn = Connection(name="erp", project_id=p.id, db_type="postgresql", host="db")
    session.add(conn)
    session.commit()
    ds = make_datasource(session, name="orders", project_id=p.id, kind="database", connection_id=conn.id)
    graph = get_lineage(type="connection", id=conn.id, direction="downstream", depth=2, user=reader, session=session)
    assert graph.center == f"conn:{conn.id}"
    assert {n.id for n in graph.nodes} == {f"conn:{conn.id}", f"ds:{ds.id}"}
    with pytest.raises(HTTPException) as e:
        get_lineage(type="connection", id=conn.id + 999, direction="both", depth=2, user=reader, session=session)
    assert e.value.status_code == 404
