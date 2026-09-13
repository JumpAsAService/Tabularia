"""Durata massima di un task: configurabile, non più due letterali nel conf.

`task_time_limit` e `task_soft_time_limit` erano scritti in duro dentro
`celery_app.conf.update()` — gli unici due valori fissi in un blocco per il resto
interamente derivato dalle impostazioni, e per giunta senza un commento che
spiegasse la scelta.

Il contratto che conta non è però il singolo numero: è l'ACCORDO col gateway.
`ENGINE__RUN_STALE_TIMEOUT_SECONDS` deve restare sopra il limite duro, perché il
gateway dichiara perso un run solo dopo che Celery lo ha ucciso. Qui si verifica
la metà engine; l'altra metà sta in `gateway/tests/test_run_time_limits.py`.
"""
from app.core.config import Settings


def test_defaults_are_the_historical_values():
    """Chi aggiorna non deve vedere alcun cambiamento: erano 3600 e 3300."""
    c = Settings().celery
    assert c.task_time_limit == 3600
    assert c.task_soft_time_limit == 3300


def test_the_soft_limit_leaves_room_to_shut_down():
    """Il limite soft serve a far chiudere il task in ordine PRIMA di essere
    ucciso: se non fosse minore del duro non servirebbe a nulla."""
    c = Settings().celery
    assert c.task_soft_time_limit < c.task_time_limit


def test_both_are_configurable():
    c = Settings(celery={"task_time_limit": 10800, "task_soft_time_limit": 10500}).celery
    assert (c.task_time_limit, c.task_soft_time_limit) == (10800, 10500)


def test_celery_reads_the_settings_and_not_a_literal():
    """La prova che il valore arriva davvero a Celery. Senza questa asserzione si
    potrebbe rendere configurabile l'impostazione e lasciare il numero fisso nel
    conf, che è esattamente lo stato di partenza."""
    from app.tasks.celery_app import celery_app

    c = Settings().celery
    assert celery_app.conf.task_time_limit == c.task_time_limit
    assert celery_app.conf.task_soft_time_limit == c.task_soft_time_limit
