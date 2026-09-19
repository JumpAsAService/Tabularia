"""Modelli AI che l'amministratore consente di usare nell'assistente.

Regola opposta a quella dei motori: si memorizzano i modelli ABILITATI.
Tabella vuota = nessun modello = assistente non utilizzabile. Un modello costa
a ogni messaggio e il catalogo del provider cambia da solo: un modello nuovo
non deve diventare usabile senza che qualcuno l'abbia deciso.
"""
from datetime import datetime, timezone
from typing import Optional

from sqlmodel import Field, SQLModel


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class AiModel(SQLModel, table=True):
    __tablename__ = "ai_models"

    # l'id del modello presso il provider ("gpt-oss-120b", …) E' la chiave
    model_id: str = Field(primary_key=True)
    enabled: bool = True
    updated_at: datetime = Field(default_factory=_now)
    updated_by: Optional[int] = Field(default=None, foreign_key="users.id")
