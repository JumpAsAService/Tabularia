"""Google Sheets connections and datasources: the gateway's part.

The connection reuses the columns of the others: the encrypted secret = the service
account's whole JSON key, `host` = its email and `database` = its project, both READ
FROM THE KEY (the form does not choose them). Fixed here: what is refused at save
time, that the key is stored and travels encrypted and never comes back, the routes
of the test, of the list of spreadsheets, of the tabs and of the preview, and that the
datasource refreshes down the same road as the others.
"""
import json

import pytest
from fastapi import HTTPException

from app.core.crypto import decrypt_secret
from app.models import Connection, Datasource
from app.models.permission import Capability
from app.routes import connections as rc
from app.routes.datasources import create_gsheets_datasource, refresh_datasource, update_datasource
from app.schemas.models import (
    ConnectionCreate, ConnectionTest, ConnectionUpdate, DatasourceUpdate, GsheetsDatasourceCreate, GsheetsTab,
    GsheetsTabsQuery,
)
from tests.conftest import make_permission, make_project, make_user

pytestmark = pytest.mark.anyio

EMAIL = "tabularia-reader@acme-data.iam.gserviceaccount.com"
PEM = "-----BEGIN PRIVATE KEY-----\nMIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQC7-made-up\n-----END PRIVATE KEY-----\n"
SID = "1AbCdEfGhIjKlMnOpQrStUvWxYz0123456789-_abcd"


def _key(**kw) -> str:
    base = {"type": "service_account", "project_id": "acme-data", "private_key_id": "abc123", "private_key": PEM,
            "client_email": EMAIL, "client_id": "1234", "token_uri": "https://oauth2.googleapis.com/token"}
    base.update(kw)
    return json.dumps(base)


class _Req:
    client = None
    headers = {}


def _setup(session):
    return make_user(session, email="a@x.it", is_superuser=True), make_project(session, name="p")


def _create(session, user, project_id, **kw):
    body = dict(name="sheets", db_type="gsheets", host="", password=_key())
    body.update(kw)
    return rc.create_connection(project_id, ConnectionCreate(**body), request=None, user=user, session=session)


# ── the connection ────────────────────────────────────────────────────────────────
def test_the_account_and_project_come_from_the_key_and_the_key_never_comes_back(session):
    admin, p = _setup(session)
    out = _create(session, admin, p.id, host="whatever.example.com", database="other", username="u", port=1,
                  extra=json.dumps({"x": 1}))
    assert out.host == EMAIL and out.database == "acme-data" and out.username == "" and out.port is None
    assert out.extra == "{}" and out.has_password and "PRIVATE KEY" not in out.model_dump_json()
    conn = session.get(Connection, out.id)
    assert decrypt_secret(conn.password_encrypted) == _key() and "PRIVATE KEY" not in conn.password_encrypted


def test_the_payload_keeps_the_key_encrypted(session):
    admin, p = _setup(session)
    pay = rc.engine_connection_payload(session.get(Connection, _create(session, admin, p.id).id))
    assert pay["db_type"] == "gsheets" and pay["client_email"] == EMAIL and pay["project_id"] == "acme-data"
    assert decrypt_secret(pay["key_encrypted"]) == _key() and "PRIVATE KEY" not in json.dumps(pay)
    # no endpoint travels: they are fixed in the engine
    assert set(pay) == {"db_type", "key_encrypted", "client_email", "project_id"}


@pytest.mark.parametrize("key, message", [
    ("", "paste the service account's JSON key"),
    ("not json", "this is not JSON"),
    (json.dumps({"installed": {"client_id": "x", "client_secret": "y"}}), "OAuth client's file"),
    (json.dumps({"type": "authorized_user"}), "not a service account's key"),
    (_key(private_key=""), "no client_email or no private_key"),
    (_key(client_email=""), "no client_email or no private_key"),
])
def test_what_is_not_a_service_account_key_is_refused(session, key, message):
    admin, p = _setup(session)
    with pytest.raises(HTTPException) as e:
        _create(session, admin, p.id, password=key)
    assert e.value.status_code == 422 and message in e.value.detail
    assert "PRIVATE KEY" not in e.value.detail
    assert session.query(Connection).count() == 0


