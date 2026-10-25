"""Saved conversations: list, resume, cost, isolation between users.

The chats hold one person's questions and rows of data only they could read: they are
not shared, and someone else's chat cannot be told from one that does not exist.
"""
import pytest
from fastapi import HTTPException

from sqlmodel import select

from app.models import AiChat, AiChatTurn
from app.routes import ai as ai_routes
from app.services import ai_chats
from tests.conftest import make_user


def _turn(session, chat_id, seq, *, question="d", cost=None, inp=10, out=5):
    t = AiChatTurn(
        chat_id=chat_id, seq=seq, question=question, model_id="m",
        messages='[{"parts": [{"part_kind": "text", "content": "risposta"}]}]',
        input_tokens=inp, output_tokens=out, requests=1, cost_usd=cost,
    )
    session.add(t)
    session.commit()
    return t


@pytest.fixture
def two_users(session):
    anna = make_user(session, email="anna@x.it")
    bruno = make_user(session, email="bruno@x.it")
    chat = ai_chats.create(session, anna, "Quanti ordini a marzo?", "gpt-oss-120b", "polars")
    return anna, bruno, chat


def test_the_first_question_becomes_the_title(session, two_users):
    _, _, chat = two_users
    assert chat.title == "Quanti ordini a marzo?"


def test_a_very_long_question_is_truncated(session):
    anna = make_user(session, email="anna@x.it")
    chat = ai_chats.create(session, anna, "x" * 500, "m", None)
    assert len(chat.title) == ai_chats.MAX_TITLE and chat.title.endswith("…")


def test_costs_are_summed_exactly(session, two_users):
    anna, _, chat = two_users
    # small decimals: summed in floating point they would give 0.000059999...
    for i, c in enumerate(["0.000017613", "0.000021", "0.000021387"]):
        _turn(session, chat.id, i, cost=c)
    assert ai_chats.summary(session, chat)["cost_usd"] == "0.000060000"


def test_an_unknown_cost_is_not_zero(session, two_users):
    anna, _, chat = two_users
    _turn(session, chat.id, 0, cost=None)
    assert ai_chats.summary(session, chat)["cost_usd"] is None  # «I don't know», not «free»
    _turn(session, chat.id, 1, cost="0.001")
    assert ai_chats.summary(session, chat)["cost_usd"] == "0.001"  # the known ones are summed all the same


def test_the_listing_shows_only_your_own_chats(session, two_users):
    anna, bruno, chat = two_users
    _turn(session, chat.id, 0)
    others_ = ai_chats.create(session, bruno, "roba di Bruno", "m", None)
    _turn(session, others_.id, 0)
    assert [c["id"] for c in ai_chats.listing(session, anna, 50)] == [chat.id]
    assert [c["title"] for c in ai_chats.listing(session, bruno, 50)] == ["roba di Bruno"]


def test_someone_elses_chat_is_indistinguishable_from_a_missing_one(session, two_users):
    anna, bruno, chat = two_users
    messages = []
    for action in (lambda: ai_chats.detail(session, bruno, chat.id),
                   lambda: ai_chats.detail(session, bruno, 999999),
                   lambda: ai_chats.remove(session, bruno, chat.id)):
        with pytest.raises(HTTPException) as e:
            action()
        assert e.value.status_code == 404
        messages.append(e.value.detail)
    assert messages[0] == messages[1]  # the same message: no oracle
    assert session.get(AiChat, chat.id) is not None  # and it was not deleted


def test_deleting_a_chat_takes_its_turns_with_it(session, two_users):
    anna, _, chat = two_users
    _turn(session, chat.id, 0)
    ai_chats.remove(session, anna, chat.id)
    assert session.get(AiChat, chat.id) is None
    assert session.query(AiChatTurn).count() == 0


def test_the_most_recently_used_chat_comes_first(session, two_users):
    anna, _, before = two_users
    _turn(session, before.id, 0)
    second_one = ai_chats.create(session, anna, "domanda nuova", "m", None)
    _turn(session, second_one.id, 0)
    assert [c["id"] for c in ai_chats.listing(session, anna, 50)] == [second_one.id, before.id]
    ai_chats.save_turn(session, before.id, question="ancora", messages=[], model_id="m",
                         input_tokens=1, output_tokens=1, requests=1, cost=None)
    assert [c["id"] for c in ai_chats.listing(session, anna, 50)][0] == before.id


def test_a_corrupt_turn_does_not_break_reopening(session, two_users):
    anna, _, chat = two_users
    t = _turn(session, chat.id, 0)
    t.messages = "{non è json"
    session.add(t)
    session.commit()
    assert ai_chats.history(session, chat, 60) == []  # skips the turn instead of blowing up
    assert ai_chats.detail(session, anna, chat.id)["messages"][0]["answer"] == ""


def test_deleting_the_user_deletes_their_chats(session, two_users):
    from app.routes import users as user_routes

    anna, bruno, chat = two_users
    _turn(session, chat.id, 0)
    boss = make_user(session, email="capo@x.it", is_superuser=True)
    user_routes.delete_user(anna.id, session=session, current=boss)
    assert session.get(AiChat, chat.id) is None and session.query(AiChatTurn).count() == 0


def test_a_conversation_without_turns_is_not_listed(session, two_users):
    """An interrupted request must not leave an empty chat in the list."""
    anna, _, chat = two_users
    assert ai_chats.listing(session, anna, 50) == []
    _turn(session, chat.id, 0)
    assert [c["id"] for c in ai_chats.listing(session, anna, 50)] == [chat.id]


