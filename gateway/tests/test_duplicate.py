"""Duplicating objects as a file manager copies files: the copy lands in the same
folder as «name (i)», with the rights it takes to create one, and owns what it
copies. A datasource's copy gets a data file of its own, made on the storage."""
import json

import pytest
from fastapi import HTTPException
from sqlmodel import select

from app.core.crypto import decrypt_secret, encrypt_secret
from app.models import AuditLog, Connection, Datasource, Flow, FlowGroup, FlowVersion, SavedView
from app.models.contract import DataContract
from app.routes.connections import duplicate_connection
from app.routes.datasources import duplicate_datasource
from app.routes.flow_groups import duplicate_flow_group
from app.routes.flows import duplicate_flow
from app.routes.saved_views import duplicate_saved_view
from app.schemas.models import FlowGroupDefinition
from app.services import audit
from app.services import contracts as contract_service
from app.services.copy_names import copy_name, free_copy_name
from tests.conftest import make_datasource, make_flow, make_permission, make_project, make_user


class _Req:
    client = None; headers = {}


@pytest.fixture
def world(session):
    admin = make_user(session, email="admin@x.local", is_superuser=True)
    p = make_project(session, name="Sales")
    secret = make_project(session, name="Secret")
    return dict(admin=admin, p=p, secret=secret)


def _user(session, email, project_id, *capabilities):
    u = make_user(session, email=email)
    for c in capabilities:
        make_permission(session, user_id=u.id, project_id=project_id, capability=c)
    return u


def _audit(session, action):
    return [json.loads(a.detail or "{}") for a in session.exec(select(AuditLog).where(AuditLog.action == action)).all()]


# ── names ────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("name, taken, expected", [
    ("Orders", [], "Orders (1)"),
    ("Orders", ["Orders (1)"], "Orders (2)"),
    ("Orders", ["Orders (2)"], "Orders (1)"),  # the first free number, as files get
    ("Orders (1)", ["Orders (1)"], "Orders (2)"),  # never «Orders (1) (1)»
    ("Orders (7)", ["Orders (7)", "Orders (1)"], "Orders (2)"),
    ("Q (3) report", [], "Q (3) report (1)"),  # a number inside the name is part of it
    ("(1)", [], "(1) (1)"),  # nothing but a number: that is the name
])
def test_a_copy_is_named_as_a_file_manager_names_it(name, taken, expected):
    assert copy_name(name, taken) == expected


def test_only_names_of_the_same_folder_count_and_wildcards_are_plain_text(session, world):
    make_flow(session, name="a_b (1)", project_id=world["p"].id)
    make_flow(session, name="axb (2)", project_id=world["p"].id)  # «_» would match it in a LIKE
    make_flow(session, name="a_b (2)", project_id=world["secret"].id)
    assert free_copy_name(session, Flow, world["p"].id, "a_b") == "a_b (2)"


# ── flows ────────────────────────────────────────────────────────────────────
def test_a_flow_copy_keeps_the_definition_and_leaves_the_schedule(session, world):
    definition = json.dumps({"nodes": [{"id": "out", "type": "output", "data": {"name": "orders_clean"}}], "edges": []})
    src = make_flow(session, name="orders", project_id=world["p"].id, definition=definition, engine="duckdb",
                    production_engine="polars", description="nightly orders", run_schedule="daily:02:00",
                    run_scheduled_by=world["admin"].id, notify_emails="ops@x.local")
    editor = _user(session, "ed@x.local", world["p"].id, "edit")

    copy = duplicate_flow(src.id, _Req(), editor, session)

    assert (copy.name, copy.project_id, copy.owner_id) == ("orders (1)", world["p"].id, editor.id)
    assert (copy.definition, copy.engine, copy.production_engine, copy.description) == (
        definition, "duckdb", "polars", "nightly orders")  # Output nodes identical: the user renames them
    assert (copy.run_schedule, copy.run_scheduled_by, copy.next_run_at, copy.notify_emails) == (None, None, None, None)
    versions = session.exec(select(FlowVersion).where(FlowVersion.flow_id == copy.id)).all()
    assert [(v.version, v.definition) for v in versions] == [(1, definition)]
    assert _audit(session, audit.FLOW_CREATE)[-1]["duplicate_of"] == src.id
    assert duplicate_flow(src.id, _Req(), editor, session).name == "orders (2)"
    assert duplicate_flow(copy.id, _Req(), editor, session).name == "orders (3)"


def test_copying_a_flow_needs_edit_on_its_folder(session, world):
    src = make_flow(session, name="orders", project_id=world["p"].id)
    reader = _user(session, "r@x.local", world["p"].id, "view")
    with pytest.raises(HTTPException) as e:
        duplicate_flow(src.id, _Req(), reader, session)
    assert e.value.status_code == 403
    assert session.exec(select(Flow).where(Flow.name == "orders (1)")).first() is None