def test_a_new_key_brings_its_account_and_the_form_cannot_change_it(session):
    admin, p = _setup(session)
    out = _create(session, admin, p.id)
    out = rc.update_connection(out.id, ConnectionUpdate(host="evil.example.com", database="x", description="d"),
                               request=None, user=admin, session=session)
    assert out.host == EMAIL and out.database == "acme-data" and out.description == "d"
    other = _key(client_email="second@other-proj.iam.gserviceaccount.com", project_id="other-proj")
    out = rc.update_connection(out.id, ConnectionUpdate(password=other), request=None, user=admin, session=session)
    assert out.host == "second@other-proj.iam.gserviceaccount.com" and out.database == "other-proj"
    with pytest.raises(HTTPException) as e:  # removing the key would leave a connection that cannot work
        rc.update_connection(out.id, ConnectionUpdate(password=""), request=None, user=admin, session=session)
    assert e.value.status_code == 422


async def test_the_draft_test_sends_the_typed_key_encrypted(session, fake_engine):
    admin, p = _setup(session)
    out = await rc.test_draft_connection(p.id, ConnectionTest(name="s", db_type="gsheets", host="", password=_key()),
                                         request=_Req(), user=admin, session=session)
    assert out["spreadsheets"] == 2
    (sent,) = fake_engine.gsheets_inspects
    assert sent["action"] == "test" and decrypt_secret(sent["connection"]["key_encrypted"]) == _key()
    assert "PRIVATE KEY" not in json.dumps(sent)


async def test_an_engine_refusal_of_the_test_reaches_the_user(session, fake_engine):
    admin, p = _setup(session)
    out = _create(session, admin, p.id)
    fake_engine.gsheets_responses["test"] = (400, {"detail": "Google Sheets: enable the Google Sheets API in the Google Cloud project acme-data"})
    with pytest.raises(HTTPException) as e:
        await rc.test_connection(out.id, None, request=_Req(), user=admin, session=session)
    assert e.value.status_code == 400 and "enable the Google Sheets API" in e.value.detail


async def test_spreadsheets_tabs_and_preview_need_connect_and_a_gsheets_connection(session, fake_engine):
    admin, p = _setup(session)
    conn = _create(session, admin, p.id)
    await rc.list_gsheets_spreadsheets(conn.id, user=admin, session=session)
    await rc.list_gsheets_tabs(conn.id, GsheetsTabsQuery(spreadsheet=f" https://docs.google.com/spreadsheets/d/{SID}/edit "),
                               user=admin, session=session)
    await rc.preview_gsheets_tab(conn.id, GsheetsTab(spreadsheet_id=SID, sheet_id=0, range="A1:C"), user=admin,
                                 session=session)
    actions = [(b["action"], b.get("spreadsheet"), b.get("source")) for b in fake_engine.gsheets_inspects]
    assert actions == [("spreadsheets", None, None),
                       ("tabs", f"https://docs.google.com/spreadsheets/d/{SID}/edit", None),
                       ("preview", None, {"spreadsheet_id": SID, "sheet_id": 0, "sheet": "", "range": "A1:C"})]
    viewer = make_user(session, email="v@x.it")
    make_permission(session, user_id=viewer.id, project_id=p.id, capability=Capability.EDIT)  # EDIT is not CONNECT
    with pytest.raises(HTTPException) as e:
        await rc.list_gsheets_spreadsheets(conn.id, user=viewer, session=session)
    assert e.value.status_code in (403, 404)
    pg = rc.create_connection(p.id, ConnectionCreate(name="pg", db_type="postgresql", host="db"), request=None,
                              user=admin, session=session)
    with pytest.raises(HTTPException) as e:
        await rc.list_gsheets_spreadsheets(pg.id, user=admin, session=session)
    assert e.value.status_code == 422


@pytest.mark.parametrize("field, value", [("spreadsheet_id", "../../etc"), ("range", "Orders!A1:B2"), ("range", "A1")])
def test_the_tab_is_checked_before_it_reaches_the_engine(field, value):
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        GsheetsTab(**{"spreadsheet_id": SID, field: value})


