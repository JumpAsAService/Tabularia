"""engine_settings_loop, siem_loop, housekeeping_loop: one round each, the services' own
one-round functions (internal/api/jobs_engine_settings.go, jobs_siem.go,
internal/jobs/housekeeping name the same rounds)."""
import asyncio

from app.db.session import engine
from app.services import engine_panel_settings, housekeeping, siem


async def engine_settings():
    return await engine_panel_settings.one_round(engine)


async def siem_round():
    return await siem.one_round(engine)


async def housekeeping_round():
    return await asyncio.to_thread(housekeeping._round)


TICKS = {"engine_settings": engine_settings, "siem": siem_round, "housekeeping": housekeeping_round}
