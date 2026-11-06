"""backup_loop (services/db_backup.py): one_round, its one round."""
from app.services import db_backup


async def backup():
    return await db_backup.one_round()


TICKS = {"backup": backup}
