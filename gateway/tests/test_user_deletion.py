"""Cancellazione di un utente: cosa sparisce con lui e cosa DEVE sopravvivere.

Il caso che ha originato questo file: eliminare un utente rispondeva **500** su
Postgres. `audit_logs.actor_id` è una foreign key verso `users`, il percorso di
cancellazione non la azzerava, e siccome `auth.login` è auditato bastava aver
fatto accesso una volta per non essere più eliminabili — cioè qualunque utente
vero. Nei test non si vedeva perché SQLite ha le foreign key spente.

Qui non si può riprodurre la violazione (stesso SQLite, stesse FK spente), e
inseguirla sarebbe il test sbagliato: quello giusto verifica il COMPORTAMENTO
che rende la cancellazione possibile, cioè che l'audit sopravviva alla persona
perdendo solo il riferimento. Se qualcuno domani togliesse quella riga, questo
test cadrebbe su qualsiasi database.
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
    """Cancellare un account non deve cancellare le prove di ciò che ha fatto."""
    admin = _admin(session)
    tizio = make_user(session, email="tizio@x.local")
    audit.record_audit(session, actor=tizio, action=audit.LOGIN)
    audit.record_audit(session, actor=tizio, action=audit.EXPORT_DOWNLOAD, target_label="clienti.csv")

    delete_user(tizio.id, session=session, current=admin)

    righe = session.exec(select(AuditLog)).all()
    assert len(righe) == 2, "le righe di audit non devono sparire con l'utente"
    for r in righe:
        assert r.actor_id is None                  # il riferimento si azzera…
        assert r.actor_label == "tizio@x.local"    # …ma si sa ancora chi era
    assert {r.action for r in righe} == {audit.LOGIN, audit.EXPORT_DOWNLOAD}


def test_the_audit_of_other_people_is_untouched(session):
    admin = _admin(session)
    tizio = make_user(session, email="tizio@x.local")
    caio = make_user(session, email="caio@x.local")
    audit.record_audit(session, actor=tizio, action=audit.LOGIN)
    audit.record_audit(session, actor=caio, action=audit.LOGIN)

    delete_user(tizio.id, session=session, current=admin)

    per_attore = {r.actor_label: r.actor_id for r in session.exec(select(AuditLog)).all()}
    assert per_attore["tizio@x.local"] is None
    assert per_attore["caio@x.local"] == caio.id  # l'altro non è stato toccato


def test_permissions_and_memberships_go_with_the_user(session):
    """Il contorno che già funzionava, inchiodato: sono ACCESSI, non storia, e
    devono sparire davvero."""
    admin = _admin(session)
    tizio = make_user(session, email="tizio@x.local")
    p = make_project(session, name="cartella")
    make_permission(session, user_id=tizio.id, project_id=p.id, capability=Capability.VIEW.value)

    delete_user(tizio.id, session=session, current=admin)

    assert session.exec(select(Permission).where(Permission.user_id == tizio.id)).all() == []
    assert session.exec(select(UserGroupLink).where(UserGroupLink.user_id == tizio.id)).all() == []
    assert session.get(User, tizio.id) is None


def test_you_cannot_delete_yourself(session):
    admin = _admin(session)
    with pytest.raises(HTTPException) as e:
        delete_user(admin.id, session=session, current=admin)
    assert e.value.status_code == 409


def test_deleting_someone_who_is_not_there_is_404(session):
    with pytest.raises(HTTPException) as e:
        delete_user(9999, session=session, current=_admin(session))
    assert e.value.status_code == 404
