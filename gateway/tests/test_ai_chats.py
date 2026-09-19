"""Conversazioni salvate: elenco, ripresa, costo, isolamento fra utenti.

Le chat contengono le domande di una persona e righe di dati che solo lei
poteva leggere: non si condividono, e la chat di un altro non si distingue da
una inesistente.
"""
import pytest
from fastapi import HTTPException

from app.models import AiChat, AiChatTurn
from app.routes import ai as ai_routes
from app.services import ai_chats
from tests.conftest import make_user


def _turno(session, chat_id, seq, *, domanda="d", costo=None, inp=10, out=5):
    t = AiChatTurn(
        chat_id=chat_id, seq=seq, question=domanda, model_id="m",
        messages='[{"parts": [{"part_kind": "text", "content": "risposta"}]}]',
        input_tokens=inp, output_tokens=out, requests=1, cost_usd=costo,
    )
    session.add(t)
    session.commit()
    return t


@pytest.fixture
def due_utenti(session):
    anna = make_user(session, email="anna@x.it")
    bruno = make_user(session, email="bruno@x.it")
    chat = ai_chats.crea(session, anna, "Quanti ordini a marzo?", "gpt-oss-120b", "polars")
    return anna, bruno, chat


def test_the_first_question_becomes_the_title(session, due_utenti):
    _, _, chat = due_utenti
    assert chat.title == "Quanti ordini a marzo?"


def test_a_very_long_question_is_truncated(session):
    anna = make_user(session, email="anna@x.it")
    chat = ai_chats.crea(session, anna, "x" * 500, "m", None)
    assert len(chat.title) == ai_chats.MAX_TITLE and chat.title.endswith("…")


def test_costs_are_summed_exactly(session, due_utenti):
    anna, _, chat = due_utenti
    # decimali piccoli: sommati in virgola mobile darebbero 0.000059999...
    for i, c in enumerate(["0.000017613", "0.000021", "0.000021387"]):
        _turno(session, chat.id, i, costo=c)
    assert ai_chats.riepilogo(session, chat)["cost_usd"] == "0.000060000"


def test_an_unknown_cost_is_not_zero(session, due_utenti):
    anna, _, chat = due_utenti
    _turno(session, chat.id, 0, costo=None)
    assert ai_chats.riepilogo(session, chat)["cost_usd"] is None  # «non so», non «gratis»
    _turno(session, chat.id, 1, costo="0.001")
    assert ai_chats.riepilogo(session, chat)["cost_usd"] == "0.001"  # i noti si sommano lo stesso


def test_the_listing_shows_only_your_own_chats(session, due_utenti):
    anna, bruno, chat = due_utenti
    _turno(session, chat.id, 0)
    altrui = ai_chats.crea(session, bruno, "roba di Bruno", "m", None)
    _turno(session, altrui.id, 0)
    assert [c["id"] for c in ai_chats.elenco(session, anna, 50)] == [chat.id]
    assert [c["title"] for c in ai_chats.elenco(session, bruno, 50)] == ["roba di Bruno"]


def test_someone_elses_chat_is_indistinguishable_from_a_missing_one(session, due_utenti):
    anna, bruno, chat = due_utenti
    messaggi = []
    for azione in (lambda: ai_chats.dettaglio(session, bruno, chat.id),
                   lambda: ai_chats.dettaglio(session, bruno, 999999),
                   lambda: ai_chats.elimina(session, bruno, chat.id)):
        with pytest.raises(HTTPException) as e:
            azione()
        assert e.value.status_code == 404
        messaggi.append(e.value.detail)
    assert messaggi[0] == messaggi[1]  # stesso messaggio: nessun oracolo
    assert session.get(AiChat, chat.id) is not None  # e non è stata cancellata


def test_deleting_a_chat_takes_its_turns_with_it(session, due_utenti):
    anna, _, chat = due_utenti
    _turno(session, chat.id, 0)
    ai_chats.elimina(session, anna, chat.id)
    assert session.get(AiChat, chat.id) is None
    assert session.query(AiChatTurn).count() == 0


def test_the_most_recently_used_chat_comes_first(session, due_utenti):
    anna, _, prima = due_utenti
    _turno(session, prima.id, 0)
    seconda = ai_chats.crea(session, anna, "domanda nuova", "m", None)
    _turno(session, seconda.id, 0)
    assert [c["id"] for c in ai_chats.elenco(session, anna, 50)] == [seconda.id, prima.id]
    ai_chats.salva_turno(session, prima.id, domanda="ancora", messaggi=[], model_id="m",
                         input_tokens=1, output_tokens=1, requests=1, cost=None)
    assert [c["id"] for c in ai_chats.elenco(session, anna, 50)][0] == prima.id


def test_a_corrupt_turn_does_not_break_reopening(session, due_utenti):
    anna, _, chat = due_utenti
    t = _turno(session, chat.id, 0)
    t.messages = "{non è json"
    session.add(t)
    session.commit()
    assert ai_chats.storia(session, chat, 60) == []  # salta il turno invece di esplodere
    assert ai_chats.dettaglio(session, anna, chat.id)["messages"][0]["answer"] == ""


