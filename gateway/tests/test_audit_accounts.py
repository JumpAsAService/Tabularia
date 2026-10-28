"""Accounts and groups leave a trace, from the panel and from single sign-on alike.

Up to 1.2.2 the log showed a new account only when it was made administrator or
observer, a deactivation only as the closing of its sessions (and not at all when it had
none), and no membership of an ordinary group, no group deleted, no name changed. What
SSO did at a sign-in was only inside the `auth.sso_login` detail. After a leaked admin
token, "who gave this account access to Finance" is exactly what the log must answer.

The rules pinned here: one action per change, with whose account or which group, and
what changed from what to what; never a password; nothing written when nothing changes;
what SSO does on its own is the same action with `"via": "sso"`.
"""
import json

import pytest
from sqlmodel import Session, select
from starlette.requests import Request

from app.core.config import OidcSettings, Settings
from app.db import seed
from app.models import AuditLog, Group, User, UserGroupLink
from app.routes import audit as audit_routes
from app.routes import groups as group_routes
from app.routes import users as user_routes
from app.schemas.models import GroupCreate, GroupUpdate, UserCreate, UserUpdate
from app.services import audit, sso
from tests.conftest import make_user, token_of

PASSWORD = "a-long-password-of-mine-2026"
ISS = "https://idp.example/realms/t"


def _req() -> Request:
    return Request({"type": "http", "method": "POST", "path": "/", "headers": [(b"user-agent", b"pytest")],
                    "client": ("172.18.0.1", 1), "query_string": b""})


def _entries(session, action=None):
    """The log of accounts and groups: the personal folder an account gets when it is
    created or first signs in is a folder's action, pinned in test_personal_folders.py."""
    query = select(AuditLog).order_by(AuditLog.id)
    if action is not None:
        query = query.where(AuditLog.action == action)
    else:
        query = query.where(AuditLog.action != audit.FOLDER_CREATE)
    return session.exec(query).all()


def _actions(session):
    return [e.action for e in _entries(session)]


def _detail(entry):
    return json.loads(entry.detail) if entry.detail else None


def _group(session, name, **flags):
    g = Group(name=name, **flags)
    session.add(g)
    session.commit()
    session.refresh(g)
    return g


@pytest.fixture
def admin(session):
    return make_user(session, email="admin@x.it", is_superuser=True)


# ── accounts, from the panel ─────────────────────────────────────────────────

def test_creating_a_user(session, admin):
    out = user_routes.create_user(UserCreate(email="anna@x.it", password=PASSWORD, full_name="Anna"),
                                  request=_req(), session=session, current=admin)
    (entry,) = _entries(session)
    assert entry.action == audit.USER_CREATE and entry.actor_id == admin.id
    assert (entry.target_type, entry.target_id, entry.target_label) == ("user", out.id, "anna@x.it")
    assert entry.ip == "172.18.0.1"


def test_a_user_created_administrator_and_observer_keeps_its_role_actions(session, admin):
    user_routes.create_user(UserCreate(email="capo@x.it", password=PASSWORD, is_superuser=True, is_observer=True),
                            request=_req(), session=session, current=admin)
    assert _actions(session) == [audit.USER_CREATE, audit.USER_PROMOTE, audit.OBSERVER_GRANT]


def test_no_password_ever_reaches_the_log(session, admin):
    out = user_routes.create_user(UserCreate(email="anna@x.it", password=PASSWORD),
                                  request=_req(), session=session, current=admin)
    other = "another-long-password-2026"
    user_routes.update_user(out.id, UserUpdate(password=other, full_name="Anna B.", is_active=False),
                            request=_req(), session=session, current=admin)
    for entry in _entries(session):
        written = f"{entry.detail} {entry.target_label} {entry.actor_label}"
        assert PASSWORD not in written and other not in written
        assert "hashed" not in (entry.detail or "")


def test_renaming_a_user(session, admin):
    anna = make_user(session, email="anna@x.it", full_name="Anna")
    user_routes.update_user(anna.id, UserUpdate(full_name="Anna Bianchi"), request=_req(), session=session,
                            current=admin)
    (entry,) = _entries(session)
    assert entry.action == audit.USER_UPDATE and entry.target_id == anna.id and entry.target_label == "anna@x.it"
    assert _detail(entry) == {"full_name": {"from": "Anna", "to": "Anna Bianchi"}}
    # the same name sent again (a form saved unchanged): nothing new
    user_routes.update_user(anna.id, UserUpdate(full_name="Anna Bianchi"), request=_req(), session=session,
                            current=admin)
    assert len(_entries(session)) == 1


