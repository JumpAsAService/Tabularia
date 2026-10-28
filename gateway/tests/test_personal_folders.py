"""Every person's own folder (services/personal_folders.py): born with the account, seen
by the person and the administrators only, never lost by mistake."""
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.core.config import OidcSettings, Settings
from app.core.security import hash_password
from app.db import seed
from app.db.session import get_session
from app.models import AuditLog, Group, Permission, Project, User, UserGroupLink
from app.routes import auth as auth_routes
from app.routes import permissions as perm_routes
from app.routes import projects as project_routes
from app.routes import users as user_routes
from app.schemas.models import PermissionCreate, ProjectUpdate, UserCreate
from app.services import audit, personal_folders, sso
from app.services import permissions as perm
from tests.conftest import make_permission, make_project, make_user

PASSWORD = "a-password-of-my-own"


def _boss(session):
    return make_user(session, email="boss@example.com", is_superuser=True)


def _create(session, boss, email, **kw):
    out = user_routes.create_user(UserCreate(email=email, password=PASSWORD, **kw),
                                  request=None, session=session, current=boss)
    return session.get(User, out.id)


def _folder_of(session, user) -> Project | None:
    pid = personal_folders.personal_folder_id(session, user.id)
    return session.get(Project, pid) if pid else None


def _listed(session, user) -> dict[int, object]:
    return {p.id: p for p in project_routes.list_projects(user=user, session=session)}


# ── born with the account ────────────────────────────────────────────────────
def test_a_new_account_gets_a_folder_named_after_its_email(session):
    boss = _boss(session)
    leo = _create(session, boss, "leonardtriv@esempio.it")
    folder = _folder_of(session, leo)
    assert folder is not None and folder.name == "leonardtriv"
    assert folder.parent_id is None and folder.owner_id == leo.id
    # MANAGE: every capability, CONNECT included
    for cap in ("view", "run", "edit", "connect", "manage"):
        assert perm.has_capability(session, leo, folder.id, cap)
    # in the audit as any folder, by who created the account, and marked personal
    row = session.exec(select(AuditLog).where(AuditLog.action == audit.FOLDER_CREATE)).one()
    assert row.actor_id == boss.id and row.target_id == folder.id and row.target_label == "leonardtriv"
    assert '"personal": true' in row.detail and f'"user_id": {leo.id}' in row.detail


def test_a_taken_name_gets_a_number_as_copies_do(session):
    boss = _boss(session)
    make_project(session, name="anna")                                   # a folder of the company
    sub = make_project(session, name="anna (1)", parent_id=1)            # not at the top: no clash
    first = _create(session, boss, "anna@one.example")
    second = _create(session, boss, "anna@two.example")
    assert _folder_of(session, first).name == "anna (1)"
    assert _folder_of(session, second).name == "anna (2)"
    assert sub.name == "anna (1)"


def test_nobody_else_sees_it_and_the_administrators_do(session):
    boss = _boss(session)
    leo = _create(session, boss, "leo@example.com")
    other = _create(session, boss, "other@example.com")
    mine, theirs = _folder_of(session, leo), _folder_of(session, other)
    assert set(_listed(session, leo)) == {mine.id}
    assert set(_listed(session, other)) == {theirs.id}
    assert mine.id not in perm.readable_project_ids(session, other)
    listed = _listed(session, boss)
    assert {mine.id, theirs.id} <= set(listed)
    # the list says whose personal folder each is
    assert listed[mine.id].personal_of == leo.id and listed[theirs.id].personal_of == other.id
    assert _listed(session, leo)[mine.id].personal_of == leo.id


def test_it_is_shared_as_any_folder(session):
    boss = _boss(session)
    leo, other = _create(session, boss, "leo@example.com"), _create(session, boss, "other@example.com")
    mine = _folder_of(session, leo)
    perm_routes.grant_permission(mine.id, PermissionCreate(user_id=other.id, capability="view"),
                                 request=None, user=leo, session=session)
    seen = _listed(session, other)
    assert mine.id in seen and seen[mine.id].personal_of == leo.id   # still Leo's, not "mine" for the other


def test_service_and_observer_accounts_get_none(session):
    boss = _boss(session)
    guest = _create(session, boss, "guest@example.com", is_observer=True)
    assert _folder_of(session, guest) is None
    robot = make_user(session, email="robot@service", is_service=True, hashed_password=None)
    assert personal_folders.ensure(session, robot) is None
    # an administrator who is also an observer is an administrator: a folder
    both = _create(session, boss, "both@example.com", is_observer=True, is_superuser=True)
    assert _folder_of(session, both) is not None


