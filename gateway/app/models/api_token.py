"""Un token API: la credenziale di uno script, di un'integrazione o di un account
di servizio, da mettere nell'intestazione `Authorization: Bearer tab_…`.

Del token si tiene solo l'impronta (SHA-256): il segreto si mostra una volta,
alla creazione, e poi non lo conosce più nessuno, gateway compreso. Ha sempre una
scadenza. Vale con i permessi del suo utente, e al più in lettura se è di sola
lettura (`scope = "read"`): ogni richiesta che modifica qualcosa è respinta.
Revocarlo è cancellarne la riga, come per le sessioni.
"""
from datetime import datetime
from typing import Optional

from sqlmodel import Field, SQLModel


class ApiToken(SQLModel, table=True):
    __tablename__ = "api_tokens"
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.id", index=True)
    name: str = Field(max_length=80)
    # l'inizio del token (`tab_` + 8 caratteri): basta a riconoscerlo in un elenco
    # o in un log, non ad usarlo
    prefix: str = Field(max_length=16)
    token_hash: str = Field(max_length=64, unique=True, index=True)
    scope: str = Field(default="read", max_length=8)  # "read" | "write"
    # tutte naive UTC, come le altre colonne
    created_at: datetime
    created_by: Optional[int] = Field(default=None, foreign_key="users.id")
    expires_at: datetime = Field(index=True)
    last_used_at: Optional[datetime] = None
    last_used_ip: Optional[str] = Field(default=None, max_length=64)