def test_deactivating_and_reactivating_an_account(session, admin):
    anna = make_user(session, email="anna@x.it")
    token_of(session, anna)  # an open session: its closing follows the deactivation
    user_routes.update_user(anna.id, UserUpdate(is_active=False), request=_req(), session=session, current=admin)
    assert _actions(session) == [audit.USER_DEACTIVATE, audit.SESSION_REVOKE]
    deactivated = _entries(session, audit.USER_DEACTIVATE)[0]
    assert deactivated.target_id == anna.id and deactivated.target_label == "anna@x.it"
    assert deactivated.actor_id == admin.id

    # already off: nothing changes, nothing is written
    user_routes.update_user(anna.id, UserUpdate(is_active=False), request=_req(), session=session, current=admin)
    assert _actions(session) == [audit.USER_DEACTIVATE, audit.SESSION_REVOKE]

    user_routes.update_user(anna.id, UserUpdate(is_active=True), request=_req(), session=session, current=admin)
    user_routes.update_user(anna.id, UserUpdate(is_active=True), request=_req(), session=session, current=admin)
    assert _actions(session) == [audit.USER_DEACTIVATE, audit.SESSION_REVOKE, audit.USER_REACTIVATE]


def test_an_account_without_sessions_is_deactivated_in_the_log_too(session, admin):
    """The case that left no trace at all: no session to close, no `auth.session_revoke`."""
    anna = make_user(session, email="anna@x.it")
    user_routes.update_user(anna.id, UserUpdate(is_active=False), request=_req(), session=session, current=admin)
    assert _actions(session) == [audit.USER_DEACTIVATE]


def test_an_unchanged_patch_writes_nothing(session, admin):
    anna = make_user(session, email="anna@x.it", full_name="Anna")
    user_routes.update_user(anna.id, UserUpdate(full_name="Anna", is_active=True, is_superuser=False,
                                                is_observer=False),
                            request=_req(), session=session, current=admin)
    assert _entries(session) == []


# ── memberships, from the panel ──────────────────────────────────────────────

def test_joining_and_leaving_an_ordinary_group(session, admin):
    anna = make_user(session, email="anna@x.it")
    finance = _group(session, "finance")
    user_routes.add_to_group(anna.id, finance.id, request=_req(), session=session, current=admin)
    user_routes.add_to_group(anna.id, finance.id, request=_req(), session=session, current=admin)  # already in
    user_routes.remove_from_group(anna.id, finance.id, request=_req(), session=session, current=admin)
    user_routes.remove_from_group(anna.id, finance.id, request=_req(), session=session, current=admin)  # out already
    added, removed = _entries(session)
    assert (added.action, removed.action) == (audit.GROUP_MEMBER_ADD, audit.GROUP_MEMBER_REMOVE)
    for entry in (added, removed):
        assert (entry.target_type, entry.target_id, entry.target_label) == ("user", anna.id, "anna@x.it")
        assert entry.actor_id == admin.id
        assert _detail(entry) == {"group": "finance", "group_id": finance.id}


def test_joining_an_observers_group_says_so(session, admin):
    """Joining it IS receiving the role: the detail says which role the group carries."""
    anna = make_user(session, email="anna@x.it")
    guests = _group(session, "guests", is_observer=True)
    user_routes.add_to_group(anna.id, guests.id, request=_req(), session=session, current=admin)
    (entry,) = _entries(session, audit.GROUP_MEMBER_ADD)
    assert _detail(entry) == {"group": "guests", "group_id": guests.id, "observer": True}


def test_an_administrators_group_has_the_membership_and_the_privilege(session, admin):
    anna = make_user(session, email="anna@x.it")
    bosses = _group(session, "bosses", is_admin=True)
    user_routes.add_to_group(anna.id, bosses.id, request=_req(), session=session, current=admin)
    assert _actions(session) == [audit.GROUP_MEMBER_ADD, audit.ADMIN_GROUP_JOIN]
    assert _detail(_entries(session)[0]) == {"group": "bosses", "group_id": bosses.id, "admin": True}


# ── groups, from the panel ───────────────────────────────────────────────────

