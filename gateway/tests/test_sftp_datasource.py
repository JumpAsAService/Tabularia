"""The datasource «SFTP folder»: the gateway's part.

It is a `kind="database"` datasource like the SharePoint one — refresh, schedule,
history, snapshot swap — with `source_type="sftp"` and the folder, the glob and
the reading options as JSON in `source_ref`. Fixed here: what leaves for the
engine, who may do what, and what the other paths refuse.
"""
import json

import pytest
from fastapi import HTTPException

from app.core.crypto import encrypt_secret
from app.models import Connection
from app.models.permission import Capability
from app.routes.datasources import create_db_datasource, create_sftp_datasource, refresh_datasource, update_datasource
from app.schemas.models import DatasourceUpdate, DbDatasourceCreate, SftpDatasourceCreate
from tests.conftest import make_permission, make_project, make_user

pytestmark = pytest.mark.anyio


class _Req:
    client = None
    headers = {}


# Long and with characters base64 never contains: the payload carries the Fernet
# ciphertext, and a short password like "pw" can turn up inside it by chance.
SECRET = "sftp-password!never-in-the-payload#42"


def _conn(session, project_id, db_type="sftp", **kw):
    base = dict(name="drop", project_id=project_id, db_type=db_type, host="sftp.example.com", port=2222,
                username="tab", password_encrypted=encrypt_secret(SECRET), extra=json.dumps({"auth": "password"}),
                host_key_type="ssh-ed25519", host_key_fingerprint="SHA256:k")
    base.update(kw)
    c = Connection(**base)
    session.add(c)
    session.commit()
    session.refresh(c)
    return c


def _body(conn_id, **kw):
    base = dict(name="vendite", connection_id=conn_id, folder="/in", pattern="vendite_*.csv")
    base.update(kw)
    return SftpDatasourceCreate(**base)


async def test_creating_it_queues_an_sftp_ingest(session, fake_engine):
    admin = make_user(session, email="a@x.it", is_superuser=True)
    p = make_project(session, name="p")
    conn = _conn(session, p.id)
    out = await create_sftp_datasource(p.id, _body(conn.id, csv={"separator": ";", "encoding": "windows-1252"}),
                                       _Req(), admin, session)
    assert out.kind == "database" and out.source_type == "sftp"
    (route, sent), = fake_engine.ingests
    assert route == "/sftp/ingest"
    assert sent["source"] == {"folder": "/in", "pattern": "vendite_*.csv", "format": "auto", "sheet": "",
                              "csv": {"separator": ";", "has_header": True, "encoding": "windows-1252", "null_values": []}}
    c = sent["connection"]
    assert (c["host"], c["port"], c["host_key_fingerprint"]) == ("sftp.example.com", 2222, "SHA256:k")
    assert SECRET not in json.dumps(sent)
    assert sent["output_key"].startswith("datasets/")


async def test_refresh_goes_down_the_same_road(session, fake_engine):
    admin = make_user(session, email="a@x.it", is_superuser=True)
    p = make_project(session, name="p")
    conn = _conn(session, p.id)
    out = await create_sftp_datasource(p.id, _body(conn.id), _Req(), admin, session)
    await refresh_datasource(out.id, _Req(), admin, session)
    assert [r for r, _ in fake_engine.ingests] == ["/sftp/ingest", "/sftp/ingest"]


async def test_it_needs_an_sftp_connection(session, fake_engine):
    admin = make_user(session, email="a@x.it", is_superuser=True)
    p = make_project(session, name="p")
    pg = _conn(session, p.id, db_type="postgresql", name="pg", extra="{}")
    with pytest.raises(HTTPException) as e:
        await create_sftp_datasource(p.id, _body(pg.id), _Req(), admin, session)
    assert e.value.status_code == 422


async def test_the_database_path_refuses_an_sftp_connection(session, fake_engine):
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
        await create_sftp_datasource(p.id, _body(conn.id), _Req(), u, session)
    assert e.value.status_code == 403
    make_permission(session, project_id=q.id, user_id=u.id, capability=Capability.CONNECT)
    out = await create_sftp_datasource(p.id, _body(conn.id), _Req(), u, session)
    assert out.source_type == "sftp"


@pytest.mark.parametrize("kw", [dict(pattern=""), dict(format="xml"), dict(csv={"separator": ";;"}),
                                dict(csv={"encoding": "utf16"}), dict(pattern="../*.csv"), dict(folder="in/../..")])
def test_what_the_form_cannot_send(kw):
    with pytest.raises(ValueError):
        _body(1, **kw)


async def test_an_sftp_datasource_has_no_sort_keys(session, fake_engine):
    admin = make_user(session, email="a@x.it", is_superuser=True)
    p = make_project(session, name="p")
    conn = _conn(session, p.id)
    out = await create_sftp_datasource(p.id, _body(conn.id), _Req(), admin, session)
    with pytest.raises(HTTPException) as e:
        update_datasource(out.id, DatasourceUpdate(sort_keys=["id"]), admin, session)
    assert e.value.status_code == 422 and "SFTP" in e.value.detail


async def test_an_engine_that_refuses_leaves_no_half_datasource(session, fake_engine, monkeypatch):
    from app.models import Datasource
    from sqlmodel import select

    admin = make_user(session, email="a@x.it", is_superuser=True)
    p = make_project(session, name="p")
    conn = _conn(session, p.id)

    async def refuse(*a, **k):
        raise HTTPException(status_code=502, detail="engine down")

    monkeypatch.setattr("app.routes.datasources.launch_ingest_run", refuse)
    with pytest.raises(HTTPException):
        await create_sftp_datasource(p.id, _body(conn.id), _Req(), admin, session)
    assert session.exec(select(Datasource)).all() == []
