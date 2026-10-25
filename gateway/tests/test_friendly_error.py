"""Translating the runs' errors in the gateway.

The OOM message is about the WORKER killed for memory. An error already translated by
the backend with the database server's text (prefix «PostgreSQL:», «ClickHouse:», …)
must be left as it is, even if it contains «out of memory»: in that case it is the
database that ran out of memory, and the advice to raise WORKER_MEM_LIMIT would be
wrong.
"""
from app.routes.runs import _OOM_MESSAGE, _friendly_error


def test_database_oom_does_not_become_worker_oom():
    err = ("PostgreSQL: out of memory (SQLSTATE 53200). The database server ran out of memory "
           "for this query: reduce the data.")
    detail = "Traceback (most recent call last):\n  ...\nadbc_driver_manager.InternalError: ERROR:  out of memory"
    assert _friendly_error(err, detail) == err


def test_worker_oom_stays_translated():
    assert _friendly_error("WorkerLostError('Worker exited prematurely: signal 9 (SIGKILL).')", None) == _OOM_MESSAGE


def test_database_messages_pass_unchanged():
    err = ("ClickHouse: Limit for result exceeded, max rows: 1.00 million, current rows: 1.02 million "
           "(TOO_MANY_ROWS_OR_BYTES, code 396). This is a limit set by the server: reduce the data "
           "with a filter or a LIMIT.")
    assert _friendly_error(err, "Traceback ... IncompleteRead(198 bytes read)") == err


def test_a_worker_lost_as_the_engine_tells_it_stays_as_told():
    # the engine's words for a task whose worker died with it (OOMKilled, its pod gone)
    err = ("The worker running it stopped before the end: killed at its memory limit (OOMKilled), or its pod "
           "restarted or removed (an upgrade, a drained node). Run it again; if it happens again, give the "
           "workers more memory or fewer processes each.")
    assert _friendly_error(err, None) == err
