"""Informazioni mostrate dalla pagina Admin: date, ultimo accesso, appartenenze.

`UserOut` e `GroupOut` espongono campi in più per l'elenco admin. Il vincolo da
non rompere: devono avere TUTTI un valore predefinito, perché `MeOut` eredita da
`UserOut` ed è costruito a mano in routes/auth.py passando solo i primi cinque —
un campo obbligatorio in più farebbe fallire `/auth/me`, cioè il login.
"""
from datetime import datetime, timezone

from sqlmodel import select

from app.models import Group, User, UserGroupLink
from app.routes.groups import create_group, list_groups
from app.routes.users import create_user, list_users, update_user
from app.schemas.models import GroupCreate, MeOut, UserCreate, UserUpdate
from tests.conftest import make_user


def _gruppo(session, nome: str) -> Group:
    g = Group(name=nome)
    session.add(g)
    session.commit()
    session.refresh(g)
    return g


def _iscrivi(session, user_id: int, group_id: int) -> None:
    session.add(UserGroupLink(user_id=user_id, group_id=group_id))
    session.commit()


# ── il vincolo che protegge il login ────────────────────────────────────────
def test_me_still_builds_with_only_the_base_fields():
    """Regressione: se un campo nuovo di UserOut diventasse obbligatorio, questa
    costruzione — quella reale di /auth/me — solleverebbe."""
    me = MeOut(id=1, email="a@x.local", full_name="A", is_active=True, is_superuser=False, groups=[])
    assert me.created_at is None and me.last_seen_at is None
    assert me.sso_only is False and me.groups == []


# ── utenti ──────────────────────────────────────────────────────────────────
def test_list_users_exposes_dates_and_memberships(session):
    u = make_user(session, email="tizio@x.local")
    g1, g2 = _gruppo(session, "analytics"), _gruppo(session, "finanza")
    _iscrivi(session, u.id, g1.id)
    _iscrivi(session, u.id, g2.id)

    riga = next(x for x in list_users(session=session) if x.id == u.id)
    assert riga.created_at is not None
    assert riga.groups == ["analytics", "finanza"]  # ordinati, non a caso
    assert riga.sso_only is False  # ha una password locale


def test_user_without_local_password_is_flagged_sso_only(session):
    solo_sso = make_user(session, email="idp@x.local", hashed_password=None)
    riga = next(x for x in list_users(session=session) if x.id == solo_sso.id)
    assert riga.sso_only is True


def test_last_seen_is_reported(session):
    quando = datetime(2026, 9, 13, 10, 30, tzinfo=timezone.utc)
    u = make_user(session, email="visto@x.local", last_seen_at=quando)
    mai = make_user(session, email="mai@x.local")

    righe = {x.id: x for x in list_users(session=session)}
    assert righe[u.id].last_seen_at is not None
    assert righe[mai.id].last_seen_at is None  # non è mai entrato


def test_created_user_starts_without_groups(session):
    capo = make_user(session, email="capo@x.local", is_superuser=True)
    out = create_user(
        body=UserCreate(email="nuovo@x.local", password="segretissima"), session=session, current=capo
    )
    assert out.groups == [] and out.created_at is not None and out.sso_only is False


def test_update_user_keeps_reporting_groups(session):
    u = make_user(session, email="mod@x.local")
    g = _gruppo(session, "analytics")
    _iscrivi(session, u.id, g.id)

    capo = make_user(session, email="capo@x.local", is_superuser=True)
    out = update_user(user_id=u.id, body=UserUpdate(full_name="Nuovo Nome"), session=session, current=capo)
    assert out.full_name == "Nuovo Nome" and out.groups == ["analytics"]


# ── gruppi ──────────────────────────────────────────────────────────────────
def test_list_groups_counts_members(session):
    g = _gruppo(session, "analytics")
    vuoto = _gruppo(session, "vuoto")
    for email in ("a@x.local", "b@x.local"):
        _iscrivi(session, make_user(session, email=email).id, g.id)

    per_nome = {x.name: x for x in list_groups(session=session)}
    assert per_nome["analytics"].member_count == 2
    assert per_nome["vuoto"].member_count == 0
    assert per_nome["analytics"].created_at is not None


def test_new_group_has_no_members(session):
    out = create_group(body=GroupCreate(name="fresco"), session=session)
    assert out.member_count == 0
    assert session.exec(select(Group).where(Group.name == "fresco")).first() is not None
