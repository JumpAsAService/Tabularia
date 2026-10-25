"""Information shown by the Admin page: dates, last sign-in, memberships.

`UserOut` and `GroupOut` expose extra fields for the admin list. The constraint not to
break: they must ALL have a default value, because `MeOut` inherits from `UserOut` and
is built by hand in routes/auth.py passing only the first five — one more required
field would fail `/auth/me`, that is the login.
"""
from datetime import datetime, timezone

from sqlmodel import select

from app.models import Group, User, UserGroupLink
from app.routes.groups import create_group, list_groups
from app.routes.users import create_user, list_users, update_user
from app.schemas.models import GroupCreate, MeOut, UserCreate, UserUpdate
from tests.conftest import make_user


def _viewer(session):
    """Who reads the list: an administrator, so without masking."""
    return make_user(session, email="capo-lettore@x.local", is_superuser=True)


def _group(session, name: str) -> Group:
    g = Group(name=name)
    session.add(g)
    session.commit()
    session.refresh(g)
    return g


def _subscribe(session, user_id: int, group_id: int) -> None:
    session.add(UserGroupLink(user_id=user_id, group_id=group_id))
    session.commit()


# ── the constraint that protects the login ──────────────────────────────────
def test_me_still_builds_with_only_the_base_fields():
    """Regression: if a new UserOut field became required, this construction — the real one
    of /auth/me — would raise."""
    me = MeOut(id=1, email="a@x.local", full_name="A", is_active=True, is_superuser=False, groups=[])
    assert me.created_at is None and me.last_seen_at is None
    assert me.sso_only is False and me.groups == []


# ── users ───────────────────────────────────────────────────────────────────
def test_list_users_exposes_dates_and_memberships(session):
    u = make_user(session, email="tizio@x.local")
    g1, g2 = _group(session, "analytics"), _group(session, "finanza")
    _subscribe(session, u.id, g1.id)
    _subscribe(session, u.id, g2.id)

    row = next(x for x in list_users(session=session, asker=_viewer(session)) if x.id == u.id)
    assert row.created_at is not None
    assert row.groups == ["analytics", "finanza"]  # sorted, not at random
    assert row.sso_only is False  # has a local password


def test_user_without_local_password_is_flagged_sso_only(session):
    sso_only = make_user(session, email="idp@x.local", hashed_password=None)
    row = next(x for x in list_users(session=session, asker=_viewer(session)) if x.id == sso_only.id)
    assert row.sso_only is True


def test_last_seen_is_reported(session):
    when = datetime(2026, 9, 13, 10, 30, tzinfo=timezone.utc)
    u = make_user(session, email="visto@x.local", last_seen_at=when)
    mai = make_user(session, email="mai@x.local")

    rows = {x.id: x for x in list_users(session=session, asker=_viewer(session))}
    assert rows[u.id].last_seen_at is not None
    assert rows[mai.id].last_seen_at is None  # never signed in


def test_created_user_starts_without_groups(session):
    boss = make_user(session, email="capo@x.local", is_superuser=True)
    out = create_user(
        body=UserCreate(email="nuovo@x.local", password="segretissima"), session=session, current=boss
    )
    assert out.groups == [] and out.created_at is not None and out.sso_only is False


def test_update_user_keeps_reporting_groups(session):
    u = make_user(session, email="mod@x.local")
    g = _group(session, "analytics")
    _subscribe(session, u.id, g.id)

    boss = make_user(session, email="capo@x.local", is_superuser=True)
    out = update_user(user_id=u.id, body=UserUpdate(full_name="Nuovo Nome"), session=session, current=boss)
    assert out.full_name == "Nuovo Nome" and out.groups == ["analytics"]


# ── groups ──────────────────────────────────────────────────────────────────
def test_list_groups_counts_members(session):
    g = _group(session, "analytics")
    empty = _group(session, "vuoto")
    for email in ("a@x.local", "b@x.local"):
        _subscribe(session, make_user(session, email=email).id, g.id)

    by_name = {x.name: x for x in list_groups(session=session)}
    assert by_name["analytics"].member_count == 2
    assert by_name["vuoto"].member_count == 0
    assert by_name["analytics"].created_at is not None


def test_new_group_has_no_members(session):
    out = create_group(body=GroupCreate(name="fresco"), session=session)
    assert out.member_count == 0
    assert session.exec(select(Group).where(Group.name == "fresco")).first() is not None
