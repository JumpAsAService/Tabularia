"""A chi può scrivere un avviso automatico: gli indirizzi e la connessione SMTP.

Le stesse barriere per ogni avviso (un flusso programmato che fallisce, un data
contract che cambia stato): gli indirizzi devono essere indirizzi; la connessione
SMTP si usa solo con CONNECT sulla sua cartella — altrimenti chi può modificare
un oggetto farebbe spedire dal server di posta di qualcun altro; e se la
connessione limita i domini a cui spedisce, l'avviso non è una scorciatoia per
aggirarla.
"""
from __future__ import annotations

from fastapi import HTTPException
from sqlmodel import Session

from app.deps.permissions import ensure_can
from app.models import Connection, User
from app.models.permission import Capability


def parse_recipients(text: str | None) -> list[str]:
    """Gli indirizzi di un elenco separato da virgole (o punti e virgola)."""
    indirizzi = [a.strip() for a in (text or "").replace(";", ",").split(",") if a.strip()]
    for a in indirizzi:
        if "@" not in a or a.startswith("@") or a.endswith("@"):
            raise HTTPException(status_code=422, detail=f"Indirizzo non valido: {a}")
    return indirizzi


def smtp_connection(session: Session, user: User, connection_id: int) -> Connection:
    conn = session.get(Connection, connection_id)
    if conn is None or conn.db_type != "smtp":
        raise HTTPException(status_code=422, detail="Serve una connessione SMTP")
    ensure_can(session, user, conn.project_id, Capability.CONNECT)
    return conn


def ensure_deliverable(conn: Connection | None, recipients: list[str]) -> None:
    from app.routes.connections import allowed_email_domains  # routes → services: import tardivo

    ammessi = allowed_email_domains(conn) if conn else []
    fuori = [a for a in recipients if a.rsplit("@", 1)[-1].lower() not in ammessi] if ammessi else []
    if fuori:
        raise HTTPException(
            status_code=422,
            detail=f"Questa connessione può spedire solo a {', '.join(ammessi)}: {', '.join(fuori)} non è ammesso",
        )
