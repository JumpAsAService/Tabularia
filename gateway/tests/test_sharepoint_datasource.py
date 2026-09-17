"""Datasource da Excel su SharePoint: la parte del gateway.

La connessione riusa le colonne esistenti (nessuna migrazione) e la datasource è
una `kind="database"` come le altre: eredita refresh, scheduler, storico e
spinner. Qui si fissa cosa parte verso l'engine — secret CIFRATO, percorso e
foglio giusti — e chi può fare cosa.
"""
import json

import pytest
from fastapi import HTTPException

from app.core.crypto import decrypt_secret, encrypt_secret
from app.models import Connection, Datasource
from app.models.permission import Capability
from app.routes.connections import SharePointFilesQuery, engine_connection_payload, list_sharepoint_files
from app.routes.datasources import create_db_datasource, create_sharepoint_datasource, refresh_datasource
from app.schemas.models import DbDatasourceCreate, SharePointDatasourceCreate
from tests.conftest import make_project, make_user

pytestmark = pytest.mark.anyio


def _conn(session, project_id, db_type="sharepoint", **kw):
    base = dict(name="sp", project_id=project_id, db_type=db_type, host="https://contoso.sharepoint.com/sites/Finance",
                username="client-id", password_encrypted=encrypt_secret("segreto"), database="tenant-id",
                extra=json.dumps({"library": "Contabilita"}))
    base.update(kw)
    c = Connection(**base); session.add(c); session.commit(); session.refresh(c)
    return c


def _body(conn_id, **kw):
    base = dict(name="budget", connection_id=conn_id, path="Budget/2026/*.xlsx", sheet="Dati")
    base.update(kw)
    return SharePointDatasourceCreate(**base)


class _Req:
    client = None; headers = {}


def test_the_payload_maps_the_columns_and_keeps_the_secret_encrypted(session):
    p = make_project(session, name="p")
    pay = engine_connection_payload(_conn(session, p.id))
    assert pay == {"tenant_id": "tenant-id", "client_id": "client-id", "site_url": "https://contoso.sharepoint.com/sites/Finance",
                   "library": "Contabilita", "client_secret_encrypted": pay["client_secret_encrypted"]}
    assert pay["client_secret_encrypted"] != "segreto" and decrypt_secret(pay["client_secret_encrypted"]) == "segreto"
    # gli endpoint Microsoft non sono scelti da chi crea la connessione
    assert "graph_base" not in pay and "login_base" not in pay


async def test_creating_it_queues_a_sharepoint_ingest_with_path_and_sheet(session, fake_engine):
    admin = make_user(session, email="a@x.it", is_superuser=True)
    p = make_project(session, name="p")
    conn = _conn(session, p.id)
    out = await create_sharepoint_datasource(p.id, _body(conn.id), _Req(), admin, session)
    assert out.kind == "database" and out.source_type == "sharepoint"
    (route, sent), = fake_engine.ingests
    assert route == "/sharepoint/ingest"
    assert sent["source"] == {"path": "Budget/2026/*.xlsx", "sheet": "Dati"}
    assert sent["connection"]["tenant_id"] == "tenant-id" and "segreto" not in json.dumps(sent)
    assert sent["output_key"].startswith("datasets/")


async def test_refresh_goes_down_the_same_road(session, fake_engine):
    """Il refresh — e quindi lo scheduler — non sa nulla di SharePoint: passa da
    launch_ingest_run, che smista sul tipo di connessione."""
    admin = make_user(session, email="a@x.it", is_superuser=True)
    p = make_project(session, name="p")
    conn = _conn(session, p.id)
    out = await create_sharepoint_datasource(p.id, _body(conn.id), _Req(), admin, session)
    await refresh_datasource(out.id, _Req(), admin, session)
    assert [r for r, _ in fake_engine.ingests] == ["/sharepoint/ingest", "/sharepoint/ingest"]


async def test_a_database_datasource_still_goes_to_db_ingest(session, fake_engine):
    admin = make_user(session, email="a@x.it", is_superuser=True)
    p = make_project(session, name="p")
    pg = _conn(session, p.id, db_type="postgresql", name="pg", host="db", extra="{}")
    await create_db_datasource(p.id, DbDatasourceCreate(name="t", connection_id=pg.id, source_type="table", source_ref="public.t"), _Req(), admin, session)
    assert fake_engine.ingests[0][0] == "/db/ingest"


