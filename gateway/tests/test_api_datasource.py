"""The datasource «API call»: the gateway's part.

A `kind="database"` datasource like the SharePoint and SFTP ones — refresh,
schedule, history, snapshot swap — with `source_type="api"` and the call as JSON
in `source_ref`. Fixed here: the call is checked by the engine BEFORE anything is
saved, what leaves for the engine at each refresh ({last_refresh}, the previous
snapshot in append mode), who may do what, and what the other paths refuse.
"""
import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlmodel import select

from app.core.crypto import encrypt_secret
from app.models import Connection, Datasource, Run
from app.models.permission import Capability
from app.routes.datasources import create_api_datasource, create_db_datasource, refresh_datasource, update_datasource
from app.schemas.models import ApiDatasourceCreate, DatasourceUpdate, DbDatasourceCreate
from tests.conftest import make_permission, make_project, make_run, make_user

pytestmark = pytest.mark.anyio

SECRET = "ds-S3CRET-77"


class _Req:
    client = None
    headers = {}


def _conn(session, project_id, db_type="api", **kw):
    base = dict(name="broker", project_id=project_id, db_type=db_type, host="https://api.example.com/v1",
                password_encrypted=encrypt_secret(SECRET),
                extra=json.dumps({"auth": "api_key", "key_name": "apikey", "key_in": "query"}))
    base.update(kw)
    c = Connection(**base)
    session.add(c)
    session.commit()
    session.refresh(c)
    return c


def _body(conn_id, **kw):
    base = dict(name="fx", connection_id=conn_id, path="/{today-30}..",
                query=[{"name": "base", "value": "EUR"}], response={"records_path": "rates", "keyed": True})
    base.update(kw)
    return ApiDatasourceCreate(**base)


async def _create(session, fake_engine, user=None, **kw):
    admin = user or make_user(session, email="a@x.it", is_superuser=True)
    p = make_project(session, name="p")
    conn = _conn(session, p.id)
    out = await create_api_datasource(p.id, _body(conn.id, **kw), _Req(), admin, session)
    return admin, p, conn, out


async def test_creating_it_checks_the_call_then_queues_an_api_ingest(session, fake_engine):
    _, _, conn, out = await _create(session, fake_engine)
    assert out.kind == "database" and out.source_type == "api"
    (check,) = fake_engine.api_inspects
    assert check["action"] == "validate" and check["source"]["path"] == "/{today-30}.."
    (route, sent), = fake_engine.ingests
    assert route == "/http-api/ingest"
    src = sent["source"]
    assert src["path"] == "/{today-30}.." and src["response"]["keyed"] is True and src["mode"] == "snapshot"
    assert src["query"] == [{"name": "base", "value": "EUR"}]
    assert sent["connection"]["base_url"] == "https://api.example.com/v1" and SECRET not in json.dumps(sent)
    assert "last_refresh" not in sent and "previous" not in sent
    assert sent["output_key"].startswith("datasets/")


async def test_a_call_the_engine_refuses_saves_nothing(session, fake_engine):
    fake_engine.api_responses["validate"] = (400, {"detail": "API: unknown placeholder {tomorrow}"})
    with pytest.raises(HTTPException) as e:
        await _create(session, fake_engine, path="/{tomorrow}")
    assert e.value.status_code == 422 and "tomorrow" in e.value.detail
    assert session.exec(select(Datasource)).all() == [] and fake_engine.ingests == []


async def test_a_refresh_sends_the_start_of_the_last_successful_refresh(session, fake_engine):
    admin, _, _, out = await _create(session, fake_engine)
    ds = session.get(Datasource, out.id)
    t0 = datetime(2026, 10, 16, 6, 0, 0)
    make_run(session, kind="ingest", datasource_id=ds.id, status="SUCCESS", task_id="old-ok", started_at=t0)
    make_run(session, kind="ingest", datasource_id=ds.id, status="FAILURE", task_id="old-ko", started_at=t0 + timedelta(hours=1))
    for r in session.exec(select(Run).where(Run.task_id == "t-ingest-1")).all():
        r.status = "FAILURE"  # the creation's own import: failed, it does not count
        session.add(r)
    session.commit()
    await refresh_datasource(ds.id, _Req(), admin, session)
    _, sent = fake_engine.ingests[-1]
    assert sent["last_refresh"].startswith("2026-10-16T06:00:00")


