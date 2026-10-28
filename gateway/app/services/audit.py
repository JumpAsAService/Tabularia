"""Recording the audit events.

`record_audit(...)` writes ONE append-only row. It is defensive on purpose: an error
writing the audit must NEVER fail the user's action (it is logged and it goes on).
IP/User-Agent are taken from the `Request` when available (X-Forwarded-For for the
case behind a reverse proxy).
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

from fastapi import Request
from sqlmodel import Session

from app.models import AuditLog, Group, User

logger = logging.getLogger(__name__)

# ── known actions (stable strings: used as a filter in the UI too) ───────────
LOGIN = "auth.login"
LOGIN_FAILED = "auth.login_failed"
SSO_LOGIN = "auth.sso_login"            # login through an external IdP (OIDC)
SSO_LOGIN_FAILED = "auth.sso_login_failed"
LOGOUT = "auth.logout"
# the try-it stack's first administrator, created in the browser (services/first_run.py)
FIRST_RUN_SETUP = "auth.first_run_setup"
# the sample-data container signing in with the start-up token (services/first_run.py)
BOOTSTRAP_LOGIN = "auth.bootstrap_login"
# sessions closed by their owner (their own) or by an administrator (others')
SESSION_REVOKE = "auth.session_revoke"
# a password set: one's own (settings menu) or another user's (administrator)
PASSWORD_CHANGE = "auth.password_change"
# API tokens and service accounts (see services/api_tokens.py)
TOKEN_CREATE = "token.create"
TOKEN_REVOKE = "token.revoke"
SERVICE_ACCOUNT_CREATE = "service_account.create"
SERVICE_ACCOUNT_DELETE = "service_account.delete"
# webhooks (see services/webhooks.py)
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
# a DIRECT run from the data plane (editor, without a saved flow): it is a run like the
# others and leaves the same trace
TRANSFORM_RUN = "transform.run"
# Administrator privileges: granted/removed to a person or a whole group
USER_PROMOTE = "user.promote"
USER_DEMOTE = "user.demote"
GROUP_PROMOTE = "group.promote"
GROUP_DEMOTE = "group.demote"
# Joining/leaving an ADMIN group changes the privileges as much as a promotion
ADMIN_GROUP_JOIN = "group.admin_join"
ADMIN_GROUP_LEAVE = "group.admin_leave"
# Observer: reads the administration panels without being able to write in them. It is
# worth the trace as much as a promotion — it opens the audit, the sessions and the
# users' list
OBSERVER_GRANT = "user.observer_grant"
OBSERVER_REVOKE = "user.observer_revoke"
GROUP_OBSERVER_GRANT = "group.observer_grant"
GROUP_OBSERVER_REVOKE = "group.observer_revoke"
PERM_GRANT = "permission.grant"
PERM_REVOKE = "permission.revoke"
# privacy notice: changes what the installation DECLARES to everyone
PRIVACY_UPDATE = "privacy.update"
# the Explore's banners: they change what the WHOLE installation sees
BANNER_CREATE = "banner.create"
BANNER_DELETE = "banner.delete"
# allowed engines: they change what the WHOLE installation can choose
ENGINE_ALLOW = "engine.allow"
ENGINE_SETTINGS_UPDATE = "engine_settings.update"
# the gateway's settings from Administration → Settings (app/core/overrides.py)
SETTINGS_UPDATE = "settings.update"
# «Roll out» of the components that read a changed setting only at start, and its cancel
SETTINGS_ROLLOUT = "settings.rollout"
SETTINGS_ROLLOUT_CANCEL = "settings.rollout_cancel"
SIEM_UPDATE = "siem.update"
SIEM_TEST = "siem.test"
# the database backups (Admin → Backups): their settings, «Back up now», each backup, a download
BACKUP_UPDATE = "backup.update"
BACKUP_REQUEST = "backup.request"
BACKUP_RUN = "backup.run"
BACKUP_DOWNLOAD = "backup.download"
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
# AI assistant: which models may be used, and every query the assistant runs on the
# data on a user's behalf (it is a data access as an export is)
AI_MODEL_ENABLE = "ai.model_enable"
AI_MODEL_DISABLE = "ai.model_disable"
AI_QUERY = "ai.query"
# the text of an agent's instructions changed from the panel, or put back to the default
AI_PROMPT_UPDATE = "ai.prompt_update"
AI_PROMPT_RESET = "ai.prompt_reset"
# the agent's plans (docs/specs/mcp-agent.md): one applied, with the outcome of each
# action (each action is recorded by its own route too, with `via`), or cancelled
AGENT_PLAN_APPLY = "agent.plan_apply"
AGENT_PLAN_CANCEL = "agent.plan_cancel"
# saved views (Viewer configurations inside a folder). Prefix "saved_view" and not
# "view": `VIEW` is the name of a capability already.
# sending a run's output by email: it is the ONLY destination that sends data out
# towards an address written by hand in the flow, so it is tracked apart — the allowed
# domains say WHERE data may go out, the audit says WHAT went out
EMAIL_SEND = "email.send"
# saved scripts: Python or R code kept in a folder (routes/scripts.py)
SCRIPT_CREATE = "script.create"
SCRIPT_UPDATE = "script.update"
SCRIPT_DELETE = "script.delete"
SCRIPT_RESTORE = "script.restore"
SCRIPT_RUN = "script.run"
SCRIPT_SCHEDULE = "script.schedule"
# SQL scripts (routes/sql_scripts.py); every run is a free query on the data, as the
# assistant's, and is recorded with its SQL
SQL_SCRIPT_CREATE = "sql_script.create"
SQL_SCRIPT_UPDATE = "sql_script.update"
SQL_SCRIPT_DELETE = "sql_script.delete"
SQL_SCRIPT_RESTORE = "sql_script.restore"
SQL_SCRIPT_RUN = "sql_script.run"
SQL_SCRIPT_EXPORT = "sql_script.export"
SQL_SCRIPT_PUBLISH = "sql_script.publish"  # a write of the script's datasource
SQL_SCRIPT_SCHEDULE = "sql_script.schedule"
# notebooks: marimo notebooks kept in a folder (routes/notebooks.py); a move is an
# update with its folders, as for saved scripts
NOTEBOOK_CREATE = "notebook.create"
NOTEBOOK_UPDATE = "notebook.update"
NOTEBOOK_DELETE = "notebook.delete"
NOTEBOOK_RESTORE = "notebook.restore"
# their pods (services/notebook_runtime.py): an app or an editor started, stopped (by
# someone, or idle), a version saved from the editor, a datasource read by a notebook
NOTEBOOK_START = "notebook.start"
NOTEBOOK_STOP = "notebook.stop"
NOTEBOOK_SAVE = "notebook.save"
NOTEBOOK_READ = "notebook.read"
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
# accounts and groups (routes/users.py, routes/groups.py). What single sign-on does by
# itself — an account created at its first sign-in, a group created by
# OIDC__AUTO_CREATE_GROUPS, a membership synchronized from the identity provider — goes
# with the same actions and `"via": "sso"` in the detail (services/sso.py)
USER_CREATE = "user.create"
USER_UPDATE = "user.update"            # the name, or the email the identity provider changed
USER_DEACTIVATE = "user.deactivate"
USER_REACTIVATE = "user.reactivate"
USER_DELETE = "user.delete"
GROUP_CREATE = "group.create"
GROUP_UPDATE = "group.update"          # the description: the two flags have their own actions
GROUP_DELETE = "group.delete"
# joining or leaving ANY group, an administrators' one too: there `group.admin_join` /
# `group.admin_leave` follow, the trace of the privilege, so that one action holds a
# group's whole membership history
GROUP_MEMBER_ADD = "group.member_add"
GROUP_MEMBER_REMOVE = "group.member_remove"
QUEUE_JOB_STOP = "queue.job_stop"


def membership_detail(group: Group) -> dict[str, Any]:
    """The detail of a `group.member_add` / `group.member_remove`, whose target is the
    person: which group, and the role it carries when it carries one — joining an
    administrators' or observers' group IS receiving that role."""
    detail: dict[str, Any] = {"group": group.name, "group_id": group.id}
    if group.is_admin:
        detail["admin"] = True
    if group.is_observer:
        detail["observer"] = True
    return detail


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
    """Writes an audit event. It never raises: a failure here must not break the user's
    action.

    Of an OBSERVER neither the IP address nor the user agent is written: WHAT was done and
    by whom stays, which is the point of the log, and from where and with what disappear.
    See `permissions.is_observer_only`.

    The rule looks at the ACTOR, so it does not cover the FAILED login, where there is no
    actor — and there the address is KEPT on purpose: it is the signal the checks against
    brute-force attempts rest on (the user's choice, 2026-09-30). The test
    `test_a_failed_login_still_records_the_address` pins it, so it stays a choice and not
    an oversight."""
    try:
        minimize = False
        if actor is not None:
            try:
                from app.services.permissions import is_observer_only

                minimize = is_observer_only(session, actor)
            except Exception:  # pragma: no cover — when in doubt the event is written
                logger.debug("role cannot be resolved for the audit", exc_info=True)
        # done with an API token: say which (the name, never the secret)
        from app.services.api_tokens import current_name

        via = current_name(request)
        if via:
            detail = {**(detail or {}), "token": via}
        # done by the agent (services/agent): from the chat or over MCP, and which plan
        state = getattr(request, "state", None) if request is not None else None
        agent_via = getattr(state, "via", None) if state is not None else None
        if isinstance(agent_via, str) and agent_via:
            detail = {**(detail or {}), "via": agent_via}
            plan_id = getattr(state, "agent_plan", None)
            if plan_id:
                detail["plan"] = plan_id
        entry = AuditLog(
            actor_id=actor.id if actor else None,
            actor_label=actor_label or (actor.email if actor else "anonymous"),
            action=action,
            outcome=outcome,
            target_type=target_type,
            target_id=target_id,
            target_label=target_label,
            detail=json.dumps(detail, default=str, ensure_ascii=False) if detail else None,
            ip=None if minimize else client_ip(request),
            user_agent=None if minimize else (request.headers.get("user-agent") if request else None),
        )
        session.add(entry)
        session.commit()
    except Exception:  # pragma: no cover — the audit must never propagate errors
        logger.exception("record_audit: cannot write event %s", action)
        session.rollback()
