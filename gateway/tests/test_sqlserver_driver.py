"""SQL Server without its driver: the gateway's part.

The published images do not contain Microsoft's SQL Server driver (it is under
Microsoft's licence, docs/sqlserver.md). The engine answers a SQL Server test or
table list with the code `driver_not_installed`: the gateway says it in the user's
language with the link to the guide, never a raw engine payload. And it relays
which optional drivers the engine has, so that the form can warn before a test.
"""
import pytest
from fastapi import HTTPException

from app.routes import connections as rc
from app.schemas.models import ConnectionCreate, ConnectionTest
from tests.conftest import make_project, make_user

pytestmark = pytest.mark.anyio

MISSING = (422, {"detail": {"code": "driver_not_installed", "db_type": "sqlserver",
                            "message": "The SQL Server driver is not installed in this installation: …"}})


class _Req:
    client = None

    def __init__(self, lang: str = "en"):
        self.headers = {"accept-language": lang}


def _create(session, user, project_id):
    body = ConnectionCreate(name="erp", db_type="sqlserver", host="mssql.example.com", port=None,
                            username="sa", password="pw", database="sales")
    return rc.create_connection(project_id, body, request=None, user=user, session=session)


@pytest.mark.parametrize("lang, words", [("en", "is not installed"), ("it", "non è installato"),
                                         ("de", "nicht installiert"), ("fr", "n'est pas installé"),
                                         ("es", "no está instalado")])
async def test_a_saved_connection_says_the_driver_is_missing_in_the_users_language(session, fake_engine, lang, words):
    admin = make_user(session, email="a@x.it", is_superuser=True)
    p = make_project(session, name="p")
    out = _create(session, admin, p.id)  # creating it is allowed: a config can be ready before the driver
    fake_engine.db_responses["test"] = MISSING
    with pytest.raises(HTTPException) as e:
        await rc.test_connection(out.id, None, _Req(lang), admin, session)
    assert e.value.status_code == 422
    assert words in e.value.detail and "SQL Server" in e.value.detail
    assert rc.SQLSERVER_DOCS_URL in e.value.detail


async def test_the_form_test_and_the_table_list_say_it_too(session, fake_engine):
    admin = make_user(session, email="a@x.it", is_superuser=True)
    p = make_project(session, name="p")
    fake_engine.db_responses["test"] = MISSING
    fake_engine.db_responses["tables"] = MISSING
    draft = ConnectionTest(name="erp", db_type="sqlserver", host="h", port=None, username="sa", password="pw", database="d")
    with pytest.raises(HTTPException) as e:
        await rc.test_draft_connection(p.id, draft, _Req("it"), admin, session)
    assert "non è installato" in e.value.detail
    out = _create(session, admin, p.id)
    with pytest.raises(HTTPException) as e:
        await rc.list_connection_tables(out.id, _Req("fr"), admin, session)
    assert "n'est pas installé" in e.value.detail


async def test_another_engine_refusal_passes_as_before(session, fake_engine):
    admin = make_user(session, email="a@x.it", is_superuser=True)
    p = make_project(session, name="p")
    out = _create(session, admin, p.id)
    fake_engine.db_responses["test"] = (400, {"detail": "SQL Server: Login failed for user 'sa'."})
    with pytest.raises(HTTPException) as e:
        await rc.test_connection(out.id, None, _Req("it"), admin, session)
    assert e.value.status_code == 400 and e.value.detail == "SQL Server: Login failed for user 'sa'."


async def test_the_drivers_the_engine_has(session, fake_engine):
    user = make_user(session, email="u@x.it")
    fake_engine.drivers = {"sqlserver": {"installed": False}}
    assert await rc.connection_drivers(user) == {"sqlserver": {"installed": False, "docs": rc.SQLSERVER_DOCS_URL}}
    fake_engine.drivers = {"sqlserver": {"installed": True}}
    assert (await rc.connection_drivers(user))["sqlserver"]["installed"] is True


async def test_an_engine_that_does_not_report_drivers_marks_nothing(session, fake_engine):
    # an older engine (no route) or one that does not answer: no warning rather than a false one
    user = make_user(session, email="u@x.it")
    fake_engine.drivers = None
    assert await rc.connection_drivers(user) == {}
