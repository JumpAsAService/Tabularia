"""Outgoing datetimes ALWAYS carry the offset.

The DB's TIMESTAMP columns are naive and UTC by convention (scheduler.py). Serialized
bare, they reached the browser as "2026-09-16T20:57:02" and `new Date()` read them as
LOCAL time: every run time showed shifted by the client's offset (in Rome, two hours
earlier), and in the Gantt a run just started, compared with `Date.now()`, seemed to
last two hours. Seen in production. The `UtcDateTime` type makes explicit what the DB
implies.
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


# ── the type, alone ─────────────────────────────────────────────────────────────
def test_a_naive_datetime_is_stamped_utc_in_json():
    assert _Out(t=NAIVE).model_dump(mode="json")["t"] == "2026-09-16T20:57:02+00:00"


def test_an_aware_datetime_keeps_its_own_offset():
    rome = timezone(timedelta(hours=2))
    assert _Out(t=NAIVE.replace(tzinfo=rome)).model_dump(mode="json")["t"] == "2026-09-16T20:57:02+02:00"


def test_python_mode_still_yields_datetimes():
    """The stamp holds only in JSON: whoever works in Python (comparisons, tests) must not
    find a string in place of a datetime."""
    assert isinstance(_Out(t=NAIVE).model_dump()["t"], datetime)


def test_none_stays_none():
    assert _Out(t=None).model_dump(mode="json")["t"] is None


# ── the real schemas ────────────────────────────────────────────────────────────
def test_user_out_created_at_is_stamped():
    js = UserOut(id=1, email="a@x.local", full_name="A", is_active=True, is_superuser=False, created_at=NAIVE).model_dump(mode="json")
    assert js["created_at"] == "2026-09-16T20:57:02+00:00"


def test_flow_out_next_run_at_is_stamped_too():
    """The scheduling too: it is computed aware in the .env's zone and saved naive-UTC."""
    js = FlowOut(id=1, name="f", description="", project_id=1, owner_id=None, next_run_at=datetime(2026, 9, 17, 6, 0)).model_dump(mode="json")
    assert js["next_run_at"] == "2026-09-17T06:00:00+00:00"


async def test_a_run_reloaded_from_the_db_comes_out_with_the_offset(session):
    """The real case: the DB returns naive, the answer must say +00:00 and the value must stay
    the UTC one, not shifted."""
    make_user(session, email="a@x.local", is_superuser=True)
    p = make_project(session, name="p")
    flow = make_flow(session, name="f", project_id=p.id)
    # the model's NOT NULLs without a default: task_id, input_key, output_bucket, output_key
    run = Run(flow_id=flow.id, status="SUCCESS", kind="flow", task_id="t-1",
              input_key="datasets/x.parquet", output_bucket="b", output_key="out/x.parquet")
    run.finished_at = datetime.now(timezone.utc)
    session.add(run); session.commit(); session.refresh(run)
    assert run.started_at.tzinfo is None, "premessa: dal DB torna naive"

    js = RunOut(id=run.id, status=run.status, launched_by=None, output_key=run.output_key, rows_written=None,
                error=None, publish_name=None, datasource_id=None,
                started_at=run.started_at, finished_at=run.finished_at).model_dump(mode="json")
    for field in ("started_at", "finished_at"):
        parsed = datetime.fromisoformat(js[field])
        assert parsed.utcoffset() == timedelta(0), (field, js[field])
    assert datetime.fromisoformat(js["started_at"]).replace(tzinfo=None) == run.started_at