def test_a_flow_copy_reads_its_sources_with_the_rights_of_whoever_copies_it(session, world):
    hidden = make_datasource(session, name="payroll", project_id=world["secret"].id, key="datasets/9/pay.parquet")
    definition = json.dumps({"nodes": [{"id": "s", "type": "source", "data": {"datasourceId": hidden.id,
                                                                             "key": hidden.key, "bucket": hidden.bucket}}]})
    src = make_flow(session, name="pay", project_id=world["p"].id, definition=definition)
    editor = _user(session, "ed@x.local", world["p"].id, "edit")
    with pytest.raises(HTTPException) as e:
        duplicate_flow(src.id, _Req(), editor, session)
    assert e.value.status_code == 403


# ── flow groups ──────────────────────────────────────────────────────────────
def _group(session, world, cells):
    definition = FlowGroupDefinition.model_validate({"steps": [cells]})
    g = FlowGroup(name="nightly", project_id=world["p"].id, owner_id=world["admin"].id, definition=definition.model_dump_json(),
                  run_schedule="daily:01:00", run_scheduled_by=world["admin"].id)
    session.add(g)
    session.commit()
    session.refresh(g)
    return g


def test_a_group_copy_keeps_its_grid_and_leaves_the_schedule(session, world):
    f = make_flow(session, name="orders", project_id=world["p"].id)
    src = _group(session, world, [{"kind": "flow", "id": f.id}])
    editor = _user(session, "ed@x.local", world["p"].id, "edit")
    copy = duplicate_flow_group(src.id, _Req(), editor, session)
    assert copy.name == "nightly (1)" and copy.definition.steps[0][0].id == f.id
    stored = session.get(FlowGroup, copy.id)
    assert (stored.run_schedule, stored.owner_id) == (None, editor.id)


def test_a_group_copy_cannot_point_at_what_its_author_cannot_see(session, world):
    hidden = make_flow(session, name="private", project_id=world["secret"].id)
    src = _group(session, world, [{"kind": "flow", "id": hidden.id}])
    editor = _user(session, "ed@x.local", world["p"].id, "edit")
    with pytest.raises(HTTPException) as e:
        duplicate_flow_group(src.id, _Req(), editor, session)
    assert e.value.status_code == 422


# ── saved views ──────────────────────────────────────────────────────────────
def _view(session, world, ds):
    v = SavedView(name="by region", project_id=world["p"].id, owner_id=world["admin"].id, datasource_id=ds.id,
                  spec='{"filters": []}', description="regions")
    session.add(v)
    session.commit()
    session.refresh(v)
    return v


def test_a_view_copy_reads_the_same_datasource(session, world):
    ds = make_datasource(session, name="orders", project_id=world["p"].id)
    src = _view(session, world, ds)
    editor = _user(session, "ed@x.local", world["p"].id, "edit")
    copy = duplicate_saved_view(src.id, _Req(), editor, session)
    assert (copy.name, copy.datasource_id, copy.spec, copy.description) == ("by region (1)", ds.id, '{"filters": []}', "regions")
    assert _audit(session, audit.SAVED_VIEW_CREATE)[-1]["duplicate_of"] == src.id


def test_two_copies_at_the_same_moment_get_a_clear_refusal(session, world, monkeypatch):
    ds = make_datasource(session, name="orders", project_id=world["p"].id)
    src = _view(session, world, ds)
    duplicate_saved_view(src.id, _Req(), world["admin"], session)
    # the other request read the names before this one wrote «by region (1)»
    import app.routes.saved_views as routes

    monkeypatch.setattr(routes, "free_copy_name", lambda *a: "by region (1)")
    with pytest.raises(HTTPException) as e:
        duplicate_saved_view(src.id, _Req(), world["admin"], session)
    assert e.value.status_code == 409
    assert len(session.exec(select(SavedView)).all()) == 2


# ── connections ──────────────────────────────────────────────────────────────
def _connection(session, world, project_id=None, db_type="postgresql"):
    c = Connection(name="erp", project_id=project_id or world["p"].id, owner_id=world["admin"].id, db_type=db_type,
                   host="db.local", port=5432, username="reader", password_encrypted=encrypt_secret("s3cret"),
                   database="erp", db_schema="public", extra='{"tls": "verify"}', description="the ERP")
    session.add(c)
    session.commit()
    session.refresh(c)
    return c


