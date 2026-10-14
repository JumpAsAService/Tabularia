"""Flow groups in folders: the grid is checked on save, folder permissions apply as
for flows, and moving or deleting one leaves nothing broken behind.
"""
import json

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlmodel import select

from app.models import AuditLog, FlowGroup, Run
from app.models.permission import Capability
from app.routes.flow_groups import (
    create_flow_group,
    delete_flow_group,
    get_flow_group,
    list_flow_groups,
    list_project_flow_groups,
    update_flow_group,
)
from app.routes.projects import delete_project
from app.schemas.models import FlowGroupCreate, FlowGroupDefinition, FlowGroupUpdate
from tests.conftest import make_datasource, make_flow, make_permission, make_project, make_run, make_user


def grid(*rows):
    """grid(["f1", "r2"], ["f3", None]) → a definition: fN runs flow N, rN refreshes datasource N."""
    def cell(c):
        if c is None:
            return None
        return {"kind": "flow" if c[0] == "f" else "refresh", "id": int(c[1:])}
    return FlowGroupDefinition.model_validate({"steps": [[cell(c) for c in row] for row in rows]})


@pytest.fixture
def world(session):
    admin = make_user(session, email="admin@x.local", is_superuser=True)
    p = make_project(session, name="Pipelines")
    other = make_project(session, name="Elsewhere")
    secret = make_project(session, name="Secret")
    f1 = make_flow(session, name="orders", project_id=p.id)
    f2 = make_flow(session, name="margins", project_id=p.id)
    hidden_flow = make_flow(session, name="private", project_id=secret.id)
    db = make_datasource(session, name="erp", project_id=p.id, kind="database")
    published = make_datasource(session, name="report", project_id=p.id, kind="flow")
    return dict(admin=admin, p=p, other=other, secret=secret, f1=f1, f2=f2, hidden_flow=hidden_flow, db=db, published=published)


def _create(session, user, project_id, name="nightly", definition=None):
    body = FlowGroupCreate(name=name, definition=definition or FlowGroupDefinition())
    return create_flow_group(project_id, body, request=None, user=user, session=session)


def _actions(session):
    return [a.action for a in session.exec(select(AuditLog)).all()]


# ── the grid ─────────────────────────────────────────────────────────────────
def test_a_group_keeps_its_grid_as_drawn(session, world):
    w = world
    out = _create(session, w["admin"], w["p"].id, definition=grid([f"r{w['db'].id}", None, f"f{w['f1'].id}"], [f"f{w['f2'].id}"]))
    assert [[c and (c.kind, c.id) for c in row] for row in out.definition.steps] == [
        [("refresh", w["db"].id), None, ("flow", w["f1"].id)], [("flow", w["f2"].id)]]
    assert out.cells[0][0].name == "erp" and out.cells[0][1] is None and out.cells[1][0].name == "margins"
    again = get_flow_group(out.id, user=w["admin"], session=session)
    assert again.definition == out.definition
    assert "flow_group.create" in _actions(session)


def test_a_cell_must_name_something_that_exists(session, world):
    with pytest.raises(HTTPException) as e:
        _create(session, world["admin"], world["p"].id, definition=grid(["f9999"]))
    assert e.value.status_code == 422 and "flow 9999 not found" in e.value.detail


def test_a_published_datasource_cannot_be_refreshed(session, world):
    with pytest.raises(HTTPException) as e:
        _create(session, world["admin"], world["p"].id, definition=grid([f"r{world['published'].id}"]))
    assert e.value.status_code == 422 and "cannot be refreshed" in e.value.detail


def test_the_same_object_twice_in_one_step_is_refused(session, world):
    f = world["f1"].id
    with pytest.raises(HTTPException) as e:
        _create(session, world["admin"], world["p"].id, definition=grid([f"f{f}", f"f{f}"]))
    assert e.value.status_code == 422 and "twice" in e.value.detail
    # in two different steps it is fine: run, then run again
    assert _create(session, world["admin"], world["p"].id, definition=grid([f"f{f}"], [f"f{f}"])).id


def test_the_grid_has_limits():
    with pytest.raises(ValidationError):
        FlowGroupDefinition.model_validate({"steps": [[None]] * 51})
    with pytest.raises(ValidationError):
        FlowGroupDefinition.model_validate({"steps": [[None] * 21]})
    with pytest.raises(ValidationError):
        FlowGroupDefinition.model_validate({"steps": [[{"kind": "group", "id": 1}]]})


# ── permissions ──────────────────────────────────────────────────────────────
def test_view_reads_edit_writes(session, world):
    w = world
    group = _create(session, w["admin"], w["p"].id, definition=grid([f"f{w['f1'].id}"]))
    reader = make_user(session, email="reader@x.local")
    make_permission(session, user_id=reader.id, project_id=w["p"].id, capability=Capability.VIEW)
    assert get_flow_group(group.id, user=reader, session=session).name == "nightly"
    assert [g.id for g in list_flow_groups(user=reader, session=session)] == [group.id]
    assert [g.id for g in list_project_flow_groups(w["p"].id, user=reader, session=session)] == [group.id]
    for attempt in (
        lambda: _create(session, reader, w["p"].id, name="mine"),
        lambda: update_flow_group(group.id, FlowGroupUpdate(name="x"), request=None, user=reader, session=session),
        lambda: delete_flow_group(group.id, request=None, user=reader, session=session),
    ):
        with pytest.raises(HTTPException) as e:
            attempt()
        assert e.value.status_code == 403
    seen = get_flow_group(group.id, user=reader, session=session)
    assert (seen.can_edit, seen.can_run) == (False, False)  # the page offers neither
    assert get_flow_group(group.id, user=w["admin"], session=session).can_edit


