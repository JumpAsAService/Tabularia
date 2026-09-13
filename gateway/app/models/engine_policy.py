"""Motori che l'amministratore ha DISABILITATO per l'installazione.

Si registra ciò che è vietato, non ciò che è permesso: a tabella vuota tutto
funziona esattamente come prima, quindi aggiornare un'installazione esistente
non cambia il comportamento di nessuno. È la stessa idea dei banner — «cosa c'è
in elenco» è esattamente «cosa cambia», senza stati intermedi da ricordare.

Disabilitare un motore NON ferma i flussi che già lo usano: vieta soltanto di
sceglierlo da qui in avanti. La scelta è deliberata — un interruttore nel
pannello admin non deve poter fermare un DAG schedulato nel momento in cui
viene premuto. I flussi rimasti indietro si vedono nel pannello, e si migrano
a mano (vedi routes/engine_policy.py).
"""
from datetime import datetime, timezone
from typing import Optional

from sqlmodel import Field, SQLModel


def _now() -> datetime:
    return datetime.now(timezone.utc)


class DisabledEngine(SQLModel, table=True):
    __tablename__ = "disabled_engines"

    # l'id del motore nel catalogo ("polars", "duckdb", …) È la chiave: non
    # serve un id sintetico, e l'unicità è garantita dalla chiave primaria
    # invece che da un vincolo a parte.
    engine_id: str = Field(primary_key=True)
    disabled_at: datetime = Field(default_factory=_now)
    disabled_by: Optional[int] = Field(default=None, foreign_key="users.id")
