from datetime import datetime, timezone
from typing import Optional
from sqlmodel import SQLModel, Field


def _now() -> datetime:
    return datetime.now(timezone.utc)


class UserGroupLink(SQLModel, table=True):
    """Appartenenza utente↔gruppo (many-to-many)."""
    __tablename__ = "user_groups"
    user_id: int = Field(foreign_key="users.id", primary_key=True)
    group_id: int = Field(foreign_key="groups.id", primary_key=True)


class User(SQLModel, table=True):
    __tablename__ = "users"
    id: Optional[int] = Field(default=None, primary_key=True)
    email: str = Field(index=True, unique=True)
    full_name: str = ""
    # None = utente SOLO SSO (nessuna password locale): il login locale lo
    # rifiuta, entra esclusivamente dall'IdP. Vedi services/sso.py.
    hashed_password: Optional[str] = None
    is_active: bool = True
    is_superuser: bool = False
    created_at: datetime = Field(default_factory=_now)
    # ultima attività autenticata (per le "sessioni attive" dell'audit): il JWT è
    # stateless, quindi tracciamo l'ultimo istante/IP visti (aggiornati con
    # throttling nel dependency di auth, non a ogni richiesta).
    last_seen_at: Optional[datetime] = Field(default=None, index=True)
    last_seen_ip: Optional[str] = None

    # Identità presso l'IdP, quando l'account è collegato a un single sign-on.
    # È la coppia (issuer, subject) del token: il subject è assegnato dall'IdP,
    # è stabile e — a differenza di email e UPN — l'utente non se lo può
    # scegliere. È QUESTA la chiave con cui l'SSO riconosce chi sta entrando;
    # l'email serve solo al primo collegamento. Entrambe nulle = account che
    # non è mai entrato dall'IdP.
    oidc_issuer: Optional[str] = Field(default=None, index=True)
    oidc_subject: Optional[str] = Field(default=None, index=True)


class Group(SQLModel, table=True):
    __tablename__ = "groups"
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(index=True, unique=True)
    description: str = ""
    # Gruppo di amministratori: chi vi appartiene è admin finché vi appartiene
    # (vedi services/permissions.is_admin). Il flag personale `User.is_superuser`
    # resta e si somma: basta uno dei due.
    is_admin: bool = False
    created_at: datetime = Field(default_factory=_now)
