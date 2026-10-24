"""PostgreSQL not answering: a 503 that says so (seen on kind, 2026-10-23, its node drained:
the sign-in answered a bare «Internal Server Error»)."""
import pytest
from sqlalchemy.exc import IntegrityError, OperationalError
from starlette.requests import Request

pytestmark = pytest.mark.anyio


def _request():
    return Request({"type": "http", "method": "POST", "path": "/auth/login", "headers": [], "query_string": b""})


async def test_a_database_not_answering_is_a_503_that_says_so():
    from app import main

    r = await main._database_down(_request(), OperationalError("SELECT 1", {}, Exception("connection refused")))
    assert r.status_code == 503 and b"database is not answering" in r.body


async def test_other_database_errors_stay_errors():
    from app import main

    with pytest.raises(IntegrityError):
        await main._database_down(_request(), IntegrityError("INSERT", {}, Exception("duplicate key")))


async def test_an_engine_not_answering_is_a_503_that_says_so(monkeypatch):
    # an engine pod stopping (its socket closed before the Service dropped it): a bare 500
    import httpx

    from app import main
    from app.core.config import get_settings

    url = get_settings().engine.base_url.rstrip("/") + "/tasks/preview"
    r = await main._service_down(_request(), httpx.ConnectError("Connection refused",
                                                                 request=httpx.Request("POST", url)))
    assert r.status_code == 503 and b"The engine is not answering" in r.body
    r = await main._service_down(_request(), httpx.ReadTimeout("timed out",
                                                               request=httpx.Request("GET", "https://idp.example/jwks")))
    assert r.status_code == 503 and b"A service Tabularia calls is not answering" in r.body
    assert main.app.exception_handlers[httpx.TransportError] is main._service_down


async def test_the_probe_answers_on_the_event_loop():
    # twelve slow requests held every thread, and /health with them: Kubernetes restarted
    # a busy gateway
    import inspect

    from app import main

    assert inspect.iscoroutinefunction(main.health)
