"""Alembic's environment for the gateway's schema: the SQLModel models are the truth,
`app.db.migra` passes the connection (inside its transaction and the start lock).
See docs/database-migrations.md."""
from alembic import context
from sqlmodel import SQLModel

import app.models  # noqa: F401 — registers every model on SQLModel.metadata
from app.db.migra import COMPARE_OPTIONS


def _run_migrations(connection_) -> None:
    context.configure(connection=connection_, target_metadata=SQLModel.metadata, **COMPARE_OPTIONS)
    with context.begin_transaction():
        context.run_migrations()


connection_ = context.config.attributes.get("connection")
if connection_ is not None:
    _run_migrations(connection_)
else:  # launched without a connection (it should not happen: it goes through app.db.migra)
    from app.db.session import engine

    with engine.begin() as conn:
        _run_migrations(conn)
