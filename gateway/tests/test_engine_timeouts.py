"""Le chiamate di CHECK verso l'engine hanno un tetto (audit 2026-10-08, M3).

Il client verso l'engine non ha timeout, per gli upload da gigabyte. Ma lo usano
anche il lancio dei run, la riconciliazione e le pulizie: un engine inchiodato o
una connessione TCP mezza aperta tenevano ferma l'esecuzione che aspettava, e il
battito la dichiarava viva per sempre — flusso bloccato, schedulazioni saltate,
fino al riavvio. Qui l'engine è un server che accetta la connessione e tace."""
import asyncio

import httpx
import pytest

import app.core.engine_client as ec
import app.routes.runs as runs
from tests.conftest import make_run

pytestmark = pytest.mark.anyio


@pytest.fixture
async def engine_muto(monkeypatch):
    """Un engine che accetta e non risponde mai, e un tetto di controllo corto."""
    async def taci(reader, writer):
        await asyncio.sleep(3600)

    server = await asyncio.start_server(taci, "127.0.0.1", 0)
    porta = server.sockets[0].getsockname()[1]
    client = httpx.AsyncClient(base_url=f"http://127.0.0.1:{porta}", timeout=None)  # come quello vero
    monkeypatch.setattr(ec, "_client", client)
    monkeypatch.setattr(runs, "CHECK", httpx.Timeout(0.3, connect=0.3))
    yield
    await client.aclose()
    server.close()


async def test_la_riconciliazione_non_resta_appesa_a_un_engine_muto(session, engine_muto):
    run = make_run(session, status="STARTED", task_id="t-muto")
    tornato = await asyncio.wait_for(runs._reconcile(session, run), timeout=5)
    assert tornato.status == "STARTED"            # si riprova al prossimo giro


async def test_la_revoca_non_resta_appesa_a_un_engine_muto(session, engine_muto):
    run = make_run(session, status="STARTED", task_id="t-muto")
    await asyncio.wait_for(runs._revoke_task(run), timeout=5)  # best-effort: torna


async def test_il_tetto_vale_per_il_controllo_non_per_il_resto():
    assert ec.CHECK.read == 30.0 and ec.CHECK.connect == 5.0
    ec._client = None
    try:
        assert ec.get_engine_client().timeout.read is None   # upload ed export restano senza tetto
    finally:
        await ec.close_engine_client()
