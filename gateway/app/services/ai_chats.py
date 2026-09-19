"""Salvataggio e ripresa delle conversazioni con l'assistente.

La storia sta sul SERVER: il client manda solo l'id della chat. Oltre a far
sopravvivere le conversazioni alla chiusura della scheda — e a evitare di
ripagare la stessa domanda — questo toglie di mezzo il vettore per cui una
storia contraffatta dal client arrivava intatta al modello (audit 2026-09-19,
A8).

Una chat appartiene a una persona e non si condivide: contiene le sue domande e
righe di dati che solo lei poteva leggere. Ogni lettura passa da
`_chat_di(user)`, che tratta la chat di un altro come inesistente.
"""
from __future__ import annotations

import json
import logging
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

from fastapi import HTTPException
from sqlmodel import Session, func, select

from app.models import AiChat, AiChatTurn, User

logger = logging.getLogger(__name__)

MAX_TITLE = 80


def _titolo(domanda: str) -> str:
    pulita = " ".join((domanda or "").split())
    return (pulita[: MAX_TITLE - 1] + "…") if len(pulita) > MAX_TITLE else (pulita or "Nuova conversazione")


def _chat_di(session: Session, user: User, chat_id: int) -> AiChat:
    """La chat, se è di questo utente. Altrimenti 404: la chat di un altro non
    esiste, e non si distingue da una inesistente."""
    chat = session.get(AiChat, chat_id)
    if chat is None or chat.user_id != user.id:
        raise HTTPException(status_code=404, detail="Conversazione non trovata")
    return chat


def apri(session: Session, user: User, chat_id: int, model_id: str, engine: Optional[str]) -> AiChat:
    """Riapre una conversazione esistente, ricordandone modello e motore."""
    chat = _chat_di(session, user, chat_id)
    chat.model_id, chat.engine = model_id, engine
    session.add(chat)
    session.commit()
    session.refresh(chat)
    return chat


def crea(session: Session, user: User, domanda: str, model_id: str, engine: Optional[str]) -> AiChat:
    """Apre una conversazione nuova.

    Si chiama quando il primo turno è FINITO, non quando arriva la domanda: una
    richiesta interrotta a metà lascerebbe altrimenti una conversazione vuota
    nell'elenco (visto dal vivo il 2026-09-19)."""
    chat = AiChat(user_id=user.id, title=_titolo(domanda), model_id=model_id, engine=engine)
    session.add(chat)
    session.commit()
    session.refresh(chat)
    return chat


def storia(session: Session, chat: AiChat, max_messaggi: int):
    """I messaggi dei turni precedenti, pronti per `message_history`.

    Un turno illeggibile viene SALTATO invece di far fallire la chat: meglio una
    conversazione che riparte monca che una che non si riapre più."""
    from pydantic_ai import ModelMessagesTypeAdapter

    grezzi: list[Any] = []
    turni = session.exec(
        select(AiChatTurn).where(AiChatTurn.chat_id == chat.id).order_by(AiChatTurn.seq)
    ).all()
    for turno in turni:
        try:
            grezzi.extend(json.loads(turno.messages))
        except ValueError:
            logger.warning("chat %s: turno %s illeggibile, saltato", chat.id, turno.seq)
    if max_messaggi:
        grezzi = grezzi[-max_messaggi:]
    if not grezzi:
        return []
    try:
        return ModelMessagesTypeAdapter.validate_python(grezzi)
    except Exception:  # noqa: BLE001 — storia incompatibile (es. dopo un upgrade): si riparte puliti
        logger.warning("chat %s: storia non validabile, la conversazione riparte vuota", chat.id)
        return []


