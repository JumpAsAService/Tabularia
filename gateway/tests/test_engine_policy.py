"""The engines allowed in the installation (admin panel).

The contracts that count, and that this file nails down:

1. with an empty table everything is allowed — updating changes nothing;
2. disabling prevents CHOOSING the engine, at every point where it is chosen
   (creation, change, production engine);
3. disabling does NOT stop the flows that use it already: it is the design decision,
   not an oversight, so it is checked explicitly;
4. the last allowed engine cannot be removed;
5. every change ends up in the audit.
"""
import pytest
from fastapi import HTTPException

from app.models import DisabledEngine
from app.routes.engine_policy import list_engine_policy, set_engine_policy
from app.routes.flows import _validate_engine, _validate_production_engine
from app.routes.runs import resolve_run_engine
from app.schemas.models import EnginePolicyUpdate
from app.services import audit
from app.services.engine_policy import KNOWN_ENGINES, allowed_engines, disabled_engines
from tests.conftest import make_flow, make_user
from sqlmodel import select


def _admin(session):
    return make_user(session, email="admin@x.local", is_superuser=True)


def _actions(session) -> list[str]:
    from app.models import AuditLog

    return [r.action for r in session.exec(select(AuditLog)).all()]


def _forbid(session, admin, engine_id: str):
    return set_engine_policy(engine_id, EnginePolicyUpdate(allowed=False), user=admin, session=session)


# ── 1. default: everything allowed ──────────────────────────────────────────
def test_by_default_every_engine_is_allowed(session):
    assert disabled_engines(session) == set()
    assert allowed_engines(session) == set(KNOWN_ENGINES)
    state = list_engine_policy(user=_admin(session), session=session)
    assert {s.engine_id for s in state} == set(KNOWN_ENGINES)
    assert all(s.allowed for s in state)


def test_validation_without_a_session_ignores_the_policy(session):
    """The signature stays usable without a session (catalog only): it is what lets the
    existing validation tests not change."""
    _forbid(session, _admin(session), "duckdb")
    assert _validate_engine("duckdb") == "duckdb"  # no session = no criterion


# ── 2. disabling prevents choosing ──────────────────────────────────────────
def test_disabling_refuses_the_choice_everywhere(session):
    admin = _admin(session)
    _forbid(session, admin, "polars")

    for choice in (lambda: _validate_engine("polars", session),
                   lambda: _validate_production_engine("polars", session)):
        with pytest.raises(HTTPException) as e:
            choice()
        assert e.value.status_code == 422
        assert "administrator" in str(e.value.detail)

    # the others stay choosable
    assert _validate_engine("clickhouse", session) == "clickhouse"


def test_reallowing_restores_the_choice(session):
    admin = _admin(session)
    _forbid(session, admin, "chdb")
    with pytest.raises(HTTPException):
        _validate_engine("chdb", session)

    set_engine_policy("chdb", EnginePolicyUpdate(allowed=True), user=admin, session=session)
    assert _validate_engine("chdb", session) == "chdb"
    assert session.exec(select(DisabledEngine)).all() == []


def test_unknown_engine_is_404(session):
    with pytest.raises(HTTPException) as e:
        set_engine_policy("nope", EnginePolicyUpdate(allowed=False), user=_admin(session), session=session)
    assert e.value.status_code == 404


# ── 3. the existing flows do NOT stop (design decision) ─────────────────────
def test_existing_flows_keep_running_on_a_disabled_engine(session):
    """The chosen contract: the CHOICE is forbidden, not the run. A switch in the panel must
    not stop a scheduled DAG."""
    admin = _admin(session)
    flow = make_flow(session, name="storico", engine="polars", production_engine="polars")
    _forbid(session, admin, "polars")

    assert resolve_run_engine(flow, "development") == "polars"
    assert resolve_run_engine(flow, "production") == "polars"


def test_flows_using_counts_both_development_and_production(session):
    admin = _admin(session)
    make_flow(session, name="a", engine="polars")                          # development only
    make_flow(session, name="b", engine="duckdb", production_engine="polars")  # production only
    make_flow(session, name="c", engine="clickhouse")                      # neither

    state = {s.engine_id: s.flows_using for s in list_engine_policy(user=admin, session=session)}
    assert state["polars"] == 2       # counted for development AND for production
    assert state["duckdb"] == 1
    assert state["clickhouse"] == 1
    assert state["chdb"] == 0


# ── 4. no running out of engines ────────────────────────────────────────────
def test_the_last_allowed_engine_cannot_be_removed(session):
    admin = _admin(session)
    for e in KNOWN_ENGINES[:-1]:
        _forbid(session, admin, e)

    with pytest.raises(HTTPException) as err:
        _forbid(session, admin, KNOWN_ENGINES[-1])
    assert err.value.status_code == 422
    assert allowed_engines(session) == {KNOWN_ENGINES[-1]}


# ── 5. audit ────────────────────────────────────────────────────────────────
def test_both_directions_are_audited(session):
    admin = _admin(session)
    _forbid(session, admin, "duckdb")
    assert audit.ENGINE_DISALLOW in _actions(session)

    set_engine_policy("duckdb", EnginePolicyUpdate(allowed=True), user=admin, session=session)
    assert audit.ENGINE_ALLOW in _actions(session)


def test_audit_records_how_many_flows_were_left_behind(session):
    from app.models import AuditLog

    admin = _admin(session)
    make_flow(session, name="a", engine="chdb")
    make_flow(session, name="b", engine="chdb")
    _forbid(session, admin, "chdb")

    row = session.exec(select(AuditLog).where(AuditLog.action == audit.ENGINE_DISALLOW)).one()
    assert '"flows_using": 2' in (row.detail or "")


# ── 6. the catalog served to the selector ───────────────────────────────────
# `GET /engines` is no longer a blind forward: it is the point where the installation's
# policy is laid over what the engine declares. It is new logic in a proxy route, so it
# is covered — the guard next to this one had been left without tests, and that is where
# a defect had got through.
@pytest.mark.anyio
async def test_catalogue_marks_the_disallowed_engine(session, fake_engine):
    from app.routes.proxy import engines as catalog

    _forbid(session, _admin(session), "polars")
    by_id = {e["id"]: e for e in await catalog(request=None, session=session)}

    assert by_id["polars"]["available"] is False
    assert by_id["polars"]["disabled_by_admin"] is True
    # «not allowed here» must not infect the others, nor be confused with «not configured»:
    # whoever stays allowed gets no marker
    assert by_id["duckdb"]["available"] is True
    assert "disabled_by_admin" not in by_id["duckdb"]


@pytest.mark.anyio
async def test_catalogue_is_untouched_when_nothing_is_disallowed(session, fake_engine):
    """The normal case: no policy, no rewriting."""
    from app.routes.proxy import engines as catalog

    listing = await catalog(request=None, session=session)
    assert all(e["available"] for e in listing)
    assert not any("disabled_by_admin" in e for e in listing)
