"""Staleness threshold: DERIVED from Celery's hard limit, not copied by hand.

The gateway declares a run lost only AFTER Celery has killed it — it is also what
makes it safe not to enable `task_acks_late` on the engine (a lost run is marked
FAILURE by this threshold, not redelivered and run again).

While the two numbers were written by hand in two different services — `3600` in the
engine, `3600 + 300` here — raising only one broke the invariant silently: either a
run still alive was marked FAILURE, or one waited longer only to find out it had died
at the hard limit already.

The other half of the contract is in `backend/tests/test_run_time_limits.py`.
"""
import pytest

from app.core.config import EngineSettings, Settings


def test_the_default_tracks_the_celery_hard_limit(monkeypatch):
    """Raising the engine's limit moves the gateway too: it is the same variable, read from
    the same `.env` by both containers."""
    monkeypatch.setenv("CELERY__TASK_TIME_LIMIT", "10800")  # 3 ore
    assert EngineSettings().run_stale_timeout_seconds == 10800 + 300


def test_without_celery_configured_the_historical_value_stands(monkeypatch):
    monkeypatch.delenv("CELERY__TASK_TIME_LIMIT", raising=False)
    assert EngineSettings().run_stale_timeout_seconds == 3900


@pytest.mark.parametrize("limit", ["600", "3600", "86400"])
def test_the_threshold_always_stays_above_the_kill_time(monkeypatch, limit):
    """The invariant, whatever the limit chosen."""
    monkeypatch.setenv("CELERY__TASK_TIME_LIMIT", limit)
    assert EngineSettings().run_stale_timeout_seconds > int(limit)


def test_an_explicit_threshold_still_wins(monkeypatch):
    """The default is derived, not imposed: whoever wants another margin sets it and the env
    beats the default, as for every other field."""
    monkeypatch.setenv("CELERY__TASK_TIME_LIMIT", "3600")
    monkeypatch.setenv("ENGINE__RUN_STALE_TIMEOUT_SECONDS", "7200")
    assert Settings().engine.run_stale_timeout_seconds == 7200


def test_a_malformed_limit_says_which_variable_is_wrong(monkeypatch):
    """A typo must not become a silent default: better not to start, saying which variable to
    look at."""
    monkeypatch.setenv("CELERY__TASK_TIME_LIMIT", "un'ora")
    with pytest.raises(RuntimeError, match="CELERY__TASK_TIME_LIMIT"):
        EngineSettings()