def salva_turno(
    session: Session, chat_id: int, *, domanda: str, messaggi: list[Any], model_id: str,
    input_tokens: int, output_tokens: int, requests: int, cost: Optional[Decimal],
) -> AiChatTurn:
    prossimo = session.exec(
        select(func.coalesce(func.max(AiChatTurn.seq), -1)).where(AiChatTurn.chat_id == chat_id)
    ).one() + 1
    turno = AiChatTurn(
        chat_id=chat_id, seq=prossimo, question=domanda[:2000],
        messages=json.dumps(messaggi, ensure_ascii=False, default=str),
        model_id=model_id, input_tokens=input_tokens or 0, output_tokens=output_tokens or 0,
        requests=requests or 0,
        # `None` = costo non determinabile per questo modello, che NON è zero
        cost_usd=(str(cost) if cost is not None else None),
    )
    session.add(turno)
    chat = session.get(AiChat, chat_id)
    if chat is not None:
        from app.models.ai_chat import _now

        chat.updated_at = _now()
        if not chat.title or chat.title == "Nuova conversazione":
            chat.title = _titolo(domanda)
        session.add(chat)
    session.commit()
    session.refresh(turno)
    return turno


def _somma_costi(valori) -> Optional[str]:
    """Somma esatta dei costi noti. `None` se nessun turno ha un costo."""
    totale, visto = Decimal(0), False
    for v in valori:
        if not v:
            continue
        try:
            totale += Decimal(v)
            visto = True
        except InvalidOperation:
            continue
    return str(totale) if visto else None


def riepilogo(session: Session, chat: AiChat) -> dict[str, Any]:
    righe = session.exec(
        select(AiChatTurn.cost_usd, AiChatTurn.input_tokens, AiChatTurn.output_tokens)
        .where(AiChatTurn.chat_id == chat.id)
    ).all()
    return {
        "turns": len(righe),
        "input_tokens": sum(r[1] or 0 for r in righe),
        "output_tokens": sum(r[2] or 0 for r in righe),
        "cost_usd": _somma_costi(r[0] for r in righe),
    }


def elenco(session: Session, user: User, limite: int) -> list[dict[str, Any]]:
    chats = session.exec(
        select(AiChat).where(AiChat.user_id == user.id).order_by(AiChat.updated_at.desc()).limit(limite)
    ).all()
    righe = [{"id": c.id, "title": c.title, "model_id": c.model_id, "engine": c.engine,
              "created_at": c.created_at, "updated_at": c.updated_at, **riepilogo(session, c)} for c in chats]
    # Una conversazione senza turni non ha niente da mostrare: non si elenca.
    # La creazione è ormai differita al primo turno finito, ma la rete di
    # sicurezza vale per le righe rimaste da prima e per qualunque corsa.
    return [r for r in righe if r["turns"]]


def dettaglio(session: Session, user: User, chat_id: int) -> dict[str, Any]:
    """La conversazione da rimettere a schermo: domande, risposte e costo di ogni turno."""
    chat = _chat_di(session, user, chat_id)
    turni = session.exec(
        select(AiChatTurn).where(AiChatTurn.chat_id == chat.id).order_by(AiChatTurn.seq)
    ).all()
    return {
        "id": chat.id, "title": chat.title, "model_id": chat.model_id, "engine": chat.engine,
        "created_at": chat.created_at, "updated_at": chat.updated_at,
        **riepilogo(session, chat),
        "messages": [_turno_per_ui(t) for t in turni],
    }


