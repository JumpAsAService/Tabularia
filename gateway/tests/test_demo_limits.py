"""I limiti che servono prima di aprire l'installazione a sconosciuti.

Scritti per una demo pubblica con UN account condiviso, ma non sono
caratteristiche della demo: sono buchi che valgono sempre.
"""
import pytest
from fastapi import HTTPException

from sqlmodel import delete, select

from app.services import login_throttle, permissions
from app.models.permission import Capability, Permission
from tests.conftest import make_project, make_user


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

def test_the_daily_spend_sums_only_today(session):
    from datetime import datetime, timedelta, timezone
    from app.models import AiSpend
    from app.services import ai_chats

    anna = make_user(session, email="anna@x.it")
    oggi = datetime.now(timezone.utc).replace(tzinfo=None)
    session.add(AiSpend(user_id=anna.id, cost_usd="0.010", ts=oggi))
    session.add(AiSpend(user_id=anna.id, cost_usd="0.005", ts=oggi))
    # ieri non conta
    session.add(AiSpend(user_id=anna.id, cost_usd="9.99", ts=oggi - timedelta(days=1)))
    # costo sconosciuto conta zero: non è «gratis», è «non lo sappiamo»
    session.add(AiSpend(user_id=anna.id, cost_usd=None, ts=oggi))
    session.commit()
    assert str(ai_chats.speso_oggi(session, anna)) == "0.015"


def test_the_spend_is_per_user(session):
    from datetime import datetime, timezone
    from app.models import AiSpend
    from app.services import ai_chats

    anna, bruno = make_user(session, email="a@x.it"), make_user(session, email="b@x.it")
    session.add(AiSpend(user_id=anna.id, cost_usd="0.5",
                        ts=datetime.now(timezone.utc).replace(tzinfo=None)))
    session.commit()
    assert ai_chats.speso_oggi(session, bruno) == 0


def test_the_whole_installation_has_its_own_ceiling(session):
    """Dove l'account è condiviso il tetto per utente non protegge niente: il
    conto è uno solo, e va guardato tutto insieme."""
    from datetime import datetime, timezone
    from app.models import AiSpend
    from app.services import ai_chats

    anna, bruno = make_user(session, email="a2@x.it"), make_user(session, email="b2@x.it")
    ora = datetime.now(timezone.utc).replace(tzinfo=None)
    session.add(AiSpend(user_id=anna.id, cost_usd="0.30", ts=ora))
    session.add(AiSpend(user_id=bruno.id, cost_usd="0.20", ts=ora))
    session.commit()
    assert str(ai_chats.speso_oggi(session, anna)) == "0.30"
    assert str(ai_chats.speso_oggi_tutti(session)) == "0.50"


def test_deleting_the_conversation_does_not_reset_the_ceiling(session):
    """È il motivo per cui il registro della spesa esiste: le chat di un
    osservatore si cancellano quando se ne va, e se il tetto leggesse da lì
    basterebbe aprirne una nuova per ricominciare a spendere."""
    from decimal import Decimal
    from app.models import AiChat, AiChatTurn
    from app.services import ai_chats

    ospite = make_user(session, email="ospite-spesa@x.it")
    chat = ai_chats.crea(session, ospite, "quanto vendiamo?", "m", None)
    ai_chats.salva_turno(session, chat.id, domanda="quanto vendiamo?", messaggi=[],
                         model_id="m", input_tokens=10, output_tokens=5, requests=1,
                         cost=Decimal("0.40"))
    assert str(ai_chats.speso_oggi(session, ospite)) == "0.40"

    # via la conversazione, contenuto compreso
    session.exec(delete(AiChatTurn).where(AiChatTurn.chat_id == chat.id))
    session.exec(delete(AiChat).where(AiChat.id == chat.id))
    session.commit()

    assert session.exec(select(AiChat).where(AiChat.user_id == ospite.id)).all() == []
    assert str(ai_chats.speso_oggi(session, ospite)) == "0.40"      # il conto resta
    assert str(ai_chats.speso_oggi_tutti(session)) == "0.40"


# ── freno sul login ──────────────────────────────────────────────────────────

def test_the_first_attempts_pass_then_it_holds(session):
    for _ in range(login_throttle.SOGLIA):
        assert login_throttle.attesa_richiesta(session, "1.2.3.4", "a@x.it") == 0
        login_throttle.registra_errore(session, "1.2.3.4", "a@x.it")
    assert login_throttle.attesa_richiesta(session, "1.2.3.4", "a@x.it") > 0


