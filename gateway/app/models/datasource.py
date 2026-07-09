from datetime import datetime, timezone
from typing import Optional
from sqlmodel import SQLModel, Field
from sqlalchemy import Column, Text, UniqueConstraint


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Datasource(SQLModel, table=True):
    """
    Un dataset NOMINATO nel catalogo: un parquet nello storage con nome, cartella
    e permessi (ereditati dal progetto, come i flussi). Oggi nasce dalla
    pubblicazione dell'output di un run; domani anche da upload e connessioni DB
    (campo `kind`).
    """
    __tablename__ = "datasources"
    __table_args__ = (UniqueConstraint("project_id", "name", name="uq_datasource_project_name"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    name: str
    description: str = ""
    project_id: int = Field(foreign_key="projects.id", index=True)
    owner_id: Optional[int] = Field(default=None, foreign_key="users.id")

    bucket: str
    key: str  # parquet nello storage (datasets/…)
    rows: Optional[int] = None
    columns: str = Field(default="[]", sa_column=Column(Text, nullable=False))  # JSON [{name, dtype}]

    kind: str = "flow"  # flow | database (| upload, futuro)
    flow_id: Optional[int] = Field(default=None, foreign_key="flows.id")  # provenienza

    # per kind="database": la definizione della sorgente e l'ultimo refresh.
    # Il parquet è uno SNAPSHOT: refresh = nuovo ingest che sostituisce il blob.
    connection_id: Optional[int] = Field(default=None, foreign_key="connections.id")
    source_type: Optional[str] = None  # table | sql
    source_ref: Optional[str] = Field(default=None, sa_column=Column(Text))
    refreshed_at: Optional[datetime] = None

    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)
