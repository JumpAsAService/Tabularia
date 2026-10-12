"""Regolazioni del motore dal pannello: dove stanno e come arrivano al motore.

Il gateway le conserva (tabella `engine_settings`, con l'audit di ogni modifica) e
le spedisce al motore (`PUT /settings`), che le controlla e le tiene in Valkey per
tutti i suoi processi (backend/app/core/regolazioni.py: quali parametri, con che
limiti, e perché solo quelli). `spingi_loop` le rispedisce ogni `SPINGI_OGNI`
secondi: se Valkey si svuota o il motore riparte, tornano da sole.

Chi salva e il giro periodico prendono un lucchetto nel database (`blocca`):
lettura, spedizione e salvataggio vanno in fila. Senza, due amministratori che
salvano insieme chiavi diverse le trovavano entrambe nel database, ma il motore
ne applicava una sola (ognuno spediva l'insieme letto prima dell'altro) fino al
giro successivo — trovato provando sul prodtest, 7 coppie su 8.

Un vincolo lo conosce solo il gateway: dichiara perso un run dopo
ENGINE__RUN_STALE_TIMEOUT_SECONDS, e il limite duro dei task deve restarle sotto
(altrimenti chiuderebbe come fallito un run ancora vivo). Lo passa al motore a
ogni spedizione (`limite_duro_max`). Alzare il limite oltre si fa nell'ambiente,
alzando anche quella soglia.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

import httpx
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlmodel import Session, select

from app.core.config import get_settings
from app.core.engine_client import CONTROLLO, get_engine_client
from app.models import EngineSetting

logger = logging.getLogger(__name__)

SPINGI_OGNI = 60.0
# la chiave del lucchetto (pg_advisory_xact_lock): un numero qualunque, uguale per tutti
LUCCHETTO = 7_402_118
# lo stesso margine del default di ENGINE__RUN_STALE_TIMEOUT_SECONDS (limite duro + 5 min)
MARGINE = 300


async def blocca(session: Session, attesa: float = 10.0, chiave: int = LUCCHETTO) -> bool:
    """Il lucchetto delle impostazioni, fino alla fine della transazione di `session`;
    vale fra tutte le repliche (è nel database). Si chiede senza bloccare il ciclo
    degli eventi: chi lo tiene sta parlando col motore, e un motore lento terrebbe
    fermo tutto il processo. False se non arriva entro `attesa` secondi."""
    if session.get_bind().dialect.name != "postgresql":
        return True  # SQLite dei test: un processo solo, niente da mettere in fila
    fine = time.monotonic() + attesa
    while True:
        if session.exec(text("SELECT pg_try_advisory_xact_lock(:k)").bindparams(k=chiave)).one()[0]:
            return True
        if time.monotonic() >= fine:
            return False
        await asyncio.sleep(0.1)


def limite_duro_max() -> int:
    return get_settings().engine.run_stale_timeout_seconds - MARGINE


def dal_pannello() -> bool:
    return get_settings().engine.settings_panel


def salvate(session: Session) -> dict[str, EngineSetting]:
    return {r.key: r for r in session.exec(select(EngineSetting)).all()}


def in_vigore(session: Session) -> dict[str, int]:
    """Quelle da spedire al motore: nessuna se il pannello è spento dall'ambiente."""
    if not dal_pannello():
        return {}
    return {k: r.value for k, r in salvate(session).items()}


async def catalogo() -> dict:
    """Il catalogo del motore: le voci, i loro limiti, i valori dell'ambiente e
    quelli che sta applicando adesso."""
    r = await get_engine_client().get("/settings", params={"max_task_time_limit": limite_duro_max()}, timeout=CONTROLLO)
    r.raise_for_status()
    return r.json()


async def spingi(valori: dict[str, int], cambiate: Optional[list[str]] = None) -> httpx.Response:
    return await get_engine_client().put(
        "/settings", json={"values": valori, "max_task_time_limit": limite_duro_max(), "changed": cambiate or []},
        timeout=CONTROLLO,
    )


async def spingi_scartando(valori: dict[str, int]) -> dict[str, int]:
    """Spedisce; se il motore ne rifiuta qualcuna (salvata quando valeva, non vale
    più: l'ambiente o la soglia del gateway sono cambiati) la toglie e rispedisce,
    finché il motore accetta, invece di lasciare in vigore quelle vecchie o di non
    spedire niente. Torna quelle che il motore applica; le scartate restano salvate
    (il pannello le mostra «non applicate») e finiscono nel log."""
    attuali = dict(valori)
    for _ in range(len(valori) + 1):
        r = await spingi(attuali)
        if r.status_code != 422:
            r.raise_for_status()
            return attuali
        errori = (r.json().get("detail") or {}).get("errors") or []
        cattive = {e.get("key") for e in errori} & set(attuali)
        if not cattive:  # il motore rifiuta, ma niente di quello che c'è: non si insiste
            r.raise_for_status()
        logger.warning("regolazioni del motore: scartate perché non valgono più: %s", errori)
        attuali = {k: v for k, v in attuali.items() if k not in cattive}
    raise RuntimeError("regolazioni del motore: il motore continua a rifiutarle")


async def un_giro(bind: Engine) -> bool:
    """Rispedisce le salvate. Se qualcuno sta salvando, salta: le spedisce lui."""
    with Session(bind) as session:
        if not await blocca(session, attesa=0):
            return False
        await spingi_scartando(in_vigore(session))
        session.commit()  # rilascia il lucchetto
    return True


async def spingi_loop(stop: asyncio.Event) -> None:
    from app.db.session import engine

    while not stop.is_set():
        try:
            await un_giro(engine)
        except Exception as e:  # noqa: BLE001 — motore giù o in riavvio: si riprova al giro dopo
            logger.info("regolazioni del motore: non spedite (%s), si riprova fra %ss", e, SPINGI_OGNI)
        try:
            await asyncio.wait_for(stop.wait(), timeout=SPINGI_OGNI)
        except asyncio.TimeoutError:
            pass
