"""The observer role: reads the administration panels, writes nothing.

Two things to prove, and the second counts as much as the first: that the panels
open, and that the personal data inside does NOT open with them.
"""
import pytest
from fastapi import HTTPException

from sqlmodel import select

from app.deps.auth import require_observer, require_superuser
from app.models import AuditLog, Group, User, UserGroupLink
from app.routes import audit as audit_routes
from app.routes import engine_policy as policy_routes
from app.routes import groups as group_routes
from app.routes import users as user_routes
from app.schemas.models import GroupUpdate, UserCreate, UserUpdate
from app.services import audit as audit_svc
from app.services import permissions
from app.services.masking import mask_email
from tests.conftest import make_user


def _empty_filters() -> dict:
    """The audit route's filters, explicit: calling it as a function, the `Query(None)`
    defaults are not `None` and would end up in the WHERE."""
    return {
        "q": None, "action": None, "target_type": None, "outcome": None,
        "actor_id": None, "since": None, "until": None, "limit": 50, "offset": 0,
    }


def _observer(session, email="ospite@x.it"):
    u = make_user(session, email=email)
    u.is_observer = True
    session.add(u)
    session.commit()
    return u


# ── who is an observer ───────────────────────────────────────────────────────

def test_a_plain_user_is_not_an_observer(session):
    assert permissions.is_observer(session, make_user(session, email="tizio@x.it")) is False


def test_the_flag_makes_an_observer(session):
    assert permissions.is_observer(session, _observer(session)) is True


def test_an_administrator_is_already_an_observer(session):
    """Whoever is in charge sees: no need to give them two roles."""
    boss = make_user(session, email="capo@x.it", is_superuser=True)
    assert permissions.is_observer(session, boss) is True


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


# ── reads, but does not write ────────────────────────────────────────────────

def test_an_observer_passes_the_read_guard_and_fails_the_write_one(session):
    guest = _observer(session)
    assert require_observer(guest, session) is guest
    with pytest.raises(HTTPException) as e:
        require_superuser(guest, session)
    assert e.value.status_code == 403


def test_an_observer_can_read_the_engine_policy(session):
    guest = _observer(session)
    assert isinstance(policy_routes.list_engine_policy(user=guest, session=session), list)


# ── the personal data does NOT open with the panel ───────────────────────────

def test_the_users_list_is_masked_for_an_observer(session):
    guest = _observer(session)
    make_user(session, email="alice.rossi@esempio.it", full_name="Alice Rossi")

    boss = make_user(session, email="c@x.it", is_superuser=True)
    as_admin = user_routes.list_users(session=session, asker=boss)
    assert any(u.email == "alice.rossi@esempio.it" and u.full_name == "Alice Rossi" for u in as_admin)

    as_guest = user_routes.list_users(session=session, asker=guest)
    emails = [u.email for u in as_guest]
    assert "alice.rossi@esempio.it" not in emails
    assert "a***@esempio.it" in emails  # the domain stays: it says inside or outside
    assert all(u.full_name == "" for u in as_guest)


def test_the_audit_keeps_what_happened_and_drops_who(session):
    guest = _observer(session)
    from datetime import datetime as _dt, timezone as _tz
    session.add(AuditLog(
        action="flow.run", actor_label="alice@esempio.it", outcome="success", ip="10.0.0.7",
        ts=_dt.now(_tz.utc).replace(tzinfo=None),
    ))
    session.commit()

    item = audit_routes.list_audit(**_empty_filters(), session=session, asker=guest).items[0]
    assert item.action == "flow.run"  # the log's point stays
    assert item.actor_label == "a***@esempio.it"
    assert item.ip is None

    boss = make_user(session, email="capo2@x.it", is_superuser=True)
    admin_item = audit_routes.list_audit(**_empty_filters(), session=session, asker=boss).items[0]
    assert admin_item.actor_label == "alice@esempio.it" and admin_item.ip == "10.0.0.7"


def test_the_active_sessions_hide_the_addresses(session):
    from types import SimpleNamespace

    from app.services import sessions

    guest = _observer(session)
    u = make_user(session, email="bruno@esempio.it")
    sessions.open_session(session, u, _Fake(), "password")

    rows = audit_routes.active_sessions(request=SimpleNamespace(), limit=100, session=session, asker=guest).items
    mia = next(r for r in rows if r.user_id == u.id)
    assert mia.email == "b***@esempio.it" and mia.ip is None and mia.user_agent is None
    assert mia.online is True  # the STATE stays visible


# ── the masking itself ───────────────────────────────────────────────────────

