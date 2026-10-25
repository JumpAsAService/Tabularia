"""Saved scripts, piece 6: the guided choice of an image and the Container cell of flow
groups (services/container_registry.py `image_config`, routes/registries.py,
routes/flow_groups.py, services/flow_group_runner.py; docs/specs/saved-scripts.md).

Fixed here:
- an image's configuration is read from its registry: through a multi-platform index
  (the platforms it is built for), its entrypoint, command, variables, labels, size;
- the editor reads it only for an image the person may use; nothing about others;
- a Container cell of a flow group: well formed and of an image the person may use to
  be saved; a group with a cell of an image one may not use does not open; the cell
  runs as a Container node with no edge, a run under the group's, readable with it.
"""
import json

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.db.session import get_session
from app.models import ContainerRegistryAccess, FlowGroup, Run
from app.routes import flow_groups as group_routes
from app.schemas.models import FlowGroupDefinition
from app.services import container_registry as cr
from app.services import orchestrator as orch
from tests.conftest import make_permission, make_project, make_user
from tests.test_container_runs import _registry, cluster, engine, launches  # noqa: F401 — the shared fixtures
from tests.test_registries import DIGEST, FakeRegistry

pytestmark = pytest.mark.anyio
AMD = "sha256:" + "a1" * 32
ARM = "sha256:" + "b2" * 32
CONFIG = "sha256:" + "c3" * 32


