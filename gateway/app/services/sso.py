"""Optional OIDC SSO: the way in towards an external IdP (Keycloak, Entra ID/MSAL,
Auth0, Okta…) and the groups' synchronization.

Principle: the IdP proves WHO the user is, then the USUAL internal session opens
(`sessions.open_session`). `get_current_user`, RBAC and the audit stay the same — the blast
radius is confined to these two seams:

1. **acceptance**: authorization code flow with PKCE, `state` and `nonce`; the
   id_token is validated against the IdP's JWKS (signature, `iss`, `aud`, `exp`,
   `nonce`);
2. **synchronization**: the groups' claim (`groups` on Keycloak, `roles` for Entra's
   app roles) is reconciled onto `user_groups`, which is the only thing RBAC reads
   (`user_group_ids`). The mapping is BY NAME: no external id enters the permission
   model.

No new dependency: discovery and token exchange with `httpx`, validation with
`PyJWT` (`PyJWKClient`), PKCE with the stdlib — both used by the gateway already.
"""
from __future__ import annotations

import base64
import hashlib
import logging
import secrets
import ssl
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from urllib.parse import urlencode

import httpx
import jwt
from sqlalchemy import or_
from sqlmodel import Session, select

from app.core.config import OidcSettings, get_settings
from app.core.security import CLOCK_SKEW_SECONDS
from app.models import Group, User, UserGroupLink
from app.services.permissions import user_group_ids

logger = logging.getLogger(__name__)


class SsoError(Exception):
    """An error of the SSO flow. The message is for the LOGS: the user gets a generic code
    (no detail of the IdP or of the configuration is exposed)."""

    def __init__(self, message: str, code: str = "sso_failed"):
        super().__init__(message)
        self.code = code


# the signing algorithms ACCEPTED for the id_token: asymmetric only. Never `none`,
# never HS* (it would use the client secret as a symmetric key: a needless surface).
ALLOWED_ALGS = ("RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384", "ES512")

# the type of the login transaction's cookie (state/nonce/PKCE), signed with the
# gateway's secret: no server-side state, it works with several replicas too.
TX_TOKEN_TYPE = "sso_tx"
TX_COOKIE = "tab_sso_tx"


def tx_cookie_name(state: str) -> str:
    """One cookie per sign-in, named after its `state`: a second sign-in started meanwhile
    (a double click, another tab, the Back button) no longer overwrites the first one's,
    which then came back as `state_mismatch`. The name holds a digest of the state, never
    the state itself."""
    return f"{TX_COOKIE}_{hashlib.sha256(state.encode()).hexdigest()[:16]}"


# ─────────────────────────────────────────────────────────────────────────────
# Discovery (.well-known) and signing keys, cached
# ─────────────────────────────────────────────────────────────────────────────
_discovery_cache: dict[str, tuple[float, dict]] = {}
_jwks_clients: dict[tuple[str, float], jwt.PyJWKClient] = {}  # by address and timeout: the timeout changes from the panel


_tls_contexts: dict[str, ssl.SSLContext] = {}


def tls(cfg: OidcSettings) -> ssl.SSLContext | bool:
    """How the provider's certificate is checked: against the public authorities, and,
    with OIDC__CA_FILE, the company's own too (a Keycloak whose certificate its own
    authority signed). With OIDC__VERIFY_TLS=false, not at all: the installer's choice."""
    if not cfg.verify_tls:
        return False
    if not cfg.ca_file:
        return True
    ctx = _tls_contexts.get(cfg.ca_file)
    if ctx is None:
        import certifi

        ctx = ssl.create_default_context(cafile=certifi.where())
        ctx.load_verify_locations(cafile=cfg.ca_file)
        _tls_contexts[cfg.ca_file] = ctx
    return ctx


def _untrusted(e: BaseException) -> bool:
    return isinstance(e, ssl.SSLCertVerificationError) or "CERTIFICATE_VERIFY_FAILED" in str(e)


