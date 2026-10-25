"""Password hashing (bcrypt) and JWT tokens. Minimal and readable on purpose."""
from datetime import datetime, timedelta, timezone
import bcrypt
import jwt

from app.core.config import get_settings


# ── Password ────────────────────────────────────────────────────────────────
def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode(), bcrypt.gensalt()).decode()


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode(), hashed.encode())
    except ValueError:
        # malformed hash / not bcrypt
        return False


# ── JWT ───────────────────────────────────────────────────────────────────────
def create_access_token(subject: int | str, session_id: str, expires_at: datetime | None = None) -> str:
    """A session's token (see services/sessions.py, which is who asks for it): it holds as
    long as the `session_id` row exists, and never past `expires_at`."""
    s = get_settings()
    now = datetime.now(timezone.utc)
    if expires_at is None:
        expires_at = now + timedelta(minutes=s.jwt.access_ttl_minutes)
    elif expires_at.tzinfo is None:  # the columns are naive UTC
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    payload = {"sub": str(subject), "sid": session_id, "iat": now, "exp": expires_at}
    return jwt.encode(payload, s.jwt.secret.get_secret_value(), algorithm=s.jwt.algorithm)


# How far apart the clocks of the machines that sign and read a token may be. A token
# signed by one gateway replica is read by another, and the identity provider's by the
# gateway: without a margin, a clock a fraction of a second behind refuses a token as
# "not yet valid (iat)" — an SSO round trip through a provider where the person is
# already signed in takes less than that. A minute, as the identity providers allow.
CLOCK_SKEW_SECONDS = 60


def decode_access_token(token: str) -> dict:
    """Raises jwt.PyJWTError when the token is invalid/expired."""
    s = get_settings()
    return jwt.decode(token, s.jwt.secret.get_secret_value(), algorithms=[s.jwt.algorithm], leeway=CLOCK_SKEW_SECONDS)