@pytest.mark.anyio
async def test_the_routes_are_scoped_to_the_caller(session, two_users):
    anna, bruno, chat = two_users
    _turn(session, chat.id, 0)
    assert [c["id"] for c in ai_routes.list_chats(user=anna, session=session)] == [chat.id]
    assert ai_routes.list_chats(user=bruno, session=session) == []
    assert ai_routes.get_chat(chat.id, user=anna, session=session)["id"] == chat.id
    with pytest.raises(HTTPException):
        ai_routes.get_chat(chat.id, user=bruno, session=session)


# ── downloading the whole result ─────────────────────────────────────────────

def _turn_with_query(session, chat_id, ds_id, sql="SELECT 1 AS n FROM self", step="call-1"):
    import json as _json
    messages = [{"parts": [{
        "part_kind": "tool-call", "tool_name": "query_datasource", "tool_call_id": step,
        "args": _json.dumps({"datasource_id": ds_id, "sql": sql}),
    }]}]
    t = AiChatTurn(chat_id=chat_id, seq=0, question="d", model_id="m",
                   messages=_json.dumps(messages), input_tokens=1, output_tokens=1, requests=1)
    session.add(t)
    session.commit()
    return t


def test_the_query_is_read_back_from_the_turn_not_from_the_caller(session, two_users):
    anna, _, chat = two_users
    _turn_with_query(session, chat.id, 7, sql="SELECT paese FROM self")
    assert ai_chats.step_query(session, anna, chat.id, 0, "call-1") == (7, "SELECT paese FROM self")


def test_you_cannot_export_a_step_of_someone_elses_chat(session, two_users):
    anna, bruno, chat = two_users
    _turn_with_query(session, chat.id, 7)
    with pytest.raises(HTTPException) as e:
        ai_chats.step_query(session, bruno, chat.id, 0, "call-1")
    assert e.value.status_code == 404


def test_a_step_that_is_not_a_query_cannot_be_exported(session, two_users):
    import json as _json

    anna, _, chat = two_users
    messages = [{"parts": [{"part_kind": "tool-call", "tool_name": "list_datasources",
                            "tool_call_id": "c9", "args": "{}"}]}]
    session.add(AiChatTurn(chat_id=chat.id, seq=0, question="d", model_id="m",
                           messages=_json.dumps(messages)))
    session.commit()
    with pytest.raises(HTTPException) as e:
        ai_chats.step_query(session, anna, chat.id, 0, "c9")
    assert e.value.status_code == 422


def test_an_unknown_step_is_not_found(session, two_users):
    anna, _, chat = two_users
    _turn_with_query(session, chat.id, 7)
    with pytest.raises(HTTPException) as e:
        ai_chats.step_query(session, anna, chat.id, 0, "non-esiste")
    assert e.value.status_code == 404


def test_reopening_a_conversation_brings_back_the_evidence(session, two_users):
    """A reopened turn must show the TABLES, not only the steps."""
    import json as _json

    anna, _, chat = two_users
    messages = [{"parts": [
        {"part_kind": "tool-call", "tool_name": "query_datasource", "tool_call_id": "c1",
         "args": _json.dumps({"datasource_id": 7, "sql": "SELECT 1"})},
        {"part_kind": "tool-return", "tool_call_id": "c1", "content": {
            "datasource": "ordini", "columns": [{"name": "n"}], "rows": [{"n": 3}],
            "row_count": 1, "truncated": False, "chart": {"type": "bar", "x": "paese", "y": "n"}}},
        {"part_kind": "text", "content": "Sono 3."},
    ]}]
    session.add(AiChatTurn(chat_id=chat.id, seq=0, question="quanti?", model_id="m",
                           messages=_json.dumps(messages)))
    session.commit()

    step = ai_chats.detail(session, anna, chat.id)["messages"][0]["steps"][0]
    assert step["table"]["rows"] == [{"n": 3}]
    assert step["chart"] == {"type": "bar", "x": "paese", "y": "n"}


# ── observer: the conversation is not saved, the bill is ─────────────────────

def test_an_observer_gets_no_history_even_if_a_chat_exists(session):
    """The list is empty by construction — their chats are not written — but the route says
    so anyway: if one day something created one in their name, it would not end up in
    front of anyone signing in with the shared account."""
    from app.routes import ai as ai_routes
    from app.services import ai_chats

    guest = make_user(session, email="ospite-chat@x.it")
    guest.is_observer = True
    session.add(guest)
    session.commit()
    ai_chats.create(session, guest, "una domanda rimasta lì", "m", None)

    assert ai_routes.list_chats(user=guest, session=session) == []


def test_an_ordinary_user_keeps_their_history(session):
    from app.routes import ai as ai_routes
    from app.services import ai_chats

    someone = make_user(session, email="tizio-chat@x.it")
    chat = ai_chats.create(session, someone, "d", "m", None)
    ai_chats.save_turn(session, chat.id, question="d", messages=[], model_id="m",
                         input_tokens=1, output_tokens=1, requests=1, cost=None)
    assert len(ai_routes.list_chats(user=someone, session=session)) == 1


def test_the_bill_survives_without_a_conversation(session):
    """It is the deal: no text, but the daily cap must see the spending of whoever comes in
    from the shared account — which is exactly the one to watch."""
    from decimal import Decimal
    from app.models import AiChat, AiSpend
    from app.services import ai_chats

    guest = make_user(session, email="ospite-conto@x.it")
    ai_chats.record_spend(session, user_id=guest.id, model_id="m",
                            input_tokens=100, output_tokens=20, cost=Decimal("0.02"))

    assert session.exec(select(AiChat).where(AiChat.user_id == guest.id)).all() == []
    row = session.exec(select(AiSpend).where(AiSpend.user_id == guest.id)).one()
    assert row.turn_id is None and row.cost_usd == "0.02"
    assert str(ai_chats.spent_today(session, guest)) == "0.02"
    assert str(ai_chats.spent_today_all(session)) == "0.02"