def test_a_connection_copy_keeps_the_password_encrypted(session, world):
    src = _connection(session, world)
    holder = _user(session, "c@x.local", world["p"].id, "connect")
    copy = duplicate_connection(src.id, _Req(), holder, session)
    stored = session.get(Connection, copy.id)
    assert copy.name == "erp (1)" and copy.has_password
    assert decrypt_secret(stored.password_encrypted) == "s3cret"
    assert (stored.host, stored.port, stored.username, stored.database, stored.db_schema, stored.extra, stored.owner_id) == (
        "db.local", 5432, "reader", "erp", "public", '{"tls": "verify"}', holder.id)
    assert "s3cret" not in json.dumps(_audit(session, audit.CONN_CREATE))


def test_copying_a_connection_needs_connect_not_edit(session, world):
    src = _connection(session, world)
    editor = _user(session, "ed@x.local", world["p"].id, "edit")
    with pytest.raises(HTTPException) as e:
        duplicate_connection(src.id, _Req(), editor, session)
    assert e.value.status_code == 403


# ── datasources ──────────────────────────────────────────────────────────────
def _published(session, world, **kw):
    f = make_flow(session, name="orders flow", project_id=world["p"].id)
    kw.setdefault("key", "datasets/1/20261015T000000Z-aaaa.parquet")
    return make_datasource(
        session, name="orders", project_id=world["p"].id, kind="flow", flow_id=f.id, rows=42,
        columns=json.dumps([{"name": "id", "dtype": "Int64"}]), sort_keys='["id"]',
        column_descriptions='{"id": "order number"}', description="all orders", **kw)


@pytest.mark.anyio
async def test_a_datasource_copy_gets_a_data_file_of_its_own(session, world, fake_engine):
    src = _published(session, world)
    contract_service.save(session, src, {"description": "", "rules": [{"id": "r1", "type": "not_null", "column": "id"}]},
                          True, world["admin"].id, notify=("ops@x.local", None))
    session.commit()
    editor = _user(session, "ed@x.local", world["p"].id, "edit")

    copy = await duplicate_datasource(src.id, _Req(), editor, session)

    stored = session.get(Datasource, copy.id)
    assert fake_engine.copies == [("data-prep", src.key, stored.key)]
    assert stored.key.startswith(f"datasets/{copy.id}/") and stored.key != src.key
    assert (copy.name, copy.rows, copy.sort_keys, copy.column_descriptions, copy.description) == (
        "orders (1)", 42, ["id"], {"id": "order number"}, "all orders")
    assert (stored.kind, stored.flow_id, stored.owner_id) == ("flow", None, editor.id)  # its flow publishes the original
    contract = session.exec(select(DataContract).where(DataContract.datasource_id == copy.id)).one()
    assert json.loads(contract.document)["rules"][0]["column"] == "id"
    assert (contract.status, contract.notify_emails) == ("pending", None)
    assert copy.contract.status == "pending"
    assert _audit(session, audit.DS_CREATE)[-1]["duplicate_of"] == src.id


@pytest.mark.anyio
async def test_a_database_datasource_copy_needs_connect_and_leaves_the_schedule(session, world, fake_engine):
    conn = _connection(session, world, project_id=world["secret"].id)
    src = make_datasource(session, name="erp orders", project_id=world["p"].id, kind="database", connection_id=conn.id,
                          source_type="table", source_ref="public.orders", refresh_schedule="interval:60",
                          refresh_scheduled_by=world["admin"].id)
    editor = _user(session, "ed@x.local", world["p"].id, "edit")
    with pytest.raises(HTTPException) as e:
        await duplicate_datasource(src.id, _Req(), editor, session)
    assert e.value.status_code == 403 and fake_engine.copies == []

    make_permission(session, user_id=editor.id, project_id=world["secret"].id, capability="connect")
    copy = await duplicate_datasource(src.id, _Req(), editor, session)
    stored = session.get(Datasource, copy.id)
    assert (stored.connection_id, stored.source_type, stored.source_ref) == (conn.id, "table", "public.orders")
    assert (stored.refresh_schedule, stored.next_refresh_at) == (None, None)


@pytest.mark.anyio
async def test_a_copy_that_fails_leaves_nothing_behind(session, world, fake_engine):
    src = _published(session, world)
    fake_engine.copy_status = 404
    with pytest.raises(HTTPException) as e:
        await duplicate_datasource(src.id, _Req(), world["admin"], session)
    assert e.value.status_code == 502 and "missing" in e.value.detail
    assert [d.name for d in session.exec(select(Datasource)).all()] == ["orders"]


@pytest.mark.anyio
async def test_a_datasource_never_imported_is_copied_without_data(session, world, fake_engine):
    conn = _connection(session, world)
    src = make_datasource(session, name="erp orders", project_id=world["p"].id, kind="database", connection_id=conn.id,
                          source_type="table", source_ref="public.orders", key="")
    copy = await duplicate_datasource(src.id, _Req(), world["admin"], session)
    assert fake_engine.copies == [] and session.get(Datasource, copy.id).key == ""

