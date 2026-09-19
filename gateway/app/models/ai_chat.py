"""Conversazioni dell'assistente, salvate nel database dei metadati.

Prima la storia viveva SOLO nel browser: il client la rimandava a ogni turno e
il server la accettava così com'era. Due conseguenze, entrambe risolte qui:
chiudere la scheda buttava via la conversazione (e rifare la stessa domanda
costa di nuovo), e una storia contraffatta arrivava intatta al modello —
comprese parti di ruolo «system», cioè istruzioni (audit 2026-09-19, A8).
Ora la storia la tiene il server e il client manda solo l'id della chat.

Un TURNO = una domanda dell'utente e tutto ciò che ne è seguito (risposte
intermedie, chiamate agli strumenti, risposta finale). È l'unità giusta anche
per il costo: `pydantic-ai` riporta il costo dell'intero giro, non del singolo
messaggio. Rigiocare una conversazione significa concatenare i messaggi dei
turni in ordine di `seq`.
"""
from datetime import datetime, timezone
from typing import Optional

from sqlmodel import Field, SQLModel


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)  # naive-UTC, come il resto


class AiChat(SQLModel, table=True):
    __tablename__ = "ai_chats"

    id: Optional[int] = Field(default=None, primary_key=True)
    # niente FK verso users: una chat sopravvive alla cancellazione dell'account
    # solo se lo decidiamo noi, e oggi NON lo vogliamo — vedi routes/users.py,
    # che la cancella esplicitamente insieme all'utente
    user_id: int = Field(index=True)
    # prima domanda accorciata: è il titolo che l'utente riconosce nell'elenco
    title: str = ""
    # ultimi usati, per riaprire la chat com'era
    model_id: str = ""
    engine: Optional[str] = None
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now, index=True)


class AiChatTurn(SQLModel, table=True):
    """Una domanda e la sua risposta, con quanto è costata."""

    __tablename__ = "ai_chat_turns"

    id: Optional[int] = Field(default=None, primary_key=True)
    chat_id: int = Field(index=True, foreign_key="ai_chats.id")
    seq: int = 0  # ordine di rigioco
    question: str = ""
    # i messaggi NUOVI di questo turno, serializzati da pydantic-ai
    # (`ModelMessagesTypeAdapter.dump_python(..., mode="json")`)
    messages: str = "[]"
    model_id: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    requests: int = 0
    # Costo in dollari COME LO RIPORTA pydantic-ai (`RunUsage.cost`), che lo
    # ricava dal listino del modello. Stringa e non float: è un decimale esatto
    # e sommarlo in virgola mobile introdurrebbe errore. `None` = il costo non
    # è determinabile per quel modello, che è diverso da «zero».
    cost_usd: Optional[str] = None
    created_at: datetime = Field(default_factory=_now)
