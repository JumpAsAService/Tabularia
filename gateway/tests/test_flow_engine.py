"""Selezione del motore di esecuzione per flusso (Flow.engine).

L'engine si sceglie alla CREAZIONE ed è persistito; solo gli engine disponibili
sono accettati (DuckDB entrerà quando il suo engine sarà pronto)."""
import pytest
from fastapi import HTTPException

from app.routes.flows import _validate_engine, create_flow, update_flow
from app.schemas.models import FlowCreate, FlowUpdate
from tests.conftest import make_project, make_user

pytestmark = pytest.mark.anyio


def test_validate_engine_default_and_known():
    assert _validate_engine(None) == "polars"
    assert _validate_engine("polars") == "polars"
    assert _validate_engine("  POLARS ") == "polars"  # normalizzato


def test_validate_engine_rejects_unavailable():
    with pytest.raises(HTTPException) as e:
        _validate_engine("duckdb")
    assert e.value.status_code == 422


def test_create_flow_persists_engine(session):
    admin = make_user(session, email="a@x.local", is_superuser=True)
    p = make_project(session, name="p")
    flow = create_flow(p.id, FlowCreate(name="f", engine="polars"), user=admin, session=session)
    assert flow.engine == "polars"


def test_create_flow_defaults_engine_to_polars(session):
    admin = make_user(session, email="a@x.local", is_superuser=True)
    p = make_project(session, name="p")
    flow = create_flow(p.id, FlowCreate(name="f"), user=admin, session=session)
    assert flow.engine == "polars"


def test_create_flow_rejects_unavailable_engine(session):
    admin = make_user(session, email="a@x.local", is_superuser=True)
    p = make_project(session, name="p")
    with pytest.raises(HTTPException) as e:
        create_flow(p.id, FlowCreate(name="f", engine="duckdb"), user=admin, session=session)
    assert e.value.status_code == 422


def test_update_flow_can_change_engine(session):
    admin = make_user(session, email="a@x.local", is_superuser=True)
    p = make_project(session, name="p")
    flow = create_flow(p.id, FlowCreate(name="f", engine="polars"), user=admin, session=session)
    updated = update_flow(flow.id, FlowUpdate(engine="polars"), user=admin, session=session)
    assert updated.engine == "polars"
    with pytest.raises(HTTPException):
        update_flow(flow.id, FlowUpdate(engine="nope"), user=admin, session=session)
