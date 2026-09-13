"""Soglia di staleness: DERIVATA dal limite duro di Celery, non ricopiata a mano.

Il gateway dichiara perso un run solo DOPO che Celery lo ha ucciso — è anche ciò
che rende sicuro non abilitare `task_acks_late` sull'engine (un run perso viene
marcato FAILURE da questa soglia, non riconsegnato e rieseguito).

Finché i due numeri sono stati scritti a mano in due servizi diversi — `3600`
nell'engine, `3600 + 300` qui — alzarne uno solo rompeva l'invariante in
silenzio: o si marcava FAILURE un run ancora vivo, o si aspettava più a lungo
soltanto per scoprire che era già morto al limite duro.

L'altra metà del contratto sta in `backend/tests/test_run_time_limits.py`.
"""
import pytest

from app.core.config import EngineSettings, Settings


def test_the_default_tracks_the_celery_hard_limit(monkeypatch):
    """Alzare il limite dell'engine sposta anche il gateway: è la stessa
    variabile, letta dallo stesso `.env` da entrambi i container."""
    monkeypatch.setenv("CELERY__TASK_TIME_LIMIT", "10800")  # 3 ore
    assert EngineSettings().run_stale_timeout_seconds == 10800 + 300


def test_without_celery_configured_the_historical_value_stands(monkeypatch):
    monkeypatch.delenv("CELERY__TASK_TIME_LIMIT", raising=False)
    assert EngineSettings().run_stale_timeout_seconds == 3900


@pytest.mark.parametrize("limite", ["600", "3600", "86400"])
def test_the_threshold_always_stays_above_the_kill_time(monkeypatch, limite):
    """L'invariante, qualunque sia il limite scelto."""
    monkeypatch.setenv("CELERY__TASK_TIME_LIMIT", limite)
    assert EngineSettings().run_stale_timeout_seconds > int(limite)


def test_an_explicit_threshold_still_wins(monkeypatch):
    """Il default è derivato, non imposto: chi vuole un margine diverso lo
    imposta e l'env batte il default, come per ogni altro campo."""
    monkeypatch.setenv("CELERY__TASK_TIME_LIMIT", "3600")
    monkeypatch.setenv("ENGINE__RUN_STALE_TIMEOUT_SECONDS", "7200")
    assert Settings().engine.run_stale_timeout_seconds == 7200


def test_a_malformed_limit_says_which_variable_is_wrong(monkeypatch):
    """Un refuso non deve diventare un default silenzioso: meglio non partire,
    dicendo quale variabile guardare."""
    monkeypatch.setenv("CELERY__TASK_TIME_LIMIT", "un'ora")
    with pytest.raises(RuntimeError, match="CELERY__TASK_TIME_LIMIT"):
        EngineSettings()
