"""Deleting a flow, a datasource or a connection, with foreign keys enforced as on
Postgres. Three deletes answered 500 there: a flow whose runs had started imports or
sub-flow outputs, a datasource with saved views, a connection used for the failure
notices of flows. The first now detaches those runs (they belong to the history of
what they ran); the other two say what blocks the delete."""
import pytest
from fastapi import HTTPException
from sqlmodel import select

from app.models import Connection, Datasource, Flow, Run, SavedView
from app.routes.connections import delete_connection
from app.routes.datasources import delete_datasource
from app.routes.flows import delete_flow
from tests.conftest import make_datasource, make_flow, make_project, make_user

pytestmark = pytest.mark.anyio


def _run(s, **kw):
    run = Run(task_id="", status="SUCCESS", input_key="", output_bucket="b", output_key="", **kw)
    s.add(run)
    s.commit()
    s.refresh(run)
    return run


@pytest.fixture
def world(fk_session):
    s = fk_session
    admin = make_user(s, email="admin@x.local", is_superuser=True)
    p = make_project(s, name="p", owner_id=admin.id)
    return s, admin, p


def test_a_flow_whose_runs_started_other_runs_can_be_deleted(world):
    s, admin, p = world
    flow = make_flow(s, name="main", project_id=p.id)
    sub = make_flow(s, name="sub", project_id=p.id)
    ds = make_datasource(s, name="erp", project_id=p.id, kind="database")
    orchestration = _run(s, kind="orchestration", flow_id=flow.id, launched_by=admin.id)
    imported = _run(s, kind="ingest", datasource_id=ds.id, parent_run_id=orchestration.id)
    sub_output = _run(s, kind="flow", flow_id=sub.id, parent_run_id=orchestration.id)
    ids = (flow.id, imported.id, sub_output.id)

    delete_flow(ids[0], request=None, user=admin, session=s)  # raised IntegrityError before

    s.expunge_all()
    assert s.get(Flow, ids[0]) is None
    kept = {r.id: r.parent_run_id for r in s.exec(select(Run)).all()}
    assert kept == {ids[1]: None, ids[2]: None}  # the import and the sub-flow output stay, detached


async def test_a_datasource_with_saved_views_says_which(world):
    s, admin, p = world
    ds = make_datasource(s, name="sales", project_id=p.id)
    s.add(SavedView(name="Monthly sales", project_id=p.id, datasource_id=ds.id))
    s.commit()
    with pytest.raises(HTTPException) as e:
        delete_datasource(ds.id, request=None, user=admin, session=s)
    assert e.value.status_code == 409 and "'Monthly sales'" in e.value.detail
    assert s.get(Datasource, ds.id) is not None


def test_a_connection_sending_failure_notices_says_which_flows(world):
    s, admin, p = world
    smtp = Connection(name="mail", project_id=p.id, db_type="smtp", host="smtp.example")
    s.add(smtp)
    s.commit()
    make_flow(s, name="nightly", project_id=p.id, notify_connection_id=smtp.id, notify_emails="ops@example.com")
    with pytest.raises(HTTPException) as e:
        delete_connection(smtp.id, request=None, user=admin, session=s)
    assert e.value.status_code == 409 and "'nightly'" in e.value.detail
    assert s.get(Connection, smtp.id) is not None
