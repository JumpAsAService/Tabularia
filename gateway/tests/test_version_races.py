"""Two saves of the same object at the same moment (bug hunt 2026-10-28), on a real
Postgres: both read the last version, both wrote the next number, and the second fell
on the unique (object, version) with a 500.

Fixed here, for notebooks, saved scripts, SQL scripts and flows:
- two saves at once take the next numbers in turn, and the object's current version is
  the last one written: never a 500;
- a notebook saved on a version that is no longer the current one (`base_version`) is a
  409 also when the other save landed a moment before, not only when it was long done;
- a flow's history ends with the definition the flow holds.
"""
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, select

from app.core.config import get_settings
from app.db.session import get_session
from app.models import (
    Flow, FlowVersion, Notebook, NotebookVersion, SavedScript, SavedScriptVersion, ScriptEnvironment, SqlScript,
    SqlScriptVersion, User,
)
from app.services import notebooks, saved_scripts, sql_scripts
from tests.conftest import make_project, make_user
from tests.test_migrations import temporary_database  # noqa: F401 — the fixture

CSRF = {"X-Tabularia-Csrf": "1"}
PASSWORD = "una-password-lunga"
SOURCE = "import marimo\n\napp = marimo.App()\n\n\n@app.cell\ndef _():\n    return\n"

CASES = {
    "notebook": (Notebook, NotebookVersion, NotebookVersion.notebook_id, notebooks),
    "saved script": (SavedScript, SavedScriptVersion, SavedScriptVersion.script_id, saved_scripts),
    "sql script": (SqlScript, SqlScriptVersion, SqlScriptVersion.script_id, sql_scripts),
}


@pytest.fixture
def db(temporary_database):  # noqa: F811
    SQLModel.metadata.create_all(temporary_database)
    return temporary_database


def _in_step(parties: int = 2):
    """Lets the saves go on together once all of them got there, so that with the defect
    they all read before anyone writes. A save that cannot get there (it waits on the
    lock the other holds) lets the first go on after a while."""
    barrier = threading.Barrier(parties)

    def wait():
        try:
            barrier.wait(timeout=3)
        except threading.BrokenBarrierError:
            pass

    return wait


def _object(s: Session, kind: str) -> tuple[int, int]:
    model, _, _, service = CASES[kind]
    owner = make_user(s, email="owner@x.local", is_superuser=True)
    folder = make_project(s, name="F")
    env = ScriptEnvironment(name="py", language="python")
    s.add(env)
    s.commit()
    extra = {} if kind == "sql script" else {"environment_id": env.id}
    obj = model(name="X", project_id=folder.id, owner_id=owner.id, current_version=0, **extra)
    s.add(obj)
    s.flush()
    service.add_version(s, obj, {"source": SOURCE}, "created", owner)
    s.commit()
    return obj.id, owner.id


@pytest.mark.parametrize("kind", list(CASES))
def test_two_saves_at_once_take_the_next_numbers_in_turn(db, kind):
    model, version_model, fk, service = CASES[kind]
    with Session(db) as s:
        oid, uid = _object(s, kind)
    in_step = _in_step()

    def save(i: int) -> int:
        with Session(db) as s:
            obj, user = s.get(model, oid), s.get(User, uid)
            in_step()
            row = service.add_version(s, obj, {"source": f"{SOURCE}# {i}\n"}, "edited", user)
            s.commit()
            return row.version

    with ThreadPoolExecutor(2) as pool:
        assert sorted(pool.map(save, (1, 2))) == [2, 3]
    with Session(db) as s:
        assert sorted(s.exec(select(version_model.version).where(fk == oid)).all()) == [1, 2, 3]
        assert s.get(model, oid).current_version == 3


