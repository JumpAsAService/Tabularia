"""The label of a task's queries.

Whoever starts an interactive job (the preview) declares a label; the ClickHouse engine
puts it as the `log_comment` of EVERY query of that job. It serves one thing: killing
them on the server (`KILL QUERY WHERE Settings['log_comment'] = …`) when the job is
interrupted. Interrupting the worker is not enough — the server goes on running, and a
step-cache write goes on for a minute for a result nobody will read.

A ContextVar and not a parameter of `preview()`: the signature is common to the engines
and the others would have no use for it.
"""
from contextlib import contextmanager
from contextvars import ContextVar
import re

_TAG: ContextVar[str | None] = ContextVar("tabularia_query_tag", default=None)
_SAFE = re.compile(r"^[A-Za-z0-9:_-]{1,96}$")


def current_query_tag() -> str | None:
    return _TAG.get()


def is_safe_tag(tag: str) -> bool:
    """The label ends up inside a KILL QUERY: harmless characters only."""
    return bool(_SAFE.match(tag or ""))


@contextmanager
def query_tag(tag: str | None):
    token = _TAG.set(tag if tag and is_safe_tag(tag) else None)
    try:
        yield
    finally:
        _TAG.reset(token)


def was_interrupted(exc: BaseException | None) -> bool:
    """True when the chain of causes holds the task's interruption (the preview was
    superseded: the worker gets SIGUSR1 → SoftTimeLimitExceeded). By NAME and not by
    type — the engine does not import Celery — going up the chain: the exception almost
    always arrives WRAPPED, in an EngineError when the task was talking to ClickHouse, in
    botocore's HTTPClientError when it was talking to S3. Seen live: 19 "raised
    unexpected" tracebacks in an hour for previews nobody waited for."""
    seen = 0
    while exc is not None and seen < 12:
        if type(exc).__name__ in ("SoftTimeLimitExceeded", "KeyboardInterrupt"):
            return True
        if "SoftTimeLimitExceeded" in str(exc):  # botocore reports it only in the message
            return True
        exc = exc.__cause__ or exc.__context__
        seen += 1
    return False
