from datetime import datetime, timezone
from typing import Optional
from sqlmodel import SQLModel, Field
from sqlalchemy import Column, Text


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Flow(SQLModel, table=True):
    """
    Un flusso salvato (il DAG dell'editor). Vive dentro un progetto/cartella e
    ne eredita i permessi: VIEW per vederlo/aprirlo, EDIT per salvarlo/spostarlo/
    eliminarlo. `definition` è il JSON serializzato del canvas (nodi+archi).
    """
    __tablename__ = "flows"
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str
    description: str = ""
    project_id: int = Field(foreign_key="projects.id", index=True)
    owner_id: Optional[int] = Field(default=None, foreign_key="users.id")
    definition: str = Field(default="{}", sa_column=Column(Text, nullable=False))

    # motore di esecuzione scelto alla CREAZIONE (persistito): "polars" (default)
    # o "duckdb" (in arrivo). Passato all'engine in preview/run.
    engine: str = Field(default="polars")

    # esecuzione SCHEDULATA (cron): lo scheduler del gateway ri-risolve la
    # definizione CORRENTE e lancia i nodi Output con l'autorità di
    # `run_scheduled_by`. schedule=None → disabilitato.
    run_schedule: Optional[str] = None
    run_scheduled_by: Optional[int] = Field(default=None, foreign_key="users.id")
    next_run_at: Optional[datetime] = Field(default=None, index=True)

    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)
