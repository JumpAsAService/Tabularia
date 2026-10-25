"""A task's longest duration: configurable, no longer two literals in the conf.

`task_time_limit` and `task_soft_time_limit` were hard-coded in `celery_app.conf.update()`
— the only two fixed values in a block otherwise derived from the settings, and without a
comment explaining the choice.

The contract that counts is not the single number though: it is the AGREEMENT with the
gateway. `ENGINE__RUN_STALE_TIMEOUT_SECONDS` must stay above the hard limit, because the
gateway calls a run lost only after Celery has killed it. Here the engine's half is
checked; the other half is in `gateway/tests/test_run_time_limits.py`.
"""
from app.core.config import Settings


def test_defaults_are_the_historical_values():
    """Whoever upgrades must see no change: they were 3600 and 3300."""
    c = Settings().celery
    assert c.task_time_limit == 3600
    assert c.task_soft_time_limit == 3300


def test_the_soft_limit_leaves_room_to_shut_down():
    """The soft limit lets the task close in order BEFORE being killed: if it were not lower
    than the hard one it would serve nothing."""
    c = Settings().celery
    assert c.task_soft_time_limit < c.task_time_limit


def test_both_are_configurable():
    c = Settings(celery={"task_time_limit": 10800, "task_soft_time_limit": 10500}).celery
    assert (c.task_time_limit, c.task_soft_time_limit) == (10800, 10500)


def test_celery_reads_the_settings_and_not_a_literal():
    """The proof that the value really reaches Celery. Without this assertion one could make
    the setting configurable and leave the fixed number in the conf, which is exactly the
    starting state."""
    from app.tasks.celery_app import celery_app

    c = Settings().celery
    assert celery_app.conf.task_time_limit == c.task_time_limit
    assert celery_app.conf.task_soft_time_limit == c.task_soft_time_limit
