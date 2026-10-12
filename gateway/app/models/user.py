from datetime import datetime, timezone
from typing import Optional
from sqlalchemy import Index
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
    __table_args__ = (
        # un'identità dell'IdP (issuer + subject) appartiene a UN account solo: è il
        # vincolo che impedisce di entrare in un account altrui con lo SSO
        Index("uq_users_oidc_identity", "oidc_issuer", "oidc_subject", unique=True),
    )
    id: Optional[int] = Field(default=None, primary_key=True)
    email: str = Field(index=True, unique=True)
    full_name: str = ""
    # None = utente SOLO SSO (nessuna password locale): il login locale lo
    # rifiuta, entra esclusivamente dall'IdP. Vedi services/sso.py.
    hashed_password: Optional[str] = None
    is_active: bool = True
    is_superuser: bool = False
    # Osservatore: LEGGE i pannelli di amministrazione e non scrive nulla. Nasce
    # per aprire un'installazione a sconosciuti (una demo) senza dare i comandi,
    # ma è un ruolo vero — chi verifica che le cose girino senza poterle toccare.
    # Un amministratore è già osservatore: non serve dargli entrambi.
    is_observer: bool = False
    created_at: datetime = Field(default_factory=_now)
    # ultima attività autenticata (la colonna «visto» della pagina Admin),
    # aggiornata con throttling nel dependency di auth, non a ogni richiesta.
    last_seen_at: Optional[datetime] = Field(default=None, index=True)
    # NON PIÙ SCRITTA (2026-10): l'indirizzo sta nella sessione (user_sessions.ip) e
    # se ne va con lei; la migrazione 0002 l'ha svuotata. Resta finché una revisione
    # successiva non la toglie (expand/contract: il codice vecchio la scrive ancora).
    last_seen_ip: Optional[str] = None

    # Identità presso l'IdP, quando l'account è collegato a un single sign-on.
    # È la coppia (issuer, subject) del token: il subject è assegnato dall'IdP,
    # è stabile e — a differenza di email e UPN — l'utente non se lo può
    # scegliere. È QUESTA la chiave con cui l'SSO riconosce chi sta entrando;
    # l'email serve solo al primo collegamento. Entrambe nulle = account che
    # non è mai entrato dall'IdP.
    oidc_issuer: Optional[str] = Field(default=None, index=True)
    oidc_subject: Optional[str] = Field(default=None, index=True)

    # Account di SERVIZIO: un'integrazione (Airflow, una CI, uno script di
    # reparto), non una persona. Non entra dal modulo né dall'SSO: solo con un
    # token API (vedi models/api_token.py). Prende i permessi come gli altri, da
    # gruppi e cartelle. L'email è sintetica (dominio .invalid, che non riceve posta).
    is_service: bool = Field(default=False, sa_column_kwargs={"server_default": "false"})


class Group(SQLModel, table=True):
    __tablename__ = "groups"
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(index=True, unique=True)
    description: str = ""
    # Gruppo di amministratori: chi vi appartiene è admin finché vi appartiene
    # (vedi services/permissions.is_admin). Il flag personale `User.is_superuser`
    # resta e si somma: basta uno dei due.
    is_admin: bool = False
    # come `is_admin`, ma di sola lettura (vedi User.is_observer)
    is_observer: bool = False
    created_at: datetime = Field(default_factory=_now)
