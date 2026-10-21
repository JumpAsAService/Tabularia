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
LOGOUT = "auth.logout"
# the try-it stack's first administrator, created in the browser (services/first_run.py)
FIRST_RUN_SETUP = "auth.first_run_setup"
# the sample-data container signing in with the start-up token (services/first_run.py)
BOOTSTRAP_LOGIN = "auth.bootstrap_login"
# sessioni chiuse da chi le possiede (le sue) o da un amministratore (di altri)
SESSION_REVOKE = "auth.session_revoke"
# a password set: one's own (settings menu) or another user's (administrator)
PASSWORD_CHANGE = "auth.password_change"
# token API e account di servizio (vedi services/api_tokens.py)
TOKEN_CREATE = "token.create"
TOKEN_REVOKE = "token.revoke"
SERVICE_ACCOUNT_CREATE = "service_account.create"
SERVICE_ACCOUNT_DELETE = "service_account.delete"
# webhook (vedi services/webhooks.py)
WEBHOOK_CREATE = "webhook.create"
WEBHOOK_UPDATE = "webhook.update"
WEBHOOK_DELETE = "webhook.delete"
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
CONTRACT_SAVE = "contract.save"
CONTRACT_DELETE = "contract.delete"
CONN_CREATE = "connection.create"
CONN_UPDATE = "connection.update"
CONN_DELETE = "connection.delete"
# SFTP: a server's host key accepted by a test (trust on first use), or a new key
# accepted in place of the previous one — what proves the server is the right one
CONN_HOST_KEY_ACCEPT = "connection.host_key_accept"
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
ENGINE_SETTINGS_UPDATE = "engine_settings.update"
SIEM_UPDATE = "siem.update"
SIEM_TEST = "siem.test"
# workers (Admin → Workers): the autoscaling bounds of their pods on Kubernetes; the
# processes per worker are engine settings and go as ENGINE_SETTINGS_UPDATE
WORKERS_SCALING_UPDATE = "workers.scaling_update"
WORKERS_SCALING_RESET = "workers.scaling_reset"
# scripting environments (Admin → Scripting environments): the bucket the scripts work
# in, and what their code runs with. A variable's value never goes in the detail
SCRIPTING_STORAGE_UPDATE = "scripting.storage_update"
SCRIPTING_STORAGE_TEST = "scripting.storage_test"
SCRIPTING_ENV_CREATE = "scripting.environment_create"
SCRIPTING_ENV_UPDATE = "scripting.environment_update"
SCRIPTING_ENV_DELETE = "scripting.environment_delete"
SCRIPTING_VERSION_CURRENT = "scripting.version_current"
SCRIPTING_VARIABLE_SET = "scripting.variable_set"
SCRIPTING_VARIABLE_DELETE = "scripting.variable_delete"
# the editor's «Run» of a Script node: code executed on the cluster, by whom
SCRIPTING_TRIAL = "scripting.trial"
# Administration → Container registries (the images of the Container node)
REGISTRY_CREATE = "registry.create"
REGISTRY_UPDATE = "registry.update"
REGISTRY_DELETE = "registry.delete"
REGISTRY_TEST = "registry.test"
REGISTRY_CLUSTER_TEST = "registry.cluster_test"
# the editor's «Run» of a Container node: an image run on the cluster, by whom
CONTAINER_TRIAL = "container.trial"
REGISTRY_VARIABLE_SET = "registry.variable_set"
REGISTRY_VARIABLE_DELETE = "registry.variable_delete"
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
FLOW_GROUP_CREATE = "flow_group.create"
FLOW_GROUP_UPDATE = "flow_group.update"
FLOW_GROUP_DELETE = "flow_group.delete"
FLOW_GROUP_RUN = "flow_group.run"
FLOW_GROUP_SCHEDULE = "flow_group.schedule"
# folders: moving one changes the inherited permissions of a whole subtree
FOLDER_CREATE = "folder.create"
FOLDER_UPDATE = "folder.update"
FOLDER_MOVE = "folder.move"
FOLDER_DELETE = "folder.delete"
DS_UPDATE = "datasource.update"
USER_DELETE = "user.delete"
GROUP_CREATE = "group.create"
QUEUE_JOB_STOP = "queue.job_stop"


def client_ip(request: Optional[Request]) -> Optional[str]:
    """The client's address: the one of the connection, as the server resolved it.

    Behind a reverse proxy the real client is in X-Forwarded-For, but only the
    server knows whether the hop that wrote it is a proxy we trust: uvicorn reads
    the header from the addresses in FORWARDED_ALLOW_IPS (`--forwarded-allow-ips`,
    127.0.0.1 by default) and puts the result here. Reading the header again in
    the application took a client's own `X-Forwarded-For: <anything>` at its word,
    in the audit log, in sessions and tokens."""
    if request is None:
        return None
    client = getattr(request, "client", None)
    return client.host if client else None


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
        # fatto con un token API: si dice con quale (il nome, mai il segreto)
        from app.services.api_tokens import nome_corrente

        via = nome_corrente(request)
        if via:
            detail = {**(detail or {}), "token": via}
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
