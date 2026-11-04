"""webhooks_loop (services/webhooks.py delivery_loop): deliver_due, its one round."""
from app.db.session import engine
from app.services import webhooks


async def deliver():
    return await webhooks.deliver_due(engine)


TICKS = {"webhooks": deliver}