async def test_the_two_kinds_of_connection_cannot_be_swapped(session, fake_engine):
    admin = make_user(session, email="a@x.it", is_superuser=True)
    p = make_project(session, name="p")
    sp, pg = _conn(session, p.id), _conn(session, p.id, db_type="postgresql", name="pg", host="db", extra="{}")
    with pytest.raises(HTTPException) as e:
        await create_sharepoint_datasource(p.id, _body(pg.id), _Req(), admin, session)
    assert e.value.status_code == 422
    with pytest.raises(HTTPException) as e:
        await create_db_datasource(p.id, DbDatasourceCreate(name="t", connection_id=sp.id, source_type="table", source_ref="x"), _Req(), admin, session)
    assert e.value.status_code == 422 and fake_engine.ingests == []


async def test_without_connect_on_the_connection_nothing_is_created(session, fake_engine):
    from app.models import Permission

    admin = make_user(session, email="a@x.it", is_superuser=True)
    editor = make_user(session, email="e@x.it")
    p = make_project(session, name="p"); altrove = make_project(session, name="altrove")
    conn = _conn(session, altrove.id)
    session.add(Permission(project_id=p.id, user_id=editor.id, capability=Capability.EDIT)); session.commit()
    with pytest.raises(HTTPException) as e:
        await create_sharepoint_datasource(p.id, _body(conn.id), _Req(), editor, session)
    assert e.value.status_code in (403, 404)
    assert session.query(Datasource).count() == 0 and fake_engine.ingests == []


async def test_if_the_engine_refuses_no_half_made_datasource_is_left(session, fake_engine, monkeypatch):
    admin = make_user(session, email="a@x.it", is_superuser=True)
    p = make_project(session, name="p")
    conn = _conn(session, p.id)
    import httpx
    monkeypatch.setattr(fake_engine, "_handler", lambda req: httpx.Response(503, json={"detail": "giu'"}))
    import app.core.engine_client as ec
    monkeypatch.setattr(ec, "_client", httpx.AsyncClient(transport=httpx.MockTransport(fake_engine._handler), base_url="http://engine"))
    with pytest.raises(HTTPException):
        await create_sharepoint_datasource(p.id, _body(conn.id), _Req(), admin, session)
    assert session.query(Datasource).count() == 0


async def test_listing_the_files_of_a_glob_asks_the_engine(session, fake_engine):
    admin = make_user(session, email="a@x.it", is_superuser=True)
    p = make_project(session, name="p")
    conn = _conn(session, p.id)
    await list_sharepoint_files(conn.id, SharePointFilesQuery(path="Budget/**/*.xlsx"), admin, session)
    assert fake_engine.inspects[0]["action"] == "files" and fake_engine.inspects[0]["path"] == "Budget/**/*.xlsx"


@pytest.mark.parametrize("field", ["path", "sheet"])
def test_path_and_sheet_are_mandatory(field):
    with pytest.raises(Exception):
        _body(1, **{field: ""})


async def test_a_sharepoint_connection_cannot_be_a_database_destination(session, fake_engine):
    """Non è un database: senza il controllo l'errore arriverebbe dal driver, a
    run già partito, parlando d'altro."""
    from app.routes.runs import _launch_flow_run
    from app.schemas.models import RunCreate, RunDestinationSpec
    from tests.conftest import make_flow

    admin = make_user(session, email="a@x.it", is_superuser=True)
    p = make_project(session, name="p")
    conn = _conn(session, p.id)
    flow = make_flow(session, name="f", project_id=p.id)
    body = RunCreate(bucket="data-prep", input_key="datasets/x.parquet", operations=[],
                     destination=RunDestinationSpec(type="database", connection_id=conn.id, table="t"))
    with pytest.raises(HTTPException) as e:
        await _launch_flow_run(session, admin, flow, body)
    assert e.value.status_code == 422 and fake_engine.transforms == []
