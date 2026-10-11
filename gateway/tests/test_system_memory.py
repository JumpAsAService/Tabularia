"""Il badge della RAM senza un node-exporter: spento, non guasto. Su Kubernetes il
chart non ne installa uno; prima il gateway ricadeva sull'indirizzo del compose e
rispondeva 503 a ogni poll di ogni browser, con un avviso nel log ogni volta."""
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import app.routes.system as system

pytestmark = pytest.mark.anyio


async def test_senza_node_exporter_il_badge_e_spento_e_non_si_chiama_niente(monkeypatch):
    monkeypatch.setattr(system, "get_settings", lambda: SimpleNamespace(monitoring=SimpleNamespace(node_exporter_url=" ")))

    async def mai():
        raise AssertionError("non deve chiamare il node-exporter")

    monkeypatch.setattr(system, "_read_memory", mai)
    with pytest.raises(HTTPException) as e:
        await system.system_memory(user=None)
    assert e.value.status_code == 404


async def test_con_node_exporter_irraggiungibile_resta_503(monkeypatch):
    monkeypatch.setattr(system, "get_settings", lambda: SimpleNamespace(monitoring=SimpleNamespace(node_exporter_url="http://node-exporter:9100/metrics")))

    async def giu():
        raise OSError("connessione rifiutata")

    monkeypatch.setattr(system, "_read_memory", giu)
    with pytest.raises(HTTPException) as e:
        await system.system_memory(user=None)
    assert e.value.status_code == 503
