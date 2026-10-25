"""On WHOSE behalf a query runs: today only the AI assistant has an identity of its own.

The engine talks to ClickHouse with a user that reads everything. While a flow's node
writes the queries that is fine; the assistant instead puts a free SELECT in the hands of
anyone with VIEW, and on 2026-09-19 a query got out of its input (`merge()`, see
sql_guard). The allowlist closed that hole; the `ai` user is the SECOND barrier: if
another query ever got through, on the server it would find a user that can neither read
`system` nor write anything.

A ContextVar as `query_tag`, for the same reason: the signature of `preview()` is common
to every engine and only ClickHouse has a use for it.
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
