from datetime import datetime, timezone
from typing import Optional
from sqlmodel import SQLModel, Field


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Upload(SQLModel, table=True):
    """
    Registro dei file caricati via `POST /files`: chi ha caricato quale oggetto.

    Serve al controllo RBAC sul data plane: un upload appena fatto non vive in
    nessun progetto, quindi è leggibile SOLO dal proprietario; entra nella
    sfera dei permessi di progetto quando un flusso salvato lo referenzia
    (vedi services/objects.py).
    """
    __tablename__ = "uploads"

    id: Optional[int] = Field(default=None, primary_key=True)
    dataset_id: str = Field(index=True)
    bucket: str
    parquet_key: str = Field(index=True)
    raw_key: str = ""
    filename: str = ""
    owner_id: Optional[int] = Field(default=None, foreign_key="users.id", index=True)
    created_at: datetime = Field(default_factory=_now)
