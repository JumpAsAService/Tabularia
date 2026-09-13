"""Motori consentiti nell'installazione (pannello admin).

I contratti che contano, e che questo file inchioda:

1. a tabella vuota tutto è consentito — aggiornare non cambia nulla;
2. disabilitare impedisce di SCEGLIERE il motore, in tutti i punti in cui lo si
   sceglie (creazione, cambio, motore di produzione);
3. disabilitare NON ferma i flussi che lo usano già: è la decisione di
   progetto, non una svista, quindi è verificata esplicitamente;
4. l'ultimo motore consentito non si può togliere;
5. ogni cambio finisce nell'audit.
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


def _azioni(session) -> list[str]:
    from app.models import AuditLog

    return [r.action for r in session.exec(select(AuditLog)).all()]


def _vieta(session, admin, engine_id: str):
    return set_engine_policy(engine_id, EnginePolicyUpdate(allowed=False), user=admin, session=session)


# ── 1. default: tutto consentito ────────────────────────────────────────────
def test_by_default_every_engine_is_allowed(session):
    assert disabled_engines(session) == set()
    assert allowed_engines(session) == set(KNOWN_ENGINES)
    stato = list_engine_policy(user=_admin(session), session=session)
    assert {s.engine_id for s in stato} == set(KNOWN_ENGINES)
    assert all(s.allowed for s in stato)


def test_validation_without_a_session_ignores_the_policy(session):
    """La firma resta usabile senza sessione (catalogo e basta): è ciò che
    permette ai test di validazione esistenti di non cambiare."""
    _vieta(session, _admin(session), "duckdb")
    assert _validate_engine("duckdb") == "duckdb"  # nessuna sessione = nessun criterio


# ── 2. disabilitare impedisce di scegliere ──────────────────────────────────
def test_disabling_refuses_the_choice_everywhere(session):
    admin = _admin(session)
    _vieta(session, admin, "polars")

    for scelta in (lambda: _validate_engine("polars", session),
                   lambda: _validate_production_engine("polars", session)):
        with pytest.raises(HTTPException) as e:
            scelta()
        assert e.value.status_code == 422
        assert "amministratore" in str(e.value.detail)

    # gli altri restano scegliibili
    assert _validate_engine("clickhouse", session) == "clickhouse"


def test_reallowing_restores_the_choice(session):
    admin = _admin(session)
    _vieta(session, admin, "chdb")
    with pytest.raises(HTTPException):
        _validate_engine("chdb", session)

    set_engine_policy("chdb", EnginePolicyUpdate(allowed=True), user=admin, session=session)
    assert _validate_engine("chdb", session) == "chdb"
    assert session.exec(select(DisabledEngine)).all() == []


def test_unknown_engine_is_404(session):
    with pytest.raises(HTTPException) as e:
        set_engine_policy("nope", EnginePolicyUpdate(allowed=False), user=_admin(session), session=session)
    assert e.value.status_code == 404


# ── 3. i flussi esistenti NON si fermano (decisione di progetto) ────────────
def test_existing_flows_keep_running_on_a_disabled_engine(session):
    """Il contratto scelto: si vieta la SCELTA, non l'esecuzione. Un
    interruttore nel pannello non deve fermare un DAG schedulato."""
    admin = _admin(session)
    flow = make_flow(session, name="storico", engine="polars", production_engine="polars")
    _vieta(session, admin, "polars")

    assert resolve_run_engine(flow, "development") == "polars"
    assert resolve_run_engine(flow, "production") == "polars"


def test_flows_using_counts_both_development_and_production(session):
    admin = _admin(session)
    make_flow(session, name="a", engine="polars")                          # solo sviluppo
    make_flow(session, name="b", engine="duckdb", production_engine="polars")  # solo produzione
    make_flow(session, name="c", engine="clickhouse")                      # nessuno dei due

    stato = {s.engine_id: s.flows_using for s in list_engine_policy(user=admin, session=session)}
    assert stato["polars"] == 2       # contato per sviluppo E per produzione
    assert stato["duckdb"] == 1
    assert stato["clickhouse"] == 1
    assert stato["chdb"] == 0


# ── 4. non si resta senza motori ────────────────────────────────────────────
def test_the_last_allowed_engine_cannot_be_removed(session):
    admin = _admin(session)
    for e in KNOWN_ENGINES[:-1]:
        _vieta(session, admin, e)

    with pytest.raises(HTTPException) as err:
        _vieta(session, admin, KNOWN_ENGINES[-1])
    assert err.value.status_code == 422
    assert allowed_engines(session) == {KNOWN_ENGINES[-1]}


# ── 5. audit ────────────────────────────────────────────────────────────────
def test_both_directions_are_audited(session):
    admin = _admin(session)
    _vieta(session, admin, "duckdb")
    assert audit.ENGINE_DISALLOW in _azioni(session)

    set_engine_policy("duckdb", EnginePolicyUpdate(allowed=True), user=admin, session=session)
    assert audit.ENGINE_ALLOW in _azioni(session)


def test_audit_records_how_many_flows_were_left_behind(session):
    from app.models import AuditLog

    admin = _admin(session)
    make_flow(session, name="a", engine="chdb")
    make_flow(session, name="b", engine="chdb")
    _vieta(session, admin, "chdb")

    riga = session.exec(select(AuditLog).where(AuditLog.action == audit.ENGINE_DISALLOW)).one()
    assert '"flows_using": 2' in (riga.detail or "")


# ── 6. il catalogo servito al selettore ─────────────────────────────────────
# `GET /engines` non è più un inoltro cieco: è il punto in cui la politica
# dell'installazione si sovrappone a ciò che l'engine dichiara. È logica nuova
# in una rotta del proxy, quindi è coperta — la guardia accanto a questa era
# rimasta senza test, ed è da lì che era passato un difetto.
@pytest.mark.anyio
async def test_catalogue_marks_the_disallowed_engine(session, fake_engine):
    from app.routes.proxy import engines as catalogo

    _vieta(session, _admin(session), "polars")
    per_id = {e["id"]: e for e in await catalogo(request=None, session=session)}

    assert per_id["polars"]["available"] is False
    assert per_id["polars"]["disabled_by_admin"] is True
    # «non consentito qui» non deve contagiare gli altri, né confondersi con
    # «non configurato»: chi resta consentito non prende alcun marcatore
    assert per_id["duckdb"]["available"] is True
    assert "disabled_by_admin" not in per_id["duckdb"]


@pytest.mark.anyio
async def test_catalogue_is_untouched_when_nothing_is_disallowed(session, fake_engine):
    """Il caso normale: nessuna politica, nessuna riscrittura."""
    from app.routes.proxy import engines as catalogo

    elenco = await catalogo(request=None, session=session)
    assert all(e["available"] for e in elenco)
    assert not any("disabled_by_admin" in e for e in elenco)
