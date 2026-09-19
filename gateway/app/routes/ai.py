"""Assistente AI: stato, modelli (pannello admin) e la chat in streaming.

- `GET  /ai/status`            per tutti: l'assistente e' acceso? con quali modelli?
- `GET  /ai/models`            superuser: catalogo del provider + scelta dell'admin
- `PUT  /ai/models/{id}`       superuser: abilita/disabilita un modello (audit)
- `POST /ai/chat`              per tutti: un turno di conversazione, in SSE

La conversazione NON e' salvata sul server: la storia torna al client a fine
turno (`done.history`) e il client la rimanda al turno dopo. Una storia
contraffatta puo' solo confondere la conversazione di chi la manda: ogni
strumento ricontrolla i permessi a ogni chiamata.
"""
from __future__ import annotations

import asyncio
import json
import logging
from decimal import Decimal
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlmodel import Session, select

from app.core.config import get_settings
from app.core.engine_client import get_engine_client
from app.db.session import engine as db_engine, get_session
from app.deps.auth import get_current_user, require_superuser
from app.models import AiChat, AiModel, User
from app.services import ai_agent, ai_chats, ai_pricing, audit
from app.services.ai_models import enabled_model_ids, ensure_configured, is_chat_model, provider_models
from app.services.engine_policy import allowed_engines

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/ai", tags=["ai"])


def _new_session() -> Session:
    """Sessione DB per gli strumenti dell'agente (una per chiamata): lo stream
    vive piu' a lungo della sessione della richiesta. I test la sostituiscono."""
    return Session(db_engine)


# ── stato e modelli ──────────────────────────────────────────────────────────
class AiStatusOut(BaseModel):
    enabled: bool
    models: list[str] = []
    default_model: Optional[str] = None


@router.get("/status", response_model=AiStatusOut)
def ai_status(user: User = Depends(get_current_user), session: Session = Depends(get_session)) -> AiStatusOut:
    cfg = get_settings().ai
    if not cfg.enabled:
        return AiStatusOut(enabled=False)
    models = enabled_model_ids(session)
    default = cfg.default_model if cfg.default_model in models else (models[0] if models else None)
    return AiStatusOut(enabled=True, models=models, default_model=default)


class AiModelOut(BaseModel):
    model_id: str
    enabled: bool
    chat: bool  # False = embedding/audio/reranker: non abilitabile per la chat
    available: bool  # False = abilitato in passato, ma il provider non lo elenca piu'


class AiModelUpdate(BaseModel):
    enabled: bool


@router.get("/models", response_model=list[AiModelOut])
async def list_ai_models(user: User = Depends(require_superuser), session: Session = Depends(get_session)) -> list[AiModelOut]:
    offered = await provider_models()
    chosen = {m.model_id: m.enabled for m in session.exec(select(AiModel)).all()}
    out = [AiModelOut(model_id=m, enabled=bool(chosen.get(m)), chat=is_chat_model(m), available=True) for m in offered]
    # abilitati in passato e spariti dal catalogo: visibili, cosi' si possono spegnere
    out += [AiModelOut(model_id=m, enabled=e, chat=is_chat_model(m), available=False)
            for m, e in sorted(chosen.items()) if m not in offered and e]
    return out


@router.put("/models/{model_id:path}", response_model=AiModelOut)
async def set_ai_model(
    model_id: str,
    body: AiModelUpdate,
    request: Request = None,  # type: ignore[assignment]
    user: User = Depends(require_superuser),
    session: Session = Depends(get_session),
) -> AiModelOut:
    offered = await provider_models()
    row = session.get(AiModel, model_id)
    if model_id not in offered and row is None:
        raise HTTPException(status_code=404, detail=f"Modello sconosciuto al provider: {model_id}")
    if body.enabled and not is_chat_model(model_id):
        raise HTTPException(status_code=422, detail="Questo modello non e' un modello di chat (embedding, audio o reranker)")
    if body.enabled and model_id not in offered:
        raise HTTPException(status_code=422, detail="Il provider non offre piu' questo modello")
    if row is None:
        row = AiModel(model_id=model_id)
    row.enabled = body.enabled
    row.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
    row.updated_by = user.id
    session.add(row)
    session.commit()
    audit.record_audit(
        session, actor=user, action=audit.AI_MODEL_ENABLE if body.enabled else audit.AI_MODEL_DISABLE,
        target_type="ai_model", target_label=model_id, request=request,
    )
    return AiModelOut(model_id=model_id, enabled=body.enabled, chat=is_chat_model(model_id), available=model_id in offered)


