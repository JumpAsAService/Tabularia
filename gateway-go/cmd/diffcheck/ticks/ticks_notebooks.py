"""reaper_loop (services/notebook_runtime.py): its round, as internal/api/jobs_notebooks.go
names it."""
from app.db.session import engine
from app.services import notebook_runtime


async def notebooks():
    # the loop's guard: it returns at once where the scripting environments are off
    if notebook_runtime.disabled_reason():
        return None
    return await notebook_runtime.one_round(engine)


TICKS = {"notebooks": notebooks}