@pytest.mark.parametrize("inside,outside", [
    ("alice.rossi@esempio.it", "a***@esempio.it"),
    ("x@y.z", "x***@y.z"),
    ("@senzalocale.it", "***@senzalocale.it"),
    ("scheduler", "***"),  # a label that is not an email
    ("", ""),
])
def test_how_an_address_is_masked(inside, outside):
    assert mask_email(inside) == outside


def test_the_same_address_always_gives_the_same_mask():
    """It serves to recognize that two actions are the same person's without knowing whose."""
    assert mask_email("alice@x.it") == mask_email("alice@x.it")
    assert mask_email("alice@x.it") != mask_email("bruno@x.it")


# ── granted from the panel, not with a line of SQL ────────────────────────────

def test_an_administrator_can_grant_and_revoke_it(session):
    """The role is useless if turning it on needs psql: this is the path the panel's button
    uses."""
    boss = make_user(session, email="capo3@x.it", is_superuser=True)
    guest = make_user(session, email="ospite2@x.it")

    outside = user_routes.update_user(
        guest.id, UserUpdate(is_observer=True), request=None, session=session, current=boss,
    )
    assert outside.is_observer is True
    assert permissions.is_observer(session, session.get(User, guest.id)) is True

    outside = user_routes.update_user(
        guest.id, UserUpdate(is_observer=False), request=None, session=session, current=boss,
    )
    assert outside.is_observer is False
    assert permissions.is_observer(session, session.get(User, guest.id)) is False


def test_granting_it_leaves_a_trace(session):
    """It opens audit, sessions and the users' list: it is worth the trace as much as a
    promotion."""
    boss = make_user(session, email="capo4@x.it", is_superuser=True)
    guest = make_user(session, email="ospite3@x.it")
    user_routes.update_user(
        guest.id, UserUpdate(is_observer=True), request=None, session=session, current=boss,
    )
    actions = [a.action for a in session.exec(select(AuditLog)).all()]
    assert audit_svc.OBSERVER_GRANT in actions
    assert audit_svc.USER_PROMOTE not in actions  # it is not a promotion to admin

    user_routes.update_user(
        guest.id, UserUpdate(is_observer=False), request=None, session=session, current=boss,
    )
    assert audit_svc.OBSERVER_REVOKE in [a.action for a in session.exec(select(AuditLog)).all()]


def test_it_can_be_granted_at_creation(session):
    """The demo account is created in one go, with the tick in the form."""
    boss = make_user(session, email="capo5@x.it", is_superuser=True)
    outside = user_routes.create_user(
        UserCreate(email="demo@x.it", password="segretissima", is_observer=True),
        request=None, session=session, current=boss,
    )
    assert outside.is_observer is True
    assert audit_svc.OBSERVER_GRANT in [a.action for a in session.exec(select(AuditLog)).all()]


def test_a_group_can_be_switched_to_observer(session):
    """The way to use with SSO: the IdP rewrites the personal flag at every login."""
    boss = make_user(session, email="capo6@x.it", is_superuser=True)
    g = Group(name="Ospiti")
    session.add(g)
    session.commit()
    session.refresh(g)
    anna = make_user(session, email="anna2@x.it")
    session.add(UserGroupLink(user_id=anna.id, group_id=g.id))
    session.commit()
    assert permissions.is_observer(session, anna) is False

    outside = group_routes.update_group(
        g.id, GroupUpdate(is_observer=True), request=None, session=session, current=boss,
    )
    assert outside.is_observer is True
    assert permissions.is_observer(session, session.get(User, anna.id)) is True
    assert audit_svc.GROUP_OBSERVER_GRANT in [a.action for a in session.exec(select(AuditLog)).all()]


def test_the_list_says_which_group_grants_it(session):
    """As for the admins: the list must show the permission is the group's, otherwise one
    looks for a personal flag that is not there."""
    g = Group(name="Ospiti", is_observer=True)
    session.add(g)
    session.commit()
    session.refresh(g)
    anna = make_user(session, email="anna3@x.it")
    session.add(UserGroupLink(user_id=anna.id, group_id=g.id))
    session.commit()
    boss = make_user(session, email="capo7@x.it", is_superuser=True)

    row = next(u for u in user_routes.list_users(session=session, asker=boss) if u.id == anna.id)
    assert row.is_observer is False  # no personal flag
    assert row.observer_groups == ["Ospiti"]


# ── an observer's IP address is not collected at all ─────────────────────────

