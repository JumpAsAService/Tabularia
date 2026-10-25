"""The CHECK calls towards the engine have a cap (audit 2026-10-08, M3).

The client towards the engine has no timeout, for the gigabyte uploads. But launching
runs, reconciling and the cleanups use it too: a stuck engine or a half-open TCP
connection held the run waiting for it, and the heartbeat declared it alive forever —
flow blocked, schedules skipped, until a restart. Here the engine is a server that
accepts the connection and stays silent."""
import asyncio

import httpx
import pytest

import app.core.engine_client as ec
import app.routes.runs as runs
from tests.conftest import make_run

pytestmark = pytest.mark.anyio


@pytest.fixture
async def silent_engine(monkeypatch):
    """An engine that accepts and never answers, and a short check cap."""
    async def keep_silent(reader, writer):
        await asyncio.sleep(3600)

    server = await asyncio.start_server(keep_silent, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    client = httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=None)  # as the real one
    monkeypatch.setattr(ec, "_client", client)
    monkeypatch.setattr(runs, "CHECK", httpx.Timeout(0.3, connect=0.3))
    yield
    await client.aclose()
    server.close()


async def test_the_reconciliation_does_not_hang_on_a_silent_engine(session, silent_engine):
    run = make_run(session, status="STARTED", task_id="t-muto")
    back = await asyncio.wait_for(runs._reconcile(session, run), timeout=5)
    assert back.status == "STARTED"            # retried at the next round


async def test_the_revocation_does_not_hang_on_a_silent_engine(session, silent_engine):
    run = make_run(session, status="STARTED", task_id="t-muto")
    await asyncio.wait_for(runs._revoke_task(run), timeout=5)  # best effort: it returns


async def test_the_cap_holds_for_the_check_not_for_the_rest():
    assert ec.CHECK.read == 30.0 and ec.CHECK.connect == 5.0
    ec._client = None
    try:
        assert ec.get_engine_client().timeout.read is None   # upload and export stay without a cap
    finally:
        await ec.close_engine_client()
