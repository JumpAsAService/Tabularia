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

from sqlalchemy import delete
from sqlmodel import Session, select

from app.core.config import get_settings
from app.core.engine_client import get_engine_client
from app.models.blob_deletion import PendingBlobDeletion

logger = logging.getLogger(__name__)

# Un task in coda che ha risolto la vecchia chiave viene fallito da _reconcile
# dopo il timeout stale del run; oltre questa finestra nessun run legge più il
# blob superato. Grace = quel tetto + margine (deve restare ≥ stale timeout).
BLOB_DELETION_GRACE_SECONDS = get_settings().engine.run_stale_timeout_seconds + 600


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
    # valori semplici, non oggetti: dopo ogni commit qui sotto gli oggetti
    # scadrebbero, e rileggere una riga che un altro processo ha già tolto è un errore
    due = [
        (r.id, r.bucket, r.key)
        for r in session.exec(select(PendingBlobDeletion).where(PendingBlobDeletion.delete_after <= now)).all()
    ]
    session.rollback()  # lettura finita: niente transazione aperta durante le chiamate all'engine
    if not due:
        return 0
    client = get_engine_client()
    removed = 0
    for row_id, bucket, key in due:
        try:
            resp = await client.delete("/files/object", params={"bucket": bucket, "key": key})
        except Exception as e:  # engine irraggiungibile → riprova al prossimo tick
            logger.warning("sweep blob %s/%s rimandato: %s", bucket, key, e)
            continue
        if resp.status_code < 400 or resp.status_code == 404:
            # Per id e non `session.delete(riga)`: con più processi che fanno la
            # stessa passata la riga può averla già tolta un altro, e l'ORM lo
            # tratterebbe come un errore (cancellare due volte lo stesso blob è
            # innocuo: la seconda trova 404, che conta come fatto). E commit
            # SUBITO: la DELETE tiene un lucchetto sulla riga finché non si
            # conferma, e tenerlo per le chiamate all'engine che seguono fermerebbe
            # un altro processo arrivato alla stessa riga per tutto quel tempo.
            tolta = session.exec(delete(PendingBlobDeletion).where(PendingBlobDeletion.id == row_id))
            session.commit()
            removed += tolta.rowcount or 0
        else:
            logger.warning("sweep blob %s/%s non eliminato (%s): riprovo", bucket, key, resp.status_code)
    return removed
