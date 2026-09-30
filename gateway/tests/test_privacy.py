"""L'informativa sulla privacy: una sola, la legge chiunque, la scrive l'admin."""
import pytest
from fastapi import HTTPException
from sqlmodel import select

from app.models import AuditLog, PrivacyNotice
from app.routes import privacy as privacy_routes
from app.routes.privacy import PrivacyUpdate
from app.services import audit as audit_svc
from tests.conftest import make_user


def test_the_first_read_creates_it_with_a_text_that_is_actually_true(session):
    """Il testo di partenza non è un modulo da riempire: dice quello che il
    prodotto raccoglie davvero, ed è verificabile leggendo il codice."""
    tizio = make_user(session, email="tizio@x.it")
    fuori = privacy_routes.get_privacy(user=tizio, session=session)

    assert fuori.enabled is True and fuori.summary
    # le tre rinunce di cui il prodotto è responsabile
    assert "We do not record your IP address" in fuori.body
    assert "We do not record your browser" in fuori.body
    assert "questions to the assistant are not saved" in fuori.body
    # e le due cose scomode, dette lo stesso
    assert "sent to the model provider" in fuori.body
    assert "no automatic expiry" in fuori.body


def test_it_is_one_row_not_a_list(session):
    tizio = make_user(session, email="tizio2@x.it")
    privacy_routes.get_privacy(user=tizio, session=session)
    privacy_routes.get_privacy(user=tizio, session=session)
    assert len(session.exec(select(PrivacyNotice)).all()) == 1


def test_an_administrator_rewrites_it_and_it_leaves_a_trace(session):
    """Cambiare l'informativa cambia ciò che l'installazione DICHIARA a tutti:
    è una modifica che deve restare a registro come le altre."""
    capo = make_user(session, email="capo@x.it", is_superuser=True)
    fuori = privacy_routes.update_privacy(
        PrivacyUpdate(summary="La nostra informativa", url="https://esempio.it/privacy"),
        current=capo, session=session,
    )
    assert fuori.summary == "La nostra informativa"
    assert fuori.url == "https://esempio.it/privacy"
    assert fuori.body  # non tocco ciò che non mi passano

    azioni = [a.action for a in session.exec(select(AuditLog)).all()]
    assert audit_svc.PRIVACY_UPDATE in azioni


def test_it_can_be_switched_off(session):
    capo = make_user(session, email="capo2@x.it", is_superuser=True)
    privacy_routes.update_privacy(PrivacyUpdate(enabled=False), current=capo, session=session)
    tizio = make_user(session, email="tizio3@x.it")
    assert privacy_routes.get_privacy(user=tizio, session=session).enabled is False


def test_an_observer_reads_it_but_cannot_rewrite_it(session):
    """La deve VEDERE — parla di lui — ma non è lui a deciderne il testo."""
    from app.deps.auth import require_superuser

    ospite = make_user(session, email="ospite@x.it")
    ospite.is_observer = True
    session.add(ospite)
    session.commit()

    assert privacy_routes.get_privacy(user=ospite, session=session).enabled is True
    with pytest.raises(HTTPException) as e:
        require_superuser(ospite, session)
    assert e.value.status_code == 403