# ── chat ─────────────────────────────────────────────────────────────────────
class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=8000)
    model: str = Field(min_length=1, max_length=200)
    # La conversazione da proseguire. La storia la tiene il SERVER: il client
    # non la manda piu' (e quindi non puo' piu' contraffarla — audit A8).
    # Assente = nuova conversazione.
    chat_id: Optional[int] = None
    engine: Optional[str] = Field(default=None, max_length=40)
    locale: str = Field(default="en", max_length=10)
    # datasource messe a fuoco nella pagina: contesto per l'assistente, non un permesso
    focus: list[int] = Field(default_factory=list, max_length=8)


async def pick_engine(session: Session, requested: Optional[str]) -> Optional[str]:
    """Il motore delle query dell'assistente: uno DISPONIBILE sull'engine e
    CONSENTITO dall'amministratore. Se il client ne chiede uno che non lo e',
    l'errore dice quali si possono scegliere; se non ne chiede, si prende il
    primo utilizzabile (Polars, se c'e')."""
    allowed = allowed_engines(session)
    try:
        resp = await get_engine_client().get("/engines", timeout=15)
        resp.raise_for_status()
        usable = [e["id"] for e in resp.json() if e.get("available") and e.get("id") in allowed]
    except Exception:  # noqa: BLE001 — catalogo non leggibile: ci si fida della sola politica
        usable = sorted(allowed)
    if requested:
        if requested not in usable:
            raise HTTPException(status_code=422, detail=f"Motore non utilizzabile: {requested}. Disponibili: {', '.join(usable) or 'nessuno'}")
        return requested
    if not usable:
        raise HTTPException(status_code=422, detail="Nessun motore disponibile per l'assistente")
    return "polars" if "polars" in usable else usable[0]


def _sse(event: str, data: dict[str, Any]) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n".encode()


def _tool_result_payload(part: Any) -> dict[str, Any]:
    """Cio' che la chat mostra di un risultato: l'esito, e la TABELLA quando lo
    strumento ne ha restituita una (i numeri veri, non la trascrizione del modello)."""
    content = getattr(part, "content", None)
    ok = type(part).__name__ != "RetryPromptPart"
    out: dict[str, Any] = {"id": getattr(part, "tool_call_id", None), "name": getattr(part, "tool_name", None), "ok": ok}
    # una TABELLA e' il risultato di una query: `rows` e' una lista. (Anche
    # describe_datasource ha `rows`, ma li' e' il numero di righe.)
    if isinstance(content, dict) and isinstance(content.get("rows"), list) and isinstance(content.get("columns"), list) and "row_count" in content:
        out["table"] = {k: content.get(k) for k in ("datasource", "columns", "rows", "row_count", "truncated")}
    elif isinstance(content, dict) and content.get("error"):
        out["ok"], out["error"] = False, str(content["error"])[:500]
    elif isinstance(content, dict) and isinstance(content.get("datasources"), list):
        out["count"] = len(content["datasources"])
    elif isinstance(content, list):
        out["count"] = len(content)
    elif not ok:
        out["error"] = str(content)[:500]
    return out


# ── conversazioni salvate ────────────────────────────────────────────────────
@router.get("/chats")
def list_chats(user: User = Depends(get_current_user), session: Session = Depends(get_session)):
    """Le conversazioni di CHI CHIEDE, dalla piu' recente. Mai quelle di altri."""
    return ai_chats.elenco(session, user, get_settings().ai.max_chats_listed)