def test_an_account_is_never_given_two(session):
    boss = _boss(session)
    leo = _create(session, boss, "leo@example.com")
    first = _folder_of(session, leo).id
    assert personal_folders.ensure(session, leo, via="sign-in") == first
    project_routes.update_project(first, ProjectUpdate(name="My stuff"), user=leo, session=session)
    assert personal_folders.ensure(session, leo, via="sign-in") == first   # renamed, still the one
    assert len(session.exec(select(Project).where(Project.owner_id == leo.id)).all()) == 1


# ── what its person cannot do to it by mistake ───────────────────────────────
def test_its_person_cannot_delete_move_or_leave_it(session):
    boss = _boss(session)
    leo = _create(session, boss, "leo@example.com")
    mine = _folder_of(session, leo)
    elsewhere = make_project(session, name="Elsewhere")
    make_permission(session, user_id=leo.id, project_id=elsewhere.id, capability="manage")
    own_grant = session.exec(select(Permission).where(Permission.project_id == mine.id)).one()
    for attempt in (
        lambda: project_routes.delete_project(mine.id, user=leo, session=session),
        lambda: project_routes.update_project(mine.id, ProjectUpdate(parent_id=elsewhere.id), user=leo,
                                              session=session),
        lambda: perm_routes.revoke_permission(own_grant.id, request=None, user=leo, session=session),
    ):
        with pytest.raises(HTTPException) as e:
            attempt()
        assert e.value.status_code == 409 and "personal folder" in e.value.detail
    assert _folder_of(session, leo).id == mine.id and mine.parent_id is None


def test_who_it_was_shared_with_cannot_take_it_away_either(session):
    boss = _boss(session)
    leo, other = _create(session, boss, "leo@example.com"), _create(session, boss, "other@example.com")
    mine = _folder_of(session, leo)
    make_permission(session, user_id=other.id, project_id=mine.id, capability="manage")
    own_grant = session.exec(select(Permission).where(Permission.project_id == mine.id,
                                                      Permission.user_id == leo.id)).one()
    with pytest.raises(HTTPException):
        perm_routes.revoke_permission(own_grant.id, request=None, user=other, session=session)
    with pytest.raises(HTTPException):
        project_routes.delete_project(mine.id, user=other, session=session)
    # the other grants on it come and go as usual
    theirs = session.exec(select(Permission).where(Permission.project_id == mine.id,
                                                   Permission.user_id == other.id)).one()
    perm_routes.revoke_permission(theirs.id, request=None, user=leo, session=session)


def test_an_administrator_can_and_the_next_sign_in_brings_a_new_one(session):
    boss = _boss(session)
    leo = _create(session, boss, "leo@example.com")
    mine = _folder_of(session, leo)
    project_routes.delete_project(mine.id, user=boss, session=session)
    assert _folder_of(session, leo) is None
    assert personal_folders.ensure(session, leo, actor=leo, via="sign-in") is not None
    assert _folder_of(session, leo).name == "leo"


def test_the_folder_outlives_the_account_as_an_ordinary_one(session):
    boss = _boss(session)
    leo, other = _create(session, boss, "leo@example.com"), _create(session, boss, "other@example.com")
    mine = _folder_of(session, leo).id
    user_routes.delete_user(leo.id, session=session, current=boss)
    folder = session.get(Project, mine)
    assert folder is not None and folder.owner_id is None and folder.name == "leo"
    assert _listed(session, boss)[mine].personal_of is None
    assert mine not in _listed(session, other)
    project_routes.delete_project(mine, user=boss, session=session)   # an ordinary, empty folder


def test_a_deactivated_account_keeps_it(session):
    boss = _boss(session)
    leo = _create(session, boss, "leo@example.com")
    mine = _folder_of(session, leo).id
    from app.schemas.models import UserUpdate

    user_routes.update_user(leo.id, UserUpdate(is_active=False), request=None, session=session, current=boss)
    assert personal_folders.personal_folder_id(session, leo.id) == mine
    assert _listed(session, boss)[mine].personal_of == leo.id


# ── the other ways an account is born, and the accounts from before ─────────
def _sso_cfg(**kw) -> OidcSettings:
    return OidcSettings(issuer="https://idp.example/realms/t", client_id="tabularia-gateway", **kw)


