"""The SQL Server driver is optional: an installation without it says so.

`mssql-python` depends on Microsoft's ODBC driver, under Microsoft's licence, so the
published images leave it out (see docs/sqlserver.md). Here the driver is made
absent with `sys.modules["mssql_python"] = None`, which is what an image built
without it looks like to an `import`: every path that needs it — the connection
test, the table list, an import — must end in one clear message, never in an
ImportError or a 500, and the engine must report what it has."""
import sys

import pytest
from fastapi import HTTPException

from app.api.routes import db as rotta
from app.ingest import db_source as ds
from app.ingest.db_source import DbConnectionSpec, DbSourceError, DbSourceSpec, DriverNotInstalledError


def _conn() -> DbConnectionSpec:
    return DbConnectionSpec(db_type="sqlserver", host="mssql.example.com", username="sa", password="x", database="d")


@pytest.fixture
def without_driver(monkeypatch):
    monkeypatch.setitem(sys.modules, "mssql_python", None)


def _says_not_installed(message: str) -> None:
    assert "SQL Server driver is not installed" in message
    assert "docs/sqlserver.md" in message


def test_opening_a_connection_without_the_driver(without_driver):
    with pytest.raises(DriverNotInstalledError) as e:
        ds._sqlserver_connect(_conn())
    _says_not_installed(str(e.value))
    assert e.value.db_type == "sqlserver" and e.value.code == "driver_not_installed"
    assert isinstance(e.value, DbSourceError)  # the import paths already show these as they are


def test_the_message_survives_the_trip_through_celery(without_driver):
    # a failed task's exception is rebuilt from its args (pickle, or Celery's JSON
    # result backend: cls(*args)): the message must come back as it left
    import pickle

    with pytest.raises(DriverNotInstalledError) as e:
        ds._sqlserver_connect(_conn())
    for again in (pickle.loads(pickle.dumps(e.value)), type(e.value)(*e.value.args)):
        assert str(again) == str(e.value)


def test_the_connection_test_and_the_table_list_say_it_as_it_is(without_driver):
    for call in (ds.test_connection, ds.list_tables):
        with pytest.raises(DriverNotInstalledError) as e:
            call(_conn())
        _says_not_installed(str(e.value))


def test_an_import_fails_with_the_same_message(without_driver):
    with pytest.raises(DbSourceError) as e:
        ds.ingest_db_to_parquet(_conn(), DbSourceSpec(mode="table", ref="dbo.orders"), "b", "k.parquet", storage=object())
    _says_not_installed(str(e.value))


def test_the_inspect_route_answers_with_a_code_the_gateway_translates(without_driver):
    with pytest.raises(HTTPException) as e:
        rotta.inspect(rotta.DbInspectRequest(connection=_conn().model_dump(), action="test"))
    assert e.value.status_code == 422
    detail = e.value.detail
    assert detail["code"] == "driver_not_installed" and detail["db_type"] == "sqlserver"
    _says_not_installed(detail["message"])


def test_the_engine_reports_the_driver_missing(without_driver):
    assert rotta.drivers() == {"sqlserver": {"installed": False}}


def test_the_engine_reports_the_driver_present(monkeypatch):
    monkeypatch.setattr(ds, "_module_installed", lambda name: name == "mssql_python")
    assert rotta.drivers() == {"sqlserver": {"installed": True}}


def test_another_missing_module_is_not_mistaken_for_the_driver(monkeypatch):
    # a module the driver needs that is missing is a broken install, not «not installed»
    def _import(name, *a, **kw):
        if name == "mssql_python":
            raise ModuleNotFoundError("No module named 'mssql_py_core'", name="mssql_py_core")
        return real(name, *a, **kw)

    import builtins

    real = builtins.__import__
    monkeypatch.setattr(builtins, "__import__", _import)
    with pytest.raises(ModuleNotFoundError):
        ds._sqlserver_connect(_conn())