async def test_append_mode_sends_the_previous_snapshot(session, fake_engine):
    admin, _, _, out = await _create(session, fake_engine, mode="append", keys=["_key"])
    ds = session.get(Datasource, out.id)
    assert "previous" not in fake_engine.ingests[0][1]  # the first import has none
    ds.key = "datasets/5/snap1.parquet"
    session.add(ds)
    for r in session.exec(select(Run)).all():
        r.status = "SUCCESS"
        session.add(r)
    session.commit()
    await refresh_datasource(ds.id, _Req(), admin, session)
    _, sent = fake_engine.ingests[-1]
    assert sent["previous"] == {"bucket": ds.bucket, "key": "datasets/5/snap1.parquet"}
    assert sent["source"]["mode"] == "append" and sent["source"]["keys"] == ["_key"]


async def test_it_needs_an_api_connection(session, fake_engine):
    admin = make_user(session, email="a@x.it", is_superuser=True)
    p = make_project(session, name="p")
    pg = _conn(session, p.id, db_type="postgresql", name="pg", extra="{}")
    with pytest.raises(HTTPException) as e:
        await create_api_datasource(p.id, _body(pg.id), _Req(), admin, session)
    assert e.value.status_code == 422


async def test_the_database_path_refuses_an_api_connection(session, fake_engine):
    admin = make_user(session, email="a@x.it", is_superuser=True)
    p = make_project(session, name="p")
    conn = _conn(session, p.id)
    with pytest.raises(HTTPException) as e:
        await create_db_datasource(p.id, DbDatasourceCreate(name="x", connection_id=conn.id, source_type="table",
                                                            source_ref="t"), _Req(), admin, session)
    assert e.value.status_code == 422 and fake_engine.ingests == []


async def test_it_needs_edit_on_the_folder_and_connect_on_the_connection(session, fake_engine):
    p = make_project(session, name="p")
    q = make_project(session, name="q")
    conn = _conn(session, q.id)
    u = make_user(session, email="u@x.it")
    make_permission(session, project_id=p.id, user_id=u.id, capability=Capability.EDIT)
    with pytest.raises(HTTPException) as e:
        await create_api_datasource(p.id, _body(conn.id), _Req(), u, session)
    assert e.value.status_code == 403 and fake_engine.api_inspects == []
    make_permission(session, project_id=q.id, user_id=u.id, capability=Capability.CONNECT)
    out = await create_api_datasource(p.id, _body(conn.id), _Req(), u, session)
    assert out.source_type == "api"


async def test_an_api_datasource_has_no_sort_keys(session, fake_engine):
    admin, _, _, out = await _create(session, fake_engine)
    with pytest.raises(HTTPException) as e:
        update_datasource(out.id, DatasourceUpdate(sort_keys=["_key"]), admin, session)
    assert e.value.status_code == 422 and "API" in e.value.detail


async def test_an_engine_that_refuses_the_ingest_leaves_no_half_datasource(session, fake_engine, monkeypatch):
    async def refuse(*a, **k):
        raise HTTPException(status_code=502, detail="engine down")

    monkeypatch.setattr("app.routes.datasources.launch_ingest_run", refuse)
    with pytest.raises(HTTPException):
        await _create(session, fake_engine)
    assert session.exec(select(Datasource)).all() == []


@pytest.mark.parametrize("kw", [
    dict(method="DELETE"), dict(name=""), dict(response={"format": "xml"}), dict(pagination={"type": "magic"}),
    dict(mode="merge"), dict(variables=[{"name": "s", "values": []}]), dict(limits={"max_pages": -1}),
    dict(query=[{"name": "a" * 300, "value": "x"}]),
])
def test_what_the_form_cannot_send(kw):
    with pytest.raises(ValidationError):
        _body(1, **kw)