def _turno_per_ui(turno: AiChatTurn) -> dict[str, Any]:
    """Un turno come lo mostra la chat: la domanda, il testo della risposta, i
    passi compiuti, e quanto è costato.

    Si ricostruisce dai messaggi salvati invece di tenere una seconda copia del
    testo: una copia sola non può divergere dall'altra."""
    testo: list[str] = []
    passi: list[dict[str, Any]] = []
    per_id: dict[str, dict[str, Any]] = {}
    try:
        messaggi = json.loads(turno.messages)
    except ValueError:
        messaggi = []
    for m in messaggi:
        for parte in m.get("parts", []) or []:
            tipo = parte.get("part_kind")
            if tipo == "text" and parte.get("content"):
                testo.append(str(parte["content"]))
            elif tipo == "tool-call":
                passo = {
                    "id": parte.get("tool_call_id"),
                    "name": parte.get("tool_name"),
                    "args": parte.get("args"),
                }
                passi.append(passo)
                if passo["id"]:
                    per_id[str(passo["id"])] = passo
            elif tipo == "tool-return":
                # la TABELLA che giustifica la risposta: senza, una conversazione
                # riapribile mostrerebbe i passi e non le prove
                passo = per_id.get(str(parte.get("tool_call_id")))
                contenuto = parte.get("content")
                if passo is None or not isinstance(contenuto, dict):
                    continue
                if isinstance(contenuto.get("rows"), list) and "row_count" in contenuto:
                    passo["table"] = {
                        k: contenuto.get(k)
                        for k in ("datasource", "columns", "rows", "row_count", "truncated")
                    }
                if isinstance(contenuto.get("chart"), dict):
                    passo["chart"] = contenuto["chart"]
    return {
        "seq": turno.seq,
        "question": turno.question,
        "answer": "".join(testo),
        "steps": passi,
        "model_id": turno.model_id,
        "usage": {
            "input_tokens": turno.input_tokens,
            "output_tokens": turno.output_tokens,
            "requests": turno.requests,
            "cost_usd": turno.cost_usd,
        },
        "created_at": turno.created_at,
    }


def elimina(session: Session, user: User, chat_id: int) -> None:
    from sqlalchemy import delete as sa_delete

    chat = _chat_di(session, user, chat_id)
    session.exec(sa_delete(AiChatTurn).where(AiChatTurn.chat_id == chat.id))
    session.delete(chat)
    session.commit()


def aggiungi_costo(session: Session, turno_id: int, extra: Optional[Decimal]) -> None:
    """Somma un costo a un turno già salvato (la chiamata che scrive il titolo).

    Il totale della conversazione è una promessa della pagina: una chiamata a
    pagamento che non ci finisce dentro lo fa mentire per difetto."""
    if extra is None:
        return
    turno = session.get(AiChatTurn, turno_id)
    if turno is None:
        return
    base = Decimal(turno.cost_usd) if turno.cost_usd else Decimal(0)
    turno.cost_usd = str(base + extra)
    session.add(turno)
    session.commit()


def rinomina(session: Session, chat_id: int, titolo: str) -> None:
    """Sostituisce il titolo di ripiego con quello scritto dal modello."""
    chat = session.get(AiChat, chat_id)
    if chat is None:
        return
    pulito = " ".join((titolo or "").split())[:MAX_TITLE]
    if not pulito:
        return
    chat.title = pulito
    session.add(chat)
    session.commit()


def query_del_passo(session: Session, user: User, chat_id: int, seq: int, step_id: str) -> tuple[int, str]:
    """La datasource e la SQL di un passo salvato, per riesportarlo.

    La query si rilegge dal TURNO, non la si accetta dal client: chi scarica non
    deve poter scegliere che cosa viene eseguito. Ritorna `(datasource_id, sql)`."""
    _chat_di(session, user, chat_id)  # 404 se non è sua
    turno = session.exec(
        select(AiChatTurn).where(AiChatTurn.chat_id == chat_id, AiChatTurn.seq == seq)
    ).first()
    if turno is None:
        raise HTTPException(status_code=404, detail="Passo non trovato")
    try:
        messaggi = json.loads(turno.messages)
    except ValueError:
        messaggi = []
    for m in messaggi:
        for parte in m.get("parts", []) or []:
            if parte.get("part_kind") != "tool-call" or str(parte.get("tool_call_id")) != str(step_id):
                continue
            if parte.get("tool_name") != "query_datasource":
                raise HTTPException(status_code=422, detail="Questo passo non è una query")
            args = parte.get("args")
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except ValueError:
                    args = {}
            if not isinstance(args, dict) or not args.get("sql"):
                raise HTTPException(status_code=422, detail="Il passo non ha una query da rieseguire")
            return int(args.get("datasource_id") or 0), str(args["sql"])
    raise HTTPException(status_code=404, detail="Passo non trovato")
