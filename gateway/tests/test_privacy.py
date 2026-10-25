"""The privacy notice: only one, anyone reads it, the admin writes it."""
import pytest
from fastapi import HTTPException
from sqlmodel import select

from app.models import AuditLog, PrivacyNotice
from app.routes import privacy as privacy_routes
from app.routes.privacy import PrivacyUpdate
from app.services import audit as audit_svc
from tests.conftest import make_user


def test_it_is_readable_without_signing_in(session):
    """It is read BEFORE signing in: asking to authenticate to know what is collected would
    mean informing someone after taking their data."""
    import inspect

    parameters = inspect.signature(privacy_routes.get_privacy).parameters
    assert "user" not in parameters and "current" not in parameters
    assert privacy_routes.get_privacy(session=session).enabled is True


def test_the_first_read_creates_it_with_a_text_that_is_actually_true(session):
    """The starting text is not a form to fill: it says what the product really collects, and
    it can be checked by reading the code."""
    outside = privacy_routes.get_privacy(session=session)

    assert outside.enabled is True and outside.summary
    # the three renunciations the product is responsible for
    assert "We do not record your IP address" in outside.body
    assert "We do not record your browser" in outside.body
    assert "questions to the assistant are not saved" in outside.body
    # and the two awkward things, said all the same
    assert "sent to the model provider" in outside.body
    # the retention is not a number copied by hand: it is read from the configuration
    assert "Automatic deletion of audit entries:" in outside.body
    assert "{audit_retention}" in outside.body_template


def test_it_is_one_row_not_a_list(session):
    privacy_routes.get_privacy(session=session)
    privacy_routes.get_privacy(session=session)
    assert len(session.exec(select(PrivacyNotice)).all()) == 1


def test_an_administrator_rewrites_it_and_it_leaves_a_trace(session):
    """Changing the notice changes what the installation DECLARES to everyone: it is a change
    that must stay on record like the others."""
    boss = make_user(session, email="capo@x.it", is_superuser=True)
    outside = privacy_routes.update_privacy(
        PrivacyUpdate(summary="La nostra informativa", url="https://esempio.it/privacy"),
        current=boss, session=session,
    )
    assert outside.summary == "La nostra informativa"
    assert outside.url == "https://esempio.it/privacy"
    assert outside.body  # I do not touch what I am not passed

    actions = [a.action for a in session.exec(select(AuditLog)).all()]
    assert audit_svc.PRIVACY_UPDATE in actions


def test_it_can_be_switched_off(session):
    boss = make_user(session, email="capo2@x.it", is_superuser=True)
    privacy_routes.update_privacy(PrivacyUpdate(enabled=False), current=boss, session=session)
    assert privacy_routes.get_privacy(session=session).enabled is False


def test_an_observer_reads_it_but_cannot_rewrite_it(session):
    """They must SEE it — it speaks of them — but they do not decide its text."""
    from app.deps.auth import require_superuser

    guest = make_user(session, email="ospite@x.it")
    guest.is_observer = True
    session.add(guest)
    session.commit()

    assert privacy_routes.get_privacy(session=session).enabled is True
    with pytest.raises(HTTPException) as e:
        require_superuser(guest, session)
    assert e.value.status_code == 403


# ── the retention value is a PARAMETER, not a copied number ──────────────────

def test_the_retention_window_is_filled_in_from_the_configuration(session, monkeypatch):
    """It serves one thing only: that the notice cannot lie. If someone changes the window
    and forgets the text, the text updates by itself."""
    from app.core.config import get_settings

    cfg = get_settings()
    monkeypatch.setattr(cfg.audit, "retention_minutes", 60, raising=False)
    outside = privacy_routes.get_privacy(session=session)

    assert "{audit_retention}" not in outside.body        # replaced in the text shown
    assert "1 hour" in outside.body
    assert "{audit_retention}" in outside.body_template   # untouched in the one to edit


def test_no_expiry_is_said_in_words_not_left_blank(session, monkeypatch):
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings().audit, "retention_minutes", 0, raising=False)
    outside = privacy_routes.get_privacy(session=session)
    assert "none — entries are kept until an administrator removes them" in outside.body


@pytest.mark.parametrize("minutes,expected", [
    (1, "1 minute"), (45, "45 minutes"), (60, "1 hour"), (120, "2 hours"),
    (1440, "1 day"), (43200, "30 days"), (90, "90 minutes"),
])
def test_the_window_is_said_the_way_a_person_would(minutes, expected):
    from app.routes.privacy import _duration

    assert _duration(minutes) == expected


def test_an_unknown_placeholder_is_left_alone(session, monkeypatch):
    """Whoever writes a notice must not be able to break it with a brace."""
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings().audit, "retention_minutes", 60, raising=False)
    boss = make_user(session, email="capo-par@x.it", is_superuser=True)
    outside = privacy_routes.update_privacy(
        PrivacyUpdate(body="Conserviamo per {audit_retention}. Scrivi a {qualcosa}."),
        current=boss, session=session,
    )
    assert outside.body == "Conserviamo per 1 hour. Scrivi a {qualcosa}."
