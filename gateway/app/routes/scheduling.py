"""The schedules' forecast load: day×hour heatmap of the active crons and detection of
the critical slots (collisions beyond the workers' capacity).
See app/services/schedule_load.py. Filtered by RBAC (readable projects).
"""
import logging

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlmodel import Session

from app.db.session import get_session
from app.deps.auth import get_current_user
from app.models import User
from app.services.schedule_load import compute_schedule_load
from app.core.routing import ReleasingRoute

logger = logging.getLogger(__name__)

router = APIRouter(route_class=ReleasingRoute, tags=["scheduling"])


class LoadCell(BaseModel):
    weekday: int          # 0 = Monday … 6 = Sunday
    hour: int             # 0–23
    count: int            # number of scheduled fires in the cell (over the next N days)
    peak_concurrent: int  # the maximum number of jobs in the same minute inside the cell
    critical: bool        # a peak beyond the workers' capacity


class Collision(BaseModel):
    weekday: int
    hour: int
    minute: int
    count: int            # jobs starting in the same minute
    queued: int           # how many end up queued (count - capacity)
    schedules: list[str]  # the names of the jobs involved


class ScheduleLoadOut(BaseModel):
    days: int
    timezone: str
    worker_capacity: int
    total_schedules: int
    total_firings: int
    cells: list[LoadCell]
    collisions: list[Collision]


@router.get("/schedule/load", response_model=ScheduleLoadOut)
def schedule_load(
    days: int = Query(7, ge=1, le=31, description="projection horizon in days"),
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """Projects the active schedules (flows + datasource refreshes) over the next `days`
    days and returns the day×hour grid of the load + the critical slots (collisions
    beyond the workers' capacity)."""
    return compute_schedule_load(session, user, days)
