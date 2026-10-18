"""Changes to who can see what leave a trace (review 2026-10-14, quality-architecture-6).

Folders had no audit entries at all, yet moving one changes the inherited permissions
of a whole subtree; deleting a user, moving a datasource (which can switch someone
else's schedule off), creating a group and stopping a worker's job were silent too.
After a leaked admin token, those are exactly the actions the log must show."""
import json

import httpx
import pytest
from sqlmodel import select
from starlette.requests import Request

from app.models import AuditLog, Group
from app.routes import datasources as ds_routes
from app.routes import groups as group_routes
from app.routes import projects as project_routes
from app.routes import queue as queue_routes
from app.routes import users as user_routes
from app.schemas.models import DatasourceUpdate, GroupCreate, ProjectCreate, ProjectUpdate
from app.services import audit
from tests.conftest import make_datasource, make_project, make_user


def _req() -> Request:
    return Request({"type": "http", "method": "POST", "path": "/", "headers": [(b"user-agent", b"pytest")],
                    "client": ("172.18.0.1", 1), "query_string": b""})


def _entries(session, action):
    return session.exec(select(AuditLog).where(AuditLog.action == action)).all()


@pytest.fixture
def admin(session):
    return make_user(session, email="admin@x.it", is_superuser=True)


def test_creating_renaming_moving_and_deleting_a_folder(session, admin):
    a = project_routes.create_project(ProjectCreate(name="Finance"), request=_req(), user=admin, session=session)
    b = project_routes.create_project(ProjectCreate(name="Shared"), request=_req(), user=admin, session=session)
    project_routes.update_project(a.id, ProjectUpdate(name="Finance 2026"), request=_req(), user=admin, session=session)
    project_routes.update_project(a.id, ProjectUpdate(parent_id=b.id), request=_req(), user=admin, session=session)
    project_routes.delete_project(a.id, request=_req(), user=admin, session=session)

    (created, _) = _entries(session, audit.FOLDER_CREATE)
    assert created.target_id == a.id and created.target_label == "Finance" and created.ip == "172.18.0.1"
    (renamed,) = _entries(session, audit.FOLDER_UPDATE)
    assert json.loads(renamed.detail)["name"] == {"from": "Finance", "to": "Finance 2026"}
    (moved,) = _entries(session, audit.FOLDER_MOVE)
    assert json.loads(moved.detail) == {"from_parent": None, "to_parent": b.id}
    (deleted,) = _entries(session, audit.FOLDER_DELETE)
    assert deleted.target_id == a.id and deleted.target_label == "Finance 2026"


def test_moving_a_datasource(session, admin):
    src, dst = make_project(session, name="src"), make_project(session, name="dst")
    ds = make_datasource(session, name="sales", project_id=src.id, refresh_schedule="0 3 * * *",
                         refresh_scheduled_by=999)
    ds_routes.update_datasource(ds.id, DatasourceUpdate(project_id=dst.id), request=_req(), user=admin,
                                session=session)
    (entry,) = _entries(session, audit.DS_UPDATE)
    assert json.loads(entry.detail) == {"moved_to": dst.id, "from_project": src.id, "schedule_cleared": True}


def test_deleting_a_user(session, admin):
    target = make_user(session, email="anna@x.it")
    user_routes.delete_user(target.id, request=_req(), session=session, current=admin)
    (entry,) = _entries(session, audit.USER_DELETE)
    assert entry.target_id == target.id and entry.target_label == "anna@x.it" and entry.actor_id == admin.id


def test_creating_a_group(session, admin):
    group_routes.create_group(GroupCreate(name="finance"), request=_req(), session=session, current=admin)
    (entry,) = _entries(session, audit.GROUP_CREATE)
    group = session.exec(select(Group)).one()
    assert entry.target_id == group.id and entry.target_label == "finance"


@pytest.mark.anyio
async def test_stopping_a_job(session, admin, monkeypatch):
    def handler(request):
        return httpx.Response(200, json={"ok": True})

    monkeypatch.setattr("app.core.engine_client._client",
                        httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://engine"))
    await queue_routes.stop_job("abc-123", request=_req(), session=session, current=admin)
    (entry,) = _entries(session, audit.QUEUE_JOB_STOP)
    assert entry.target_label == "abc-123" and entry.actor_id == admin.id