def test_an_outsider_sees_nothing(session, world):
    group = _create(session, world["admin"], world["p"].id)
    outsider = make_user(session, email="out@x.local")
    assert list_flow_groups(user=outsider, session=session) == []
    with pytest.raises(HTTPException) as e:
        get_flow_group(group.id, user=outsider, session=session)
    assert e.value.status_code == 403


def test_a_cell_out_of_the_editors_sight_is_refused_like_a_missing_one(session, world):
    w = world
    editor = make_user(session, email="ed@x.local")
    make_permission(session, user_id=editor.id, project_id=w["p"].id, capability=Capability.EDIT)
    with pytest.raises(HTTPException) as e:
        _create(session, editor, w["p"].id, definition=grid([f"f{w['hidden_flow'].id}"]))
    assert e.value.status_code == 422 and "not found" in e.value.detail


def test_cells_others_added_survive_an_edit_by_someone_who_cannot_see_them(session, world):
    w = world
    group = _create(session, w["admin"], w["p"].id, definition=grid([f"f{w['hidden_flow'].id}"]))
    editor = make_user(session, email="ed@x.local")
    make_permission(session, user_id=editor.id, project_id=w["p"].id, capability=Capability.EDIT)
    seen = get_flow_group(group.id, user=editor, session=session)
    assert seen.cells[0][0].hidden and seen.cells[0][0].name is None  # no name leaks out
    out = update_flow_group(group.id, FlowGroupUpdate(definition=grid([f"f{w['hidden_flow'].id}", f"f{w['f1'].id}"])),
                            request=None, user=editor, session=session)
    assert [(c.kind, c.id) for c in out.definition.steps[0]] == [("flow", w["hidden_flow"].id), ("flow", w["f1"].id)]


def test_a_deleted_object_shows_as_missing(session, world):
    w = world
    group = _create(session, w["admin"], w["p"].id, definition=grid([f"f{w['f2'].id}"]))
    session.delete(session.get(type(w["f2"]), w["f2"].id))
    session.commit()
    assert get_flow_group(group.id, user=w["admin"], session=session).cells[0][0].missing


# ── names, moves, deletes ────────────────────────────────────────────────────
def test_names_are_unique_in_a_folder(session, world):
    _create(session, world["admin"], world["p"].id)
    with pytest.raises(HTTPException) as e:
        _create(session, world["admin"], world["p"].id)
    assert e.value.status_code == 409
    assert _create(session, world["admin"], world["other"].id).id  # same name elsewhere


def test_moving_needs_edit_on_both_folders_and_drops_a_foreign_schedule(session, world):
    w = world
    group = _create(session, w["admin"], w["p"].id)
    stored = session.get(FlowGroup, group.id)
    stored.run_schedule, stored.run_scheduled_by = "0 2 * * *", w["admin"].id
    session.add(stored)
    session.commit()
    mover = make_user(session, email="mv@x.local")
    make_permission(session, user_id=mover.id, project_id=w["p"].id, capability=Capability.EDIT)
    with pytest.raises(HTTPException) as e:
        update_flow_group(group.id, FlowGroupUpdate(project_id=w["other"].id), request=None, user=mover, session=session)
    assert e.value.status_code == 403
    make_permission(session, user_id=mover.id, project_id=w["other"].id, capability=Capability.EDIT)
    out = update_flow_group(group.id, FlowGroupUpdate(project_id=w["other"].id), request=None, user=mover, session=session)
    assert out.project_id == w["other"].id and out.run_schedule is None and out.next_run_at is None
    detail = session.exec(select(AuditLog).where(AuditLog.action == "flow_group.update")).all()[-1].detail
    assert "schedule_cleared" in (detail if isinstance(detail, str) else json.dumps(detail))


def test_a_running_group_cannot_be_deleted(session, world):
    group = _create(session, world["admin"], world["p"].id)
    make_run(session, kind="orchestration", status="STARTED", flow_group_id=group.id, task_id="")
    with pytest.raises(HTTPException) as e:
        delete_flow_group(group.id, request=None, user=world["admin"], session=session)
    assert e.value.status_code == 409


def test_deleting_keeps_the_runs_of_its_cells(session, world):
    w = world
    group = _create(session, w["admin"], w["p"].id)
    group_run = make_run(session, kind="orchestration", status="SUCCESS", flow_group_id=group.id, task_id="")
    cell_run = make_run(session, kind="flow", status="SUCCESS", flow_id=w["f1"].id, parent_run_id=group_run.id, task_id="t-cell")
    cell_id, group_run_id = cell_run.id, group_run.id
    delete_flow_group(group.id, request=None, user=w["admin"], session=session)
    session.expunge_all()  # read the rows again, not what the session remembers
    assert session.get(FlowGroup, group.id) is None
    assert session.get(Run, group_run_id) is None
    kept = session.exec(select(Run).where(Run.id == cell_id)).first()
    assert kept is not None and kept.parent_run_id is None  # the flow's own history keeps it
    assert "flow_group.delete" in _actions(session)


def test_a_folder_with_groups_or_views_is_not_deleted_by_accident(session, world):
    w = world
    _create(session, w["admin"], w["other"].id)
    with pytest.raises(HTTPException) as e:
        delete_project(w["other"].id, user=w["admin"], session=session)
    assert e.value.status_code == 409 and "flow groups" in e.value.detail


def test_a_folder_with_saved_views_says_so_instead_of_failing(session, world):
    from app.models import SavedView

    w = world
    session.add(SavedView(name="v", project_id=w["other"].id, datasource_id=w["db"].id))
    session.commit()
    with pytest.raises(HTTPException) as e:
        delete_project(w["other"].id, user=w["admin"], session=session)
    assert e.value.status_code == 409 and "saved views" in e.value.detail
