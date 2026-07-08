"""Hashing password (bcrypt) e token JWT. Volutamente minimale e leggibile."""
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
        # hash malformato / non bcrypt
        return False


# ── JWT ───────────────────────────────────────────────────────────────────────
def create_access_token(subject: int | str) -> str:
    s = get_settings()
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(subject),
        "iat": now,
        "exp": now + timedelta(minutes=s.jwt.access_ttl_minutes),
    }
    return jwt.encode(payload, s.jwt.secret.get_secret_value(), algorithm=s.jwt.algorithm)


def decode_access_token(token: str) -> dict:
    """Solleva jwt.PyJWTError se il token è invalido/scaduto."""
    s = get_settings()
    return jwt.decode(token, s.jwt.secret.get_secret_value(), algorithms=[s.jwt.algorithm])
