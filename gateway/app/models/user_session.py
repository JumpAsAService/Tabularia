"""An open session: a successful sign-in from a browser or a script.

The token stays a signed JWT, but it carries the id of one of these rows (`sid`) and
the gateway looks it up at every request, in the same read as the user. As long as
the row is there the token holds; removing it is the way to close the session:
signing out, the administrator's revocation, the password change, the expiry. That is
why there is no «revoked» column: a closed session serves nothing and must not keep
the address of whoever opened it.
"""
from datetime import datetime
from typing import Optional

from sqlmodel import Field, SQLModel


class UserSession(SQLModel, table=True):
    __tablename__ = "user_sessions"
    # random (secrets.token_urlsafe): alone it opens nothing, the JWT's signature is needed
    id: str = Field(primary_key=True, max_length=64)
    user_id: int = Field(foreign_key="users.id", index=True)
    # all naive UTC, as the other columns
    created_at: datetime
    # the last request seen, updated at most once a minute: it decides the idle timeout
    last_seen_at: datetime
    # the absolute cap: past it, one signs in again anyway
    expires_at: datetime = Field(index=True)
    # from where and with what it came in; empty for an OBSERVER (see
    # permissions.is_observer_only)
    ip: Optional[str] = None
    user_agent: Optional[str] = Field(default=None, max_length=512)
    # "password" o "sso"
    method: str = Field(default="password", max_length=16)
