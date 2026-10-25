"""An API token: the credential of a script, an integration or a service account, to put
in the `Authorization: Bearer tab_…` header.

Of the token only the fingerprint (SHA-256) is kept: the secret is shown once, at
creation, and then nobody knows it any more, the gateway included. It always has an
expiry. It holds with its user's permissions, and at most for reading when it is
read-only (`scope = "read"`): every request that changes something is refused.
Revoking it is deleting its row, as for the sessions.
"""
from datetime import datetime
from typing import Optional

from sqlmodel import Field, SQLModel


class ApiToken(SQLModel, table=True):
    __tablename__ = "api_tokens"
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.id", index=True)
    name: str = Field(max_length=80)
    # the token's beginning (`tab_` + 8 characters): enough to recognize it in a list or in
    # a log, not to use it
    prefix: str = Field(max_length=16)
    token_hash: str = Field(max_length=64, unique=True, index=True)
    scope: str = Field(default="read", max_length=8)  # "read" | "write"
    # all naive UTC, as the other columns
    created_at: datetime
    created_by: Optional[int] = Field(default=None, foreign_key="users.id")
    expires_at: datetime = Field(index=True)
    last_used_at: Optional[datetime] = None
    last_used_ip: Optional[str] = Field(default=None, max_length=64)