# ── a notebook's base_version, through the route ─────────────────────────────
@pytest.fixture
def notebook_app(db, monkeypatch):
    from app.routes import auth as auth_routes
    from app.routes import notebooks as notebooks_routes

    for k, v in {"SCRIPTS__ENABLED": "true", "SCRIPTS__NAMESPACE": "tab-scripts",
                 "SCRIPTS__REGISTRY": "registry.example.com/tab"}.items():
        monkeypatch.setenv(k, v)
    get_settings.cache_clear()

    def session():
        with Session(db) as s:
            yield s

    app = FastAPI()
    for r in (auth_routes.router, notebooks_routes.router):
        app.include_router(r)
    app.dependency_overrides[get_session] = session
    yield app
    get_settings.cache_clear()


@pytest.fixture
def client(notebook_app, db):
    """A logged-in client and the notebook. ONE event loop for every request: FastAPI
    builds its routing table at the first request, and two loops in two threads building
    it at once answered 405 (a fact of the test, not of the gateway's single loop)."""
    from app.core.security import hash_password

    with Session(db) as s:
        oid, uid = _object(s, "notebook")
        s.get(User, uid).hashed_password = hash_password(PASSWORD)
        s.commit()
    with TestClient(notebook_app, raise_server_exceptions=False) as c:
        assert c.post("/auth/login", json={"email": "owner@x.local", "password": PASSWORD},
                      headers=CSRF).status_code == 200
        yield c, oid


def _two_patches(c: TestClient, oid: int, bodies: list[dict], monkeypatch) -> list[int]:
    in_step, add_version = _in_step(), notebooks.add_version

    def held(*args, **kwargs):
        in_step()
        return add_version(*args, **kwargs)

    monkeypatch.setattr(notebooks, "add_version", held)
    with ThreadPoolExecutor(2) as pool:
        return sorted(pool.map(lambda b: c.patch(f"/notebooks/{oid}", json=b, headers=CSRF).status_code, bodies))


@pytest.mark.parametrize("base", [True, False])
def test_two_notebook_saves_at_once(client, db, monkeypatch, base):
    """On the same version: the second is a 409, as it is when it comes later. Without
    a version to compare: two versions, the second save's the current one."""
    c, oid = client
    bodies = [{"content": {"source": f"{SOURCE}# {i}\n"}, **({"base_version": 1} if base else {})} for i in (1, 2)]
    statuses = _two_patches(c, oid, bodies, monkeypatch)
    with Session(db) as s:
        versions = sorted(s.exec(select(NotebookVersion.version).where(NotebookVersion.notebook_id == oid)).all())
        current = s.get(Notebook, oid).current_version
    if base:
        assert (statuses, versions, current) == ([200, 409], [1, 2], 2)
    else:
        assert (statuses, versions, current) == ([200, 200], [1, 2, 3], 3)


# ── a flow's history ─────────────────────────────────────────────────────────
def test_two_flow_saves_at_once_end_the_history_with_what_the_flow_holds(db):
    from app.routes.flows import _snapshot_version

    with Session(db) as s:
        owner = make_user(s, email="owner@x.local", is_superuser=True)
        folder = make_project(s, name="F")
        flow = Flow(name="f", project_id=folder.id, owner_id=owner.id, definition='{"nodes": []}')
        s.add(flow)
        s.commit()
        _snapshot_version(s, flow, owner, note="created")
        fid, uid = flow.id, owner.id
    in_step = _in_step()

    def save(i: int) -> None:
        with Session(db) as s:
            flow, user = s.get(Flow, fid), s.get(User, uid)
            flow.definition = f'{{"nodes": [], "n": {i}}}'
            s.add(flow)
            s.commit()
            in_step()
            _snapshot_version(s, flow, user, note="edited")

    with ThreadPoolExecutor(2) as pool:
        list(pool.map(save, (1, 2)))
    with Session(db) as s:
        rows = s.exec(select(FlowVersion).where(FlowVersion.flow_id == fid).order_by(FlowVersion.version)).all()
        assert [r.version for r in rows] == list(range(1, len(rows) + 1)) and len(rows) >= 2
        assert rows[-1].definition == s.get(Flow, fid).definition


