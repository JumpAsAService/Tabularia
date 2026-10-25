"""Deleting a user: what disappears with them and what MUST survive.

The case that started this file: deleting a user answered **500** on Postgres.
`audit_logs.actor_id` is a foreign key towards `users`, the deletion path did not reset
it, and since `auth.login` is audited having signed in once was enough to be no longer
deletable — that is any real user. The tests did not show it because SQLite has
foreign keys off.

Here the violation cannot be reproduced (same SQLite, same FKs off), and chasing it
would be the wrong test: the right one checks the BEHAVIOUR that makes the deletion
possible, that is the audit surviving the person losing only the reference. If someone
removed that line tomorrow, this test would fail on any database.
"""
import pytest
from fastapi import HTTPException
from sqlmodel import select

from app.models import AuditLog, Permission, User, UserGroupLink
from app.models.permission import Capability
from app.routes.users import delete_user
from app.services import audit
from tests.conftest import make_permission, make_project, make_user


def _admin(session):
    return make_user(session, email="admin@x.local", is_superuser=True)


def test_the_audit_survives_the_person(session):
    """Deleting an account must not delete the evidence of what it did."""
    admin = _admin(session)
    someone = make_user(session, email="tizio@x.local")
    audit.record_audit(session, actor=someone, action=audit.LOGIN)
    audit.record_audit(session, actor=someone, action=audit.EXPORT_DOWNLOAD, target_label="clienti.csv")

    delete_user(someone.id, session=session, current=admin)

    # the deletion itself is now an entry of its own, by the administrator
    (deletion,) = session.exec(select(AuditLog).where(AuditLog.action == audit.USER_DELETE)).all()
    assert deletion.actor_id == admin.id and deletion.target_label == "tizio@x.local"
    rows = session.exec(select(AuditLog).where(AuditLog.action != audit.USER_DELETE)).all()
    assert len(rows) == 2, "le righe di audit non devono sparire con l'utente"
    for r in rows:
        assert r.actor_id is None                  # the reference is reset…
        assert r.actor_label == "tizio@x.local"    # …but who it was is still known
    assert {r.action for r in rows} == {audit.LOGIN, audit.EXPORT_DOWNLOAD}


def test_the_audit_of_other_people_is_untouched(session):
    admin = _admin(session)
    someone = make_user(session, email="tizio@x.local")
    caio = make_user(session, email="caio@x.local")
    audit.record_audit(session, actor=someone, action=audit.LOGIN)
    audit.record_audit(session, actor=caio, action=audit.LOGIN)

    delete_user(someone.id, session=session, current=admin)

    by_actor = {r.actor_label: r.actor_id for r in session.exec(select(AuditLog)).all()}
    assert by_actor["tizio@x.local"] is None
    assert by_actor["caio@x.local"] == caio.id  # the other was not touched


def test_permissions_and_memberships_go_with_the_user(session):
    """The surroundings that worked already, nailed down: they are ACCESS, not history, and
    must really disappear."""
    admin = _admin(session)
    someone = make_user(session, email="tizio@x.local")
    p = make_project(session, name="cartella")
    make_permission(session, user_id=someone.id, project_id=p.id, capability=Capability.VIEW.value)

    delete_user(someone.id, session=session, current=admin)

    assert session.exec(select(Permission).where(Permission.user_id == someone.id)).all() == []
    assert session.exec(select(UserGroupLink).where(UserGroupLink.user_id == someone.id)).all() == []
    assert session.get(User, someone.id) is None


def test_you_cannot_delete_yourself(session):
    admin = _admin(session)
    with pytest.raises(HTTPException) as e:
        delete_user(admin.id, session=session, current=admin)
    assert e.value.status_code == 409


def test_deleting_someone_who_is_not_there_is_404(session):
    with pytest.raises(HTTPException) as e:
        delete_user(9999, session=session, current=_admin(session))
    assert e.value.status_code == 404
