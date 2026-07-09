from datetime import datetime, timezone
from typing import Optional
from sqlmodel import SQLModel, Field
from sqlalchemy import Column, Text


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Run(SQLModel, table=True):
    """
    Un'esecuzione di un flusso: la riga nasce al lancio (PENDING) e viene
    riconciliata pigramente con lo stato del task Celery ogni volta che
    qualcuno la legge (nessun poller in background: chi guarda, aggiorna).

    Se al lancio è stata chiesta la pubblicazione (`publish_name`), al passaggio
    a SUCCESS l'output diventa una Datasource riusabile come sorgente.
    """
    __tablename__ = "runs"
    id: Optional[int] = Field(default=None, primary_key=True)
    flow_id: int = Field(foreign_key="flows.id", index=True)
    task_id: str = Field(index=True)  # id del task Celery sull'engine
    status: str = "PENDING"  # PENDING | STARTED | SUCCESS | FAILURE
    launched_by: Optional[int] = Field(default=None, foreign_key="users.id")

    input_key: str
    output_bucket: str
    output_key: str
    rows_written: Optional[int] = None
    error: Optional[str] = Field(default=None, sa_column=Column(Text))

    # richiesta di pubblicazione (facoltativa, decisa al lancio)
    publish_name: Optional[str] = None
    publish_project_id: Optional[int] = Field(default=None, foreign_key="projects.id")
    publish_description: str = ""
    datasource_id: Optional[int] = Field(default=None, foreign_key="datasources.id")

    started_at: datetime = Field(default_factory=_now)
    finished_at: Optional[datetime] = None


TERMINAL_STATES = {"SUCCESS", "FAILURE"}
