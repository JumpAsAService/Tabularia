"""build_loop (services/script_builds.py): its round, as internal/api/jobs_script_builds.go
names it."""
from app.db.session import engine
from app.services import script_builds


async def script_builds_round():
    # the loop's guard: it returns at once where the scripting environments are off
    if script_builds.disabled_reason():
        return None
    return await script_builds.one_round(engine)


TICKS = {"script_builds": script_builds_round}
