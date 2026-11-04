"""rollout_loop (services/settings_rollout.py): its step, as internal/jobs/settingsrollout
names it."""
from app.db.session import engine
from app.services import settings_rollout


async def settings_rollout_step():
    # the loop's guard: it returns at once off Kubernetes or without the chart's right
    if settings_rollout.mode() != "kubernetes":
        return None
    return await settings_rollout.one_step(engine)


TICKS = {"settings_rollout": settings_rollout_step}