def test_describing_a_group(session, admin):
    g = group_routes.create_group(GroupCreate(name="finance"), request=_req(), session=session, current=admin)
    group_routes.update_group(g.id, GroupUpdate(description="Budget and closing"), request=_req(),
                              session=session, current=admin)
    # the same text again, or only a flag: no `group.update`
    group_routes.update_group(g.id, GroupUpdate(description="Budget and closing"), request=_req(),
                              session=session, current=admin)
    group_routes.update_group(g.id, GroupUpdate(is_observer=True), request=_req(), session=session, current=admin)
    assert _actions(session) == [audit.GROUP_CREATE, audit.GROUP_UPDATE, audit.GROUP_OBSERVER_GRANT]
    (entry,) = _entries(session, audit.GROUP_UPDATE)
    assert (entry.target_type, entry.target_id, entry.target_label) == ("group", g.id, "finance")
    assert _detail(entry) == {"description": {"from": "", "to": "Budget and closing"}}


def test_deleting_an_ordinary_group(session, admin):
    g = _group(session, "finance")
    for email in ("anna@x.it", "bruno@x.it"):
        session.add(UserGroupLink(user_id=make_user(session, email=email).id, group_id=g.id))
    session.commit()
    group_routes.delete_group(g.id, request=_req(), session=session, current=admin)
    (entry,) = _entries(session)
    assert entry.action == audit.GROUP_DELETE and entry.actor_id == admin.id
    assert (entry.target_type, entry.target_id, entry.target_label) == ("group", g.id, "finance")
    assert _detail(entry) == {"members": 2}


def test_deleting_an_observers_group_withdraws_the_role(session, admin):
    g = _group(session, "guests", is_observer=True)
    group_routes.delete_group(g.id, request=_req(), session=session, current=admin)
    assert _actions(session) == [audit.GROUP_DELETE, audit.GROUP_OBSERVER_REVOKE]
    assert _detail(_entries(session)[0]) == {"members": 0, "observer": True}
    assert _detail(_entries(session)[1]) == {"deleted": True}


def test_an_observer_does_not_read_a_persons_name_in_the_detail(session, admin):
    """The users' list blanks the names for an observer: the log must not give them back."""
    anna = make_user(session, email="anna@x.it", full_name="Anna")
    user_routes.update_user(anna.id, UserUpdate(full_name="Anna Bianchi"), request=_req(), session=session,
                            current=admin)
    (entry,) = _entries(session)
    seen = audit_routes._to_out(entry, full=False)
    assert seen.detail == {"full_name": "…"} and seen.target_label != "anna@x.it"
    assert audit_routes._to_out(entry).detail == {"full_name": {"from": "Anna", "to": "Anna Bianchi"}}