def test_deleting_the_user_deletes_their_chats(session, due_utenti):
    from app.routes import users as user_routes

    anna, bruno, chat = due_utenti
    _turno(session, chat.id, 0)
    capo = make_user(session, email="capo@x.it", is_superuser=True)
    user_routes.delete_user(anna.id, session=session, current=capo)
    assert session.get(AiChat, chat.id) is None and session.query(AiChatTurn).count() == 0


def test_a_conversation_without_turns_is_not_listed(session, due_utenti):
    """Una richiesta interrotta non deve lasciare una chat vuota nell'elenco."""
    anna, _, chat = due_utenti
    assert ai_chats.elenco(session, anna, 50) == []
    _turno(session, chat.id, 0)
    assert [c["id"] for c in ai_chats.elenco(session, anna, 50)] == [chat.id]


@pytest.mark.anyio
async def test_the_routes_are_scoped_to_the_caller(session, due_utenti):
    anna, bruno, chat = due_utenti
    _turno(session, chat.id, 0)
    assert [c["id"] for c in ai_routes.list_chats(user=anna, session=session)] == [chat.id]
    assert ai_routes.list_chats(user=bruno, session=session) == []
    assert ai_routes.get_chat(chat.id, user=anna, session=session)["id"] == chat.id
    with pytest.raises(HTTPException):
        ai_routes.get_chat(chat.id, user=bruno, session=session)


# ── scarico del risultato completo ───────────────────────────────────────────

def _turno_con_query(session, chat_id, ds_id, sql="SELECT 1 AS n FROM self", step="call-1"):
    import json as _json
    messaggi = [{"parts": [{
        "part_kind": "tool-call", "tool_name": "query_datasource", "tool_call_id": step,
        "args": _json.dumps({"datasource_id": ds_id, "sql": sql}),
    }]}]
    t = AiChatTurn(chat_id=chat_id, seq=0, question="d", model_id="m",
                   messages=_json.dumps(messaggi), input_tokens=1, output_tokens=1, requests=1)
    session.add(t)
    session.commit()
    return t


def test_the_query_is_read_back_from_the_turn_not_from_the_caller(session, due_utenti):
    anna, _, chat = due_utenti
    _turno_con_query(session, chat.id, 7, sql="SELECT paese FROM self")
    assert ai_chats.query_del_passo(session, anna, chat.id, 0, "call-1") == (7, "SELECT paese FROM self")


def test_you_cannot_export_a_step_of_someone_elses_chat(session, due_utenti):
    anna, bruno, chat = due_utenti
    _turno_con_query(session, chat.id, 7)
    with pytest.raises(HTTPException) as e:
        ai_chats.query_del_passo(session, bruno, chat.id, 0, "call-1")
    assert e.value.status_code == 404


def test_a_step_that_is_not_a_query_cannot_be_exported(session, due_utenti):
    import json as _json

    anna, _, chat = due_utenti
    messaggi = [{"parts": [{"part_kind": "tool-call", "tool_name": "list_datasources",
                            "tool_call_id": "c9", "args": "{}"}]}]
    session.add(AiChatTurn(chat_id=chat.id, seq=0, question="d", model_id="m",
                           messages=_json.dumps(messaggi)))
    session.commit()
    with pytest.raises(HTTPException) as e:
        ai_chats.query_del_passo(session, anna, chat.id, 0, "c9")
    assert e.value.status_code == 422


def test_an_unknown_step_is_not_found(session, due_utenti):
    anna, _, chat = due_utenti
    _turno_con_query(session, chat.id, 7)
    with pytest.raises(HTTPException) as e:
        ai_chats.query_del_passo(session, anna, chat.id, 0, "non-esiste")
    assert e.value.status_code == 404


def test_reopening_a_conversation_brings_back_the_evidence(session, due_utenti):
    """Un turno riaperto deve mostrare le TABELLE, non solo i passi."""
    import json as _json

    anna, _, chat = due_utenti
    messaggi = [{"parts": [
        {"part_kind": "tool-call", "tool_name": "query_datasource", "tool_call_id": "c1",
         "args": _json.dumps({"datasource_id": 7, "sql": "SELECT 1"})},
        {"part_kind": "tool-return", "tool_call_id": "c1", "content": {
            "datasource": "ordini", "columns": [{"name": "n"}], "rows": [{"n": 3}],
            "row_count": 1, "truncated": False, "chart": {"type": "bar", "x": "paese", "y": "n"}}},
        {"part_kind": "text", "content": "Sono 3."},
    ]}]
    session.add(AiChatTurn(chat_id=chat.id, seq=0, question="quanti?", model_id="m",
                           messages=_json.dumps(messaggi)))
    session.commit()

    passo = ai_chats.dettaglio(session, anna, chat.id)["messages"][0]["steps"][0]
    assert passo["table"]["rows"] == [{"n": 3}]
    assert passo["chart"] == {"type": "bar", "x": "paese", "y": "n"}
