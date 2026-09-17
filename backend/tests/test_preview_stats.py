"""Prestazioni delle preview senza tabelle e senza RAM di processo: contatori a
dimensione FISSA su Valkey + le più lente delle ultime 24 ore."""
import json

import pytest

from app.observability import preview_stats as ps


class _MiniRedis:
    """Solo i comandi che preview_stats usa: hash, sorted set e una pipeline che
    esegue subito (l'atomicità qui non è ciò che si sta provando)."""
    def __init__(self): self.h: dict = {}; self.z: dict = {}
    def pipeline(self): self._out = []; return self
    def execute(self): out, self._out = self._out, []; return out
    def _ret(self, v):
        if hasattr(self, "_out"): self._out.append(v)
        return v
    def hincrby(self, k, f, n): d = self.h.setdefault(k, {}); d[f] = int(d.get(f, 0)) + n; return self._ret(d[f])
    def hincrbyfloat(self, k, f, n): d = self.h.setdefault(k, {}); d[f] = float(d.get(f, 0)) + n; return self._ret(d[f])
    def hgetall(self, k): return self._ret({f: str(v) for f, v in self.h.get(k, {}).items()})
    def zadd(self, k, mapping): self.z.setdefault(k, {}).update(mapping); return self._ret(len(mapping))
    def _sorted(self, k): return sorted(self.z.get(k, {}).items(), key=lambda kv: kv[1])
    def zremrangebyrank(self, k, lo, hi):
        items = self._sorted(k); n = len(items)
        lo, hi = (lo if lo >= 0 else n + lo), (hi if hi >= 0 else n + hi)
        for m, _ in items[max(lo, 0):hi + 1]: self.z[k].pop(m, None)
        return self._ret(0)
    def expire(self, k, s): return self._ret(True)
    def zrevrange(self, k, lo, hi): return [m for m, _ in reversed(self._sorted(k))]
    def zrem(self, k, *ms):
        for m in ms: self.z.get(k, {}).pop(m, None)
    def zcard(self, k): return len(self.z.get(k, {}))


@pytest.fixture(autouse=True)
def _redis(monkeypatch):
    r = _MiniRedis()
    monkeypatch.setattr(ps, "_client", r)
    return r


def _rec(ms, **kw):
    base = dict(engine="clickhouse", outcome="ok", total_ms=ms, phases={"query": ms * 0.9, "client": 5}, dataset="datasets/1/x.parquet", ops=2)
    base.update(kw); ps.record(**base)


def test_durations_land_in_the_right_bucket():
    for ms in (80, 240, 240, 900, 200_000):
        _rec(ms)
    h = ps.snapshot()["hist"]
    assert h == {"clickhouse|ok|100": 1, "clickhouse|ok|250": 2, "clickhouse|ok|1000": 1, "clickhouse|ok|+Inf": 1}


def test_a_thousand_previews_cost_the_same_keys_as_ten(_redis):
    """Il punto di tutto il disegno: lo spazio NON cresce col traffico."""
    for i in range(10): _rec(300 + i)
    few = len(ps.snapshot()["hist"])
    for i in range(1000): _rec(300 + (i % 40))
    assert len(ps.snapshot()["hist"]) == few


def test_outcomes_and_engines_are_kept_apart():
    _rec(300); _rec(300, outcome="superseded"); _rec(300, engine="polars"); _rec(300, outcome="boh")
    assert set(ps.snapshot()["hist"]) == {"clickhouse|ok|500", "clickhouse|superseded|500", "polars|ok|500", "clickhouse|error|500"}


def test_phase_time_adds_up_and_unknown_phases_are_ignored():
    _rec(1000, phases={"query": 700, "sql": 200, "inventata": 99})
    _rec(1000, phases={"query": 300})
    assert ps.snapshot()["phase_ms"] == {"clickhouse|query": 1000.0, "clickhouse|sql": 200.0}


def test_only_slow_successful_previews_enter_the_ranking():
    _rec(400); _rec(5000, outcome="superseded"); _rec(3000); _rec(9000, dataset="datasets/9/big.parquet")
    top = ps.slowest()
    assert [e["ms"] for e in top] == [9000, 3000] and top[0]["dataset"] == "datasets/9/big.parquet"
    assert top[0]["phases"]["query"] == 8100


def test_the_ranking_is_bounded(_redis):
    for i in range(ps.SLOWEST_KEEP * 6): _rec(2000 + i, dataset=f"d{i}")
    assert _redis.zcard(ps.SLOWEST_KEY) <= ps.SLOWEST_KEEP * 4
    assert len(ps.slowest()) == ps.SLOWEST_KEEP and ps.slowest()[0]["ms"] == 2000 + ps.SLOWEST_KEEP * 6 - 1


def test_entries_older_than_a_day_drop_out_on_read(_redis):
    _rec(5000, now=1_000_000); _rec(4000, now=1_000_000 + ps.SLOWEST_WINDOW_SECONDS)
    assert [e["ms"] for e in ps.slowest(now=1_000_000 + ps.SLOWEST_WINDOW_SECONDS + 10)] == [4000]
    assert _redis.zcard(ps.SLOWEST_KEY) == 1  # ripulito in lettura: nessun job


def test_measuring_never_breaks_what_it_measures(monkeypatch):
    class Boom:
        def pipeline(self): raise ConnectionError("giu'")
        def zrevrange(self, *a): raise ConnectionError("giu'")
    monkeypatch.setattr(ps, "_client", Boom())
    ps.record(engine="x", outcome="ok", total_ms=5000)
    assert ps.slowest() == [] and ps.snapshot()["hist"] == {}


def test_phases_travel_from_engine_to_task_once():
    ps.set_phases({"query": 12.0}, "s3")
    assert ps.take_phases() == ({"query": 12.0}, "s3")
    assert ps.take_phases() == ({}, "")  # non restano appese alla preview successiva


def test_the_collector_emits_cumulative_buckets():
    from app.observability.metrics import _preview_metrics

    for ms in (80, 240, 900): _rec(ms)
    hist = next(m for m in _preview_metrics() if m.name == "dataprep_preview_duration_ms")
    b = {s.labels["le"]: s.value for s in hist.samples if s.name.endswith("_bucket")}
    assert (b["100"], b["250"], b["1000"], b["+Inf"]) == (1, 2, 3, 3)
    assert next(s.value for s in hist.samples if s.name.endswith("_sum")) == 1220.0
