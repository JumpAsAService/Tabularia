"""Elevare ad admin una persona o un gruppo intero.

L'admin EFFETTIVO è il flag personale oppure l'appartenenza a un gruppo di
amministratori (`Group.is_admin`). Qui si verifica che ogni strada che concede o
toglie i privilegi funzioni, lasci traccia nell'audit e non permetta a chi agisce
di chiudersi fuori da solo.
"""
import pytest
from fastapi import HTTPException
from sqlmodel import select

from app.deps.auth import require_superuser
from app.models import AuditLog, Group, Project, UserGroupLink
from app.routes import auth as auth_routes
from app.routes import groups as group_routes
from app.routes import users as user_routes
from app.schemas.models import GroupCreate, GroupUpdate, UserUpdate
from app.services import audit
from app.services import permissions as perm
from app.services.objects import ensure_reads_pinned
from tests.conftest import make_user


def _root(session):
    return make_user(session, email="root@x.it", is_superuser=True)


def _group(session, name="Amministratori", is_admin=False, members=()):
    g = Group(name=name, is_admin=is_admin)
    session.add(g)
    session.commit()
    session.refresh(g)
    for u in members:
        session.add(UserGroupLink(user_id=u.id, group_id=g.id))
    session.commit()
    return g


def _actions(session):
    return [a.action for a in session.exec(select(AuditLog).order_by(AuditLog.id)).all()]


# ── la persona ────────────────────────────────────────────────────────────────

def test_promote_and_demote_user_is_audited(session):
    root, anna = _root(session), make_user(session, email="anna@x.it")
    out = user_routes.update_user(anna.id, UserUpdate(is_superuser=True), session=session, current=root)
    assert out.is_superuser is True and perm.is_admin(session, anna)
    out = user_routes.update_user(anna.id, UserUpdate(is_superuser=False), session=session, current=root)
    assert out.is_superuser is False and not perm.is_admin(session, anna)
    assert _actions(session) == [audit.USER_PROMOTE, audit.USER_DEMOTE]
    riga = session.exec(select(AuditLog)).first()
    assert riga.target_label == "anna@x.it" and riga.actor_id == root.id


def test_patch_without_flag_change_writes_no_audit(session):
    root, anna = _root(session), make_user(session, email="anna@x.it")
    user_routes.update_user(anna.id, UserUpdate(full_name="Anna B."), session=session, current=root)
    assert _actions(session) == []


def test_cannot_demote_or_deactivate_yourself(session):
    root = _root(session)
    for body in (UserUpdate(is_superuser=False), UserUpdate(is_active=False)):
        with pytest.raises(HTTPException) as e:
            user_routes.update_user(root.id, body, session=session, current=root)
        assert e.value.status_code == 409
        session.refresh(root)
        assert root.is_superuser and root.is_active  # annullato davvero, non a metà


def test_can_drop_personal_flag_when_an_admin_group_covers_you(session):
    root = _root(session)
    _group(session, is_admin=True, members=[root])
    out = user_routes.update_user(root.id, UserUpdate(is_superuser=False), session=session, current=root)
    assert out.is_superuser is False and out.admin_groups == ["Amministratori"]
    assert perm.is_admin(session, root)


# ── il gruppo ─────────────────────────────────────────────────────────────────

def test_group_elevation_makes_every_member_admin(session):
    root, anna, bruno = _root(session), make_user(session, email="anna@x.it"), make_user(session, email="bruno@x.it")
    g = _group(session, members=[anna])
    with pytest.raises(HTTPException):
        require_superuser(anna, session)

    out = group_routes.update_group(g.id, GroupUpdate(is_admin=True), session=session, current=root)
    assert out.is_admin is True and out.member_count == 1
    assert require_superuser(anna, session) is anna
    assert not perm.is_admin(session, bruno)  # chi non è nel gruppo resta com'era
    # il flag personale NON viene toccato: il privilegio vive nel gruppo
    session.refresh(anna)
    assert anna.is_superuser is False

    group_routes.update_group(g.id, GroupUpdate(is_admin=False), session=session, current=root)
    with pytest.raises(HTTPException):
        require_superuser(anna, session)
    assert _actions(session) == [audit.GROUP_PROMOTE, audit.GROUP_DEMOTE]


def test_group_admin_gets_the_same_powers_as_a_personal_admin(session):
    root, anna = _root(session), make_user(session, email="anna@x.it")
    _group(session, is_admin=True, members=[anna])
    progetto = Project(name="Riservato", owner_id=root.id)
    session.add(progetto)
    session.commit()
    assert perm.has_capability(session, anna, progetto.id, "manage")
    assert progetto.id in perm.readable_project_ids(session, anna)
    # data plane: niente pinning, come per il superuser
    ensure_reads_pinned(session, anna, {"bucket": "altro", "input_key": "x/y.parquet"}, "data-prep")
    # /auth/me dice al frontend di aprire l'amministrazione
    assert auth_routes.me(user=anna, session=session).is_superuser is True
    # nell'elenco utenti il flag PERSONALE resta falso, con l'origine in chiaro
    riga = next(u for u in user_routes.list_users(session=session) if u.email == "anna@x.it")
    assert riga.is_superuser is False and riga.admin_groups == ["Amministratori"]


