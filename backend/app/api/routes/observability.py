"""Performance (INTERNAL endpoints, reached by the gateway only and for admins only).

No table of ours behind these routes: the slowest previews are in a fixed-size sorted set
on Valkey, ClickHouse keeps the heavy queries in its `system.query_log` already.
"""
import logging

from fastapi import APIRouter, Query

from app.core.config import get_settings
from app.observability import preview_stats
from app.core.redaction import redact_secrets, redact_storage_args

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
    """The heaviest queries on the external ClickHouse in the last window. It keeps them
    already: we just ask. The previews' queries carry the task's label (`tab-prev:…`), so
    they can be told from the runs'."""
    cfg = get_settings().clickhouse_external
    if not cfg.enabled:
        return {"enabled": False, "queries": []}
    try:
        from app.engine import get_engine

        client = get_engine("clickhouse")._client()
        rows = client.query(
            "SELECT query_start_time, query_kind, type, exception_code, query_duration_ms, read_rows, read_bytes, "
            "memory_usage, log_comment, substring(replaceRegexpAll(query, '\\\\s+', ' '), 1, 4000) "
            "FROM system.query_log WHERE event_time > now() - INTERVAL %(m)s MINUTE AND type IN ('QueryFinish', 'ExceptionWhileProcessing') "
            "AND query_kind IN ('Select', 'Insert') AND query NOT LIKE '%%system.query_log%%' "
            "ORDER BY query_duration_ms DESC LIMIT %(n)s",
            parameters={"m": minutes, "n": limit},
        ).result_rows
    except Exception as e:
        logger.warning("warehouse query_log cannot be read: %s", e)
        return {"enabled": True, "queries": [], "error": str(e)[:200]}
    return {
        "enabled": True,
        "queries": [
            {"at": r[0].isoformat() + "+00:00" if r[0].tzinfo is None else r[0].isoformat(), "kind": r[1], "ok": r[2] == "QueryFinish",
             "exception_code": r[3], "duration_ms": r[4], "read_rows": r[5], "read_bytes": r[6], "memory_bytes": r[7],
             "origin": "preview" if str(r[8]).startswith("tab-prev:") else "run",
             # the text tells WHICH query weighs, not on which key: the arguments of
             # s3()/url() go (see core/redaction.py)
             # CUT AFTER redacting: ClickHouse cut at 160 characters and split `s3(`
             # half way, leaving the access key in clear
             "query": (redact_secrets(redact_storage_args(r[9])) or "")[:200]}
            for r in rows
        ],
    }
