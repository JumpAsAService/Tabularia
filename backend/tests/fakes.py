"""In-memory doubles of the storage and Redis: the tests run without Docker, in ms.

The idea of the fakes: an object that imitates ONLY the methods the real code uses. If
the code calls `storage.download_file(...)`, the fake only needs that method — no real
MinIO needed.
"""
from __future__ import annotations

from collections import defaultdict
from pathlib import Path


class FakeStorage:
    """A fake S3 storage: a dict {(bucket, key): bytes}."""

    def __init__(self):
        self.blobs: dict[tuple[str, str], bytes] = {}

    def create_bucket(self, bucket: str) -> None:
        pass  # idempotent as the real service: nothing to do here

    def upload_file(self, local_path: str, bucket: str, key: str) -> None:
        self.blobs[(bucket, key)] = Path(local_path).read_bytes()

    def download_file(self, bucket: str, key: str, local_path: str) -> None:
        data = self.blobs.get((bucket, key))
        if data is None:
            raise FileNotFoundError(f"{bucket}/{key} non esiste nel FakeStorage")
        Path(local_path).write_bytes(data)

    def delete_object(self, bucket: str, key: str) -> None:
        # idempotent as S3: deleting what is not there is not an error
        self.blobs.pop((bucket, key), None)

    # a convenience for the tests' asserts
    def exists(self, bucket: str, key: str) -> bool:
        return (bucket, key) in self.blobs


class FakeRedis:
    """A fake Redis/Valkey: implements only what StepCache uses."""

    def __init__(self):
        self.sets: dict[str, set] = defaultdict(set)
        self.zsets: dict[str, dict[str, float]] = defaultdict(dict)  # member -> score
        self.counters: dict[str, int] = defaultdict(int)

    # ── SET ──────────────────────────────────────────────────────────────
    def sismember(self, key, member):
        return member in self.sets[key]

    def sadd(self, key, member):
        self.sets[key].add(member)

    def smembers(self, key):
        return set(self.sets[key])

    def srem(self, key, member):
        self.sets[key].discard(member)

    # ── ZSET ─────────────────────────────────────────────────────────────
    def zadd(self, key, mapping):
        self.zsets[key].update(mapping)

    def zrange(self, key, start, stop):
        ordered = sorted(self.zsets[key], key=lambda m: self.zsets[key][m])
        stop = None if stop == -1 else stop + 1
        return ordered[start:stop]

    def zrangebyscore(self, key, lo, hi):
        lo = float("-inf") if lo == "-inf" else float(lo)
        hi = float("inf") if hi == "+inf" else float(hi)
        return [m for m, s in self.zsets[key].items() if lo <= s <= hi]

    def zrem(self, key, member):
        self.zsets[key].pop(member, None)

    def zscore(self, key, member):
        return self.zsets[key].get(member)

    def zremrangebyscore(self, key, lo, hi):
        lo = float("-inf") if lo == "-inf" else float(lo)
        hi = float("inf") if hi == "+inf" else float(hi)
        doomed = [m for m, s in self.zsets[key].items() if lo <= s <= hi]
        for m in doomed:
            del self.zsets[key][m]
        return len(doomed)

    # ── strings (SET NX lock, «too large» steps) ───────────────
    def set(self, key, value, nx=False, ex=None):
        if not hasattr(self, "strings"):
            self.strings: dict[str, str] = {}
        if nx and key in self.strings:
            return None
        self.strings[key] = value
        return True

    def get(self, key):
        return getattr(self, "strings", {}).get(key)

    def exists(self, key):
        return 1 if key in getattr(self, "strings", {}) else 0

    def expire(self, key, seconds):
        return 1 if key in getattr(self, "strings", {}) else 0  # the time itself is not kept

    # ── counters / keys ───────────────────────────────────────────────
    def incr(self, key):
        self.counters[key] += 1
        return self.counters[key]

    def delete(self, *keys):
        for k in keys:
            self.sets.pop(k, None)
            getattr(self, "strings", {}).pop(k, None)
            self.zsets.pop(k, None)
            self.counters.pop(k, None)
            getattr(self, "strings", {}).pop(k, None)


class BrokenRedis:
    """A Redis that always fails: checks the best-effort behavior (Valkey down → the cache
    behaves as empty, never an error for the user)."""

    def __getattr__(self, name):
        import redis

        def _fail(*args, **kwargs):
            raise redis.RedisError("valkey non raggiungibile (finto)")

        return _fail
