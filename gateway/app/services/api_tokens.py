"""API tokens: creating, recognizing and revoking them.

A token is `tab_` followed by 40 random characters. It is shown once, at creation; in
the database only its SHA-256 fingerprint stays (the secret has 240 bits of chance
already: a slow hash like bcrypt would add nothing, and would cost a quarter of a
second at EVERY request). The gateway recognizes it by the prefix and looks it up by
fingerprint in the same read that loads the user (deps/auth.py).

Two levels: `read` refuses every request that changes something, `write` is worth as
much as its user. In both cases the permissions are the user's: a token never adds
any.
"""
from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import HTTPException, Request
from sqlalchemy import delete
from sqlmodel import Session, select

from app.core.config import get_settings
from app.models import ApiToken, User

PREFIX = "tab_"

# messages for whoever integrates: in the request's language (Accept-Language)
READ_ONLY = {
    "en": "Read-only token: this request changes data",
    "it": "Token di sola lettura: questa richiesta modifica dati",
    "fr": "Jeton en lecture seule : cette requête modifie des données",
    "de": "Nur-Lese-Token: Diese Anfrage ändert Daten",
    "es": "Token de solo lectura: esta petición modifica datos",
}
NO_TOKEN_FROM_TOKEN = {
    "en": "An API token cannot create credentials: do it from the interface",
    "it": "Un token API non può creare credenziali: si fa dall'interfaccia",
    "fr": "Un jeton d'API ne peut pas créer d'identifiants : faites-le depuis l'interface",
    "de": "Ein API-Token kann keine Zugangsdaten erstellen: Das geht über die Oberfläche",
    "es": "Un token de API no puede crear credenciales: hazlo desde la interfaz",
}
NO_ROLES_FROM_TOKEN = {
    "en": "An API token cannot make an account administrator or observer, nor reactivate it: do it from the interface",
    "it": "Un token API non rende un account amministratore od osservatore, né lo riattiva: si fa dall'interfaccia",
    "fr": "Un jeton d'API ne peut pas rendre un compte administrateur ou observateur, ni le réactiver : faites-le depuis l'interface",
    "de": "Ein API-Token kann ein Konto weder zum Administrator oder Beobachter machen noch reaktivieren: Das geht über die Oberfläche",
    "es": "Un token de API no puede hacer una cuenta administradora u observadora, ni reactivarla: hazlo desde la interfaz",
}
NO_SETTINGS_FROM_TOKEN = {
    "en": "An API token cannot change the settings: do it from the interface",
    "it": "Un token API non cambia le impostazioni: si fa dall'interfaccia",
    "fr": "Un jeton d'API ne peut pas modifier les réglages : faites-le depuis l'interface",
    "de": "Ein API-Token kann die Einstellungen nicht ändern: Das geht über die Oberfläche",
    "es": "Un token de API no puede cambiar los ajustes: hazlo desde la interfaz",
}
NO_WORKER_FROM_TOKEN = {
    "en": "An API token cannot change the workers or their scaling: do it from the interface",
    "it": "Un token API non cambia i worker né la loro scala: si fa dall'interfaccia",
    "fr": "Un jeton d'API ne peut pas modifier les workers ni leur mise à l'échelle : faites-le depuis l'interface",
    "de": "Ein API-Token kann die Worker und ihre Skalierung nicht ändern: Das geht über die Oberfläche",
    "es": "Un token de API no puede cambiar los workers ni su escalado: hazlo desde la interfaz",
}
NO_SIEM_FROM_TOKEN = {
    "en": "An API token cannot change where the audit log is sent: do it from the interface",
    "it": "Un token API non cambia dove va il registro di audit: si fa dall'interfaccia",
    "fr": "Un jeton d'API ne peut pas modifier la destination du journal d'audit : faites-le depuis l'interface",
    "de": "Ein API-Token kann nicht ändern, wohin das Audit-Protokoll geht: Das geht über die Oberfläche",
    "es": "Un token de API no puede cambiar adónde va el registro de auditoría: hazlo desde la interfaz",
}
NO_BACKUP_FROM_TOKEN = {
    "en": "An API token cannot change, run or download the database backups: do it from the interface",
    "it": "Un token API non cambia, non avvia e non scarica i backup del database: si fa dall'interfaccia",
    "fr": "Un jeton d'API ne peut pas modifier, lancer ni télécharger les sauvegardes de la base : faites-le depuis l'interface",
    "de": "Ein API-Token kann die Datenbank-Backups nicht ändern, starten oder herunterladen: Das geht über die Oberfläche",
    "es": "Un token de API no puede cambiar, lanzar ni descargar las copias de seguridad de la base de datos: hazlo desde la interfaz",
}
NO_SCRIPTING_FROM_TOKEN = {
    "en": "An API token cannot change the scripting environments or their bucket: do it from the interface",
    "it": "Un token API non cambia gli ambienti di scripting né il loro bucket: si fa dall'interfaccia",
    "fr": "Un jeton d'API ne peut pas modifier les environnements de script ni leur bucket : faites-le depuis l'interface",
    "de": "Ein API-Token kann die Skriptumgebungen und ihren Bucket nicht ändern: Das geht über die Oberfläche",
    "es": "Un token de API no puede cambiar los entornos de scripting ni su bucket: hazlo desde la interfaz",
}
NO_REGISTRY_FROM_TOKEN = {
    "en": "An API token cannot change the container registries: do it from the interface",
    "it": "Un token API non cambia i container registry: si fa dall'interfaccia",
    "fr": "Un jeton d'API ne peut pas modifier les registres de conteneurs : faites-le depuis l'interface",
    "de": "Ein API-Token kann die Container-Registries nicht ändern: Das geht über die Oberfläche",
    "es": "Un token de API no puede cambiar los registros de contenedores: hazlo desde la interfaz",
}
# the POSTs that only read: a read-only token may use them (an export, a preview or a
# report's render compute and return data, they change nothing)
READING_POSTS = {"/tasks/export", "/tasks/exports", "/tasks/preview", "/reports/{report_id}/render"}


