"""retention_loop (services/audit_retention.py): its rounds, as internal/jobs/retention names them."""
import asyncio

from sqlmodel import Session

from app.core.config import get_settings
from app.db.session import engine
from app.services import audit_retention


def _round(addresses: bool) -> None:
    settings = get_settings()
    if not audit_retention.to_do(settings):
        return None
    with Session(engine) as session:
        audit_retention.one_round(session, settings, None, addresses)


async def retention():
    return await asyncio.to_thread(_round, True)


async def retention_rows():
    return await asyncio.to_thread(_round, False)


TICKS = {"retention": retention, "retention.rows": retention_rows}
