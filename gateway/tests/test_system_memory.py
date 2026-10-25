"""The RAM badge without a node-exporter: off, not broken. On Kubernetes the chart
installs none; before, the gateway fell back on the compose's address and answered 503
to every poll of every browser, with a warning in the log each time."""
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import app.routes.system as system

pytestmark = pytest.mark.anyio


async def test_without_node_exporter_the_badge_is_off_and_nothing_is_called(monkeypatch):
    monkeypatch.setattr(system, "get_settings", lambda: SimpleNamespace(monitoring=SimpleNamespace(node_exporter_url=" ")))

    async def mai():
        raise AssertionError("non deve chiamare il node-exporter")

    monkeypatch.setattr(system, "_read_memory", mai)
    with pytest.raises(HTTPException) as e:
        await system.system_memory(user=None)
    assert e.value.status_code == 404


async def test_with_node_exporter_unreachable_it_stays_503(monkeypatch):
    monkeypatch.setattr(system, "get_settings", lambda: SimpleNamespace(monitoring=SimpleNamespace(node_exporter_url="http://node-exporter:9100/metrics")))

    async def giu():
        raise OSError("connessione rifiutata")

    monkeypatch.setattr(system, "_read_memory", giu)
    with pytest.raises(HTTPException) as e:
        await system.system_memory(user=None)
    assert e.value.status_code == 503
