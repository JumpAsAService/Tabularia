"""Utenza ClickHouse dedicata alle query dell'assistente AI.

Seconda barriera dietro la lista bianca del nodo sql (test_sql_guard): le query
dell'assistente girano con un utente di sola lettura, se configurato. Qui si
verifica CHI viene scelto e quando; i grant dell'utente sono documentati (e
provati su un server vero) in docs/engines/clickhouse-ai-user.md.
"""
import inspect

import pytest
from pydantic import SecretStr, ValidationError

from app.api.models import PreviewRequest
from app.core.config import ClickHouseExternalSettings
from app.engine.clickhouse_engine import ClickHouseEngine
from app.engine.principal import current_principal, query_principal
from app.tasks.jobs import preview_task


def _engine(**cfg) -> ClickHouseEngine:
    base = dict(host="ch.example", username="scwadmin", password=SecretStr("pw-admin"), transport="s3")
    eng = object.__new__(ClickHouseEngine)  # niente connessioni: interessa solo la scelta
    eng.cfg = ClickHouseExternalSettings(**{**base, **cfg})
    return eng


AI_SET = dict(ai_username="ai", ai_password=SecretStr("pw-ai"))


def test_without_a_principal_the_main_user_is_used_even_if_the_ai_user_exists():
    assert _engine(**AI_SET)._credentials() == ("scwadmin", "pw-admin")


def test_the_assistant_runs_as_the_ai_user_when_configured():
    eng = _engine(**AI_SET)
    with query_principal("ai"):
        assert eng._credentials() == ("ai", "pw-ai") and eng._as_ai()
    assert eng._credentials() == ("scwadmin", "pw-admin")  # fuori dal contesto torna com'era


@pytest.mark.parametrize("cfg", [
    {},                                              # non configurata: tutto come prima
    {"ai_username": "ai"},                           # manca la password: inattiva, non un login a vuoto
    {"ai_password": SecretStr("pw-ai")},             # manca il nome
    {**AI_SET, "transport": "push"},                 # push crea tabelle di appoggio: serve l'utenza principale
])
def test_half_configured_or_push_falls_back_to_the_main_user(cfg):
    with query_principal("ai"):
        assert _engine(**cfg)._credentials() == ("scwadmin", "pw-admin")


def test_unknown_principals_are_ignored():
    with query_principal("root"):
        assert current_principal() is None


def test_the_request_accepts_only_the_ai_principal():
    assert PreviewRequest(bucket="b", input_key="k", operations=[], principal="ai").principal == "ai"
    assert PreviewRequest(bucket="b", input_key="k", operations=[]).principal is None
    with pytest.raises(ValidationError):
        PreviewRequest(bucket="b", input_key="k", operations=[], principal="admin")


def test_the_worker_signature_takes_the_principal_with_a_default():
    # default None: le preview dell'editor non lo mandano, e un'API vecchia
    # accanto a un worker nuovo continua a funzionare
    p = inspect.signature(preview_task.run if hasattr(preview_task, "run") else preview_task).parameters["principal"]
    assert p.default is None
