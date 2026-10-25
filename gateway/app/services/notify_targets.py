"""Whom an automatic notice may write to: the addresses and the SMTP connection.

The same barriers for every notice (a scheduled flow that fails, a data contract that
changes state): the addresses must be addresses; the SMTP connection is used only with
CONNECT on its folder — otherwise whoever can change an object would make someone
else's mail server send; and when the connection limits the domains it sends to, the
notice is not a shortcut around it.
"""
from __future__ import annotations

from fastapi import HTTPException
from sqlmodel import Session

from app.deps.permissions import ensure_can
from app.models import Connection, User
from app.models.permission import Capability


def parse_recipients(text: str | None) -> list[str]:
    """The addresses of a list separated by commas (or semicolons)."""
    addresses = [a.strip() for a in (text or "").replace(";", ",").split(",") if a.strip()]
    for a in addresses:
        if "@" not in a or a.startswith("@") or a.endswith("@"):
            raise HTTPException(status_code=422, detail=f"Invalid address: {a}")
    return addresses


def smtp_connection(session: Session, user: User, connection_id: int) -> Connection:
    conn = session.get(Connection, connection_id)
    if conn is None or conn.db_type != "smtp":
        raise HTTPException(status_code=422, detail="An SMTP connection is required")
    ensure_can(session, user, conn.project_id, Capability.CONNECT)
    return conn


def ensure_deliverable(conn: Connection | None, recipients: list[str]) -> None:
    from app.routes.connections import allowed_email_domains  # routes → services: late import

    allowed = allowed_email_domains(conn) if conn else []
    outside = [a for a in recipients if a.rsplit("@", 1)[-1].lower() not in allowed] if allowed else []
    if outside:
        raise HTTPException(
            status_code=422,
            detail=f"This connection can only send to {', '.join(allowed)}: {', '.join(outside)} is not allowed",
        )
