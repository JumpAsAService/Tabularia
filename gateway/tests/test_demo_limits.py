"""I limiti che servono prima di aprire l'installazione a sconosciuti.

Scritti per una demo pubblica con UN account condiviso, ma non sono
caratteristiche della demo: sono buchi che valgono sempre.
"""
import pytest
from fastapi import HTTPException

from app.services import login_throttle, permissions
from app.models.permission import Capability, Permission
from tests.conftest import make_project, make_user


@pytest.fixture(autouse=True)
def _contatori_puliti():
    login_throttle.azzera()
    yield
    login_throttle.azzera()


# ── upload: serve saper creare qualcosa, non solo guardare ───────────────────

def test_a_read_only_account_cannot_upload(session):
    """Era il buco: `POST /files` chiedeva solo di essere autenticati, quindi un
    account di sola lettura poteva scrivere nel bucket dell'installazione."""
    p = make_project(session, name="P")
    ospite = make_user(session, email="ospite@x.it")
    session.add(Permission(project_id=p.id, user_id=ospite.id, capability=Capability.VIEW))
    session.commit()
    assert permissions.can_upload(session, ospite) is False


def test_who_can_edit_somewhere_can_upload(session):
    p = make_project(session, name="P")
    autore = make_user(session, email="autore@x.it")
    session.add(Permission(project_id=p.id, user_id=autore.id, capability=Capability.EDIT))
    session.commit()
    assert permissions.can_upload(session, autore) is True


def test_an_administrator_can_always_upload(session):
    assert permissions.can_upload(session, make_user(session, email="capo@x.it", is_superuser=True)) is True


# ── tetto di spesa giornaliero dell'assistente ───────────────────────────────

def test_the_daily_spend_sums_only_todays_turns(session):
    from datetime import datetime, timedelta, timezone
    from app.models import AiChat, AiChatTurn
    from app.services import ai_chats

    anna = make_user(session, email="anna@x.it")
    chat = ai_chats.crea(session, anna, "d", "m", None)
    oggi = datetime.now(timezone.utc).replace(tzinfo=None)
    session.add(AiChatTurn(chat_id=chat.id, seq=0, cost_usd="0.010", created_at=oggi))
    session.add(AiChatTurn(chat_id=chat.id, seq=1, cost_usd="0.005", created_at=oggi))
    # ieri non conta
    session.add(AiChatTurn(chat_id=chat.id, seq=2, cost_usd="9.99", created_at=oggi - timedelta(days=1)))
    # costo sconosciuto conta zero: non è «gratis», è «non lo sappiamo»
    session.add(AiChatTurn(chat_id=chat.id, seq=3, cost_usd=None, created_at=oggi))
    session.commit()
    assert str(ai_chats.speso_oggi(session, anna)) == "0.015"


def test_the_spend_is_per_user(session):
    from app.services import ai_chats
    from app.models import AiChatTurn
    from datetime import datetime, timezone

    anna, bruno = make_user(session, email="a@x.it"), make_user(session, email="b@x.it")
    chat = ai_chats.crea(session, anna, "d", "m", None)
    session.add(AiChatTurn(chat_id=chat.id, seq=0, cost_usd="0.5",
                           created_at=datetime.now(timezone.utc).replace(tzinfo=None)))
    session.commit()
    assert ai_chats.speso_oggi(session, bruno) == 0


# ── freno sul login ──────────────────────────────────────────────────────────

def test_the_first_attempts_pass_then_it_holds():
    for _ in range(login_throttle.SOGLIA):
        assert login_throttle.attesa_richiesta("1.2.3.4", "a@x.it") == 0
        login_throttle.registra_errore("1.2.3.4", "a@x.it")
    assert login_throttle.attesa_richiesta("1.2.3.4", "a@x.it") > 0


def test_a_success_clears_the_count():
    for _ in range(login_throttle.SOGLIA):
        login_throttle.registra_errore("1.2.3.4", "a@x.it")
    login_throttle.registra_successo("1.2.3.4", "a@x.it")
    assert login_throttle.attesa_richiesta("1.2.3.4", "a@x.it") == 0


def test_one_blocked_account_does_not_block_another():
    """Si conta per (IP, email): un ufficio dietro un NAT non si blocca a vicenda
    e cambiare account non azzera il conto dell'altro."""
    for _ in range(login_throttle.SOGLIA + 2):
        login_throttle.registra_errore("1.2.3.4", "vittima@x.it")
    assert login_throttle.attesa_richiesta("1.2.3.4", "vittima@x.it") > 0
    assert login_throttle.attesa_richiesta("1.2.3.4", "altro@x.it") == 0
    assert login_throttle.attesa_richiesta("9.9.9.9", "vittima@x.it") == 0


def test_the_wait_grows_with_the_attempts():
    prima = None
    for i in range(login_throttle.SOGLIA + 3):
        login_throttle.registra_errore("5.5.5.5", "a@x.it")
        if i >= login_throttle.SOGLIA:
            adesso = login_throttle.attesa_richiesta("5.5.5.5", "a@x.it")
            if prima is not None:
                assert adesso > prima
            prima = adesso


def test_the_route_answers_429_with_retry_after(session):
    from app.routes import auth as auth_routes
    from app.schemas.models import LoginRequest
    from types import SimpleNamespace

    request = SimpleNamespace(client=SimpleNamespace(host="7.7.7.7"), headers={}, url="", method="POST")
    for _ in range(login_throttle.SOGLIA):
        login_throttle.registra_errore("7.7.7.7", "a@x.it")
    with pytest.raises(HTTPException) as e:
        auth_routes.login(LoginRequest(email="a@x.it", password="x"), request=request, session=session)
    assert e.value.status_code == 429 and "Retry-After" in (e.value.headers or {})