# ── single sign-on ───────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _sso_isolated_from_environment(monkeypatch):
    import os

    from app.core.config import get_settings

    for key in [k for k in os.environ if k.startswith("OIDC__")]:
        monkeypatch.delenv(key, raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _cfg(**kw) -> OidcSettings:
    return OidcSettings(**{"issuer": ISS, "client_id": "tabularia-gateway", **kw})


def _claims(email: str, sub: str | None = None, **kw) -> dict:
    return {"iss": ISS, "sub": sub or f"sub-of-{email}", "email": email, **kw}


def _sso(session, claims, cfg=None):
    return sso.provision_and_sync(session, claims, cfg or _cfg(), request=_req())


def test_the_first_sso_sign_in_creates_the_account_and_its_memberships(session):
    analytics = _group(session, "analytics")
    user, _ = _sso(session, _claims("alice@x.local", name="Alice", groups=["analytics", "unknown"]))
    assert _actions(session) == [audit.USER_CREATE, audit.GROUP_MEMBER_ADD]
    created, joined = _entries(session)
    # the person signing in is the actor: the change happens in their sign-in
    for entry in (created, joined):
        assert entry.actor_id == user.id and entry.actor_label == "alice@x.local"
        assert (entry.target_type, entry.target_id, entry.target_label) == ("user", user.id, "alice@x.local")
        assert entry.ip == "172.18.0.1"
    assert _detail(created) == {"via": "sso"}
    assert _detail(joined) == {"group": "analytics", "group_id": analytics.id, "via": "sso"}


def test_a_sign_in_that_changes_nothing_writes_nothing(session):
    _group(session, "analytics")
    claims = _claims("alice@x.local", name="Alice", groups=["analytics"])
    _sso(session, claims)
    before = len(_entries(session))
    _sso(session, claims)
    assert len(_entries(session)) == before


def test_groups_created_by_sso(session):
    user, _ = _sso(session, _claims("d@x.local", groups=["new-team"]), _cfg(auto_create_groups=True))
    group = session.exec(select(Group).where(Group.name == "new-team")).one()
    assert _actions(session) == [audit.USER_CREATE, audit.GROUP_CREATE, audit.GROUP_MEMBER_ADD]
    (entry,) = _entries(session, audit.GROUP_CREATE)
    assert (entry.target_type, entry.target_id, entry.target_label) == ("group", group.id, "new-team")
    assert entry.actor_id == user.id and _detail(entry) == {"via": "sso"}


def test_memberships_removed_by_an_authoritative_sync(session):
    analytics, finance = _group(session, "analytics"), _group(session, "finance")
    bob = make_user(session, email="bob@x.local")
    for g in (analytics, finance):
        session.add(UserGroupLink(user_id=bob.id, group_id=g.id))
    session.commit()
    _sso(session, _claims("bob@x.local", groups=["analytics"], email_verified=True))
    (entry,) = _entries(session)  # linked, not created; analytics was there already
    assert entry.action == audit.GROUP_MEMBER_REMOVE and entry.target_id == bob.id
    assert _detail(entry) == {"group": "finance", "group_id": finance.id, "via": "sso"}


def test_the_email_and_the_name_the_identity_provider_changes(session):
    user, _ = _sso(session, _claims("before@x.local", sub="s-1"))
    _sso(session, _claims("after@x.local", sub="s-1", name="Alice R."))
    (entry,) = _entries(session, audit.USER_UPDATE)
    assert entry.target_id == user.id and entry.target_label == "after@x.local"
    assert _detail(entry) == {"email": {"from": "before@x.local", "to": "after@x.local"},
                              "full_name": {"from": "", "to": "Alice R."}, "via": "sso"}


def test_the_administrators_group_of_the_identity_provider(session):
    c = _cfg(superuser_group="tabularia-admins")
    user, _ = _sso(session, _claims("e@x.local", groups=["tabularia-admins"]), c)
    assert _actions(session) == [audit.USER_CREATE, audit.USER_PROMOTE]
    assert _detail(_entries(session, audit.USER_PROMOTE)[0]) == {"at_creation": True, "via": "sso"}
    _sso(session, _claims("e@x.local", groups=["tabularia-admins"]), c)  # still one: nothing
    _sso(session, _claims("e@x.local", groups=[]), c)
    assert _actions(session) == [audit.USER_CREATE, audit.USER_PROMOTE, audit.USER_DEMOTE]
    (entry,) = _entries(session, audit.USER_DEMOTE)
    assert entry.target_id == user.id and _detail(entry) == {"via": "sso"}


def test_an_observer_signing_in_through_sso_leaves_no_address(session):
    """The actor is the person: an observer's address stays out of the log, as for anything
    else they do."""
    guests = _group(session, "guests", is_observer=True)
    user, _ = _sso(session, _claims("guest@x.local", groups=["guests"]), _cfg(group_allowlist="guests"))
    assert session.get(UserGroupLink, (user.id, guests.id)) is not None
    created, joined = _entries(session)
    # written once the sign-in is saved, the person an observer by then: no address on either
    assert (created.action, joined.action) == (audit.USER_CREATE, audit.GROUP_MEMBER_ADD)
    assert created.ip is None and joined.ip is None and created.user_agent is None
    assert _detail(joined)["observer"] is True


# ── the administrator created at start from the environment ──────────────────

@pytest.fixture
def seed_settings(monkeypatch, db_engine):
    holder = {}
    monkeypatch.setattr(seed, "engine", db_engine)
    monkeypatch.setattr(seed, "get_settings", lambda: holder["s"])
    holder["s"] = Settings(app={"env_name": "development"},
                           auth={"admin_email": "boss@x.it", "admin_password": PASSWORD})


def test_the_administrator_of_the_environment(seed_settings, db_engine):
    seed.seed_admin()
    seed.seed_admin()  # present already: nothing
    with Session(db_engine) as s:
        boss = s.exec(select(User).where(User.email == "boss@x.it")).one()
        entries = _entries(s)
        assert [e.action for e in entries] == [audit.USER_CREATE, audit.USER_PROMOTE]
        for e in entries:
            assert e.actor_id is None and e.actor_label == seed.STARTUP_ACTOR
            assert (e.target_id, e.target_label) == (boss.id, "boss@x.it")
            assert PASSWORD not in (e.detail or "")
        assert _detail(entries[0]) == {"via": "startup"}
        assert _detail(entries[1]) == {"at_creation": True, "via": "startup"}
