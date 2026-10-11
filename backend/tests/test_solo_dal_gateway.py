"""L'API dell'engine risponde solo al gateway (audit 2026-10-08, A4): la rete la
chiude, e il token derivato dalla chiave Fernet è il secondo lucchetto. Il
middleware si prova come applicazione ASGI, senza client HTTP."""
import asyncio
from types import SimpleNamespace

import pytest

from app.api.main import app
from app.api.solo_dal_gateway import SoloDalGateway
from app.core import crypto
from app.core.crypto import INTESTAZIONE_INTERNA, token_interno

# lo stesso valore è fissato nel test del gateway: i due servizi devono calcolarlo uguale
CHIAVE = "x" * 43 + "="
ATTESO = "2afa379a593fc05cd4acfa942cb246ca918ab43729da93df0a8d4a443013050f"


@pytest.fixture
def chiave_nota(monkeypatch):
    monkeypatch.setattr(crypto, "get_settings", lambda: SimpleNamespace(security=SimpleNamespace(fernet_key=CHIAVE)))
    token_interno.cache_clear()
    yield
    token_interno.cache_clear()


def _stato(percorso: str, intestazioni: dict) -> int:
    """Lo stato con cui la guardia risponde, davanti a un'app che dice sempre 200."""
    async def dietro(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    eventi = []

    async def ricevi():
        return {"type": "http.request", "body": b""}

    async def manda(messaggio):
        eventi.append(messaggio)

    scope = {"type": "http", "method": "GET", "path": percorso,
             "headers": [(k.lower().encode(), v.encode()) for k, v in intestazioni.items()]}
    asyncio.run(SoloDalGateway(dietro)(scope, ricevi, manda))
    return eventi[0]["status"]


def test_il_token_e_quello_che_calcola_il_gateway(chiave_nota):
    assert token_interno() == ATTESO


@pytest.mark.parametrize("intestazioni", [{}, {INTESTAZIONE_INTERNA: "sbagliato"}, {INTESTAZIONE_INTERNA: ATTESO[:-1]}])
@pytest.mark.parametrize("percorso", ["/engines", "/tasks/preview", "/files/object", "/docs", "/"])
def test_senza_il_token_giusto_e_un_401(chiave_nota, percorso, intestazioni):
    assert _stato(percorso, intestazioni) == 401


def test_col_token_passa(chiave_nota):
    assert _stato("/engines", {INTESTAZIONE_INTERNA: ATTESO}) == 200


@pytest.mark.parametrize("percorso", ["/metrics", "/healthcheck", "/healthcheck/", "/healthcheck/redis"])
def test_salute_e_metriche_restano_aperte(chiave_nota, percorso):
    assert _stato(percorso, {}) == 200


def test_la_guardia_e_montata_sull_app():
    assert any(m.cls is SoloDalGateway for m in app.user_middleware)
