from datetime import datetime, timezone
from typing import Optional

from sqlmodel import SQLModel, Field
from sqlalchemy import Column, Text


def _now() -> datetime:
    return datetime.now(timezone.utc)


class AuditLog(SQLModel, table=True):
    """Registro di audit: chi ha fatto cosa, quando, da dove.

    Append-only: ogni evento significativo (login, CRUD flussi/datasource/
    connessioni, run, download, cambi di permesso…) è una riga. Gli identificativi
    dell'attore e del bersaglio sono SNAPSHOT testuali (`actor_label`,
    `target_label`) oltre agli id, così l'audit resta leggibile anche se l'oggetto
    referenziato viene poi rinominato o eliminato.
    """
    __tablename__ = "audit_logs"

    id: Optional[int] = Field(default=None, primary_key=True)
    ts: datetime = Field(default_factory=_now, index=True)

    actor_id: Optional[int] = Field(default=None, foreign_key="users.id", index=True)
    actor_label: str = ""  # email/nome al momento dell'azione (sopravvive alla cancellazione)

    action: str = Field(index=True)  # es. auth.login, flow.create, export.download
    outcome: str = "success"         # success | failure

    target_type: Optional[str] = Field(default=None, index=True)  # flow | datasource | connection | user | permission | export
    target_id: Optional[int] = None
    target_label: Optional[str] = None

    # contesto extra (JSON): formato export, righe, engine, cron, motivo errore…
    detail: Optional[str] = Field(default=None, sa_column=Column(Text))
    ip: Optional[str] = None
    user_agent: Optional[str] = Field(default=None, sa_column=Column(Text))
