"""The rows a test writes for its report are remembered in Valkey for an hour at most, the heavy
ones for minutes: the cache has no ceiling of its own, and a key is born for every alpha."""
from app.engine import rowcount
from app.engine.stats.output import OUTPUT_NAMES
from app.tasks import report_jobs


class Recorder:
    def __init__(self):
        self.set_with: dict[str, int] = {}

    def set(self, key, value, ex=None, **_):
        self.set_with[key] = ex


def _rows(n: int, pad: int = 0) -> list[dict]:
    return [{name: ("x" * pad if name == OUTPUT_NAMES[0] else 1) for name in OUTPUT_NAMES} for _ in range(n)]


def test_the_rows_live_an_hour_and_the_heavy_ones_minutes(monkeypatch):
    client = Recorder()
    monkeypatch.setattr(rowcount, "_redis", lambda: client)
    monkeypatch.setattr(rowcount, "_ttl", lambda: 86400)
    report_jobs._stat_rows_remember("small", _rows(5))
    report_jobs._stat_rows_remember("heavy", _rows(5, pad=300_000))
    report_jobs._stat_rows_remember("too heavy", _rows(5, pad=report_jobs.STAT_ROWS_MAX_BYTES))
    assert client.set_with == {"small": 3600, "heavy": 600}


def test_the_cache_ttl_of_the_installation_is_a_ceiling(monkeypatch):
    client = Recorder()
    monkeypatch.setattr(rowcount, "_redis", lambda: client)
    monkeypatch.setattr(rowcount, "_ttl", lambda: 120)
    report_jobs._stat_rows_remember("small", _rows(5))
    assert client.set_with == {"small": 120}