def unreachable(what: str, url: str, e: BaseException) -> "SsoError":
    """The provider could not be reached: a certificate the gateway does not trust has a
    code and a remedy of its own (the most common case in a company)."""
    if _untrusted(e):
        host = url.split("/")[2] if "://" in url else url
        return SsoError(f"{what}: the certificate of {host} is not signed by an authority the gateway trusts "
                        f"({e}). Give the company's certificate authority in OIDC__CA_FILE (the chart's "
                        "oidc.caCert), or accept it with OIDC__VERIFY_TLS=false", "idp_untrusted")
    return SsoError(f"{what} on {url}: {e}", "idp_unreachable")


def discovery(cfg: Optional[OidcSettings] = None, *, force: bool = False) -> dict:
    """Documento di discovery dell'IdP (authorize/token/JWKS), cache con TTL."""
    cfg = cfg or get_settings().oidc
    cached = _discovery_cache.get(cfg.discovery_url)
    if cached and not force and time.time() - cached[0] < cfg.discovery_ttl_seconds:
        return cached[1]
    try:
        res = httpx.get(cfg.discovery_url, timeout=cfg.timeout_seconds, verify=tls(cfg))
        res.raise_for_status()
        doc = res.json()
    except Exception as e:
        raise unreachable("OIDC discovery failed", cfg.discovery_url, e) from e
    for field in ("authorization_endpoint", "token_endpoint", "jwks_uri", "issuer"):
        if not doc.get(field):
            raise SsoError(f"discovery document without '{field}'", "idp_invalid")
    _discovery_cache[cfg.discovery_url] = (time.time(), doc)
    return doc


def reset_discovery_cache() -> None:
    """Used by the tests and after a configuration change."""
    _discovery_cache.clear()
    _jwks_clients.clear()
    _tls_contexts.clear()


def _jwks_client(jwks_uri: str, timeout: float) -> jwt.PyJWKClient:
    client = _jwks_clients.get((jwks_uri, timeout))
    if client is None:
        # PyJWKClient caches the keys and downloads them again at rollover
        context = tls(get_settings().oidc)
        if context is False:  # OIDC__VERIFY_TLS=false: the keys too, on the same terms
            context = ssl._create_unverified_context()
        client = jwt.PyJWKClient(jwks_uri, timeout=int(timeout) or 10,
                                 ssl_context=context if isinstance(context, ssl.SSLContext) else None)
        _jwks_clients[(jwks_uri, timeout)] = client
    return client


# ─────────────────────────────────────────────────────────────────────────────
# Login transaction: state + nonce + PKCE in a signed, short-lived cookie
# ─────────────────────────────────────────────────────────────────────────────
def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def new_pkce_pair() -> tuple[str, str]:
    """(code_verifier, code_challenge S256). PKCE protects the code exchange for a
    confidential client too: OAuth 2.1 recommends it."""
    verifier = _b64url(secrets.token_bytes(48))
    challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
    return verifier, challenge


def issue_tx_token(state: str, nonce: str, verifier: str, next_url: str = "") -> str:
    s = get_settings()
    now = datetime.now(timezone.utc)
    payload = {
        "typ": TX_TOKEN_TYPE,
        "state": state,
        "nonce": nonce,
        "cv": verifier,
        "next": next_url,
        "iat": now,
        "exp": now + timedelta(seconds=s.oidc.login_tx_ttl_seconds),
    }
    return jwt.encode(payload, s.jwt.secret.get_secret_value(), algorithm=s.jwt.algorithm)


