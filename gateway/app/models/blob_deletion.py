from datetime import datetime, timezone
from typing import Optional

from sqlmodel import Field, SQLModel


def _now() -> datetime:
    return datetime.now(timezone.utc)


class PendingBlobDeletion(SQLModel, table=True):
    """Blob dello storage da eliminare in DIFFERITA.

    Uno snapshot superato da un refresh, un parquet rimpiazzato da un overwrite,
    o il blob di una datasource eliminata NON vanno cancellati subito: un run o
    una preview in corso potrebbero averne risolto la chiave e stare ancora per
    leggerli. Si registrano qui con `delete_after` = ora + grace (oltre la vita
    massima di un run) e lo sweep dello scheduler li elimina quando scade.
    """

    __tablename__ = "pending_blob_deletions"

    id: Optional[int] = Field(default=None, primary_key=True)
    bucket: str
    key: str = Field(index=True)
    delete_after: datetime = Field(index=True)
    reason: str = ""
    created_at: datetime = Field(default_factory=_now)
