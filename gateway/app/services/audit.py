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
SSO_LOGIN = "auth.sso_login"            # login via IdP esterno (OIDC)
SSO_LOGIN_FAILED = "auth.sso_login_failed"
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
# esecuzione DIRETTA dal piano dati (editor, senza flusso salvato): è un run
# come gli altri e lascia la stessa traccia
TRANSFORM_RUN = "transform.run"
# Privilegi di amministratore: concessi/tolti a una persona o a un gruppo intero
USER_PROMOTE = "user.promote"
USER_DEMOTE = "user.demote"
GROUP_PROMOTE = "group.promote"
GROUP_DEMOTE = "group.demote"
# Entrare/uscire da un gruppo ADMIN cambia i privilegi quanto una promozione
ADMIN_GROUP_JOIN = "group.admin_join"
ADMIN_GROUP_LEAVE = "group.admin_leave"
# Osservatore: legge i pannelli di amministrazione senza poterci scrivere. Vale
# la traccia quanto una promozione — apre l'audit, le sessioni e l'elenco utenti
OBSERVER_GRANT = "user.observer_grant"
OBSERVER_REVOKE = "user.observer_revoke"
GROUP_OBSERVER_GRANT = "group.observer_grant"
GROUP_OBSERVER_REVOKE = "group.observer_revoke"
PERM_GRANT = "permission.grant"
PERM_REVOKE = "permission.revoke"
# informativa sulla privacy: cambia ciò che l'installazione DICHIARA a tutti
PRIVACY_UPDATE = "privacy.update"
# banner dell'Explore: cambiano ciò che vede TUTTA l'installazione
BANNER_CREATE = "banner.create"
BANNER_DELETE = "banner.delete"
# motori consentiti: cambiano ciò che TUTTA l'installazione può scegliere
ENGINE_ALLOW = "engine.allow"
ENGINE_DISALLOW = "engine.disallow"
# assistente AI: quali modelli si possono usare, e ogni query che l'assistente
# esegue sui dati per conto di un utente (e' un accesso ai dati come un export)
AI_MODEL_ENABLE = "ai.model_enable"
AI_MODEL_DISABLE = "ai.model_disable"
AI_QUERY = "ai.query"
# viste salvate (configurazioni del Viewer dentro una cartella). Prefisso
# "saved_view" e non "view": `VIEW` è già il nome di una capability.
# invio email dell'output di un run: è l'UNICA destinazione che manda dati fuori
# verso un indirizzo scritto a mano nel flusso, quindi va tracciata a parte —
# i domini ammessi dicono DOVE può uscire un dato, l'audit dice COSA è uscito
EMAIL_SEND = "email.send"
SAVED_VIEW_CREATE = "saved_view.create"
SAVED_VIEW_UPDATE = "saved_view.update"
SAVED_VIEW_DELETE = "saved_view.delete"


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
    rompere l'azione dell'utente.

    Di un OSSERVATORE non si scrivono né l'indirizzo IP né lo user-agent: resta
    CHE COSA è stato fatto e da chi, che è il senso del registro, e spariscono
    da dove e con che cosa. Vedi `permissions.is_observer_only`.

    La regola guarda l'ATTORE, quindi non copre il login FALLITO, dove attore
    non ce n'è — e lì l'indirizzo si TIENE apposta: è il segnale su cui poggiano
    i controlli contro i tentativi di forzatura (scelta dell'utente,
    2026-09-30). Il test `test_a_failed_login_still_records_the_address` lo
    fissa, così resta una scelta e non una svista."""
    try:
        minimizza = False
        if actor is not None:
            try:
                from app.services.permissions import is_observer_only

                minimizza = is_observer_only(session, actor)
            except Exception:  # pragma: no cover — nel dubbio si scrive l'evento
                logger.debug("ruolo non risolvibile per l'audit", exc_info=True)
        entry = AuditLog(
            actor_id=actor.id if actor else None,
            actor_label=actor_label or (actor.email if actor else "anonimo"),
            action=action,
            outcome=outcome,
            target_type=target_type,
            target_id=target_id,
            target_label=target_label,
            detail=json.dumps(detail, default=str, ensure_ascii=False) if detail else None,
            ip=None if minimizza else client_ip(request),
            user_agent=None if minimizza else (request.headers.get("user-agent") if request else None),
        )
        session.add(entry)
        session.commit()
    except Exception:  # pragma: no cover — l'audit non deve mai propagare errori
        logger.exception("record_audit: impossibile scrivere l'evento %s", action)
        session.rollback()
