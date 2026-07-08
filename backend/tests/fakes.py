"""Doppioni in-memory di storage e Redis: i test girano senza Docker, in ms.

L'idea dei "fake": un oggetto finto che imita SOLO i metodi che il codice vero
usa. Se il codice chiama `storage.download_file(...)`, al fake basta avere
quel metodo — non serve un vero MinIO.
"""
from __future__ import annotations

from collections import defaultdict
from pathlib import Path


class FakeStorage:
    """Storage S3 finto: un dizionario {(bucket, key): bytes}."""

    def __init__(self):
        self.blobs: dict[tuple[str, str], bytes] = {}

    def upload_file(self, local_path: str, bucket: str, key: str) -> None:
        self.blobs[(bucket, key)] = Path(local_path).read_bytes()

    def download_file(self, bucket: str, key: str, local_path: str) -> None:
        data = self.blobs.get((bucket, key))
        if data is None:
            raise FileNotFoundError(f"{bucket}/{key} non esiste nel FakeStorage")
        Path(local_path).write_bytes(data)

    def delete_object(self, bucket: str, key: str) -> None:
        # idempotente come S3: cancellare ciò che non c'è non è un errore
        self.blobs.pop((bucket, key), None)

    # comodità per gli assert nei test
    def exists(self, bucket: str, key: str) -> bool:
        return (bucket, key) in self.blobs


class FakeRedis:
    """Redis/Valkey finto: implementa solo ciò che usa StepCache."""

    def __init__(self):
        self.sets: dict[str, set] = defaultdict(set)
        self.zsets: dict[str, dict[str, float]] = defaultdict(dict)  # membro -> score
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

    # ── contatori / chiavi ───────────────────────────────────────────────
    def incr(self, key):
        self.counters[key] += 1
        return self.counters[key]

    def delete(self, *keys):
        for k in keys:
            self.sets.pop(k, None)
            self.zsets.pop(k, None)
            self.counters.pop(k, None)


class BrokenRedis:
    """Redis che fallisce sempre: verifica il comportamento best-effort
    (Valkey giù → la cache si comporta come vuota, mai un errore all'utente)."""

    def __getattr__(self, name):
        import redis

        def _fail(*args, **kwargs):
            raise redis.RedisError("valkey non raggiungibile (finto)")

        return _fail