class _Fake:
    """A request just enough for `record_audit` and `_touch_last_seen`."""

    def __init__(self, ip="203.0.113.77"):
        self.headers = {"user-agent": "prova", "x-forwarded-for": ip}
        self.client = type("C", (), {"host": ip})()


def test_an_observers_address_is_not_written_to_the_audit(session):
    """Masking the view is not enough: the data must not get in at all."""
    from app.services import audit as audit_svc

    guest = _observer(session, email="ospite-ip@x.it")
    audit_svc.record_audit(session, actor=guest, action="flow.run", request=_Fake())

    item = session.exec(select(AuditLog).where(AuditLog.actor_id == guest.id)).one()
    assert item.ip is None
    assert item.user_agent is None            # neither browser nor operating system
    assert item.action == "flow.run"          # what was done stays
    assert item.actor_label == guest.email   # and who, for an administrator


def test_an_administrators_address_is_still_written(session):
    """Whoever can change things leaves the trace: it is the log's point."""
    from app.services import audit as audit_svc

    boss = make_user(session, email="capo-ip@x.it", is_superuser=True)
    audit_svc.record_audit(session, actor=boss, action="flow.run", request=_Fake())

    item = session.exec(select(AuditLog).where(AuditLog.actor_id == boss.id)).one()
    assert item.ip == "203.0.113.77"
    assert item.user_agent == "prova"


def test_an_ordinary_user_is_still_traced(session):
    """The renunciation holds for the OBSERVERS, not for everyone: whoever uploads, changes
    and runs stays tracked."""
    from app.services import audit as audit_svc

    someone = make_user(session, email="tizio-ip@x.it")
    audit_svc.record_audit(session, actor=someone, action="datasource.create", request=_Fake())

    item = session.exec(select(AuditLog).where(AuditLog.actor_id == someone.id)).one()
    assert item.ip == "203.0.113.77"


def test_an_admin_by_group_keeps_being_traced(session):
    """`is_observer` is true for the admins too: if the rule looked at that, we would stop
    tracking exactly whoever is in charge."""
    from app.services import audit as audit_svc

    g = Group(name="Capi", is_admin=True)
    session.add(g)
    session.commit()
    session.refresh(g)
    anna = make_user(session, email="anna-ip@x.it")
    session.add(UserGroupLink(user_id=anna.id, group_id=g.id))
    session.commit()

    audit_svc.record_audit(session, actor=anna, action="flow.run", request=_Fake())
    item = session.exec(select(AuditLog).where(AuditLog.actor_id == anna.id)).one()
    assert item.ip == "203.0.113.77"


def test_last_seen_keeps_the_moment_and_drops_the_address(session):
    """The «active sessions» must say THAT someone is connected, not from where."""
    from app.deps.auth import _touch_last_seen

    from app.services import sessions

    guest = _observer(session, email="ospite-seen@x.it")
    open_ = sessions.open_session(session, guest, _Fake(), "password")[0]
    _touch_last_seen(session, guest, open_, _Fake())
    session.refresh(guest)
    session.refresh(open_)
    assert guest.last_seen_at is not None
    assert guest.last_seen_ip is None and open_.ip is None and open_.user_agent is None


def test_an_address_collected_before_the_role_is_erased(session):
    """The role may come later: at the first pass the old address goes, otherwise it would
    stay there forever."""
    from app.deps.auth import _touch_last_seen

    from app.services import sessions

    u = make_user(session, email="prima-poi@x.it")
    open_ = sessions.open_session(session, u, _Fake(), "password")[0]
    _touch_last_seen(session, u, open_, _Fake())
    session.refresh(u)
    session.refresh(open_)
    # the address sits in the session, no longer on the user
    assert open_.ip == "203.0.113.77" and u.last_seen_ip is None

    u.is_observer = True
    u.last_seen_at = None          # skips the throttle's brake
    session.add(u)
    session.commit()
    _touch_last_seen(session, u, open_, _Fake())
    session.refresh(u)
    session.refresh(open_)
    assert u.last_seen_ip is None and open_.ip is None and open_.user_agent is None


def test_a_failed_login_still_records_the_address(session):
    """Without an actor the rule cannot apply, and rightly so: there the address is the only
    trace of a brute-force attempt. Documented, not forgotten."""
    from app.services import audit as audit_svc

    audit_svc.record_audit(session, actor=None, actor_label="ospite-ip@x.it",
                           action=audit_svc.LOGIN_FAILED, outcome="failure", request=_Fake())
    item = session.exec(select(AuditLog).where(AuditLog.action == audit_svc.LOGIN_FAILED)).one()
    assert item.ip == "203.0.113.77"
