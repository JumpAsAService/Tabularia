"""Per conto di CHI gira una query: oggi solo l'assistente AI ha un'identità sua.

L'engine parla con ClickHouse con un'utenza che legge tutto. Finché le query le
scrive un nodo del flusso va bene; l'assistente invece mette una SELECT libera
in mano a chiunque abbia VIEW, e il 2026-09-19 una query è uscita dall'input
(`merge()`, vedi sql_guard). La lista bianca ha chiuso quel buco; l'utenza `ai`
è la SECONDA barriera: se un giorno un'altra query passasse, sul server
troverebbe un utente che non può leggere `system` né scrivere nulla.

ContextVar come `query_tag`, per la stessa ragione: la firma di `preview()` è
comune a tutti gli engine e solo ClickHouse sa che farsene.
"""
from contextlib import contextmanager
from contextvars import ContextVar

AI = "ai"
_KNOWN = frozenset({AI})
_PRINCIPAL: ContextVar[str | None] = ContextVar("tabularia_query_principal", default=None)


def current_principal() -> str | None:
    return _PRINCIPAL.get()


@contextmanager
def query_principal(principal: str | None):
    token = _PRINCIPAL.set(principal if principal in _KNOWN else None)
    try:
        yield
    finally:
        _PRINCIPAL.reset(token)
