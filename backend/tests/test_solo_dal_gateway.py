"""L'API dell'engine risponde solo al gateway (audit 2026-10-08, A4): la rete la
chiude, e il token derivato dalla chiave Fernet è il secondo lucchetto. Il
middleware si prova come applicazione ASGI, senza client HTTP."""
import asyncio
from types import SimpleNamespace

import pytest

from app.api.main import app
from app.api.gateway_only import GatewayOnly
from app.core import crypto
from app.core.crypto import INTERNAL_HEADER, internal_token

# lo stesso valore è fissato nel test del gateway: i due servizi devono calcolarlo uguale
KEY = "x" * 43 + "="
ATTESO = "2afa379a593fc05cd4acfa942cb246ca918ab43729da93df0a8d4a443013050f"


@pytest.fixture
def chiave_nota(monkeypatch):
    monkeypatch.setattr(crypto, "get_settings", lambda: SimpleNamespace(security=SimpleNamespace(fernet_key=KEY)))
    internal_token.cache_clear()
    yield
    internal_token.cache_clear()


def _stato(path_: str, intestazioni: dict) -> int:
    """Lo stato con cui la guardia risponde, davanti a un'app che dice sempre 200."""
    async def dietro(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    eventi = []

    async def ricevi():
        return {"type": "http.request", "body": b""}

    async def manda(message):
        eventi.append(message)

    scope = {"type": "http", "method": "GET", "path": path_,
             "headers": [(k.lower().encode(), v.encode()) for k, v in intestazioni.items()]}
    asyncio.run(GatewayOnly(dietro)(scope, ricevi, manda))
    return eventi[0]["status"]


def test_il_token_e_quello_che_calcola_il_gateway(chiave_nota):
    assert internal_token() == ATTESO


@pytest.mark.parametrize("intestazioni", [{}, {INTERNAL_HEADER: "sbagliato"}, {INTERNAL_HEADER: ATTESO[:-1]}])
@pytest.mark.parametrize("path_", ["/engines", "/tasks/preview", "/files/object", "/docs", "/"])
def test_senza_il_token_giusto_e_un_401(chiave_nota, path_, intestazioni):
    assert _stato(path_, intestazioni) == 401


def test_col_token_passa(chiave_nota):
    assert _stato("/engines", {INTERNAL_HEADER: ATTESO}) == 200


@pytest.mark.parametrize("path_", ["/metrics", "/healthcheck", "/healthcheck/", "/healthcheck/redis"])
def test_salute_e_metriche_restano_aperte(chiave_nota, path_):
    assert _stato(path_, {}) == 200


def test_la_guardia_e_montata_sull_app():
    assert any(m.cls is GatewayOnly for m in app.user_middleware)