def read_tx_token(token: str, state_from_idp: str) -> dict:
    """Validates the transaction cookie and the `state` (CSRF defence on the callback)."""
    s = get_settings()
    try:
        payload = jwt.decode(token, s.jwt.secret.get_secret_value(), algorithms=[s.jwt.algorithm],
                             leeway=CLOCK_SKEW_SECONDS)
    except jwt.PyJWTError as e:
        raise SsoError(f"transaction cookie invalid or expired: {e}", "tx_expired") from e
    if payload.get("typ") != TX_TOKEN_TYPE:
        raise SsoError("transaction cookie of the wrong type", "tx_invalid")
    # constant-time comparison: the state is an anti-CSRF secret
    if not secrets.compare_digest(str(payload.get("state", "")), str(state_from_idp or "")):
        raise SsoError("state does not match (possible CSRF on the callback)", "state_mismatch")
    return payload


# ─────────────────────────────────────────────────────────────────────────────
# Authorization code flow
# ─────────────────────────────────────────────────────────────────────────────
def build_authorize_url(cfg: OidcSettings, state: str, nonce: str, challenge: str) -> str:
    doc = discovery(cfg)
    params = {
        "response_type": "code",
        "client_id": cfg.client_id,
        "redirect_uri": cfg.redirect_uri,
        "scope": " ".join(cfg.scope_list),
        "state": state,
        "nonce": nonce,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    sep = "&" if "?" in doc["authorization_endpoint"] else "?"
    return f"{doc['authorization_endpoint']}{sep}{urlencode(params)}"


def exchange_code(cfg: OidcSettings, code: str, verifier: str) -> dict:
    """Exchanges the code for the tokens. The client secret travels ONLY here, server to
    server: it never goes through the browser."""
    doc = discovery(cfg)
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": cfg.redirect_uri,
        "client_id": cfg.client_id,
        "client_secret": cfg.client_secret.get_secret_value(),
        "code_verifier": verifier,
    }
    try:
        res = httpx.post(doc["token_endpoint"], data=data, timeout=cfg.timeout_seconds, verify=tls(cfg))
    except Exception as e:
        raise unreachable("token endpoint unreachable", doc["token_endpoint"], e) from e
    if res.status_code != 200:
        # the body may hold configuration details: it stays in the logs
        raise SsoError(f"token exchange refused ({res.status_code}): {res.text[:300]}", "code_rejected")
    payload = res.json()
    if not payload.get("id_token"):
        raise SsoError("token endpoint response without id_token", "idp_invalid")
    return payload


def validate_id_token(cfg: OidcSettings, id_token: str, nonce: str) -> dict:
    """Checks signature (JWKS), `iss`, `aud`, `exp` and `nonce`. Returns the claims."""
    doc = discovery(cfg)
    algs = [a for a in (doc.get("id_token_signing_alg_values_supported") or []) if a in ALLOWED_ALGS]
    algs = algs or list(ALLOWED_ALGS)
    try:
        key = _jwks_client(doc["jwks_uri"], cfg.timeout_seconds).get_signing_key_from_jwt(id_token)
    except Exception as e:
        if _untrusted(e):
            raise unreachable("signing keys not readable", doc["jwks_uri"], e) from e
        raise SsoError(f"signing key not retrievable from the JWKS: {e}", "jwks_error") from e
    try:
        claims = jwt.decode(
            id_token,
            key.key,
            algorithms=algs,
            audience=cfg.client_id,
            issuer=doc["issuer"],
            options={"require": ["exp", "iat", "iss", "aud", "sub"]},
            leeway=CLOCK_SKEW_SECONDS,  # the provider's clock is not the gateway's
        )
    except jwt.PyJWTError as e:
        raise SsoError(f"invalid id_token: {e}", "token_invalid") from e
    # the nonce ties the token to THIS login request (replay protection)
    if not secrets.compare_digest(str(claims.get("nonce", "")), str(nonce or "")):
        raise SsoError("nonce does not match in the id_token", "nonce_mismatch")
    return claims


