"""
Metriche Prometheus custom dell'app, esposte su `/metrics` del backend.

Il collector legge alcuni contatori da Valkey a ogni scrape. Usare Valkey come
sorgente (invece dei contatori in-process di prometheus_client) permette di
aggregare metriche prodotte in processi diversi — backend (preview) e worker
Celery (run/convert) — senza dover esporre un endpoint dal worker.
"""
from __future__ import annotations

import redis
from prometheus_client import REGISTRY
from prometheus_client.core import GaugeMetricFamily

from app.core.config import get_settings

# chiavi Valkey (allineate a quelle usate da StepCache e dai task)
CACHE_HITS_KEY = "dataprep:metrics:cache_hits"
CACHE_MISSES_KEY = "dataprep:metrics:cache_misses"
STEPCACHE_SET = "dataprep:stepcache"
STORAGE_BYTES_HASH = "dataprep:metrics:storage_bytes"
STORAGE_OBJECTS_HASH = "dataprep:metrics:storage_objects"


class DataPrepCollector:
    """Espone i contatori applicativi letti da Valkey a ogni scrape."""

    def __init__(self, redis_client):
        self.redis = redis_client

    def collect(self):
        try:
            hits = int(self.redis.get(CACHE_HITS_KEY) or 0)
            misses = int(self.redis.get(CACHE_MISSES_KEY) or 0)
            entries = int(self.redis.scard(STEPCACHE_SET) or 0)
            storage_bytes = self.redis.hgetall(STORAGE_BYTES_HASH)
            storage_objects = self.redis.hgetall(STORAGE_OBJECTS_HASH)
        except redis.RedisError:
            return  # Valkey non raggiungibile: nessuna metrica custom questa volta

        yield GaugeMetricFamily("dataprep_cache_hits", "Step cache: riusi totali", value=hits)
        yield GaugeMetricFamily("dataprep_cache_misses", "Step cache: miss totali", value=misses)
        yield GaugeMetricFamily("dataprep_cache_entries", "Step cache: voci correnti", value=entries)

        # dimensione dello storage per prefisso (cache/, datasets/, raw/, out/)
        g_bytes = GaugeMetricFamily(
            "dataprep_storage_bytes", "Byte per prefisso di storage", labels=["prefix"]
        )
        for prefix, val in (storage_bytes or {}).items():
            g_bytes.add_metric([prefix], float(val))
        yield g_bytes

        g_obj = GaugeMetricFamily(
            "dataprep_storage_objects", "Oggetti per prefisso di storage", labels=["prefix"]
        )
        for prefix, val in (storage_objects or {}).items():
            g_obj.add_metric([prefix], float(val))
        yield g_obj


def register_app_metrics() -> None:
    """Registra il collector custom nel registry di default (quello di /metrics)."""
    settings = get_settings()
    client = redis.Redis.from_url(settings.redis.url, decode_responses=True)
    try:
        REGISTRY.register(DataPrepCollector(client))
    except ValueError:
        pass  # già registrato (es. reload di uvicorn)