# ── the same count elsewhere: a contract's versions, an environment's ────────
def test_two_contract_saves_at_once_take_the_next_versions_in_turn(db):
    """The editor's Save twice, or two people: the contract was read (to keep its
    notices) before saving, and both wrote the same version."""
    from app.models import DataContract, DataContractVersion, Datasource
    from app.services import contracts

    rule = {"id": "r1", "kind": "not_null", "column": "id", "severity": "error"}
    with Session(db) as s:
        folder = make_project(s, name="F")
        ds = Datasource(name="d", project_id=folder.id, bucket="b", key="k")
        s.add(ds)
        s.commit()
        contracts.save(s, ds, {"description": "", "rules": [rule]}, True, None)
        s.commit()
        did = ds.id
    in_step = _in_step()

    def save(i: int) -> None:
        with Session(db) as s:
            ds = s.get(Datasource, did)
            contracts.get(s, did)  # as routes/contracts.py does to keep the notices
            in_step()
            contracts.save(s, ds, {"description": f"v{i}", "rules": [rule]}, True, None)
            s.commit()

    with ThreadPoolExecutor(2) as pool:
        list(pool.map(save, (1, 2)))
    with Session(db) as s:
        versions = sorted(s.exec(select(DataContractVersion.version).where(DataContractVersion.datasource_id == did)).all())
        assert versions == [1, 2, 3] and s.exec(select(DataContract)).one().version == 3


def test_two_environment_builds_at_once_take_the_next_numbers_in_turn(db):
    from app.routes.scripting import _new_version

    with Session(db) as s:
        admin = make_user(s, email="admin@x.local", is_superuser=True)
        env = ScriptEnvironment(name="py", language="python")
        s.add(env)
        s.commit()
        _new_version(s, env, "3.12", "", admin)
        s.commit()
        eid, uid = env.id, admin.id
    in_step = _in_step()

    def build(_) -> int:
        with Session(db) as s:
            env, admin = s.get(ScriptEnvironment, eid), s.get(User, uid)
            in_step()
            v = _new_version(s, env, "3.12", "polars", admin)
            s.commit()
            return v.number

    with ThreadPoolExecutor(2) as pool:
        assert sorted(pool.map(build, (1, 2))) == [2, 3]


# ── an agent's plan: two tool calls at once make ONE plan ────────────────────
def test_two_tool_calls_at_once_add_to_one_plan(db, monkeypatch):
    """An MCP client calls two plan tools in parallel and has no pending plan yet: each
    call used to find none and make one, leaving two plans the person had to confirm
    apart. They take turns on the caller's lock now."""
    from contextlib import contextmanager

    from app.models import AgentPlan
    from app.services.agent import plans
    from app.services.agent.registry import ToolContext

    with Session(db) as s:
        uid = make_user(s, email="agent@x.local", is_superuser=True).id
    kind = next(iter(plans.ACTIONS))
    in_step = _in_step()
    real_current = plans._current_plan

    def current_then_wait(session, ctx):  # both read before either writes, with the defect
        plan = real_current(session, ctx)
        in_step()
        return plan

    monkeypatch.setattr(plans, "_current_plan", current_then_wait)

    @contextmanager
    def sessions():
        with Session(db) as s:
            yield s

    def call(i: int) -> int:
        with Session(db) as s:
            user = s.get(User, uid)
            s.expunge(user)
        ctx = ToolContext(user=user, session_factory=sessions, request=None, origin="mcp", token_name="laptop")
        return plans.add_action(ctx, {"kind": kind, "words": f"step {i}"}, key=f"k{i}")["plan_id"]

    with ThreadPoolExecutor(2) as pool:
        ids = set(pool.map(call, (1, 2)))
    with Session(db) as s:
        pending = s.exec(select(AgentPlan).where(AgentPlan.user_id == uid, AgentPlan.status == "pending")).all()
    assert len(ids) == 1 and len(pending) == 1
    assert sorted(a["key"] for a in plans.actions_of(pending[0])) == ["k1", "k2"]
