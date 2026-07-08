"""Seed idempotente allo startup: crea l'admin da variabili d'ambiente.

Come Grafana/MinIO: le credenziali admin arrivano da env (AUTH__ADMIN_EMAIL /
AUTH__ADMIN_PASSWORD) così in produzione si cambiano velocemente. Se l'utente
admin esiste già NON viene toccato (nessun reset silenzioso della password).
"""
import logging

from sqlmodel import Session, select

from app.core.config import get_settings
from app.core.security import hash_password
from app.db.session import engine
from app.models import User

logger = logging.getLogger(__name__)


def seed_admin() -> None:
    s = get_settings()
    with Session(engine) as session:
        existing = session.exec(select(User).where(User.email == s.auth.admin_email)).first()
        if existing:
            logger.info("Admin '%s' già presente, nessun seed.", s.auth.admin_email)
            return
        admin = User(
            email=s.auth.admin_email,
            full_name=s.auth.admin_name,
            hashed_password=hash_password(s.auth.admin_password.get_secret_value()),
            is_active=True,
            is_superuser=True,
        )
        session.add(admin)
        session.commit()
        logger.info("Admin '%s' creato (superuser).", s.auth.admin_email)
