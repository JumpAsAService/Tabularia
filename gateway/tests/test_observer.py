"""Il ruolo osservatore: legge i pannelli di amministrazione, non scrive nulla.

Due cose da dimostrare, e la seconda conta quanto la prima: che i pannelli si
aprano, e che i dati personali dentro NON si aprano con loro.
"""
import pytest
from fastapi import HTTPException

from app.deps.auth import require_observer, require_superuser
from app.models import AuditLog, Group, UserGroupLink
from app.routes import audit as audit_routes
from app.routes import engine_policy as policy_routes
from app.routes import users as user_routes
from app.services import permissions
from app.services.masking import mask_email
from tests.conftest import make_user


def _filtri_vuoti() -> dict:
    """I filtri della rotta dell'audit, espliciti: chiamandola come funzione i
    default `Query(None)` non sono `None` e finirebbero dentro il WHERE."""
    return {
        "q": None, "action": None, "target_type": None, "outcome": None,
        "actor_id": None, "since": None, "until": None, "limit": 50, "offset": 0,
    }


def _osservatore(session, email="ospite@x.it"):
    u = make_user(session, email=email)
    u.is_observer = True
    session.add(u)
    session.commit()
    return u


# ── chi è osservatore ────────────────────────────────────────────────────────

def test_a_plain_user_is_not_an_observer(session):
    assert permissions.is_observer(session, make_user(session, email="tizio@x.it")) is False


def test_the_flag_makes_an_observer(session):
    assert permissions.is_observer(session, _osservatore(session)) is True


def test_an_administrator_is_already_an_observer(session):
    """Chi comanda vede: non serve dargli due ruoli."""
    capo = make_user(session, email="capo@x.it", is_superuser=True)
    assert permissions.is_observer(session, capo) is True


def test_a_group_can_grant_it(session):
    g = Group(name="Supervisori", is_observer=True)
    session.add(g)
    session.commit()
    session.refresh(g)
    anna = make_user(session, email="anna@x.it")
    session.add(UserGroupLink(user_id=anna.id, group_id=g.id))
    session.commit()
    assert permissions.is_observer(session, anna) is True


def test_leaving_the_group_takes_it_away(session):
    g = Group(name="Supervisori", is_observer=True)
    session.add(g)
    session.commit()
    session.refresh(g)
    anna = make_user(session, email="anna@x.it")
    link = UserGroupLink(user_id=anna.id, group_id=g.id)
    session.add(link)
    session.commit()
    assert permissions.is_observer(session, anna) is True
    session.delete(link)
    session.commit()
    assert permissions.is_observer(session, anna) is False


# ── legge, ma non scrive ─────────────────────────────────────────────────────

def test_an_observer_passes_the_read_guard_and_fails_the_write_one(session):
    ospite = _osservatore(session)
    assert require_observer(ospite, session) is ospite
    with pytest.raises(HTTPException) as e:
        require_superuser(ospite, session)
    assert e.value.status_code == 403


def test_an_observer_can_read_the_engine_policy(session):
    ospite = _osservatore(session)
    assert isinstance(policy_routes.list_engine_policy(user=ospite, session=session), list)


# ── i dati personali NON si aprono col pannello ──────────────────────────────

def test_the_users_list_is_masked_for_an_observer(session):
    ospite = _osservatore(session)
    make_user(session, email="alice.rossi@esempio.it", full_name="Alice Rossi")

    capo = make_user(session, email="c@x.it", is_superuser=True)
    come_admin = user_routes.list_users(session=session, chi_chiede=capo)
    assert any(u.email == "alice.rossi@esempio.it" and u.full_name == "Alice Rossi" for u in come_admin)

    come_ospite = user_routes.list_users(session=session, chi_chiede=ospite)
    emails = [u.email for u in come_ospite]
    assert "alice.rossi@esempio.it" not in emails
    assert "a***@esempio.it" in emails  # il dominio resta: dice dentro o fuori
    assert all(u.full_name == "" for u in come_ospite)


def test_the_audit_keeps_what_happened_and_drops_who(session):
    ospite = _osservatore(session)
    from datetime import datetime as _dt, timezone as _tz
    session.add(AuditLog(
        action="flow.run", actor_label="alice@esempio.it", outcome="success", ip="10.0.0.7",
        ts=_dt.now(_tz.utc).replace(tzinfo=None),
    ))
    session.commit()

    voce = audit_routes.list_audit(**_filtri_vuoti(), session=session, chi_chiede=ospite).items[0]
    assert voce.action == "flow.run"  # il senso del registro resta
    assert voce.actor_label == "a***@esempio.it"
    assert voce.ip is None

    capo = make_user(session, email="capo2@x.it", is_superuser=True)
    voce_admin = audit_routes.list_audit(**_filtri_vuoti(), session=session, chi_chiede=capo).items[0]
    assert voce_admin.actor_label == "alice@esempio.it" and voce_admin.ip == "10.0.0.7"


def test_the_active_sessions_hide_the_addresses(session):
    from datetime import datetime, timezone

    ospite = _osservatore(session)
    u = make_user(session, email="bruno@esempio.it")
    u.last_seen_at = datetime.now(timezone.utc).replace(tzinfo=None)
    u.last_seen_ip = "203.0.113.9"
    session.add(u)
    session.commit()

    righe = audit_routes.active_sessions(session=session, chi_chiede=ospite)
    mia = next(r for r in righe if r.user_id == u.id)
    assert mia.email == "b***@esempio.it" and mia.last_seen_ip is None
    assert mia.online is True  # lo STATO resta visibile


# ── il mascheramento in sé ───────────────────────────────────────────────────

@pytest.mark.parametrize("dentro,fuori", [
    ("alice.rossi@esempio.it", "a***@esempio.it"),
    ("x@y.z", "x***@y.z"),
    ("@senzalocale.it", "***@senzalocale.it"),
    ("scheduler", "***"),  # un'etichetta che non è un'email
    ("", ""),
])
def test_how_an_address_is_masked(dentro, fuori):
    assert mask_email(dentro) == fuori


def test_the_same_address_always_gives_the_same_mask():
    """Serve a riconoscere che due azioni sono della stessa persona senza sapere chi."""
    assert mask_email("alice@x.it") == mask_email("alice@x.it")
    assert mask_email("alice@x.it") != mask_email("bruno@x.it")
