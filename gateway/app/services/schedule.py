"""Schedule del refresh delle datasource database: espressione cron standard a
5 campi (`minuto ora giorno-mese mese giorno-settimana`), es. `0 3 * * *`
(ogni notte alle 3), `*/15 * * * *` (ogni 15 minuti), `0 * * * *` (ogni ora).

Parsing e calcolo del prossimo run via `croniter` (collaudata: gestisce range,
step, liste e la semantica OR tra giorno-del-mese e giorno-della-settimana).

Le espressioni cron sono interpretate nel FUSO DEL DEPLOYMENT (APP__TIMEZONE):
'0 3 * * *' = le 03:00 a PARETE locale (DST incluso), non le 03:00 UTC. Il
risultato è però sempre restituito in UTC (storage e confronti in UTC).
"""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from croniter import croniter

from app.core.config import get_settings


class ScheduleError(ValueError):
    """Espressione cron non valida (messaggio mostrabile all'utente)."""


def validate_schedule(spec: str) -> str:
    """Valida e normalizza l'espressione cron. Solleva ScheduleError."""
    if not spec or not isinstance(spec, str) or not spec.strip():
        raise ScheduleError("Espressione cron vuota")
    spec = spec.strip()
    if not croniter.is_valid(spec):
        raise ScheduleError(
            f"Espressione cron non valida: '{spec}' "
            "(formato: 'minuto ora giorno mese giorno-settimana', es. '0 3 * * *')"
        )
    return spec


def next_fire(spec: str, after: datetime, tz: ZoneInfo | None = None) -> datetime:
    """Prossimo istante di esecuzione STRETTAMENTE dopo `after`, restituito in UTC.

    Il cron è valutato nel fuso `tz` (default: APP__TIMEZONE) così l'orario è a
    parete locale: '0 3 * * *' fira alle 03:00 locali anche attraverso il cambio
    d'ora legale. `after` naive è assunto UTC (come i timestamp del DB)."""
    validate_schedule(spec)
    if tz is None:
        tz = get_settings().app.tzinfo()
    if after.tzinfo is None:
        after = after.replace(tzinfo=timezone.utc)
    # base nel fuso locale → croniter calcola l'orario a parete in quel fuso
    nxt_local = croniter(spec, after.astimezone(tz)).get_next(datetime)
    return nxt_local.astimezone(timezone.utc)
