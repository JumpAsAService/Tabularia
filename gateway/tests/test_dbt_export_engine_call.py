"""The gateway → engine call of the dbt export (follow-up of the engine review).

The engine compiles the project synchronously (seeds read from the storage, the ClickHouse
syntax check), and the gateway waited for it on a client without a timeout: an engine that
hangs held the request until the ingress cut it with a bare 504, and an engine that cannot be
reached was a 500. Now the call is bounded by ENGINE__DBT_EXPORT_TIMEOUT_SECONDS and both
end in a coded refusal in the reader's language."""
from __future__ import annotations

import asyncio
import time

import httpx
import pytest
from fastapi import HTTPException
from sqlmodel import select

from app.models import AuditLog
from app.services import dbt_export as d
from app.services.dbt_messages import translate
from tests.test_dbt_export import _flusso_a_due_uscite, _richiesta_in, scena  # noqa: F401 (scena is a fixture)


def _engine(monkeypatch, handler):
    """An engine client whose /dbt/export answers with `handler` (sync or async)."""
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://engine")
    monkeypatch.setattr("app.core.engine_client._client", client)


def _limite(monkeypatch, seconds):
    """The bound from the environment, as a deployment sets it."""
    from app.core.config import get_settings

    monkeypatch.setenv("ENGINE__DBT_EXPORT_TIMEOUT_SECONDS", str(seconds))
    get_settings.cache_clear()


async def _esporta(session, scena, language="it", **options):
    from app.routes import flows as flows_routes

    flow = _flusso_a_due_uscite(session, scena)
    # the test's own guard: without the bound the call below would wait for the fake engine's 30 s
    return await asyncio.wait_for(flows_routes.export_flow_dbt_with_options(
        flow.id, d.DbtOptions(target="duckdb", **options), _richiesta_in(language), scena.capo, session), 10)


def _download_registrati(session) -> int:
    return len(session.exec(select(AuditLog).where(AuditLog.action == "export.download")).all())


def test_the_bound_has_a_default_under_the_ingress_and_comes_from_the_environment(monkeypatch):
    from pydantic import ValidationError

    from app.core.config import EngineSettings, get_settings

    # with the AI's budget (180 s) the last engine call still ends before the ingress' 300 s
    assert get_settings().engine.dbt_export_timeout_seconds == 100
    assert get_settings().engine.dbt_export_timeout_seconds + get_settings().ai.dbt_export_timeout_seconds < 300
    _limite(monkeypatch, 42.5)
    assert get_settings().engine.dbt_export_timeout_seconds == 42.5
    for sbagliato in (0, -1):
        with pytest.raises(ValidationError):
            EngineSettings(dbt_export_timeout_seconds=sbagliato)


@pytest.mark.anyio
async def test_an_engine_that_does_not_answer_stops_the_export_within_the_bound(session, scena, monkeypatch):
    async def lento(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(30)
        return httpx.Response(200, content=b"PK\x05\x06" + b"\x00" * 18)

    _engine(monkeypatch, lento)
    _limite(monkeypatch, 0.3)
    start = time.monotonic()
    with pytest.raises(HTTPException) as e:
        await _esporta(session, scena, "it")
    assert time.monotonic() - start < 5
    # 504: the engine (upstream of the gateway) did not answer in time; coded and in Italian, not the engine's text
    assert e.value.status_code == 504
    assert e.value.detail == translate("engine_timeout", {"seconds": "0.3"}, "it")
    assert e.value.detail.startswith("Il motore non ha costruito il progetto dbt entro 0.3 s"), e.value.detail
    assert "ENGINE__DBT_EXPORT_TIMEOUT_SECONDS" in e.value.detail
    assert _download_registrati(session) == 0    # nothing was served: no download in the audit


@pytest.mark.anyio
async def test_a_read_timeout_of_the_http_client_is_the_same_refusal(session, scena, monkeypatch):
    def scade(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    _engine(monkeypatch, scade)
    with pytest.raises(HTTPException) as e:
        await _esporta(session, scena, "de")
    assert e.value.status_code == 504
    assert e.value.detail == translate("engine_timeout", {"seconds": "100"}, "de"), e.value.detail


@pytest.mark.anyio
async def test_an_engine_that_cannot_be_reached_is_a_coded_refusal_not_a_500(session, scena, monkeypatch):
    def giu(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("All connection attempts failed", request=request)

    _engine(monkeypatch, giu)
    with pytest.raises(HTTPException) as e:
        await _esporta(session, scena, "fr")
    assert e.value.status_code == 502
    assert e.value.detail == translate("engine_unreachable", {"detail": "All connection attempts failed"}, "fr")
    assert e.value.detail.startswith("Le moteur est injoignable (All connection attempts failed)"), e.value.detail
    assert _download_registrati(session) == 0


@pytest.mark.anyio
async def test_an_engine_that_answers_in_time_still_gives_the_zip(session, scena, monkeypatch):
    async def pronto(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.05)
        return httpx.Response(200, content=b"PK\x05\x06" + b"\x00" * 18, headers={"content-type": "application/zip"})

    _engine(monkeypatch, pronto)
    _limite(monkeypatch, 5)
    r = await _esporta(session, scena, "en")
    assert r.status_code == 200 and r.body.startswith(b"PK") and _download_registrati(session) == 1


@pytest.mark.parametrize("code, params", [("engine_timeout", {"seconds": "100"}),
                                          ("engine_unreachable", {"detail": "All connection attempts failed"})])
def test_the_engine_call_s_refusals_read_in_every_language(code, params):
    from app.core.language import LANGUAGES

    for language in LANGUAGES:
        sentence = translate(code, params, language, "ORIGINAL")
        assert sentence != "ORIGINAL" and "{" not in sentence and all(v in sentence for v in params.values()), (code, language, sentence)
