"""
The app's custom Prometheus metrics, exposed on the backend's `/metrics`.

The collector reads a few counters from Valkey at every scrape. Using Valkey as the
source (instead of prometheus_client's in-process counters) aggregates metrics made in
different processes — the backend (previews) and the Celery workers (runs, conversions)
— without exposing an endpoint from the worker.
"""
from __future__ import annotations

import redis
from prometheus_client import REGISTRY
from prometheus_client.core import GaugeMetricFamily

from app.core.config import get_settings

# Valkey keys (in line with those StepCache and the tasks use)
CACHE_HITS_KEY = "dataprep:metrics:cache_hits"
CACHE_MISSES_KEY = "dataprep:metrics:cache_misses"
STEPCACHE_SET = "dataprep:stepcache"
STORAGE_BYTES_HASH = "dataprep:metrics:storage_bytes"
STORAGE_OBJECTS_HASH = "dataprep:metrics:storage_objects"


class DataPrepCollector:
    """Exposes the application's counters read from Valkey at every scrape."""

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
            return  # Valkey unreachable: no custom metrics this time

        yield GaugeMetricFamily("dataprep_cache_hits", "Step cache: total reuses", value=hits)
        yield GaugeMetricFamily("dataprep_cache_misses", "Step cache: total misses", value=misses)
        yield GaugeMetricFamily("dataprep_cache_entries", "Step cache: current entries", value=entries)

        # the storage's size per prefix (cache/, datasets/, raw/, out/)
        g_bytes = GaugeMetricFamily(
            "dataprep_storage_bytes", "Bytes per storage prefix", labels=["prefix"]
        )
        for prefix, val in (storage_bytes or {}).items():
            g_bytes.add_metric([prefix], float(val))
        yield g_bytes

        g_obj = GaugeMetricFamily(
            "dataprep_storage_objects", "Objects per storage prefix", labels=["prefix"]
        )
        for prefix, val in (storage_objects or {}).items():
            g_obj.add_metric([prefix], float(val))
        yield g_obj

        yield from _preview_metrics()


def _preview_metrics():
    """The previews' histogram (duration by engine and outcome) and time by phase. The
    counters live on Valkey (see preview_stats): here they are turned into the Prometheus
    format — CUMULATIVE buckets — from which Grafana derives p50/p95."""
    from prometheus_client.core import CounterMetricFamily, HistogramMetricFamily

    from app.observability import preview_stats as ps

    snap = ps.snapshot()
    series: dict[tuple[str, str], dict[str, int]] = {}
    for key, n in snap["hist"].items():
        engine, outcome, le = key.split("|", 2)
        series.setdefault((engine, outcome), {})[le] = n
    h = HistogramMetricFamily(
        "dataprep_preview_duration_ms", "Duration of the previews (ms)", labels=["engine", "outcome"]
    )
    for (engine, outcome), per_le in series.items():
        running, buckets = 0, []
        for le in [str(b) for b in ps.BUCKETS_MS] + ["+Inf"]:
            running += per_le.get(le, 0)
            buckets.append((le, running))
        h.add_metric([engine, outcome], buckets, sum_value=snap["sum_ms"].get(f"{engine}|{outcome}", 0.0))
    yield h

    c = CounterMetricFamily(
        "dataprep_preview_phase_ms", "Time the previews spend in every phase (ms)", labels=["engine", "phase"]
    )
    for key, ms in snap["phase_ms"].items():
        engine, phase = key.split("|", 1)
        c.add_metric([engine, phase], ms)
    yield c


def register_app_metrics() -> None:
    """Registers the custom collector in the default registry (/metrics')."""
    settings = get_settings()
    client = redis.Redis.from_url(settings.redis.url, decode_responses=True)
    try:
        REGISTRY.register(DataPrepCollector(client))
    except ValueError:
        pass  # registered already (a uvicorn reload)
