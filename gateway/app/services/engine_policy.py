"""Quali motori di esecuzione l'installazione consente di SCEGLIERE.

Un'azienda che standardizza la flotta (es. «qui si usa solo ClickHouse») ha
bisogno di dirlo una volta sola, non flusso per flusso. Questo modulo è la
fonte unica della risposta: lo consultano la validazione dei flussi
(routes/flows.py) e il catalogo servito al selettore (routes/proxy.py), così
non possono divergere.

Regola: si memorizzano i motori VIETATI. Tabella vuota = tutto consentito,
cioè il comportamento storico.
"""
from sqlmodel import Session, select

from app.models import DisabledEngine

# Motori noti al gateway, sincronizzati col catalogo dell'engine. Vivono qui e
# non in routes/flows.py perché servono a due rotte diverse: tenerli in una
# delle due obbligherebbe l'altra a importare da un modulo di rotte.
KNOWN_ENGINES: tuple[str, ...] = ("polars", "duckdb", "chdb", "clickhouse")


def disabled_engines(session: Session) -> set[str]:
    """Gli id dei motori disabilitati dall'amministratore (spesso l'insieme vuoto)."""
    return {r.engine_id for r in session.exec(select(DisabledEngine)).all()}


def allowed_engines(session: Session) -> set[str]:
    """I motori che si possono ancora scegliere per un flusso."""
    return set(KNOWN_ENGINES) - disabled_engines(session)
