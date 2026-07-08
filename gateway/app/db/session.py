from sqlmodel import create_engine, Session, SQLModel

from app.core.config import get_settings

# pool_pre_ping: evita connessioni morte dopo idle; utile in dev con restart di Postgres.
engine = create_engine(get_settings().db.dsn, echo=False, pool_pre_ping=True)


def init_db() -> None:
    """Crea le tabelle se non esistono. Per l'MVP niente Alembic: create_all basta.
    (Quando lo schema evolverà in produzione → introdurre le migration Alembic.)"""
    import app.models  # noqa: F401 — registra i modelli su SQLModel.metadata
    SQLModel.metadata.create_all(engine)


def get_session():
    with Session(engine) as session:
        yield session
