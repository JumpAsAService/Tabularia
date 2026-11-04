"""scheduler_loop (services/scheduler.py): one round is _tick, as internal/api/jobs_scheduler.go
names it."""
from app.services import scheduler


async def scheduler_tick():
    return await scheduler._tick()


TICKS = {"scheduler": scheduler_tick}