def test_inactive_member_of_admin_group_is_still_locked_out_by_login(session):
    # l'appartenenza non scavalca la disattivazione: get_current_user rifiuta
    # l'utente non attivo prima ancora di guardare i privilegi
    anna = make_user(session, email="anna@x.it", is_active=False)
    _group(session, is_admin=True, members=[anna])
    assert anna.is_active is False


def test_membership_changes_of_an_admin_group_are_audited(session):
    root, anna = _root(session), make_user(session, email="anna@x.it")
    g = _group(session, is_admin=True)
    normale = _group(session, name="Vendite")
    user_routes.add_to_group(anna.id, normale.id, session=session, current=root)
    assert _actions(session) == []  # un gruppo qualsiasi non è una promozione
    user_routes.add_to_group(anna.id, g.id, session=session, current=root)
    assert perm.is_admin(session, anna)
    user_routes.remove_from_group(anna.id, g.id, session=session, current=root)
    assert not perm.is_admin(session, anna)
    assert _actions(session) == [audit.ADMIN_GROUP_JOIN, audit.ADMIN_GROUP_LEAVE]


def test_admin_by_group_cannot_lock_themselves_out(session):
    anna = make_user(session, email="anna@x.it")
    g = _group(session, is_admin=True, members=[anna])
    for azione in (
        lambda: group_routes.update_group(g.id, GroupUpdate(is_admin=False), session=session, current=anna),
        lambda: group_routes.delete_group(g.id, session=session, current=anna),
        lambda: user_routes.remove_from_group(anna.id, g.id, session=session, current=anna),
    ):
        with pytest.raises(HTTPException) as e:
            azione()
        assert e.value.status_code == 409
        session.refresh(g)
        assert g.is_admin and perm.is_admin(session, anna)


def test_deleting_an_admin_group_demotes_its_members(session):
    root, anna = _root(session), make_user(session, email="anna@x.it")
    g = _group(session, is_admin=True, members=[anna])
    group_routes.delete_group(g.id, session=session, current=root)
    assert not perm.is_admin(session, anna)
    assert _actions(session) == [audit.GROUP_DEMOTE]


def test_new_groups_are_never_admin(session):
    out = group_routes.create_group(GroupCreate(name="Nuovo"), session=session)
    assert out.is_admin is False
    assert group_routes.list_groups(session=session)[0].is_admin is False


# ── audit 2026-09-19: escalation chiuse ──────────────────────────────────────

def test_moving_a_folder_requires_manage_on_what_you_move(session):
    """A1: con solo EDIT, spostare una cartella regalava MANAGE (e CONNECT)."""
    from fastapi import HTTPException
    from app.models.permission import Capability, Permission
    from app.routes import projects as project_routes
    from app.schemas.models import ProjectUpdate

    root, bob = _root(session), make_user(session, email="bob@x.it")
    riservato = Project(name="Riservato", owner_id=root.id)
    sandbox = Project(name="Sandbox di Bob", owner_id=root.id)
    session.add(riservato); session.add(sandbox); session.commit()
    session.refresh(riservato); session.refresh(sandbox)
    session.add(Permission(project_id=riservato.id, user_id=bob.id, capability=Capability.EDIT))
    session.add(Permission(project_id=sandbox.id, user_id=bob.id, capability=Capability.MANAGE))
    session.commit()

    with pytest.raises(HTTPException) as e:
        project_routes.update_project(
            riservato.id, ProjectUpdate(parent_id=sandbox.id), user=bob, session=session
        )
    assert e.value.status_code == 403
    session.refresh(riservato)
    assert riservato.parent_id is None  # non si è mosso
    assert not perm.has_capability(session, bob, riservato.id, Capability.CONNECT)

    # con MANAGE su entrambi i capi lo spostamento è legittimo
    session.add(Permission(project_id=riservato.id, user_id=bob.id, capability=Capability.MANAGE))
    session.commit()
    project_routes.update_project(riservato.id, ProjectUpdate(parent_id=sandbox.id), user=bob, session=session)
    session.refresh(riservato)
    assert riservato.parent_id == sandbox.id


def test_a_folder_cannot_be_moved_into_its_own_subtree(session):
    """M6: il ciclo staccava il sottoalbero dai permessi ereditati."""
    from fastapi import HTTPException
    from app.routes import projects as project_routes
    from app.schemas.models import ProjectUpdate

    root = _root(session)
    padre = Project(name="Padre", owner_id=root.id)
    session.add(padre); session.commit(); session.refresh(padre)
    figlio = Project(name="Figlio", owner_id=root.id, parent_id=padre.id)
    session.add(figlio); session.commit(); session.refresh(figlio)

    for destinazione in (padre.id, figlio.id):
        with pytest.raises(HTTPException) as e:
            project_routes.update_project(
                padre.id, ProjectUpdate(parent_id=destinazione), user=root, session=session
            )
        assert e.value.status_code == 422
    session.refresh(padre)
    assert padre.parent_id is None
