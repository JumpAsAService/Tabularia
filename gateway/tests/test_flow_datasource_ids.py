"""A flow may name a datasource by id alone, and a run resolves that id with the
authority of whoever runs it; for a schedule, its author. So whoever saves a flow
must be able to read every datasource it names, or EDIT on a folder would let them
make an admin's nightly schedule copy a datasource they cannot read into their folder.
"""
import json

import pytest
from fastapi import HTTPException

from app.models.permission import Capability
from app.routes.flows import update_flow
from app.schemas.models import FlowUpdate
from tests.conftest import make_datasource, make_flow, make_permission, make_project, make_user


@pytest.fixture
def scene(session):
    anna = make_user(session, email="anna@x.local", is_superuser=True)
    mario = make_user(session, email="mario@x.local")
    finance = make_project(session, name="Finance")
    sales = make_project(session, name="Sales")
    make_permission(session, user_id=mario.id, project_id=sales.id, capability=Capability.EDIT)
    salaries = make_datasource(session, name="salaries", project_id=finance.id)
    orders = make_datasource(session, name="orders", project_id=sales.id)
    report = make_flow(session, name="Sales report", project_id=sales.id, owner_id=anna.id,
                       run_schedule="0 3 * * *", run_scheduled_by=anna.id, definition="{}")
    return dict(anna=anna, mario=mario, salaries=salaries, orders=orders, report=report)


def _definition(*datasource_ids, nested=None):
    nodes = [{"id": f"src{i}", "type": "source", "data": {"datasourceId": d}} for i, d in enumerate(datasource_ids)]
    if nested is not None:
        nodes.append({"id": "loop", "type": "operation", "data": {"opType": "foreach", "params": {"source": {"datasourceId": nested}}}})
    nodes.append({"id": "out", "type": "output", "data": {"destType": "datasource", "name": "copy"}})
    return json.dumps({"nodes": nodes, "edges": []})


def _save(session, user, flow, definition):
    return update_flow(flow.id, FlowUpdate(definition=definition), request=None, user=user, session=session)


def test_a_datasource_the_editor_cannot_read_is_refused(session, scene):
    s = scene
    with pytest.raises(HTTPException) as e:
        _save(session, s["mario"], s["report"], _definition(s["salaries"].id))
    assert e.value.status_code == 403


def test_also_when_it_is_nested_in_an_operation(session, scene):
    s = scene
    with pytest.raises(HTTPException) as e:
        _save(session, s["mario"], s["report"], _definition(s["orders"].id, nested=s["salaries"].id))
    assert e.value.status_code == 403


def test_datasources_the_editor_reads_are_fine_and_the_admin_reads_all(session, scene):
    s = scene
    assert _save(session, s["mario"], s["report"], _definition(s["orders"].id)).id == s["report"].id
    assert _save(session, s["anna"], s["report"], _definition(s["orders"].id, s["salaries"].id)).id == s["report"].id


def test_a_datasource_deleted_since_does_not_block_the_save(session, scene):
    s = scene
    assert _save(session, s["mario"], s["report"], _definition(s["orders"].id, 99_999)).id == s["report"].id
