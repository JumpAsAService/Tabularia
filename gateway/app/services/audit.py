"""Registrazione degli eventi di audit.

`record_audit(...)` scrive UNA riga append-only. È volutamente difensivo: un
errore nella scrittura dell'audit NON deve mai far fallire l'azione dell'utente
(si logga e si prosegue). L'IP/User-Agent si estraggono dalla `Request` quando
disponibile (X-Forwarded-For per il caso dietro reverse proxy).
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

from fastapi import Request
from sqlmodel import Session

from app.models import AuditLog, User

logger = logging.getLogger(__name__)

# ── azioni note (stringhe stabili: usate anche come filtro nell'UI) ───────────
LOGIN = "auth.login"
LOGIN_FAILED = "auth.login_failed"
FLOW_CREATE = "flow.create"
FLOW_UPDATE = "flow.update"
FLOW_DELETE = "flow.delete"
FLOW_RUN = "flow.run"
FLOW_SCHEDULE = "flow.schedule"
FLOW_PROMOTE = "flow.promote"
DS_CREATE = "datasource.create"
DS_REFRESH = "datasource.refresh"
DS_DELETE = "datasource.delete"
DS_SCHEDULE = "datasource.schedule"
CONN_CREATE = "connection.create"
CONN_UPDATE = "connection.update"
CONN_DELETE = "connection.delete"
EXPORT_DOWNLOAD = "export.download"
PERM_GRANT = "permission.grant"
PERM_REVOKE = "permission.revoke"


def client_ip(request: Optional[Request]) -> Optional[str]:
    if request is None:
        return None
    # dietro reverse proxy il vero client è nel primo hop di X-Forwarded-For
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else None


def record_audit(
    session: Session,
    *,
    actor: Optional[User] = None,
    actor_label: Optional[str] = None,
    action: str,
    outcome: str = "success",
    target_type: Optional[str] = None,
    target_id: Optional[int] = None,
    target_label: Optional[str] = None,
    detail: Optional[dict[str, Any]] = None,
    request: Optional[Request] = None,
) -> None:
    """Scrive un evento di audit. Non solleva mai: un fallimento qui non deve
    rompere l'azione dell'utente."""
    try:
        entry = AuditLog(
            actor_id=actor.id if actor else None,
            actor_label=actor_label or (actor.email if actor else "anonimo"),
            action=action,
            outcome=outcome,
            target_type=target_type,
            target_id=target_id,
            target_label=target_label,
            detail=json.dumps(detail, default=str, ensure_ascii=False) if detail else None,
            ip=client_ip(request),
            user_agent=(request.headers.get("user-agent") if request else None),
        )
        session.add(entry)
        session.commit()
    except Exception:  # pragma: no cover — l'audit non deve mai propagare errori
        logger.exception("record_audit: impossibile scrivere l'evento %s", action)
        session.rollback()
