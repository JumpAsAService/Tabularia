"""Carico previsionale degli schedule: heatmap giorno×ora dei cron attivi e
rilevamento delle fasce critiche (collisioni oltre la capacità dei worker).
Vedi app/services/schedule_load.py. Filtrato per RBAC (progetti leggibili).
"""
import logging

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlmodel import Session

from app.db.session import get_session
from app.deps.auth import get_current_user
from app.models import User
from app.services.schedule_load import compute_schedule_load

logger = logging.getLogger(__name__)

router = APIRouter(tags=["scheduling"])


class LoadCell(BaseModel):
    weekday: int          # 0 = lunedì … 6 = domenica
    hour: int             # 0–23
    count: int            # n. di fire schedulati nella cella (sui prossimi N giorni)
    peak_concurrent: int  # massimo n. di job nello stesso minuto dentro la cella
    critical: bool        # picco oltre la capacità dei worker


class Collision(BaseModel):
    weekday: int
    hour: int
    minute: int
    count: int            # job che partono nello stesso minuto
    queued: int           # quanti finiscono in coda (count - capacità)
    schedules: list[str]  # nomi dei job coinvolti


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
    days: int = Query(7, ge=1, le=31, description="orizzonte di proiezione in giorni"),
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """Proietta gli schedule attivi (flussi + refresh datasource) sui prossimi
    `days` giorni e restituisce la griglia giorno×ora del carico + le fasce
    critiche (collisioni oltre la capacità dei worker)."""
    return compute_schedule_load(session, user, days)
