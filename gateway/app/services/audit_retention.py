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

The same loop empties the client addresses past AUDIT__IP_RETENTION_DAYS (on by
default, 90 days: services/ip_retention.py). That one does not delete a row.
"""
import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import text
from sqlmodel import Session

from app.core.config import Settings, get_settings
from app.db.session import engine
from app.services import ip_retention

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


# client addresses are kept in days: looking once an hour is plenty
IP_EVERY_SECONDS = 3600


def da_fare(settings: Settings) -> bool:
    """Whether the loop has any work: the whole-row window or the address window."""
    return settings.audit.retention_minutes > 0 or settings.audit.ip_retention_days > 0


def un_giro(session: Session, settings: Settings, ora: datetime | None = None, indirizzi: bool = True) -> None:
    """One round: the rows past AUDIT__RETENTION_MINUTES, then (when `indirizzi`)
    the client addresses past AUDIT__IP_RETENTION_DAYS (see services/ip_retention.py)."""
    minuti = settings.audit.retention_minutes
    tolte = purga(session, minuti, ora=ora)
    if tolte:
        logger.info("audit: cancellate %s voci oltre la finestra di %s minuti", tolte, minuti)
    if indirizzi and settings.audit.ip_retention_days > 0:
        svuotate = ip_retention.cancella_indirizzi(session, settings.audit.ip_retention_days, ora=ora)
        if svuotate:
            logger.info("audit: client address and browser emptied on %s rows older than %s days",
                        svuotate, settings.audit.ip_retention_days)


def _giro_con_sessione(settings: Settings, indirizzi: bool) -> None:
    with Session(engine) as session:
        un_giro(session, settings, None, indirizzi)


async def retention_loop(stop: asyncio.Event) -> None:
    settings = get_settings()
    if not da_fare(settings):
        logger.info("audit: nessuna scadenza configurata, il registro si conserva per intero")
        return
    if settings.audit.retention_minutes > 0:
        logger.info("audit: le voci più vecchie di %s minuti verranno cancellate (controllo ogni %ss)",
                    settings.audit.retention_minutes, TICK_SECONDS)
    if settings.audit.ip_retention_days > 0:
        logger.info("audit: client addresses older than %s days will be emptied (checked every %ss)",
                    settings.audit.ip_retention_days, IP_EVERY_SECONDS)
    ultimo_indirizzi = 0.0
    while not stop.is_set():
        adesso = time.monotonic()
        indirizzi = not ultimo_indirizzi or adesso - ultimo_indirizzi >= IP_EVERY_SECONDS
        try:
            await asyncio.to_thread(_giro_con_sessione, settings, indirizzi)
            if indirizzi:
                ultimo_indirizzi = adesso
        except Exception:  # noqa: BLE001 — la pulizia non deve mai fermare il gateway
            logger.exception("audit: pulizia non riuscita")
        try:
            await asyncio.wait_for(stop.wait(), timeout=TICK_SECONDS)
        except asyncio.TimeoutError:
            pass
    logger.info("audit: pulizia fermata")
