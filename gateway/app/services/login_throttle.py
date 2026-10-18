"""Freno sui tentativi di accesso ripetuti.

L'unico costo di un tentativo sbagliato era bcrypt: 25 tentativi in 6,7 secondi,
misurati durante l'audit del 2026-09-19 (M5). Su un indirizzo pubblico — una
demo, un link in un post — le credenziali finiscono in un commento e il resto lo
fa uno script.

Il conteggio sta in una TABELLA (`login_attempts`), non in memoria: con più
repliche del gateway un dizionario per processo conterebbe cinque errori per
replica, e il freno si allenterebbe proprio quando l'installazione cresce. Costa
una lettura per chiave a ogni accesso, che è raro. Per quello che deve fare —
rendere inutile un dizionario, non resistere a una botnet — basta.

Si conta per **(indirizzo IP, email)** insieme: per solo IP, un ufficio dietro
un NAT si bloccherebbe a vicenda; per sola email, basta cambiare account per
ricominciare. L'accesso riuscito azzera, così chi sbaglia due volte e poi entra
non si porta dietro niente.

A second, looser count is kept per address alone (any email): one address trying
one password on many accounts (spraying), or probing which emails exist, never
reached five errors on any single (address, email) key. Its threshold is high
enough for an office behind one NAT, and a successful sign-in does not clear it —
otherwise one valid account would be enough to keep spraying the others.

The address is the one the server resolved (see `audit.client_ip`): a client
writing its own X-Forwarded-For does not get a fresh key per request.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import case, delete, update
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session

from app.models.shared_state import LoginAttempt

logger = logging.getLogger(__name__)

# dopo quanti errori si comincia a rallentare, e per quanto
SOGLIA = 5
FINESTRA_SECONDI = 300.0
BLOCCO_SECONDI = 60.0
BLOCCO_MASSIMO = 900.0
# failures from one address, whatever the email, before that address slows down
SOGLIA_INDIRIZZO = 30
# the email of the per-address row: emails are stripped, so no login can produce it
TUTTE = " *"


def _chiave(ip: str | None, email: str) -> tuple[str, str]:
    return (ip or "?", (email or "").strip().lower())


def _attesa(session: Session, chiave: tuple[str, str], soglia: int) -> float:
    riga = session.get(LoginAttempt, chiave)
    if riga is None:
        return 0.0
    falliti, da = riga.failures, (_adesso() - riga.last_at).total_seconds()
    if falliti < soglia or da > FINESTRA_SECONDI:
        return 0.0
    # raddoppia a ogni errore oltre la soglia, con un tetto
    blocco = min(BLOCCO_SECONDI * (2 ** (falliti - soglia)), BLOCCO_MASSIMO)
    return max(0.0, blocco - da)


def _adesso() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)  # colonne naive = UTC


def attesa_richiesta(session: Session, ip: str | None, email: str) -> float:
    """Secondi da aspettare prima di poter riprovare. `0` = si può procedere."""
    chiave = _chiave(ip, email)
    try:
        return max(_attesa(session, chiave, SOGLIA), _attesa(session, (chiave[0], TUTTE), SOGLIA_INDIRIZZO))
    finally:
        session.rollback()  # solo una lettura: non si tiene aperta la transazione


def registra_errore(session: Session, ip: str | None, email: str) -> None:
    """Un errore in più, per (indirizzo, email) e per l'indirizzo intero."""
    chiave_ip, chiave_email = _chiave(ip, email)
    _conta(session, chiave_ip, chiave_email)
    _conta(session, chiave_ip, TUTTE)


def _conta(session: Session, chiave_ip: str, chiave_email: str) -> None:
    """L'incremento lo fa il database (`failures + 1`), così due repliche che
    registrano insieme non se ne perdono uno."""
    adesso = _adesso()
    scaduta = adesso - timedelta(seconds=FINESTRA_SECONDI)

    def incrementa() -> int:
        esito = session.exec(
            update(LoginAttempt)
            .where(LoginAttempt.ip == chiave_ip, LoginAttempt.email == chiave_email)
            .values(
                # oltre la finestra si ricomincia da capo
                failures=case((LoginAttempt.last_at < scaduta, 1), else_=LoginAttempt.failures + 1),
                last_at=adesso,
            )
        )
        session.commit()
        return esito.rowcount

    if incrementa():
        return
    try:
        session.add(LoginAttempt(ip=chiave_ip, email=chiave_email, failures=1, last_at=adesso))
        session.commit()
    except IntegrityError:  # un'altra replica ha scritto il primo errore un attimo prima
        session.rollback()
        incrementa()


def registra_successo(session: Session, ip: str | None, email: str) -> None:
    chiave_ip, chiave_email = _chiave(ip, email)
    session.exec(delete(LoginAttempt).where(LoginAttempt.ip == chiave_ip, LoginAttempt.email == chiave_email))
    session.commit()


def pulisci(session: Session) -> int:
    """Toglie i conteggi che non frenano più nessuno (lo chiama lo scheduler)."""
    tolte = session.exec(
        delete(LoginAttempt).where(LoginAttempt.last_at < _adesso() - timedelta(seconds=FINESTRA_SECONDI))
    )
    session.commit()
    return tolte.rowcount or 0
