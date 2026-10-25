from datetime import datetime, timezone
from typing import Optional
from sqlalchemy import Index
from sqlmodel import SQLModel, Field


def _now() -> datetime:
    return datetime.now(timezone.utc)


class UserGroupLink(SQLModel, table=True):
    """User↔group membership (many-to-many)."""
    __tablename__ = "user_groups"
    user_id: int = Field(foreign_key="users.id", primary_key=True)
    group_id: int = Field(foreign_key="groups.id", primary_key=True)


class User(SQLModel, table=True):
    __tablename__ = "users"
    __table_args__ = (
        # an IdP identity (issuer + subject) belongs to ONE account only: it is the constraint
        # that prevents signing into someone else's account with SSO
        Index("uq_users_oidc_identity", "oidc_issuer", "oidc_subject", unique=True),
    )
    id: Optional[int] = Field(default=None, primary_key=True)
    email: str = Field(index=True, unique=True)
    full_name: str = ""
    # None = an SSO-ONLY user (no local password): the local login refuses them, they sign
    # in exclusively from the IdP. See services/sso.py.
    hashed_password: Optional[str] = None
    is_active: bool = True
    is_superuser: bool = False
    # Observer: READS the administration panels and writes nothing. It was born to open an
    # installation to strangers (a demo) without handing over the controls, but it is a real
    # role — whoever checks that things run without being able to touch them. An
    # administrator is an observer already: no need to give both.
    is_observer: bool = False
    created_at: datetime = Field(default_factory=_now)
    # the last authenticated activity (the «seen» column of the Admin page), updated with
    # throttling in the auth dependency, not at every request.
    last_seen_at: Optional[datetime] = Field(default=None, index=True)
    # NO LONGER WRITTEN (2026-10): the address sits in the session (user_sessions.ip) and
    # goes with it; migration 0002 emptied it. It stays until a later revision removes it
    # (expand/contract: the old code still writes it).
    last_seen_ip: Optional[str] = None

    # The identity at the IdP, when the account is linked to a single sign-on. It is the
    # token's (issuer, subject) pair: the subject is assigned by the IdP, it is stable and —
    # unlike email and UPN — the user cannot choose it. It is THIS the key SSO recognizes
    # who is signing in with; the email serves only for the first link. Both null = an
    # account that never signed in from the IdP.
    oidc_issuer: Optional[str] = Field(default=None, index=True)
    oidc_subject: Optional[str] = Field(default=None, index=True)

    # SERVICE account: an integration (Airflow, a CI, a department's script), not a person.
    # It signs in neither from the form nor from SSO: only with an API token (see
    # models/api_token.py). It takes permissions as the others do, from groups and folders.
    # The email is synthetic (the .invalid domain, which receives no mail).
    is_service: bool = Field(default=False, sa_column_kwargs={"server_default": "false"})
    # The language the user last chose in the interface (en, it, fr, de, es): the
    # emails the product sends them speak it (services/email_texts.py). None =
    # never chose: English.
    locale: Optional[str] = Field(default=None, max_length=8)


class Group(SQLModel, table=True):
    __tablename__ = "groups"
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(index=True, unique=True)
    description: str = ""
    # Administrators' group: whoever belongs to it is admin as long as they belong (see
    # services/permissions.is_admin). The personal flag `User.is_superuser` stays and adds
    # up: either one is enough.
    is_admin: bool = False
    # as `is_admin`, but read-only (see User.is_observer)
    is_observer: bool = False
    created_at: datetime = Field(default_factory=_now)
