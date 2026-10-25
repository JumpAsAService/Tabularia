"""The ClickHouse user dedicated to the AI assistant's queries.

A second barrier behind the sql node's allowlist (test_sql_guard): the assistant's
queries run with a read-only user, when configured. Here WHO is chosen, and when, is
checked; the user's grants are documented (and tried on a real server) in
docs/engines/clickhouse-ai-user.md.
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
    eng = object.__new__(ClickHouseEngine)  # no connections: only the choice matters
    eng.cfg = ClickHouseExternalSettings(**{**base, **cfg})
    return eng


AI_SET = dict(ai_username="ai", ai_password=SecretStr("pw-ai"))


def test_without_a_principal_the_main_user_is_used_even_if_the_ai_user_exists():
    assert _engine(**AI_SET)._credentials() == ("scwadmin", "pw-admin")


def test_the_assistant_runs_as_the_ai_user_when_configured():
    eng = _engine(**AI_SET)
    with query_principal("ai"):
        assert eng._credentials() == ("ai", "pw-ai") and eng._as_ai()
    assert eng._credentials() == ("scwadmin", "pw-admin")  # outside the context it is as it was


@pytest.mark.parametrize("cfg", [
    {},                                              # not configured: everything as before
    {"ai_username": "ai"},                           # the password missing: inactive, not a login that fails
    {"ai_password": SecretStr("pw-ai")},             # the name missing
    {**AI_SET, "transport": "push"},                 # push creates staging tables: the main user is needed
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
    # default None: the editor's previews do not send it, and an old API next to a new
    # worker keeps working
    p = inspect.signature(preview_task.run if hasattr(preview_task, "run") else preview_task).parameters["principal"]
    assert p.default is None
