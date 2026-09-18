"""Prestazioni delle preview, senza tabelle e senza RAM di processo.

Una riga per preview sarebbe una tabella da migliaia di righe al giorno, con
retention e indici da gestire; una lista in memoria si perderebbe a ogni riciclo
dei worker (ogni 50 task) e ogni processo vedrebbe solo le sue. Qui invece:

- ISTOGRAMMI su Valkey: contatori per secchio di durata, per motore ed esito.
  Poche centinaia di chiavi, FISSE qualunque sia il traffico. Il collector li
  espone a VictoriaMetrics, che ne tiene lo storico (mesi) e i percentili.
- le N PIU' LENTE delle ultime 24 ore, in un sorted set a dimensione limitata:
  l'unica cosa che un istogramma non può dire è QUALE dataset era lento.

Tutto best-effort: misurare non deve mai far fallire ciò che misura.
"""
from __future__ import annotations

import json
import logging
import threading
import time

import redis

from app.core.config import get_settings

logger = logging.getLogger(__name__)

# limiti superiori dei secchi, in millisecondi (l'ultimo è +Inf)
BUCKETS_MS = (100, 250, 500, 1000, 2500, 5000, 10000, 30000, 60000, 120000)
OUTCOMES = ("ok", "superseded", "error")
PHASES = ("client", "stepcache", "sql", "query", "decode")
SLOWEST_KEEP = 50
SLOWEST_WINDOW_SECONDS = 24 * 3600
# sotto questa durata una preview non entra nella classifica: non è "lenta"
SLOWEST_MIN_MS = 1000

HIST_KEY = "dataprep:metrics:preview:hist"      # hash  "<engine>|<outcome>|<le>" → n
SUM_KEY = "dataprep:metrics:preview:sum_ms"     # hash  "<engine>|<outcome>" → ms
PHASE_KEY = "dataprep:metrics:preview:phase_ms"  # hash  "<engine>|<phase>" → ms
SLOWEST_KEY = "dataprep:metrics:preview:slowest"  # zset  json → durata ms

_local = threading.local()
_client: redis.Redis | None = None


def _r() -> redis.Redis:
    global _client
    if _client is None:
        _client = redis.Redis.from_url(get_settings().redis.url, decode_responses=True)
    return _client


# ── fasi: l'engine le dichiara, il task le raccoglie ────────────────────────────
def set_phases(phases: dict[str, float], source: str = "") -> None:
    """Chiamata dall'engine a fine preview. Thread-local: un worker Celery esegue
    un task per volta per processo, e l'API è multi-thread."""
    _local.phases = dict(phases)
    _local.source = source


def take_phases() -> tuple[dict[str, float], str]:
    out = getattr(_local, "phases", {}) or {}, getattr(_local, "source", "") or ""
    _local.phases, _local.source = {}, ""
    return out


def _bucket(ms: float) -> str:
    for le in BUCKETS_MS:
        if ms <= le:
            return str(le)
    return "+Inf"


KNOWN_ENGINES = ("polars", "duckdb", "chdb", "clickhouse", "bigquery")


def _engine_label(engine: str | None) -> str:
    """Solo nomi noti: il motore arriva dalla richiesta dell'utente, e un nome
    libero creerebbe chiavi (e serie Prometheus) senza limite."""
    e = (engine or "polars").strip().lower()
    return e if e in KNOWN_ENGINES else "unknown"


def record(*, engine: str, outcome: str, total_ms: float, phases: dict[str, float] | None = None,
           dataset: str = "", ops: int = 0, source: str = "", cache: bool = True, now: float | None = None) -> None:
    engine = _engine_label(engine)
    outcome = outcome if outcome in OUTCOMES else "error"
    now = time.time() if now is None else now
    try:
        p = _r().pipeline()
        p.hincrby(HIST_KEY, f"{engine}|{outcome}|{_bucket(total_ms)}", 1)
        p.hincrbyfloat(SUM_KEY, f"{engine}|{outcome}", round(total_ms, 1))
        for name, ms in (phases or {}).items():
            if name in PHASES and ms > 0:
                p.hincrbyfloat(PHASE_KEY, f"{engine}|{name}", round(ms, 1))
        if outcome == "ok" and total_ms >= SLOWEST_MIN_MS:
            entry = {
                "at": round(now), "ms": round(total_ms), "engine": engine, "dataset": dataset, "ops": ops,
                "source": source, "cache": cache,
                "phases": {k: round(v) for k, v in (phases or {}).items() if k in PHASES and v >= 1},
            }
            # prima via le voci SCADUTE, o una giornata di grandi lente vecchie
            # terrebbe fuori quelle di oggi (il set e' piccolo: leggerlo costa niente)
            try:
                stale = [m for m in _r().zrevrange(SLOWEST_KEY, 0, -1)
                         if now - (json.loads(m).get("at", 0)) > SLOWEST_WINDOW_SECONDS]
                if stale:
                    p.zrem(SLOWEST_KEY, *stale)
            except Exception:
                pass
            p.zadd(SLOWEST_KEY, {json.dumps(entry, sort_keys=True): total_ms})
            # dimensione LIMITATA: restano solo le più lente
            p.zremrangebyrank(SLOWEST_KEY, 0, -(SLOWEST_KEEP * 4) - 1)
            p.expire(SLOWEST_KEY, SLOWEST_WINDOW_SECONDS * 2)
        p.execute()
    except Exception as e:  # Valkey giù, o qualunque altra cosa: la preview è già riuscita
        logger.debug("preview_stats.record ignorato: %s", e)


def slowest(limit: int = SLOWEST_KEEP, now: float | None = None) -> list[dict]:
    """Le più lente delle ultime 24 ore. Le voci scadute si tolgono qui, in
    lettura: il set resta piccolo senza un job di pulizia."""
    now = time.time() if now is None else now
    try:
        raw = _r().zrevrange(SLOWEST_KEY, 0, -1)
    except Exception:
        return []
    out, stale = [], []
    for item in raw:
        try:
            e = json.loads(item)
        except ValueError:
            stale.append(item); continue
        if now - e.get("at", 0) > SLOWEST_WINDOW_SECONDS:
            stale.append(item)
        elif len(out) < limit:
            out.append(e)
    if stale:
        try:
            _r().zrem(SLOWEST_KEY, *stale)
        except Exception:
            pass
    return out


def snapshot() -> dict:
    """Istogrammi correnti, per il collector Prometheus e per la tab admin."""
    try:
        p = _r().pipeline()
        p.hgetall(HIST_KEY); p.hgetall(SUM_KEY); p.hgetall(PHASE_KEY)
        hist, sums, phases = p.execute()
    except Exception:
        return {"hist": {}, "sum_ms": {}, "phase_ms": {}}
    return {"hist": {k: int(v) for k, v in hist.items()}, "sum_ms": {k: float(v) for k, v in sums.items()},
            "phase_ms": {k: float(v) for k, v in phases.items()}}
