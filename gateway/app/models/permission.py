import enum
from typing import Optional
from sqlmodel import SQLModel, Field


class Capability(str, enum.Enum):
    """
    Cosa un soggetto può fare su un progetto. Gerarchia (chi può di più può di
    meno): VIEW < RUN < EDIT < MANAGE. CONNECT è ortogonale (usare/creare
    connessioni dati). MANAGE include tutto.
    """
    VIEW = "view"       # vedere il progetto e il suo contenuto
    RUN = "run"         # eseguire i flow
    EDIT = "edit"       # creare/modificare contenuto
    CONNECT = "connect" # usare/gestire connessioni dati
    MANAGE = "manage"   # gestire il progetto e i suoi permessi


# rango per l'implicazione lineare; CONNECT resta fuori (ortogonale)
_RANK = {Capability.VIEW: 0, Capability.RUN: 1, Capability.EDIT: 2, Capability.MANAGE: 3}


def grant_satisfies(granted: str, needed: str) -> bool:
    """Un permesso `granted` soddisfa la capability `needed`?"""
    g, n = Capability(granted), Capability(needed)
    if g is Capability.MANAGE:
        return True
    if n is Capability.CONNECT:
        return g is Capability.CONNECT
    if g is Capability.CONNECT:
        return n is Capability.CONNECT
    return _RANK[g] >= _RANK[n]


class Permission(SQLModel, table=True):
    """
    Un permesso concede una `capability` su un `project` a un soggetto: un utente
    (`user_id`) OPPURE un gruppo (`group_id`). Solo concessioni (allow-only) in
    questo primo taglio; niente regole di deny.
    """
    __tablename__ = "permissions"
    id: Optional[int] = Field(default=None, primary_key=True)
    project_id: int = Field(foreign_key="projects.id", index=True)
    user_id: Optional[int] = Field(default=None, foreign_key="users.id", index=True)
    group_id: Optional[int] = Field(default=None, foreign_key="groups.id", index=True)
    capability: str  # valore di Capability