@router.get("/chats/{chat_id}")
def get_chat(chat_id: int, user: User = Depends(get_current_user), session: Session = Depends(get_session)):
    """Una conversazione intera, pronta da rimettere a schermo: domande,
    risposte, passi e costo di ogni turno."""
    return ai_chats.dettaglio(session, user, chat_id)


@router.delete("/chats/{chat_id}", status_code=204)
def delete_chat(chat_id: int, user: User = Depends(get_current_user), session: Session = Depends(get_session)):
    ai_chats.elimina(session, user, chat_id)


@router.post("/chat")
async def chat(
    body: ChatRequest,
    request: Request,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> StreamingResponse:
    from pydantic_ai import (
        AgentRunResultEvent, FunctionToolCallEvent, FunctionToolResultEvent, ModelMessagesTypeAdapter,
        PartDeltaEvent, PartStartEvent, TextPart, TextPartDelta,
    )
    from pydantic_ai.exceptions import ModelHTTPError, UsageLimitExceeded
    from pydantic_ai.usage import UsageLimits

    ensure_configured()
    cfg = get_settings().ai
    if body.model not in enabled_model_ids(session):
        raise HTTPException(status_code=403, detail="Modello non abilitato dall'amministratore")
    engine = await pick_engine(session, body.engine)
    # Conversazione NUOVA: la riga si crea quando il turno è finito (vedi
    # ai_chats.crea), così una richiesta interrotta non lascia una chat vuota.
    chat = ai_chats.apri(session, user, body.chat_id, body.model, engine) if body.chat_id else None
    chat_id = chat.id if chat is not None else None
    history = ai_chats.storia(session, chat, cfg.max_history_messages) if chat is not None else []

    deps = ai_agent.ChatDeps(
        user=user, session_factory=_new_session, engine=engine, model_id=body.model,
        locale=body.locale, request=request,
        focus=ai_agent.readable_focus(session, user, body.focus),
    )
    model = ai_agent.build_model(body.model)

    async def stream() -> AsyncIterator[bytes]:
        # la chat NUOVA nasce qui dentro, a turno finito
        nonlocal chat_id
        try:
            async with ai_agent.agent.run_stream_events(
                body.message, message_history=history, deps=deps, model=model,
                usage_limits=UsageLimits(
                    request_limit=cfg.max_requests,
                    # i giri non limitano i token: senza questi due, una storia
                    # gonfiata fa spendere quanto vuole (audit A8)
                    cost_limit=Decimal(str(cfg.max_cost_per_turn_usd)) if cfg.max_cost_per_turn_usd else None,
                    input_tokens_limit=cfg.max_input_tokens_per_turn,
                ),
            ) as events:
                async for ev in events:
                    if isinstance(ev, PartStartEvent) and isinstance(ev.part, TextPart):
                        if ev.part.content:
                            yield _sse("text", {"delta": ev.part.content})
                    elif isinstance(ev, PartDeltaEvent) and isinstance(ev.delta, TextPartDelta):
                        if ev.delta.content_delta:
                            yield _sse("text", {"delta": ev.delta.content_delta})
                    elif isinstance(ev, FunctionToolCallEvent):
                        try:
                            args = ev.part.args_as_dict()
                        except Exception:  # noqa: BLE001 — argomenti malformati: li vedra' lo strumento
                            args = {}
                        yield _sse("tool_call", {"id": ev.part.tool_call_id, "name": ev.part.tool_name, "args": args})
                    elif isinstance(ev, FunctionToolResultEvent):
                        yield _sse("tool_result", _tool_result_payload(ev.part))
                    elif isinstance(ev, AgentRunResultEvent):
                        usage = ev.result.usage
                        usage = usage() if callable(usage) else usage  # metodo nella 1.x, proprieta' nella 2.x
                        # pydantic-ai prezza sotto il PROVIDER: col nostro
                        # endpoint compatibile OpenAI molti modelli non si
                        # trovano. Il ripiego cerca per nome (vedi ai_pricing).
                        costo = ai_pricing.cost_of(usage, body.model)
                        # i messaggi NUOVI di questo turno: la storia precedente
                        # e' gia' salvata e non va riscritta
                        try:
                            nuovi = ev.result.new_messages()
                        except AttributeError:  # pragma: no cover — pydantic-ai piu' vecchia
                            nuovi = ev.result.all_messages()[len(history):]
                        turno = None
                        try:
                            # sessione PROPRIA: quella della richiesta e' gia'
                            # chiusa, lo stream vive piu' a lungo
                            with _new_session() as s_salva:
                                if chat_id is None:
                                    chat_id = ai_chats.crea(s_salva, user, body.message, body.model, engine).id
                                turno = ai_chats.salva_turno(
                                    s_salva, chat_id, domanda=body.message,
                                    messaggi=ModelMessagesTypeAdapter.dump_python(nuovi, mode="json"),
                                    model_id=body.model, input_tokens=usage.input_tokens,
                                    output_tokens=usage.output_tokens, requests=usage.requests, cost=costo,
                                )
                                riepilogo = ai_chats.riepilogo(s_salva, s_salva.get(AiChat, chat_id))
                        except Exception:  # noqa: BLE001 — la risposta e' gia' stata data: non si perde per un errore di salvataggio
                            logger.exception("assistente: turno non salvato (chat %s)", chat_id)
                            riepilogo = None
                        yield _sse("done", {
                            "chat_id": chat_id,
                            "seq": turno.seq if turno is not None else None,
                            "usage": {
                                "requests": usage.requests, "input_tokens": usage.input_tokens,
                                "output_tokens": usage.output_tokens,
                                "cost_usd": str(costo) if costo is not None else None,
                            },
                            "chat_total": riepilogo,
                        })
                        # Il titolo lo scrive il modello, ma DOPO aver chiuso il
                        # turno: farlo prima avrebbe tenuto la riga di stato
                        # accesa su «scrive la risposta» mentre scriveva il
                        # titolo. Il suo costo viene sommato al turno.
                        if turno is not None and turno.seq == 0:
                            _in_volo.add(asyncio.create_task(
                                _nomina_chat(chat_id, turno.id, body.model, body.message, body.locale)
                            ))
        except UsageLimitExceeded as e:
            # stesso evento per passi, token e costo: all'utente serve sapere che
            # la domanda era troppo grande, non quale contatore l'ha fermata
            logger.info("assistente: tetto d'uso raggiunto (chat %s): %s", chat_id, e)
            yield _sse("error", {"code": "too_many_steps", "message": "La domanda ha superato i limiti previsti per un singolo turno. Prova a restringerla."})
        except ModelHTTPError as e:
            logger.warning("assistente: il provider ha risposto %s per %s", e.status_code, body.model)
            yield _sse("error", {"code": "provider", "message": f"Il provider AI ha rifiutato la richiesta ({e.status_code})."})
        except Exception:  # noqa: BLE001 — lo stream e' gia' partito: l'errore va DENTRO lo stream
            logger.exception("assistente: errore durante la conversazione")
            yield _sse("error", {"code": "internal", "message": "Errore inatteso dell'assistente."})

    return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# I task di sfondo vanno tenuti per riferimento: asyncio ne conserva solo una
# reference debole, e senza questo il titolo sparirebbe a meta' a discrezione
# del garbage collector.
_in_volo: set = set()


async def _nomina_chat(chat_id: int, turno_id: int, model_id: str, domanda: str, locale: str) -> None:
    """Dà un nome alla conversazione appena nata, fuori dal turno dell'utente."""
    try:
        titolo, uso = await ai_agent.suggest_title(model_id, domanda, locale)
        with _new_session() as session:
            if titolo:
                ai_chats.rinomina(session, chat_id, titolo)
            ai_chats.aggiungi_costo(session, turno_id, ai_pricing.cost_of(uso, model_id) if uso else None)
    except Exception:  # noqa: BLE001 — accessorio: la conversazione resta col titolo di ripiego
        logger.info("assistente: chat %s non rinominata", chat_id, exc_info=True)
    finally:
        _in_volo.discard(asyncio.current_task())