class ImageRegistry(FakeRegistry):
    """team/forecast:v3 is a multi-platform image with its configuration."""

    def handle(self, req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if req.method == "GET" and path.startswith("/v2/team/forecast/") and req.headers.get("authorization", "").startswith("Bearer tok:"):
            if path.endswith("/manifests/v3"):
                return httpx.Response(200, headers={"Docker-Content-Digest": DIGEST}, json={
                    "mediaType": "application/vnd.oci.image.index.v1+json", "manifests": [
                        {"digest": AMD, "platform": {"os": "linux", "architecture": "amd64"}},
                        {"digest": ARM, "platform": {"os": "linux", "architecture": "arm64", "variant": "v8"}},
                        {"digest": "sha256:" + "ee" * 32, "platform": {"os": "unknown", "architecture": "unknown"}}]})
            if path.endswith(f"/manifests/{AMD}"):
                return httpx.Response(200, json={
                    "mediaType": "application/vnd.oci.image.manifest.v1+json",
                    "config": {"digest": CONFIG, "size": 1200},
                    "layers": [{"digest": "sha256:" + "11" * 32, "size": 3000}, {"digest": "sha256:" + "22" * 32, "size": 7000}]})
            if path.endswith(f"/blobs/{CONFIG}"):
                return httpx.Response(200, json={
                    "architecture": "amd64", "os": "linux", "created": "2026-10-20T08:00:00Z",
                    "config": {"Entrypoint": ["python", "/app/run.py"], "Cmd": ["--days", "7"],
                               "Env": ["PATH=/usr/local/bin", "HORIZON=7"], "WorkingDir": "/app", "User": "10001",
                               "Labels": {"org.opencontainers.image.description": "Forecast by region",
                                          "org.opencontainers.image.version": "3.0"}}})
        return super().handle(req)


@pytest.fixture
def registry(monkeypatch):
    fake = ImageRegistry()
    monkeypatch.setattr(cr, "_transport", httpx.MockTransport(fake.handle))
    return fake


async def test_an_images_configuration_is_read_through_its_index(session, registry):
    r = _registry(session)
    from app.routes.registries import client_of

    out = await cr.image_config(client_of(r), "team/forecast", "v3")
    assert out["platforms"] == ["linux/amd64", "linux/arm64/v8"] and out["digest"] == DIGEST
    assert out["entrypoint"] == ["python", "/app/run.py"] and out["cmd"] == ["--days", "7"]
    assert {"name": "HORIZON", "value": "7"} in out["env"] and out["working_dir"] == "/app" and out["user"] == "10001"
    assert out["labels"]["org.opencontainers.image.version"] == "3.0" and out["size"] == 10000


def test_the_editor_reads_only_an_image_one_may_use(session, cluster, registry):
    from app.core.security import hash_password
    from app.routes import auth as auth_routes
    from app.routes import registries as registry_routes

    app = FastAPI()
    for router in (auth_routes.router, registry_routes.editor_router):
        app.include_router(router)
    app.dependency_overrides[get_session] = lambda: session
    registry_id = _registry(session, repositories="team/*\nother/*").id
    for email in ("granted@x.local", "stranger@x.local"):
        make_user(session, email=email, hashed_password=hash_password("una-password-lunga"))
    from app.models import User

    granted = session.exec(select(User).where(User.email == "granted@x.local")).one()
    session.add(ContainerRegistryAccess(registry_id=registry_id, user_id=granted.id, repositories="team/forecast"))
    session.commit()

    def client(email):
        c = TestClient(app)
        assert c.post("/auth/login", json={"email": email, "password": "una-password-lunga"}).status_code == 200
        return c
    g, s = client("granted@x.local"), client("stranger@x.local")
    ok = g.get(f"/registries/{registry_id}/image", params={"repository": "team/forecast", "reference": "v3"})
    assert ok.status_code == 200 and ok.json()["platforms"] == ["linux/amd64", "linux/arm64/v8"]
    assert g.get(f"/registries/{registry_id}/image", params={"repository": "other/x"}).status_code == 403
    assert s.get(f"/registries/{registry_id}/image", params={"repository": "team/forecast"}).status_code == 404


def _cell(registry_id, n=1, **data):
    return {"kind": "container", "id": n, "container": {
        "label": "Forecast", "registryId": registry_id, "repository": "team/forecast", "tag": "v3",
        "command": ["sh", "-c"], "args": ["echo hello"], "cpu": "100m", "memory": "64Mi", "timeoutSeconds": 120, **data}}


def test_a_container_cell_needs_an_image_one_may_use_and_closes_the_group_to_others(session, cluster):
    boss = make_user(session, email="boss@x.local", is_superuser=True)
    user = make_user(session, email="u@x.local")
    r = _registry(session)
    p = make_project(session, name="p")
    for cap in ("view", "edit", "run"):
        make_permission(session, user_id=user.id, project_id=p.id, capability=cap)
    definition = FlowGroupDefinition.model_validate({"steps": [[_cell(r.id)]]})
    assert group_routes._checked(session, boss, definition)
    with pytest.raises(HTTPException) as e:
        group_routes._checked(session, user, definition)
    assert "is not among those of «Harbor» you may use" in e.value.detail
    with pytest.raises(HTTPException) as e:
        group_routes._checked(session, boss, FlowGroupDefinition.model_validate({"steps": [[_cell(r.id, repository="")]]}))
    assert e.value.status_code == 422
    assert group_routes._labels(session, boss, definition)[0][0].name == "Forecast"
    group = FlowGroup(name="g", project_id=p.id, definition=definition.model_dump_json())
    session.add(group)
    session.commit()
    with pytest.raises(HTTPException) as e:
        group_routes.get_flow_group(group.id, user=user, session=session)
    assert e.value.status_code == 403 and "This flow group runs images" in e.value.detail
    assert group_routes.get_flow_group(group.id, user=boss, session=session).name == "g"


async def test_a_container_cell_runs_under_the_groups_run(session, db_engine, cluster, registry, engine, monkeypatch):
    monkeypatch.setattr(orch, "engine", db_engine)
    monkeypatch.setattr(orch, "heartbeat_engine", db_engine)

    async def quiet(s, run):
        return None
    monkeypatch.setattr("app.services.notifier.notify_failure", quiet)
    admin = make_user(session, email="a@x.local", is_superuser=True)
    r = _registry(session)
    p = make_project(session, name="p")
    group = FlowGroup(name="nightly", project_id=p.id, definition=json.dumps({"steps": [[_cell(r.id)]]}))
    session.add(group)
    session.commit()
    run = Run(kind="orchestration", flow_group_id=group.id, task_id="", status="STARTED", launched_by=admin.id,
              trigger_type="manual", claimed_by=orch.INSTANCE, heartbeat_at=orch._now(), input_key="",
              output_bucket="", output_key="")
    session.add(run)
    session.commit()
    run_id = run.id
    await orch.run_it(run_id)
    with Session(db_engine) as s:
        head = s.get(Run, run_id)
        child = s.exec(select(Run).where(Run.kind == "container")).one()
        cells = json.loads(head.group_cells)
    assert head.status == "SUCCESS", head.error
    assert child.parent_run_id == run_id and child.flow_id is None and child.status == "SUCCESS"
    assert cells[0][0]["state"] == "succeeded" and cells[0][0]["run_id"] == child.id
    job = next(b for k, _, b in cluster.created if k == "jobs")
    vars_ = {e["name"]: e["value"] for e in job["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert vars_["TABULARIA_FLOW_ID"] == "" and vars_["TABULARIA_INPUT"] == "" and vars_["TABULARIA_OUTPUT"] == ""
    assert job["spec"]["template"]["spec"]["containers"][0]["image"].endswith(DIGEST)


async def test_parallel_container_cells_each_point_to_their_own_run(session, db_engine, cluster, registry, engine,
                                                                     monkeypatch):
    # each cell linked to «the latest container run of the group»: a sister's, with parallel cells
    monkeypatch.setattr(orch, "engine", db_engine)
    monkeypatch.setattr(orch, "heartbeat_engine", db_engine)

    async def quiet(s, run):
        return None
    monkeypatch.setattr("app.services.notifier.notify_failure", quiet)
    admin = make_user(session, email="a@x.local", is_superuser=True)
    r = _registry(session)
    p = make_project(session, name="p")
    cells = [_cell(r.id, n=1, label="First"), _cell(r.id, n=2, label="Second"), _cell(r.id, n=3, label="Third")]
    group = FlowGroup(name="nightly", project_id=p.id, definition=json.dumps({"steps": [cells]}))
    session.add(group)
    session.commit()
    run = Run(kind="orchestration", flow_group_id=group.id, task_id="", status="STARTED", launched_by=admin.id,
              trigger_type="manual", claimed_by=orch.INSTANCE, heartbeat_at=orch._now(), input_key="",
              output_bucket="", output_key="")
    session.add(run)
    session.commit()
    run_id = run.id
    await orch.run_it(run_id)
    with Session(db_engine) as s:
        states = json.loads(s.get(Run, run_id).group_cells)[0]
        labels = {c.id: json.loads(c.script)["label"] for c in s.exec(select(Run).where(Run.kind == "container")).all()}
    assert [labels[c["run_id"]] for c in states] == ["First", "Second", "Third"]
