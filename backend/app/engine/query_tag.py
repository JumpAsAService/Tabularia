"""Etichetta delle query di un task.

Chi lancia un lavoro interattivo (la preview) dichiara un'etichetta; l'engine
ClickHouse la mette come `log_comment` su OGNI query di quel lavoro. Serve a una
cosa sola: poterle uccidere sul server (`KILL QUERY WHERE Settings['log_comment']
= …`) quando il lavoro viene interrotto. Interrompere il worker non basta — il
server continua a eseguire, e una scrittura di step-cache va avanti un minuto
per un risultato che nessuno leggerà.

ContextVar e non un parametro di `preview()`: la firma è comune ai quattro
engine e gli altri tre non saprebbero che farsene.
"""
from contextlib import contextmanager
from contextvars import ContextVar
import re

_TAG: ContextVar[str | None] = ContextVar("tabularia_query_tag", default=None)
_SAFE = re.compile(r"^[A-Za-z0-9:_-]{1,96}$")


def current_query_tag() -> str | None:
    return _TAG.get()


def is_safe_tag(tag: str) -> bool:
    """L'etichetta finisce dentro una KILL QUERY: solo caratteri innocui."""
    return bool(_SAFE.match(tag or ""))


@contextmanager
def query_tag(tag: str | None):
    token = _TAG.set(tag if tag and is_safe_tag(tag) else None)
    try:
        yield
    finally:
        _TAG.reset(token)
