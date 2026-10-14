"""Questions about the run history answered by the database, not by loading it.

A year of history is hundreds of thousands of rows: the Flows page used to pull
175,000 of them into Python on every open to draw a calendar of 181 cells, and the
latest run of each flow on the page by reading all their runs (review of 2026-10-14).
"""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import Interval, func, literal_column
from sqlalchemy.orm import aliased
from sqlmodel import Session, select

from app.models import Flow, Run


def latest_flow_runs_query(flow_ids: list[int]):
    """Each flow's latest run: for every flow, its runs newest first, the first one
    (index runs(flow_id, started_at), a few rows per flow). A max() grouped by flow
    read every run of those flows instead."""
    newest = aliased(Run)
    latest_id = (
        select(newest.id)
        .where(newest.flow_id == Flow.id, newest.kind == "flow")
        .order_by(newest.started_at.desc())
        .limit(1)
        .correlate(Flow)
        .scalar_subquery()
    )
    return select(Run.flow_id, Run.status, Run.started_at).where(
        Run.id.in_(select(latest_id).where(Flow.id.in_(flow_ids)))
    )


def latest_flow_runs(session: Session, flow_ids) -> dict[int, tuple[str, datetime]]:
    """{flow id: (status, started_at)} of each flow's latest run."""
    flow_ids = list(flow_ids)
    if not flow_ids:
        return {}
    return {
        flow_id: (status, started_at)
        for flow_id, status, started_at in session.exec(latest_flow_runs_query(flow_ids)).all()
    }


def local_bucket(session: Session, offset: timedelta, granularity: str):
    """The calendar cell of a run's start, in the client's local time: 'YYYY-MM-DD'
    for a day, 'HH' for an hour. `offset` is UTC minus local time."""
    seconds = int(offset.total_seconds())  # an int: safe to write into the statement
    if session.get_bind().dialect.name == "postgresql":
        local = Run.started_at - literal_column(f"INTERVAL '{seconds} seconds'", type_=Interval)
        return func.to_char(local, "HH24" if granularity == "hour" else "YYYY-MM-DD")
    return func.strftime("%H" if granularity == "hour" else "%Y-%m-%d", Run.started_at, f"{-seconds} seconds")