def _claims(email: str, **kw) -> dict:
    return {"iss": "https://idp.example/realms/t", "sub": f"sub-of-{email}", "email": email, **kw}


def test_sso_creates_it_at_the_first_sign_in_and_only_then(session):
    user, detail = sso.provision_and_sync(session, _claims("maria.rossi@azienda.it"), _sso_cfg())
    assert detail["created"]
    folder = _folder_of(session, user)
    assert folder.name == "maria.rossi"
    row = session.exec(select(AuditLog).where(AuditLog.action == audit.FOLDER_CREATE)).one()
    assert row.actor_id == user.id and '"via": "sso"' in row.detail
    again, _ = sso.provision_and_sync(session, _claims("maria.rossi@azienda.it"), _sso_cfg())
    assert again.id == user.id and _folder_of(session, again).id == folder.id
    assert len(session.exec(select(Project)).all()) == 1


def test_sso_gives_none_to_who_only_observes_through_a_group(session):
    session.add(Group(name="guests", is_observer=True))
    session.commit()
    user, _ = sso.provision_and_sync(session, _claims("ospite@azienda.it", groups=["guests"]),
                                     _sso_cfg(group_allowlist="guests"))
    assert perm.is_observer_only(session, user) and _folder_of(session, user) is None


def test_sso_gives_one_to_an_account_from_before(session):
    old = make_user(session, email="old@azienda.it", oidc_issuer="https://idp.example/realms/t",
                    oidc_subject="sub-of-old@azienda.it", hashed_password=None)
    sso.provision_and_sync(session, _claims("old@azienda.it"), _sso_cfg())
    assert _folder_of(session, old).name == "old"


@pytest.fixture
def client(session):
    app = FastAPI()
    app.include_router(auth_routes.router)
    app.dependency_overrides[get_session] = lambda: session
    return TestClient(app)


def test_a_password_sign_in_gives_one_to_an_account_from_before(session, client):
    old = make_user(session, email="old@example.com", hashed_password=hash_password(PASSWORD)).id
    for _ in range(2):
        r = client.post("/auth/login", json={"email": "old@example.com", "password": PASSWORD})
        assert r.status_code == 200, r.text
    assert _folder_of(session, session.get(User, old)).name == "old"
    assert len(session.exec(select(Project)).all()) == 1
    row = session.exec(select(AuditLog).where(AuditLog.action == audit.FOLDER_CREATE)).one()
    assert '"via": "sign-in"' in row.detail


def test_a_failed_sign_in_creates_nothing(session, client):
    make_user(session, email="old@example.com", hashed_password=hash_password(PASSWORD))
    r = client.post("/auth/login", json={"email": "old@example.com", "password": "not-the-password"})
    assert r.status_code == 401
    assert session.exec(select(Project)).first() is None


def test_the_administrator_of_the_start_up_gets_one(monkeypatch, db_engine):
    monkeypatch.setattr(seed, "engine", db_engine)
    monkeypatch.setattr(seed, "get_settings", lambda: Settings(
        app={"env_name": "development"},
        auth={"admin_email": "admin@tabularia.example", "admin_password": "a-long-password-of-mine-2026"}))
    seed.seed_admin()
    with Session(db_engine) as s:
        admin = s.exec(select(User).where(User.email == "admin@tabularia.example")).one()
        assert _folder_of(s, admin).name == "admin"
        row = s.exec(select(AuditLog).where(AuditLog.action == audit.FOLDER_CREATE)).one()
        assert row.actor_label == seed.STARTUP_ACTOR and '"via": "startup"' in row.detail


def test_a_failure_never_stops_the_account(session, monkeypatch):
    boss = _boss(session)

    def broken(*_a, **_k):
        raise RuntimeError("the database said no")

    monkeypatch.setattr(personal_folders, "_free_root_name", broken)
    leo = _create(session, boss, "leo@example.com")       # created all the same
    assert leo.id and _folder_of(session, leo) is None
    monkeypatch.undo()
    assert personal_folders.ensure(session, leo, via="sign-in") is not None   # the next sign-in


def test_the_name_is_the_part_before_the_at():
    assert personal_folders.folder_name("leonardtriv@esempio.it") == "leonardtriv"
    assert personal_folders.folder_name("no-at-sign") == "no-at-sign"
    assert personal_folders.folder_name("@weird.example") == "@weird.example"
