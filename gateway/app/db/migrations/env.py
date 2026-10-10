"""L'ambiente di Alembic per lo schema del gateway: i modelli SQLModel sono la
verità, la connessione la passa `app.db.migra` (dentro la sua transazione e il
lucchetto di avvio). Vedi docs/database-migrations.md."""
from alembic import context
from sqlmodel import SQLModel

import app.models  # noqa: F401 — registra tutti i modelli su SQLModel.metadata
from app.db.migra import OPZIONI_CONFRONTO


def _esegui(connessione) -> None:
    context.configure(connection=connessione, target_metadata=SQLModel.metadata, **OPZIONI_CONFRONTO)
    with context.begin_transaction():
        context.run_migrations()


connessione = context.config.attributes.get("connection")
if connessione is not None:
    _esegui(connessione)
else:  # lanciato senza una connessione (non dovrebbe succedere: si passa da app.db.migra)
    from app.db.session import engine

    with engine.begin() as conn:
        _esegui(conn)