# ─────────────────────────────────────────────────────────────────────────────
# JIT provisioning + the groups' synchronization
# ─────────────────────────────────────────────────────────────────────────────
def email_from_claims(claims: dict) -> str:
    """The user's email. Entra ID often does not send `email` but the UPN in
    `preferred_username`/`upn`: they are accepted as an alternative."""
    for field in ("email", "preferred_username", "upn"):
        value = str(claims.get(field) or "").strip()
        if "@" in value:
            return value.lower()
    raise SsoError("the id_token contains no email (nor UPN): the user cannot be identified", "no_email")


def identity_from_claims(claims: dict) -> tuple[str, str]:
    """The user's STABLE identity at the IdP: the (issuer, subject) pair.

    It is this — not the email — that says WHO is signing in. The IdP assigns the
    subject, it is immutable and the user cannot choose it; email, UPN and
    `preferred_username` instead can be changed in many directories, and whoever can
    choose them could otherwise get recognized as someone else.

    `validate_id_token` requires `iss` and `sub` already, so in the real flow they are
    always there: a token without them is refused here anyway.
    """
    issuer = str(claims.get("iss") or "").strip()
    subject = str(claims.get("sub") or "").strip()
    if not issuer or not subject:
        raise SsoError(
            "the id_token contains no iss/sub: it does not identify anyone stably", "no_identity"
        )
    return issuer, subject


def claim_is_true(value: Any) -> bool:
    """A boolean claim is true ONLY when it says so explicitly.

    IdPs send booleans or strings indifferently, and the old guard checked `is False`:
    the claim missing, or being `"false"` or `0`, was enough to get past it. Here the
    absence means NOT confirmed, the only safe default for a check.
    """
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"true", "1", "yes"}


def ensure_admitted(cfg: OidcSettings, email: str, idp_groups: set[str]) -> None:
    """Who may SIGN IN, which is not the same as which groups they get.

    Without these two gates an active account is born for anyone the IdP authenticates:
    pointed at the company directory, that means the whole company. Both are off by
    default, so they change nothing for whoever uses SSO already; whoever turns them on
    decides the perimeter explicitly.
    """
    if cfg.allowed_domains:
        domain = email.rsplit("@", 1)[-1].lower()
        if domain not in cfg.allowed_domains:
            raise SsoError(f"domain {domain} is not allowed to sign in", "not_allowed")

    if cfg.require_allowlisted_group:
        allowed = set(cfg.allowlist)
        if cfg.superuser_group:
            allowed.add(cfg.superuser_group)
        if not allowed:
            # contradictory configuration: an allowed group is required but none is listed →
            # nobody would ever sign in, silently
            raise SsoError(
                "OIDC__REQUIRE_ALLOWLISTED_GROUP is on but OIDC__GROUP_ALLOWLIST is empty",
                "config_error",
            )
        if not (idp_groups & allowed):
            raise SsoError(f"{email} does not belong to any allowed group", "not_allowed")


def group_sources(cfg: OidcSettings) -> list[str]:
    """Where the groups are (OIDC__GROUPS_CLAIM): one or more claims, comma-separated, each a
    top-level name (`groups`) or a path into the token (`realm_access.roles`,
    `resource_access.tabularia.roles`, Keycloak's realm and client roles)."""
    return [s.strip() for s in (cfg.groups_claim or "").split(",") if s.strip()]


def claim_at(claims: Any, path: str) -> Any:
    """The value at a dotted path of the claims, None when it is not there. A key that
    itself holds dots (a client id such as `tabularia.gateway`) is matched before the
    path is split there, the longest first."""
    if not isinstance(claims, dict):
        return None
    if path in claims:
        return claims[path]
    parts = path.split(".")
    for i in range(len(parts) - 1, 0, -1):
        head = ".".join(parts[:i])
        if head in claims:
            found = claim_at(claims[head], ".".join(parts[i:]))
            if found is not None:
                return found
    return None


