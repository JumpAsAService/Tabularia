"""Cancellazione DIFFERITA dei blob dello storage (grace period).

Uno snapshot superato / un parquet rimpiazzato / il blob di una datasource
eliminata non si cancellano subito: un run o una preview in corso potrebbero
averne già risolto la chiave e stare per leggerli (è la corsa che causava il 404
sotto lettura). Si registra la cancellazione con una grace OLTRE la vita massima
di un run e lo sweep dello scheduler la esegue quando scade.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlmodel import Session, select

from app.core.engine_client import get_engine_client
from app.models.blob_deletion import PendingBlobDeletion

logger = logging.getLogger(__name__)

# Un task in coda che ha risolto la vecchia chiave viene fallito da _reconcile
# dopo STALE_AFTER_SECONDS (3900); oltre questa finestra nessun run legge più il
# blob superato. Grace = quel tetto + margine.
BLOB_DELETION_GRACE_SECONDS = 3900 + 600  # ~75 min


def schedule_blob_deletion(
    session: Session, bucket: str, key: str, reason: str = "",
    grace_seconds: int = BLOB_DELETION_GRACE_SECONDS,
) -> None:
    """Marca un blob per la cancellazione differita (dopo la grace). Aggiunge alla
    sessione ma NON committa: il commit spetta al chiamante, così la marcatura è
    atomica con lo swap / il publish che ha reso il blob obsoleto."""
    if not key:
        return
    session.add(
        PendingBlobDeletion(
            bucket=bucket,
            key=key,
            reason=reason,
            delete_after=datetime.now(timezone.utc) + timedelta(seconds=grace_seconds),
        )
    )


async def sweep_blob_deletions(session: Session, now: datetime | None = None) -> int:
    """Elimina i blob la cui grace è scaduta (chiamato dal tick dello scheduler).
    Best-effort: se l'engine non conferma la cancellazione la riga resta e si
    riprova al giro dopo; un 404 (già sparito) conta come fatto."""
    now = now or datetime.now(timezone.utc)
    due = session.exec(
        select(PendingBlobDeletion).where(PendingBlobDeletion.delete_after <= now)
    ).all()
    if not due:
        return 0
    client = get_engine_client()
    removed = 0
    for row in due:
        try:
            resp = await client.delete(
                "/files/object", params={"bucket": row.bucket, "key": row.key}
            )
        except Exception as e:  # engine irraggiungibile → riprova al prossimo tick
            logger.warning("sweep blob %s/%s rimandato: %s", row.bucket, row.key, e)
            continue
        if resp.status_code < 400 or resp.status_code == 404:
            session.delete(row)
            removed += 1
        else:
            logger.warning(
                "sweep blob %s/%s non eliminato (%s): riprovo", row.bucket, row.key, resp.status_code
            )
    session.commit()
    return removed
