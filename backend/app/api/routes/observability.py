"""Prestazioni (endpoint INTERNI, raggiunti solo dal gateway e solo per gli admin).

Nessuna tabella nostra dietro queste rotte: le preview più lente stanno in un
sorted set a dimensione fissa su Valkey, le query pesanti le conserva già
ClickHouse nel suo `system.query_log`.
"""
import logging

from fastapi import APIRouter, Query

from app.core.config import get_settings
from app.observability import preview_stats

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/observability", tags=["observability"])


@router.get("/previews")
def previews(limit: int = Query(25, ge=1, le=50)):
    snap = preview_stats.snapshot()
    totals: dict[str, dict] = {}
    for key, n in snap["hist"].items():
        engine, outcome, _le = key.split("|", 2)
        t = totals.setdefault(engine, {"engine": engine, "ok": 0, "superseded": 0, "error": 0, "ok_ms": 0.0, "phases": {}})
        t[outcome] = t.get(outcome, 0) + n
    for key, ms in snap["sum_ms"].items():
        engine, outcome = key.split("|", 1)
        if outcome == "ok" and engine in totals:
            totals[engine]["ok_ms"] = ms
    for key, ms in snap["phase_ms"].items():
        engine, phase = key.split("|", 1)
        if engine in totals:
            totals[engine]["phases"][phase] = round(ms)
    engines = []
    for t in totals.values():
        t["avg_ok_ms"] = round(t.pop("ok_ms") / t["ok"]) if t["ok"] else None
        engines.append(t)
    return {"engines": sorted(engines, key=lambda t: -(t["ok"] + t["superseded"] + t["error"])),
            "slowest": preview_stats.slowest(limit), "window_hours": preview_stats.SLOWEST_WINDOW_SECONDS // 3600}


@router.get("/warehouse")
def warehouse(minutes: int = Query(60, ge=5, le=1440), limit: int = Query(20, ge=1, le=50)):
    """Le query più pesanti sul ClickHouse esterno nell'ultima finestra. Le tiene
    già lui: noi chiediamo e basta. Le query delle preview portano l'etichetta
    del task (`tab-prev:…`), quindi si distinguono da quelle dei run."""
    cfg = get_settings().clickhouse_external
    if not cfg.enabled:
        return {"enabled": False, "queries": []}
    try:
        from app.engine import get_engine

        client = get_engine("clickhouse")._client()
        rows = client.query(
            "SELECT query_start_time, query_kind, type, exception_code, query_duration_ms, read_rows, read_bytes, "
            "memory_usage, log_comment, substring(replaceRegexpAll(query, '\\\\s+', ' '), 1, 160) "
            "FROM system.query_log WHERE event_time > now() - INTERVAL %(m)s MINUTE AND type IN ('QueryFinish', 'ExceptionWhileProcessing') "
            "AND query_kind IN ('Select', 'Insert') AND query NOT LIKE '%%system.query_log%%' "
            "ORDER BY query_duration_ms DESC LIMIT %(n)s",
            parameters={"m": minutes, "n": limit},
        ).result_rows
    except Exception as e:
        logger.warning("warehouse query_log non leggibile: %s", e)
        return {"enabled": True, "queries": [], "error": str(e)[:200]}
    return {
        "enabled": True,
        "queries": [
            {"at": r[0].isoformat() + "+00:00" if r[0].tzinfo is None else r[0].isoformat(), "kind": r[1], "ok": r[2] == "QueryFinish",
             "exception_code": r[3], "duration_ms": r[4], "read_rows": r[5], "read_bytes": r[6], "memory_bytes": r[7],
             "origin": "preview" if str(r[8]).startswith("tab-prev:") else "run", "query": r[9]}
            for r in rows
        ],
    }
