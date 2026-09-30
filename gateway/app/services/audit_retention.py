"""Cancellazione periodica del registro delle azioni, quando è configurata.

Il registro è append-only per scelta: nessuna rotta lo cancella, e cancellare un
account non cancella quello che ha fatto. Ma «per sempre» non è una politica di
conservazione, è l'assenza di una politica — e su un'installazione aperta a
sconosciuti significa tenersi addosso dati altrui senza una ragione che scada.

Qui c'è l'unica cancellazione ammessa: una finestra dichiarata, uguale per tutti,
applicata dall'orologio e non da qualcuno. Spenta di default (0 = per sempre):
accendere la cancellazione di un registro di sicurezza deve essere una decisione
di chi installa, mai un effetto collaterale di un aggiornamento.

NON tocca `ai_spend`: lì non c'è testo di nessuno, e su quelle righe poggia il
tetto di spesa giornaliero — cancellarle lo azzererebbe.
"""
import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import text
from sqlmodel import Session

from app.core.config import get_settings
from app.db.session import engine

logger = logging.getLogger(__name__)

# Si guarda una volta al minuto: la finestra è in minuti, quindi una riga vive al
# più un minuto più del dovuto. Più spesso non servirebbe a niente.
TICK_SECONDS = 60
# quante righe per giro: un primo passaggio su un registro vecchio di mesi
# cancellerebbe altrimenti tutto in una transazione sola, tenendo il lucchetto
# mentre l'applicazione lavora
LOTTO = 5_000
MAX_LOTTI_PER_GIRO = 20


def purga(session: Session, minuti: int, ora: datetime | None = None) -> int:
    """Cancella le voci più vecchie della finestra. Torna quante ne ha tolte."""
    if minuti <= 0:
        return 0
    adesso = ora or datetime.now(timezone.utc).replace(tzinfo=None)
    taglio = adesso - timedelta(minutes=minuti)
    tolte = 0
    for _ in range(MAX_LOTTI_PER_GIRO):
        # `IN (SELECT ... LIMIT)` perché DELETE non prende un LIMIT suo
        res = session.exec(
            text("DELETE FROM audit_logs WHERE id IN "
                 "(SELECT id FROM audit_logs WHERE ts < :taglio LIMIT :lotto)")
            .bindparams(taglio=taglio, lotto=LOTTO)
        )
        session.commit()
        n = res.rowcount or 0
        tolte += n
        if n < LOTTO:
            break
    return tolte


async def retention_loop(stop: asyncio.Event) -> None:
    minuti = get_settings().audit.retention_minutes
    if minuti <= 0:
        logger.info("audit: nessuna scadenza configurata, il registro si conserva per intero")
        return
    logger.info("audit: le voci più vecchie di %s minuti verranno cancellate (controllo ogni %ss)",
                minuti, TICK_SECONDS)
    while not stop.is_set():
        try:
            with Session(engine) as session:
                tolte = purga(session, minuti)
            if tolte:
                logger.info("audit: cancellate %s voci oltre la finestra di %s minuti", tolte, minuti)
        except Exception:  # noqa: BLE001 — la pulizia non deve mai fermare il gateway
            logger.exception("audit: pulizia non riuscita")
        try:
            await asyncio.wait_for(stop.wait(), timeout=TICK_SECONDS)
        except asyncio.TimeoutError:
            pass
    logger.info("audit: pulizia fermata")
