"""Deleting an account clears every reference to it first.

Each reference is a foreign key to `users`, and Postgres refuses the delete while
one points at the account: the API answered 500 to deleting anyone who had saved a
flow version, a view or a flow group. SQLite runs the other tests with foreign keys
off, which is how it went unseen; here they are on.
"""
from sqlmodel import SQLModel, select

from app.models import (
    AiModel, Banner, ContainerRegistry, ContainerRegistryAccess, Datasource, DisabledEngine, Flow, FlowGroup,
    FlowVersion, PrivacyNotice, SavedView, User,
)
from app.routes.users import remove_user
from tests.conftest import make_datasource, make_flow, make_project, make_user

# every foreign key to users, and what deleting the account does with it. A new
# one makes the first test fail: handle it in `remove_user`, then list it here.
REFERENCES = {
    ("ai_models", "updated_by"), ("api_tokens", "user_id"), ("api_tokens", "created_by"),
    ("audit_logs", "actor_id"), ("banners", "created_by"), ("connections", "owner_id"),
    ("container_registry_access", "user_id"), ("datasources", "owner_id"),
    ("datasources", "refresh_scheduled_by"), ("disabled_engines", "disabled_by"), ("flow_groups", "owner_id"),
    ("flow_groups", "run_scheduled_by"), ("flow_versions", "created_by"), ("flows", "owner_id"),
    ("flows", "run_scheduled_by"), ("permissions", "user_id"), ("privacy_notice", "updated_by"),
    ("notebook_sessions", "user_id"), ("notebook_tickets", "user_id"), ("notebook_visitors", "user_id"), ("notebook_versions", "created_by"), ("notebooks", "draft_by"), ("notebooks", "owner_id"),
    ("projects", "owner_id"), ("runs", "launched_by"), ("saved_script_versions", "created_by"),
    ("saved_scripts", "owner_id"), ("saved_scripts", "run_scheduled_by"), ("saved_views", "owner_id"),
    ("sql_script_versions", "created_by"), ("sql_scripts", "owner_id"), ("sql_scripts", "run_scheduled_by"), ("uploads", "owner_id"),
    ("user_groups", "user_id"), ("user_sessions", "user_id"), ("webhooks", "created_by"),
}


def test_every_reference_to_users_is_known():
    found = {
        (table.name, fk.parent.name)
        for table in SQLModel.metadata.tables.values()
        for fk in table.foreign_keys
        if fk.column.table.name == "users"
    }
    assert found == REFERENCES


def test_an_account_referenced_everywhere_can_be_deleted(fk_session):
    s = fk_session
    admin = make_user(s, email="admin@x.local", is_superuser=True)
    gone = make_user(s, email="gone@x.local")
    p = make_project(s, name="p", owner_id=admin.id)
    flow = make_flow(s, name="f", project_id=p.id, owner_id=admin.id, run_schedule="0 3 * * *", run_scheduled_by=gone.id)
    ds = make_datasource(s, name="d", project_id=p.id, kind="database", refresh_schedule="0 4 * * *",
                         refresh_scheduled_by=gone.id)
    s.add_all([
        FlowVersion(flow_id=flow.id, version=1, definition="{}", created_by=gone.id),
        SavedView(name="v", project_id=p.id, datasource_id=ds.id, owner_id=gone.id),
        FlowGroup(name="g", project_id=p.id, owner_id=gone.id, run_schedule="0 5 * * *", run_scheduled_by=gone.id),
        AiModel(model_id="m", updated_by=gone.id),
        Banner(message="hello", created_by=gone.id),
        DisabledEngine(engine_id="duckdb", disabled_by=gone.id),
        PrivacyNotice(updated_by=gone.id),
        ContainerRegistry(id=1, name="Acme", host="reg.test"),
    ])
    s.commit()
    s.add(ContainerRegistryAccess(registry_id=1, user_id=gone.id))
    s.commit()
    gone_id, flow_id, ds_id = gone.id, flow.id, ds.id

    remove_user(s, gone_id)  # raised IntegrityError before
    s.expunge_all()
    assert s.get(User, gone_id) is None
    assert s.exec(select(FlowVersion.created_by)).all() == [None]
    group = s.exec(select(FlowGroup)).one()
    assert group.owner_id is None and group.run_scheduled_by is None
    assert s.get(Flow, flow_id).run_scheduled_by is None and s.get(Datasource, ds_id).refresh_scheduled_by is None
    assert s.exec(select(SavedView.owner_id)).all() == [None]
    assert s.exec(select(ContainerRegistryAccess)).all() == []  # a grant is personal: it goes with the account


def test_a_group_given_environments_and_registries_can_be_deleted(fk_session):
    """What the administration panel gives a group goes with it (its keys refused the delete)."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.core.security import hash_password
    from app.db.session import get_session
    from app.models import Group, ScriptEnvironment, ScriptEnvironmentGroup
    from app.routes import auth as auth_routes
    from app.routes import groups as groups_routes

    s = fk_session
    make_user(s, email="admin@x.local", is_superuser=True, hashed_password=hash_password("una-password-lunga"))
    g = Group(name="data science")
    s.add_all([g, ScriptEnvironment(id=1, name="Python", language="python"),
               ContainerRegistry(id=1, name="Acme", host="reg.test")])
    s.commit()
    gid = g.id
    s.add_all([ScriptEnvironmentGroup(environment_id=1, group_id=gid), ContainerRegistryAccess(registry_id=1, group_id=gid)])
    s.commit()
    app = FastAPI()
    for r in (auth_routes.router, groups_routes.router):
        app.include_router(r)
    app.dependency_overrides[get_session] = lambda: s
    c = TestClient(app)
    csrf = {"X-Tabularia-Csrf": "1"}
    login = c.post("/auth/login", json={"email": "admin@x.local", "password": "una-password-lunga"}, headers=csrf)
    assert login.status_code == 200
    assert c.delete(f"/groups/{gid}", headers=csrf).status_code == 204
    s.expunge_all()
    assert s.get(Group, gid) is None
    assert s.exec(select(ScriptEnvironmentGroup)).all() == [] and s.exec(select(ContainerRegistryAccess)).all() == []
