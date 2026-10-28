"""Idempotent seed at startup: creates the admin from environment variables.

As Grafana/MinIO do: the admin credentials come from the env (AUTH__ADMIN_EMAIL /
AUTH__ADMIN_PASSWORD) so in production they are changed quickly. When the admin user
exists already it is NOT touched (no silent reset of the password).

One exception, and one refusal, so that no installation comes up with a password
everyone knows:
- the administrator is never CREATED with an empty, published or short password
  (config.admin_password_problem), in any environment;
- an administrator that still has a published password (`admin`, from an older
  release whose code and example settings defaulted to it) gets the configured
  one, when that one is good, and its open sessions end; with no good one the
  gateway does not start. A password chosen by its owner is never touched.
AUTH__ALLOW_KNOWN_ADMIN_PASSWORD=true lets a local development stack keep a fixed
password; production ignores it.
"""
import logging

from sqlalchemy import delete
from sqlmodel import Session, select

from app.core.config import admin_password_problem, get_settings
from app.core.security import hash_password, verify_password
from app.db.session import engine
from app.models import User, UserSession
from app.services import audit, first_run

logger = logging.getLogger(__name__)

STARTUP_ACTOR = "startup"  # the audit's actor of the administrator created from the environment


# the passwords an older release actually handed out: the code and example default,
# and the getting-started guide's example. One bcrypt check each, at every start.
HANDED_OUT = ("admin", "Change-me-2026")


def _published_password_of(user: User) -> str | None:
    """The published password this account still has, if any."""
    if not user.hashed_password:
        return None
    return next((p for p in HANDED_OUT if verify_password(p, user.hashed_password)), None)


def seed_admin() -> None:
    s = get_settings()
    password = s.auth.admin_password.get_secret_value()
    if not password and first_run.enabled(s):
        # the try-it stack with nothing configured: the first administrator is created
        # in the browser, at the first sign-in (services/first_run.py)
        logger.info("No AUTH__ADMIN_PASSWORD: while no account exists, the sign-in page creates the administrator.")
        return
    problem = admin_password_problem(password)
    allowed = s.auth.allow_known_admin_password and not s.is_production()
    with Session(engine) as session:
        existing = session.exec(select(User).where(User.email == s.auth.admin_email)).first()
        if existing:
            published = _published_password_of(existing)
            if published is None:
                logger.info("Admin '%s' present already, no seed.", s.auth.admin_email)
                return
            if allowed:
                logger.warning("The administrator '%s' has a published password; kept because "
                               "AUTH__ALLOW_KNOWN_ADMIN_PASSWORD=true (development only).", s.auth.admin_email)
                return
            if problem:
                raise RuntimeError(
                    f"The administrator '{s.auth.admin_email}' still has the password '{published}', which "
                    f"everyone can try, and AUTH__ADMIN_PASSWORD cannot replace it ({problem}). Set "
                    f"AUTH__ADMIN_PASSWORD to a password of your own (at least 12 characters) and start again: "
                    f"the administrator will get it."
                )
            existing.hashed_password = hash_password(password)
            session.add(existing)
            # whoever came in with the published password is out
            session.exec(delete(UserSession).where(UserSession.user_id == existing.id))
            session.commit()
            logger.warning("The administrator '%s' had a published password: it now has "
                           "AUTH__ADMIN_PASSWORD, and its open sessions were closed.", s.auth.admin_email)
            return
        if problem and (not allowed or not password):
            raise RuntimeError(
                f"Cannot create the administrator '{s.auth.admin_email}': {problem}. Set AUTH__ADMIN_PASSWORD "
                f"to a password of your own (at least 12 characters, not 'admin') and start again."
            )
        admin = User(
            email=s.auth.admin_email,
            full_name=s.auth.admin_name,
            hashed_password=hash_password(password),
            is_active=True,
            is_superuser=True,
        )
        session.add(admin)
        session.commit()
        session.refresh(admin)
        logger.info("Admin '%s' created (superuser).", s.auth.admin_email)
        # in the audit as the panel's creations are, by no one: by the start-up, from the
        # environment (an administrator deleted comes back at the next start, and that shows)
        for action, detail in ((audit.USER_CREATE, {"via": "startup"}),
                               (audit.USER_PROMOTE, {"at_creation": True, "via": "startup"})):
            audit.record_audit(session, actor_label=STARTUP_ACTOR, action=action, target_type="user",
                               target_id=admin.id, target_label=admin.email, detail=detail)
