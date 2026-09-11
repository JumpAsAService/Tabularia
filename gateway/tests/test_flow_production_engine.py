"""Engine di SVILUPPO vs PRODUZIONE per flusso.

`Flow.engine` è il motore dell'editor (preview, run manuali); `Flow.production_engine`
è quello dei run SCHEDULATI (e di run-now ?mode=production). None = come sviluppo.
`resolve_run_engine` è la regola usata da `_launch_flow_run`.
"""
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.routes.flows import (
    _validate_production_engine,
    create_flow,
    set_flow_schedule,
    update_flow,
)
from app.routes.runs import resolve_run_engine
from app.schemas.models import FlowCreate, FlowScheduleUpdate, FlowUpdate
from tests.conftest import make_project, make_user

pytestmark = pytest.mark.anyio


def test_validate_production_engine_empty_means_same_as_dev():
    assert _validate_production_engine(None) is None
    assert _validate_production_engine("") is None
    assert _validate_production_engine("   ") is None
    assert _validate_production_engine("clickhouse") == "clickhouse"
    with pytest.raises(HTTPException) as e:
        _validate_production_engine("nope")
    assert e.value.status_code == 422


def test_resolve_run_engine_rules():
    flow = SimpleNamespace(engine="polars", production_engine="clickhouse")
    assert resolve_run_engine(flow, "development") == "polars"
    assert resolve_run_engine(flow, "production") == "clickhouse"
    # senza motore di produzione, anche in produzione si usa quello di sviluppo
    same = SimpleNamespace(engine="duckdb", production_engine=None)
    assert resolve_run_engine(same, "production") == "duckdb"
    assert resolve_run_engine(same, "development") == "duckdb"


def test_create_flow_with_production_engine(session):
    admin = make_user(session, email="a@x.local", is_superuser=True)
    p = make_project(session, name="p")
    flow = create_flow(
        p.id, FlowCreate(name="f", engine="polars", production_engine="clickhouse"),
        request=None, user=admin, session=session,
    )
    assert flow.engine == "polars" and flow.production_engine == "clickhouse"
    # "" alla creazione = nessun motore di produzione
    flow2 = create_flow(
        p.id, FlowCreate(name="g", engine="polars", production_engine=""),
        request=None, user=admin, session=session,
    )
    assert flow2.production_engine is None


def test_update_flow_sets_and_clears_production_engine(session):
    admin = make_user(session, email="a@x.local", is_superuser=True)
    p = make_project(session, name="p")
    flow = create_flow(p.id, FlowCreate(name="f"), request=None, user=admin, session=session)
    up = update_flow(flow.id, FlowUpdate(production_engine="chdb"), request=None, user=admin, session=session)
    assert up.production_engine == "chdb" and up.engine == "polars"  # lo sviluppo non cambia
    # omesso = invariato
    up = update_flow(flow.id, FlowUpdate(name="f2"), request=None, user=admin, session=session)
    assert up.production_engine == "chdb"
    # "" = torna come sviluppo
    up = update_flow(flow.id, FlowUpdate(production_engine=""), request=None, user=admin, session=session)
    assert up.production_engine is None
    with pytest.raises(HTTPException):
        update_flow(flow.id, FlowUpdate(production_engine="nope"), request=None, user=admin, session=session)


def test_schedule_can_set_production_engine(session):
    admin = make_user(session, email="a@x.local", is_superuser=True)
    p = make_project(session, name="p")
    flow = create_flow(
        p.id, FlowCreate(name="f", definition='{"nodes":[{"id":"o","type":"output","data":{}}],"edges":[]}'),
        request=None, user=admin, session=session,
    )
    up = set_flow_schedule(
        flow.id, FlowScheduleUpdate(cron="0 3 * * *", production_engine="clickhouse"),
        request=None, user=admin, session=session,
    )
    assert up.run_schedule == "0 3 * * *" and up.production_engine == "clickhouse"
    # disattivare lo schedule senza toccare il motore lo lascia com'è
    up = set_flow_schedule(flow.id, FlowScheduleUpdate(cron=""), request=None, user=admin, session=session)
    assert up.run_schedule is None and up.production_engine == "clickhouse"
