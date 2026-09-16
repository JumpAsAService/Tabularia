"""I datetime in uscita portano SEMPRE l'offset.

Le colonne TIMESTAMP del DB sono naive e per convenzione UTC (scheduler.py).
Serializzate nude, arrivavano al browser come "2026-09-16T20:57:02" e
`new Date()` le leggeva come ora LOCALE: ogni orario dei run appariva spostato
dell'offset del client (a Roma, due ore prima), e nel Gantt un run appena
partito, confrontato con `Date.now()`, sembrava durare due ore. Visto in
produzione. Il tipo `UtcDateTime` esplicita cio' che il DB sottintende.
"""
from datetime import datetime, timedelta, timezone
from typing import Optional

import pytest
from pydantic import BaseModel

from app.models import Run
from app.schemas.models import FlowOut, RunOut, UserOut, UtcDateTime
from tests.conftest import make_flow, make_project, make_user

pytestmark = pytest.mark.anyio
NAIVE = datetime(2026, 9, 16, 20, 57, 2)


class _Out(BaseModel):
    t: Optional[UtcDateTime] = None


# ── il tipo, da solo ────────────────────────────────────────────────────────────
def test_a_naive_datetime_is_stamped_utc_in_json():
    assert _Out(t=NAIVE).model_dump(mode="json")["t"] == "2026-09-16T20:57:02+00:00"


def test_an_aware_datetime_keeps_its_own_offset():
    rome = timezone(timedelta(hours=2))
    assert _Out(t=NAIVE.replace(tzinfo=rome)).model_dump(mode="json")["t"] == "2026-09-16T20:57:02+02:00"


def test_python_mode_still_yields_datetimes():
    """Il timbro vale solo in JSON: chi lavora in Python (confronti, test) non
    deve trovarsi una stringa al posto di un datetime."""
    assert isinstance(_Out(t=NAIVE).model_dump()["t"], datetime)


def test_none_stays_none():
    assert _Out(t=None).model_dump(mode="json")["t"] is None


# ── gli schemi veri ─────────────────────────────────────────────────────────────
def test_user_out_created_at_is_stamped():
    js = UserOut(id=1, email="a@x.local", full_name="A", is_active=True, is_superuser=False, created_at=NAIVE).model_dump(mode="json")
    assert js["created_at"] == "2026-09-16T20:57:02+00:00"


def test_flow_out_next_run_at_is_stamped_too():
    """Anche la schedulazione: e' calcolata aware nel fuso dell'.env e salvata naive-UTC."""
    js = FlowOut(id=1, name="f", description="", project_id=1, owner_id=None, next_run_at=datetime(2026, 9, 17, 6, 0)).model_dump(mode="json")
    assert js["next_run_at"] == "2026-09-17T06:00:00+00:00"


async def test_a_run_reloaded_from_the_db_comes_out_with_the_offset(session):
    """Il caso reale: il DB restituisce naive, la risposta deve dire +00:00 e
    il valore deve restare quello UTC, non spostato."""
    make_user(session, email="a@x.local", is_superuser=True)
    p = make_project(session, name="p")
    flow = make_flow(session, name="f", project_id=p.id)
    # i NOT NULL senza default del modello: task_id, input_key, output_bucket, output_key
    run = Run(flow_id=flow.id, status="SUCCESS", kind="flow", task_id="t-1",
              input_key="datasets/x.parquet", output_bucket="b", output_key="out/x.parquet")
    run.finished_at = datetime.now(timezone.utc)
    session.add(run); session.commit(); session.refresh(run)
    assert run.started_at.tzinfo is None, "premessa: dal DB torna naive"

    js = RunOut(id=run.id, status=run.status, launched_by=None, output_key=run.output_key, rows_written=None,
                error=None, publish_name=None, datasource_id=None,
                started_at=run.started_at, finished_at=run.finished_at).model_dump(mode="json")
    for campo in ("started_at", "finished_at"):
        parsed = datetime.fromisoformat(js[campo])
        assert parsed.utcoffset() == timedelta(0), (campo, js[campo])
    assert datetime.fromisoformat(js["started_at"]).replace(tzinfo=None) == run.started_at