def test_a_success_clears_the_count(session):
    for _ in range(login_throttle.SOGLIA):
        login_throttle.registra_errore(session, "1.2.3.4", "a@x.it")
    login_throttle.registra_successo(session, "1.2.3.4", "a@x.it")
    assert login_throttle.attesa_richiesta(session, "1.2.3.4", "a@x.it") == 0


def test_one_blocked_account_does_not_block_another(session):
    """Si conta per (IP, email): un ufficio dietro un NAT non si blocca a vicenda
    e cambiare account non azzera il conto dell'altro."""
    for _ in range(login_throttle.SOGLIA + 2):
        login_throttle.registra_errore(session, "1.2.3.4", "vittima@x.it")
    assert login_throttle.attesa_richiesta(session, "1.2.3.4", "vittima@x.it") > 0
    assert login_throttle.attesa_richiesta(session, "1.2.3.4", "altro@x.it") == 0
    assert login_throttle.attesa_richiesta(session, "9.9.9.9", "vittima@x.it") == 0


def test_the_wait_grows_with_the_attempts(session):
    prima = None
    for i in range(login_throttle.SOGLIA + 3):
        login_throttle.registra_errore(session, "5.5.5.5", "a@x.it")
        if i >= login_throttle.SOGLIA:
            adesso = login_throttle.attesa_richiesta(session, "5.5.5.5", "a@x.it")
            if prima is not None:
                assert adesso > prima
            prima = adesso


def test_the_route_answers_429_with_retry_after(session):
    from app.routes import auth as auth_routes
    from app.schemas.models import LoginRequest
    from types import SimpleNamespace

    request = SimpleNamespace(client=SimpleNamespace(host="7.7.7.7"), headers={}, url="", method="POST")
    for _ in range(login_throttle.SOGLIA):
        login_throttle.registra_errore(session, "7.7.7.7", "a@x.it")
    with pytest.raises(HTTPException) as e:
        auth_routes.login(LoginRequest(email="a@x.it", password="x"), request=request, session=session)
    assert e.value.status_code == 429 and "Retry-After" in (e.value.headers or {})


def test_two_replicas_count_the_same_failures(db_engine):
    """Il motivo per cui il conto sta in una tabella: cinque errori in tutto, non
    cinque per replica. Due sessioni = due processi."""
    from sqlmodel import Session

    with Session(db_engine) as replica_a, Session(db_engine) as replica_b:
        for i in range(login_throttle.SOGLIA):
            login_throttle.registra_errore(replica_a if i % 2 else replica_b, "1.2.3.4", "a@x.it")
        assert login_throttle.attesa_richiesta(replica_a, "1.2.3.4", "a@x.it") > 0
        assert login_throttle.attesa_richiesta(replica_b, "1.2.3.4", "a@x.it") > 0
        login_throttle.registra_successo(replica_b, "1.2.3.4", "a@x.it")
        assert login_throttle.attesa_richiesta(replica_a, "1.2.3.4", "a@x.it") == 0


def test_after_the_window_the_count_starts_over(session):
    from datetime import timedelta

    from app.models import LoginAttempt

    for _ in range(login_throttle.SOGLIA + 1):
        login_throttle.registra_errore(session, "1.2.3.4", "a@x.it")
    riga = session.get(LoginAttempt, ("1.2.3.4", "a@x.it"))
    riga.last_at = riga.last_at - timedelta(seconds=login_throttle.FINESTRA_SECONDI + 1)
    session.add(riga); session.commit()
    assert login_throttle.attesa_richiesta(session, "1.2.3.4", "a@x.it") == 0
    login_throttle.registra_errore(session, "1.2.3.4", "a@x.it")
    assert session.get(LoginAttempt, ("1.2.3.4", "a@x.it")).failures == 1


def test_stale_counts_are_swept(session):
    from datetime import timedelta

    from app.models import LoginAttempt

    login_throttle.registra_errore(session, "1.2.3.4", "vecchio@x.it")
    login_throttle.registra_errore(session, "1.2.3.4", "recente@x.it")
    riga = session.get(LoginAttempt, ("1.2.3.4", "vecchio@x.it"))
    riga.last_at = riga.last_at - timedelta(seconds=login_throttle.FINESTRA_SECONDI + 1)
    session.add(riga); session.commit()
    assert login_throttle.pulisci(session) == 1
    assert session.get(LoginAttempt, ("1.2.3.4", "recente@x.it")) is not None