async def test_a_gsheets_connection_has_no_tables(session, fake_engine):
    admin, p = _setup(session)
    conn = _create(session, admin, p.id)
    with pytest.raises(HTTPException) as e:
        await rc.list_connection_columns(conn.id, table="t", request=_Req(), user=admin, session=session)
    assert e.value.status_code == 422


# ── the datasource ────────────────────────────────────────────────────────────────
def _ds_body(conn_id, **kw):
    base = dict(name="orders", connection_id=conn_id, spreadsheet_id=SID, spreadsheet_name="Sales 2026", sheet_id=0,
                sheet="Orders", range="a2:f")
    base.update(kw)
    return GsheetsDatasourceCreate(**base)


async def test_creating_it_queues_a_gsheets_ingest_and_the_refresh_takes_the_same_road(session, fake_engine):
    admin, p = _setup(session)
    conn = _create(session, admin, p.id)
    out = await create_gsheets_datasource(p.id, _ds_body(conn.id), _Req(), admin, session)
    assert out.kind == "database" and out.source_type == "gsheets"
    assert json.loads(out.source_ref) == {"spreadsheet_id": SID, "spreadsheet_name": "Sales 2026", "sheet_id": 0,
                                          "sheet": "Orders", "range": "A2:F"}
    await refresh_datasource(out.id, _Req(), admin, session)
    assert [r for r, _ in fake_engine.ingests] == ["/gsheets/ingest", "/gsheets/ingest"]
    sent = fake_engine.ingests[0][1]
    assert sent["source"] == {"spreadsheet_id": SID, "sheet_id": 0, "sheet": "Orders", "range": "A2:F"}
    assert sent["connection"]["db_type"] == "gsheets" and "PRIVATE KEY" not in json.dumps(sent)


async def test_it_needs_a_gsheets_connection_a_tab_and_connect(session, fake_engine):
    admin, p = _setup(session)
    conn = _create(session, admin, p.id)
    pg = rc.create_connection(p.id, ConnectionCreate(name="pg", db_type="postgresql", host="db"), request=None,
                              user=admin, session=session)
    with pytest.raises(HTTPException) as e:
        await create_gsheets_datasource(p.id, _ds_body(pg.id), _Req(), admin, session)
    assert e.value.status_code == 422
    with pytest.raises(HTTPException) as e:
        await create_gsheets_datasource(p.id, _ds_body(conn.id, sheet_id=None, sheet=""), _Req(), admin, session)
    assert e.value.status_code == 422 and "tab" in e.value.detail
    editor = make_user(session, email="e@x.it")
    elsewhere = make_project(session, name="elsewhere")
    make_permission(session, user_id=editor.id, project_id=elsewhere.id, capability=Capability.EDIT)
    with pytest.raises(HTTPException) as e:
        await create_gsheets_datasource(elsewhere.id, _ds_body(conn.id), _Req(), editor, session)
    assert e.value.status_code in (403, 404)
    assert session.query(Datasource).count() == 0 and fake_engine.ingests == []


async def test_no_sort_keys(session, fake_engine):
    admin, p = _setup(session)
    conn = _create(session, admin, p.id)
    out = await create_gsheets_datasource(p.id, _ds_body(conn.id), _Req(), admin, session)
    with pytest.raises(HTTPException) as e:
        update_datasource(out.id, DatasourceUpdate(sort_keys=["a"]), admin, session, _Req())
    assert e.value.status_code == 422 and "Google Sheets" in e.value.detail


async def test_lineage_and_dbt_see_a_spreadsheet(session, fake_engine):
    from app.services import openlineage

    admin, p = _setup(session)
    conn = _create(session, admin, p.id)
    out = await create_gsheets_datasource(p.id, _ds_body(conn.id), _Req(), admin, session)
    inputs, facets = openlineage.origin_inputs(session, session.get(Datasource, out.id))
    assert inputs[0].namespace == "https://docs.google.com" and inputs[0].name == f"/spreadsheets/d/{SID}#Orders"
    assert facets == {}