def message(table: dict[str, str], request: Optional[Request]) -> str:
    from app.core.language import request_language

    return table.get(request_language(request), table["en"])
SCOPES = ("read", "write")
# the last use is written at most once a minute, as the sessions' activity
USE_EVERY = timedelta(seconds=60)


def now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def fingerprint(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def is_a_token(credential: str) -> bool:
    return credential.startswith(PREFIX)


def create(
    session: Session, user: User, name: str, scope: str, days: int, created_by: Optional[User],
) -> tuple[ApiToken, str]:
    """Creates the token and returns the row and the secret (never seen again from here
    on). Raises 422 when name, level or expiry are not right."""
    name = (name or "").strip()
    if not name or len(name) > 80:
        raise HTTPException(status_code=422, detail="The token name is required (at most 80 characters)")
    if scope not in SCOPES:
        raise HTTPException(status_code=422, detail="Token scope: 'read' or 'write'")
    maximum = get_settings().auth.api_token_max_days
    if not 1 <= days <= maximum:
        raise HTTPException(status_code=422, detail=f"The expiry must be from 1 to {maximum} days")
    secret = PREFIX + secrets.token_urlsafe(30)
    at = now()
    row = ApiToken(
        user_id=user.id, name=name, prefix=secret[:12], token_hash=fingerprint(secret), scope=scope,
        created_at=at, created_by=created_by.id if created_by else None, expires_at=at + timedelta(days=days),
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    return row, secret


def of_user(session: Session, user_id: int) -> list[ApiToken]:
    """A user's tokens still valid, newest first."""
    return list(session.exec(
        select(ApiToken).where(ApiToken.user_id == user_id, ApiToken.expires_at > now())
        .order_by(ApiToken.created_at.desc())
    ).all())


def revoke(session: Session, token_id: int, user_id: Optional[int] = None) -> Optional[ApiToken]:
    """Deletes the token (of `user_id`, when given). Returns the row as it was, or None."""
    row = session.get(ApiToken, token_id)
    if row is None or (user_id is not None and row.user_id != user_id):
        return None
    copy = ApiToken(**{c: getattr(row, c) for c in ApiToken.model_fields})
    session.exec(delete(ApiToken).where(ApiToken.id == token_id))
    session.commit()
    return copy


def revoke_all(session: Session, user_id: int, commit: bool = True) -> int:
    n = session.exec(delete(ApiToken).where(ApiToken.user_id == user_id)).rowcount or 0
    if commit:
        session.commit()
    return n


def clean(session: Session) -> int:
    """Removes the expired tokens (the scheduler calls it)."""
    n = session.exec(delete(ApiToken).where(ApiToken.expires_at <= now())).rowcount or 0
    session.commit()
    return n


def check_level(row: ApiToken, request: Request) -> None:
    """A read-only token changes nothing: it is the same split by METHOD as the observer's,
    so no new route can escape it. The only exceptions are the POSTs listed in
    READING_POSTS, which only read."""
    if row.scope != "read" or request.method in ("GET", "HEAD", "OPTIONS"):
        return
    route = getattr((getattr(request, "scope", None) or {}).get("route"), "path", None)
    if request.method == "POST" and route in READING_POSTS:
        return
    raise HTTPException(status_code=403, detail=message(READ_ONLY, request))


def withdraw_if_observer(session: Session, user_ids) -> int:
    """The personal tokens of whoever became ONLY an observer (flag, group): an observer has
    none (it is a demo's shared guest), and those created earlier would stay valid. The
    service accounts' ones are not: they belong to the integration. In the caller's
    transaction (without commit)."""
    from app.models import User
    from app.services.permissions import is_observer_only

    n = 0
    for uid in user_ids:
        u = session.get(User, uid)
        if u is not None and not u.is_service and is_observer_only(session, u):
            n += revoke_all(session, uid, commit=False)
    return n


def current_name(request: Optional[Request]) -> Optional[str]:
    """The name of the token the request came with, if it came with a token
    (`get_current_user` puts it there); it ends up in the audit's detail."""
    state = getattr(request, "state", None) if request is not None else None
    return getattr(state, "api_token", None) if state is not None else None
