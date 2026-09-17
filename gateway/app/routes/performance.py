"""Prestazioni (solo admin). Nessuna tabella nuova dietro questa pagina.

- i RUN: la tabella `runs` sa già inizio, presa in carico dal worker, fine, righe
  ed esito — gli aggregati si calcolano qui, al volo, sulla finestra richiesta;
- le PREVIEW: contatori a dimensione fissa su Valkey, tenuti dal backend;
- il WAREHOUSE: il `query_log` che ClickHouse conserva da sé.
I grafici nel tempo stanno in Grafana (pagina Monitoring), che legge le stesse
metriche da VictoriaMetrics.
"""
from datetime import datetime, timedelta, timezone
from statistics import median

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlmodel import Session, select

from app.core.engine_client import get_engine_client
from app.db.session import get_session
from app.deps.auth import require_superuser
from app.models import Datasource, Flow, Run

router = APIRouter(prefix="/admin/performance", tags=["performance"], dependencies=[Depends(require_superuser)])

# tetto alle righe lette: la finestra è scelta dall'admin, ma un'installazione con
# milioni di run non deve poter piantare il gateway con una pagina di statistiche
MAX_RUNS = 20_000


def _pct(values: list[float], q: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    return s[min(len(s) - 1, int(round(q * (len(s) - 1))))]


def _utc(dt: datetime | None) -> datetime | None:
    return dt if dt is None or dt.tzinfo else dt.replace(tzinfo=timezone.utc)


@router.get("/runs")
def runs_performance(days: int = Query(7, ge=1, le=90), session: Session = Depends(get_session)):
    since = (datetime.now(timezone.utc) - timedelta(days=days)).replace(tzinfo=None)  # colonne naive-UTC
    rows = session.exec(
        select(Run).where(Run.started_at >= since).order_by(Run.started_at.desc()).limit(MAX_RUNS)  # type: ignore[attr-defined]
    ).all()
    flows = {f.id: f.name for f in session.exec(select(Flow)).all()}
    sources = {d.id: d.name for d in session.exec(select(Datasource)).all()}

    groups: dict[tuple[str, int | None], dict] = {}
    for r in rows:
        kind = "ingest" if r.kind == "ingest" else "flow"
        ident = r.datasource_id if kind == "ingest" else r.flow_id
        g = groups.setdefault((kind, ident), {
            "kind": kind, "id": ident,
            # un run sopravvive al suo flusso/datasource: il nome è None e la pagina lo dice
            "name": (sources if kind == "ingest" else flows).get(ident),
            "runs": 0, "failures": 0, "durations": [], "waits": [], "rows": 0, "last_error": None, "last_run_at": None,
        })
        g["runs"] += 1
        started, finished, taken = _utc(r.started_at), _utc(r.finished_at), _utc(r.engine_started_at)
        if g["last_run_at"] is None:
            g["last_run_at"] = started
        if r.status == "FAILURE":
            g["failures"] += 1
            g["last_error"] = g["last_error"] or (r.error or "")[:240]
        if r.status == "SUCCESS" and started and finished:
            # tempo di ESECUZIONE: da quando il worker lo prende in carico. La coda si
            # mostra a parte — sommarla farebbe sembrare lento un flusso veloce che
            # ha solo aspettato il suo turno
            g["durations"].append((finished - (taken or started)).total_seconds())
            g["rows"] += r.rows_written or 0
        if started and taken:  # quanto ha aspettato un worker libero
            g["waits"].append(max(0.0, (taken - started).total_seconds()))

    items = []
    for g in groups.values():
        d, w = g.pop("durations"), g.pop("waits")
        items.append({**g, "median_s": median(d) if d else None, "p95_s": _pct(d, 0.95), "max_s": max(d) if d else None,
                      "total_s": sum(d), "median_wait_s": median(w) if w else None, "p95_wait_s": _pct(w, 0.95)})
    items.sort(key=lambda x: -(x["total_s"] or 0))
    waits = [max(0.0, (_utc(r.engine_started_at) - _utc(r.started_at)).total_seconds()) for r in rows if r.engine_started_at and r.started_at]
    return {
        "days": days, "truncated": len(rows) >= MAX_RUNS,
        "totals": {"runs": len(rows), "failures": sum(1 for r in rows if r.status == "FAILURE"),
                   "median_wait_s": median(waits) if waits else None, "p95_wait_s": _pct(waits, 0.95)},
        "items": items[:100],
    }


async def _engine(path: str, params: dict) -> dict:
    resp = await get_engine_client().get(path, params=params)
    if resp.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"L'engine non risponde su {path} ({resp.status_code})")
    return resp.json()


@router.get("/previews")
async def previews_performance(limit: int = Query(25, ge=1, le=50)):
    return await _engine("/observability/previews", {"limit": limit})


@router.get("/warehouse")
async def warehouse_performance(minutes: int = Query(60, ge=5, le=1440), limit: int = Query(20, ge=1, le=50)):
    return await _engine("/observability/warehouse", {"minutes": minutes, "limit": limit})
