"""reapply_loop (services/kube_scaling.py): its round, as internal/jobs/scaling names it."""
from app.db.session import engine
from app.services import kube_scaling


async def scaling():
    # the loop's guard: it returns at once where KUBERNETES__SCALING is off
    if not kube_scaling.enabled():
        return None
    return await kube_scaling.one_round(engine)


TICKS = {"scaling": scaling}
