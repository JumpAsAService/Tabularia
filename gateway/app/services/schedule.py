"""Schedule del refresh delle datasource database: espressione cron standard a
5 campi (`minuto ora giorno-mese mese giorno-settimana`), es. `0 3 * * *`
(ogni notte alle 3), `*/15 * * * *` (ogni 15 minuti), `0 * * * *` (ogni ora).

Parsing e calcolo del prossimo run via `croniter` (collaudata: gestisce range,
step, liste e la semantica OR tra giorno-del-mese e giorno-della-settimana).
Tutti i tempi in UTC.
"""
from __future__ import annotations

from datetime import datetime, timezone

from croniter import croniter


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


def next_fire(spec: str, after: datetime) -> datetime:
    """Prossimo istante di esecuzione STRETTAMENTE dopo `after` (UTC)."""
    validate_schedule(spec)
    if after.tzinfo is None:
        after = after.replace(tzinfo=timezone.utc)
    itr = croniter(spec, after.astimezone(timezone.utc))
    return itr.get_next(datetime)