def normalize_groups(claims: dict, cfg: OidcSettings, more: tuple[dict, ...] = ()) -> set[str]:
    """The groups' names from the configured claims, normalized.

    Each source is read from the id_token's claims, else from the first of `more` that
    has it (the access token's claims, userinfo: see `more_group_claims`). Keycloak may
    issue full paths (`/analytics`): the leading slash is removed. A claim may be
    a list or a single string.
    """
    names: set[str] = set()
    for source in group_sources(cfg):
        raw = claim_at(claims, source)
        for other in more:
            if raw is not None:
                break
            raw = claim_at(other, source)
        if isinstance(raw, str):
            raw = [raw]
        if not isinstance(raw, (list, tuple, set)):
            continue
        names |= {str(g).strip().lstrip("/").strip() for g in raw}
    return {n for n in names if n}


def groups_claim_found(claims: dict, cfg: OidcSettings, more: tuple[dict, ...] = ()) -> bool:
    """Whether any configured group source is in the claims at all. None found is either a
    person in no group (Keycloak and Entra then leave the claim out) or a groups claim set
    wrong: the sign-in treats both as no groups, and its audit says which claim was missing."""
    return any(claim_at(c, source) is not None for source in group_sources(cfg) for c in (claims, *more))


def _access_token_claims(cfg: OidcSettings, doc: dict, access_token: str, subject: str) -> Optional[dict]:
    """The claims of the access token, when it is a JWT signed by the provider's keys,
    issued by it and for the same person: Keycloak puts the realm's and the clients' roles
    (`realm_access`, `resource_access`) there and not in the id_token. Its audience is a
    resource server's, not ours, so it is not checked; it came straight from the token
    endpoint, server to server. Anything else (an opaque token, Entra's Graph tokens): None."""
    try:
        key = _jwks_client(doc["jwks_uri"], cfg.timeout_seconds).get_signing_key_from_jwt(access_token)
        algs = [a for a in (doc.get("id_token_signing_alg_values_supported") or []) if a in ALLOWED_ALGS] or list(ALLOWED_ALGS)
        claims = jwt.decode(access_token, key.key, algorithms=algs, issuer=doc["issuer"], leeway=CLOCK_SKEW_SECONDS,
                            options={"verify_aud": False, "require": ["exp", "iss", "sub"]})
    except Exception as e:  # noqa: BLE001 — not a token we can read: the other sources remain
        logger.info("SSO: groups not read from the access token (%s)", type(e).__name__)
        return None
    return claims if str(claims.get("sub")) == str(subject) else None


def _userinfo_claims(cfg: OidcSettings, doc: dict, access_token: str, subject: str) -> Optional[dict]:
    """The provider's userinfo answer, for the same person (OIDC: its `sub` must match)."""
    endpoint = doc.get("userinfo_endpoint")
    if not endpoint:
        return None
    try:
        res = httpx.get(endpoint, headers={"Authorization": f"Bearer {access_token}"}, timeout=cfg.timeout_seconds,
                        verify=tls(cfg))
        res.raise_for_status()
        claims = res.json()
    except Exception as e:  # noqa: BLE001
        logger.warning("SSO: userinfo not readable (%s)", type(e).__name__)
        return None
    return claims if isinstance(claims, dict) and str(claims.get("sub")) == str(subject) else None


def more_group_claims(cfg: OidcSettings, claims: dict, tokens: dict) -> tuple[dict, ...]:
    """Where to look for the group sources the id_token does not carry: the access token
    (Keycloak's roles are there by default), then userinfo. Asked only when one is missing."""
    missing = [s for s in group_sources(cfg) if claim_at(claims, s) is None]
    access_token = tokens.get("access_token")
    if not missing or not access_token:
        return ()
    doc = discovery(cfg)
    found: list[dict] = []
    at = _access_token_claims(cfg, doc, access_token, claims.get("sub"))
    if at is not None:
        found.append(at)
    if any(all(claim_at(c, s) is None for c in found) for s in missing):
        info = _userinfo_claims(cfg, doc, access_token, claims.get("sub"))
        if info is not None:
            found.append(info)
    return tuple(found)


