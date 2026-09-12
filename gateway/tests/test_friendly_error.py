"""Traduzione degli errori dei run nel gateway.

Il messaggio da OOM riguarda il WORKER ucciso per memoria. Un errore già
tradotto dal backend col testo del server del database (prefisso «PostgreSQL:»,
«ClickHouse:», …) va lasciato com'è, anche se contiene «out of memory»: in quel
caso a esaurire la memoria è il database, e il consiglio di alzare
WORKER_MEM_LIMIT sarebbe sbagliato.
"""
from app.routes.runs import _OOM_MESSAGE, _friendly_error


def test_oom_del_database_non_diventa_oom_del_worker():
    err = ("PostgreSQL: out of memory (SQLSTATE 53200). The database server ran out of memory "
           "for this query: reduce the data.")
    detail = "Traceback (most recent call last):\n  ...\nadbc_driver_manager.InternalError: ERROR:  out of memory"
    assert _friendly_error(err, detail) == err


def test_oom_del_worker_resta_tradotto():
    assert _friendly_error("WorkerLostError('Worker exited prematurely: signal 9 (SIGKILL).')", None) == _OOM_MESSAGE


def test_messaggi_del_database_passano_invariati():
    err = ("ClickHouse: Limit for result exceeded, max rows: 1.00 million, current rows: 1.02 million "
           "(TOO_MANY_ROWS_OR_BYTES, code 396). This is a limit set by the server: reduce the data "
           "with a filter or a LIMIT.")
    assert _friendly_error(err, "Traceback ... IncompleteRead(198 bytes read)") == err
