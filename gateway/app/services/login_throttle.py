"""Freno sui tentativi di accesso ripetuti.

L'unico costo di un tentativo sbagliato era bcrypt: 25 tentativi in 6,7 secondi,
misurati durante l'audit del 2026-09-19 (M5). Su un indirizzo pubblico — una
demo, un link in un post — le credenziali finiscono in un commento e il resto lo
fa uno script.

Il conteggio sta IN MEMORIA, e va detto perché è una scelta con un limite: il
gateway è singleton (una replica, strategia `Recreate`, vedi la chart), quindi un
solo processo vede tutti i tentativi; un riavvio azzera i contatori. Con più
repliche servirebbe Valkey. Per quello che deve fare — rendere inutile un
dizionario, non resistere a una botnet — questo basta e non aggiunge dipendenze.

Si conta per **(indirizzo IP, email)** insieme: per solo IP, un ufficio dietro
un NAT si bloccherebbe a vicenda; per sola email, basta cambiare account per
ricominciare. L'accesso riuscito azzera, così chi sbaglia due volte e poi entra
non si porta dietro niente.
"""
from __future__ import annotations

import logging
import time
from threading import Lock

logger = logging.getLogger(__name__)

# dopo quanti errori si comincia a rallentare, e per quanto
SOGLIA = 5
FINESTRA_SECONDI = 300.0
BLOCCO_SECONDI = 60.0
BLOCCO_MASSIMO = 900.0
_MAX_CHIAVI = 10_000  # tetto di memoria: oltre, si buttano le voci scadute

_lock = Lock()
_tentativi: dict[tuple[str, str], tuple[int, float]] = {}


def _chiave(ip: str | None, email: str) -> tuple[str, str]:
    return (ip or "?", (email or "").strip().lower())


def _pulisci(adesso: float) -> None:
    scaduti = [k for k, (_, ultimo) in _tentativi.items() if adesso - ultimo > FINESTRA_SECONDI]
    for k in scaduti:
        _tentativi.pop(k, None)


def attesa_richiesta(ip: str | None, email: str) -> float:
    """Secondi da aspettare prima di poter riprovare. `0` = si può procedere."""
    adesso = time.monotonic()
    with _lock:
        falliti, ultimo = _tentativi.get(_chiave(ip, email), (0, 0.0))
    if falliti < SOGLIA or adesso - ultimo > FINESTRA_SECONDI:
        return 0.0
    # raddoppia a ogni errore oltre la soglia, con un tetto
    blocco = min(BLOCCO_SECONDI * (2 ** (falliti - SOGLIA)), BLOCCO_MASSIMO)
    rimasti = blocco - (adesso - ultimo)
    return max(0.0, rimasti)


def registra_errore(ip: str | None, email: str) -> None:
    adesso = time.monotonic()
    with _lock:
        if len(_tentativi) > _MAX_CHIAVI:
            _pulisci(adesso)
        falliti, ultimo = _tentativi.get(_chiave(ip, email), (0, 0.0))
        if adesso - ultimo > FINESTRA_SECONDI:
            falliti = 0
        _tentativi[_chiave(ip, email)] = (falliti + 1, adesso)


def registra_successo(ip: str | None, email: str) -> None:
    with _lock:
        _tentativi.pop(_chiave(ip, email), None)


def azzera() -> None:
    """Solo per i test: i contatori sono di processo e sopravvivono fra un test e l'altro."""
    with _lock:
        _tentativi.clear()