def _group_ids(session: Session, names: set[str], auto_create: bool) -> set[int]:
    """The ids of the Tabularia groups matching the names. With `auto_create` the missing
    names become new groups; otherwise they are simply ignored (the admin decides which
    groups exist)."""
    if not names:
        return set()
    existing = {g.name: g for g in session.exec(select(Group).where(Group.name.in_(names))).all()}
    ids = {g.id for g in existing.values()}
    if auto_create:
        for missing in names - set(existing):
            group = Group(name=missing, description="created by SSO at the first login")
            session.add(group)
            session.flush()
            ids.add(group.id)
    return ids


def provision_and_sync(session: Session, claims: dict, cfg: Optional[OidcSettings] = None,
                       groups: Optional[set[str]] = None, groups_known: Optional[bool] = None) -> tuple[User, dict]:
    """Creates/updates the user from the claims and reconciles their groups.

    Returns `(user, detail)`; the detail ends up in the audit and says what changed
    (user created, groups added/removed, superuser).
    """
    cfg = cfg or get_settings().oidc
    issuer, subject = identity_from_claims(claims)
    email = email_from_claims(claims)
    # the groups the provider asserts: given by login_user (which also reads the access
    # token and userinfo), or from the claims alone
    asserted = set(groups) if groups is not None else normalize_groups(claims, cfg)
    if groups_known is None:
        groups_known = groups_claim_found(claims, cfg)
    # 0) may they sign in? Before creating or linking anything.
    ensure_admitted(cfg, email, set(asserted))

    # 1) WHO is signing in: always from the IdP's stable pair, never from the email.
    user = session.exec(
        select(User).where(User.oidc_issuer == issuer, User.oidc_subject == subject)
    ).first()
    created = False
    linked = False

    if user is None:
        # 2) An identity never seen. Is there an account with that email already?
        same_name = session.exec(select(User).where(User.email == email)).first()
        if same_name is None:
            # an SSO-only user: no local password (nullable column)
            user = User(
                email=email,
                full_name=str(claims.get("name") or claims.get("given_name") or "").strip(),
                hashed_password=None,
                is_active=True,
                oidc_issuer=issuer,
                oidc_subject=subject,
            )
            session.add(user)
            session.flush()
            created = True
        elif not same_name.is_active:
            raise SsoError(f"user {email} is deactivated in Tabularia", "user_disabled")
        elif same_name.is_service:
            # a service account signs in only with a token, never from an IdP
            raise SsoError(f"{email} is a service account", "identity_conflict")
        elif same_name.oidc_subject:
            # that account belongs to ANOTHER identity of the IdP already: two people cannot claim
            # the same user
            raise SsoError(
                f"the account {email} is already linked to another IdP identity", "identity_conflict"
            )
        elif not claim_is_true(claims.get("email_verified")):
            # HERE was the takeover: an existing account was claimed just because the token
            # carried its email. The link happens once only and requires the IdP to declare the
            # email VERIFIED.
            raise SsoError(
                f"the IdP does not declare the email {email} verified: linking to the "
                "existing account refused",
                "email_unverified",
            )
        else:
            same_name.oidc_issuer = issuer
            same_name.oidc_subject = subject
            session.add(same_name)
            user = same_name
            linked = True

    if not user.is_active:
        raise SsoError(f"user {user.email} is deactivated in Tabularia", "user_disabled")

    # The email at the IdP may change: it is updated, the identity stays the same. It is
    # not touched when the address belongs to someone else already (UNIQUE constraint).
    if user.email != email:
        busy_one = session.exec(select(User).where(User.email == email, User.id != user.id)).first()
        if busy_one is None:
            user.email = email
            session.add(user)

    if claims.get("name") and not user.full_name:
        user.full_name = str(claims["name"]).strip()
        session.add(user)

    idp_groups = set(asserted)
    # the group that grants admin privileges is not a project group: it is weighed BEFORE
    # the allowlist, so it need not be listed there too
    superuser_before = user.is_superuser
    if not groups_known:
        # No group source in the token. Keycloak and Entra leave the claim out when the
        # person is in no group, so it is read as no groups: the administrators' rights
        # and the memberships go (closing is the safe side). A claim name set wrong looks
        # the same: the log and the audit say so, and the local admin can sign in.
        logger.warning("SSO: none of the group claims (%s) is in the token of %s: read as no groups",
                       cfg.groups_claim, email)
    if cfg.superuser_group:
        user.is_superuser = cfg.superuser_group in idp_groups
        idp_groups.discard(cfg.superuser_group)
        if user.is_superuser != superuser_before:
            session.add(user)

    if cfg.allowlist:
        idp_groups &= cfg.allowlist

    wanted = _group_ids(session, idp_groups, cfg.auto_create_groups)
    # Admin and observer groups carry a ROLE, and are matched by name only: whoever
    # gets a group with that name onto their token at the IdP (self-service groups
    # on Entra, a loose Keycloak realm) would get the role. SSO never adds nor
    # removes those memberships: administration comes from the IdP only through
    # OIDC__SUPERUSER_GROUP, an observer group only when it is in the allowlist.
    managed_here = {
        g.id for g in session.exec(select(Group).where(or_(Group.is_admin, Group.is_observer))).all()
        if g.is_admin or g.name not in cfg.allowlist
    }
    wanted -= managed_here
    current = user_group_ids(session, user) - managed_here

    for gid in wanted - current:
        session.add(UserGroupLink(user_id=user.id, group_id=gid))
    removed_ids: set[int] = set()
    if cfg.authoritative:
        removed_ids = current - wanted
        for gid in removed_ids:
            link = session.exec(
                select(UserGroupLink).where(UserGroupLink.user_id == user.id, UserGroupLink.group_id == gid)
            ).first()
            if link is not None:
                session.delete(link)
    session.commit()
    session.refresh(user)

    def _names(ids: set[int]) -> list[str]:
        if not ids:
            return []
        return sorted(session.exec(select(Group.name).where(Group.id.in_(ids))).all())

    detail = {
        "created": created,
        # linking a PRE-EXISTING account to an IdP identity: it happens once only and must be
        # left in plain sight in the audit
        "linked": linked,
        "groups_added": _names(wanted - current),
        "groups_removed": _names(removed_ids),
        "groups": sorted(idp_groups),
        "is_superuser": user.is_superuser,
    }
    if user.is_superuser != superuser_before:
        detail["superuser_changed"] = True
    if not groups_known:
        detail["groups_claim_missing"] = cfg.groups_claim
    return user, detail


def login_user(session: Session, code: str, tx: dict, cfg: Optional[OidcSettings] = None) -> tuple[User, dict]:
    """The whole callback: code → token → validation → user synchronized."""
    cfg = cfg or get_settings().oidc
    tokens = exchange_code(cfg, code, tx["cv"])
    claims = validate_id_token(cfg, tokens["id_token"], tx["nonce"])
    if claims.get("email_verified") is False:
        # when the IdP declares the email NOT verified it is not trusted: it would be a way to
        # take over. When the claim is missing (typical of Entra) it goes on.
        raise SsoError("email not verified by the IdP", "email_unverified")
    more = more_group_claims(cfg, claims, tokens)
    return provision_and_sync(session, claims, cfg, groups=normalize_groups(claims, cfg, more),
                              groups_known=groups_claim_found(claims, cfg, more))


def public_config() -> dict[str, Any]:
    """What the login page may know WITHOUT being authenticated: whether SSO exists and
    what the button is called. Never issuer, client id or secrets."""
    cfg = get_settings().oidc
    return {"enabled": cfg.enabled, "button_label": cfg.button_label}
