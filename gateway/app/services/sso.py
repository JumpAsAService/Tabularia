"""SSO OIDC opzionale: porta d'ingresso verso un IdP esterno (Keycloak, Entra
ID/MSAL, Auth0, Okta…) e sincronizzazione dei gruppi.

Principio: l'IdP dimostra CHI è l'utente, poi si apre la SOLITA sessione interna
(`sessions.apri`). `get_current_user`, l'RBAC e l'audit restano identici —
il raggio d'impatto è confinato a queste due cuciture:

1. **accettazione**: authorization code flow con PKCE, `state` e `nonce`; l'
   id_token è validato contro il JWKS dell'IdP (firma, `iss`, `aud`, `exp`, `nonce`);
2. **sincronizzazione**: la claim dei gruppi (`groups` su Keycloak, `roles` per le
   app role di Entra) viene riconciliata su `user_groups`, che è l'unica cosa che
   l'RBAC legge (`user_group_ids`). La mappatura è PER NOME: nessun id esterno
   entra nel modello dei permessi.

Nessuna dipendenza nuova: discovery e token exchange con `httpx`, validazione con
`PyJWT` (`PyJWKClient`), PKCE con la stdlib — entrambe già usate dal gateway.
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
    """Errore del flusso SSO. Il messaggio è per i LOG: all'utente arriva un
    codice generico (non si espongono dettagli dell'IdP o della configurazione)."""

    def __init__(self, message: str, code: str = "sso_failed"):
        super().__init__(message)
        self.code = code


# algoritmi di firma ACCETTATI per l'id_token: solo asimmetrici. Mai `none`, mai
# HS* (userebbe il client secret come chiave simmetrica: superficie inutile).
ALLOWED_ALGS = ("RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384", "ES512")

# tipo del cookie di transazione del login (state/nonce/PKCE), firmato col segreto
# del gateway: niente stato server-side, funziona anche con più repliche.
TX_TOKEN_TYPE = "sso_tx"
TX_COOKIE = "tab_sso_tx"


def tx_cookie_name(state: str) -> str:
    """One cookie per sign-in, named after its `state`: a second sign-in started meanwhile
    (a double click, another tab, the Back button) no longer overwrites the first one's,
    which then came back as `state_mismatch`. The name holds a digest of the state, never
    the state itself."""
    return f"{TX_COOKIE}_{hashlib.sha256(state.encode()).hexdigest()[:16]}"


# ─────────────────────────────────────────────────────────────────────────────
# Discovery (.well-known) e chiavi di firma, con cache
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
    """Usata dai test e dopo un cambio di configurazione."""
    _discovery_cache.clear()
    _jwks_clients.clear()
    _tls_contexts.clear()


def _jwks_client(jwks_uri: str, timeout: float) -> jwt.PyJWKClient:
    client = _jwks_clients.get((jwks_uri, timeout))
    if client is None:
        # PyJWKClient tiene in cache le chiavi e le ri-scarica al rollover
        context = tls(get_settings().oidc)
        if context is False:  # OIDC__VERIFY_TLS=false: the keys too, on the same terms
            context = ssl._create_unverified_context()
        client = jwt.PyJWKClient(jwks_uri, timeout=int(timeout) or 10,
                                 ssl_context=context if isinstance(context, ssl.SSLContext) else None)
        _jwks_clients[(jwks_uri, timeout)] = client
    return client


# ─────────────────────────────────────────────────────────────────────────────
# Transazione di login: state + nonce + PKCE in un cookie firmato e di breve vita
# ─────────────────────────────────────────────────────────────────────────────
def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def new_pkce_pair() -> tuple[str, str]:
    """(code_verifier, code_challenge S256). PKCE protegge lo scambio del code
    anche per un client confidenziale: è raccomandato da OAuth 2.1."""
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
    """Valida il cookie di transazione e il `state` (difesa CSRF sul callback)."""
    s = get_settings()
    try:
        payload = jwt.decode(token, s.jwt.secret.get_secret_value(), algorithms=[s.jwt.algorithm],
                             leeway=CLOCK_SKEW_SECONDS)
    except jwt.PyJWTError as e:
        raise SsoError(f"transaction cookie invalid or expired: {e}", "tx_expired") from e
    if payload.get("typ") != TX_TOKEN_TYPE:
        raise SsoError("transaction cookie of the wrong type", "tx_invalid")
    # confronto a tempo costante: lo state è un segreto anti-CSRF
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
    """Scambia il code con i token. Il client secret viaggia SOLO qui, da server
    a server: non passa mai dal browser."""
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
        # il corpo può contenere dettagli della configurazione: resta nei log
        raise SsoError(f"token exchange refused ({res.status_code}): {res.text[:300]}", "code_rejected")
    payload = res.json()
    if not payload.get("id_token"):
        raise SsoError("token endpoint response without id_token", "idp_invalid")
    return payload


def validate_id_token(cfg: OidcSettings, id_token: str, nonce: str) -> dict:
    """Verifica firma (JWKS), `iss`, `aud`, `exp` e `nonce`. Ritorna le claim."""
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
    # il nonce lega il token a QUESTA richiesta di login (replay protection)
    if not secrets.compare_digest(str(claims.get("nonce", "")), str(nonce or "")):
        raise SsoError("nonce does not match in the id_token", "nonce_mismatch")
    return claims


# ─────────────────────────────────────────────────────────────────────────────
# Provisioning JIT + sincronizzazione dei gruppi
# ─────────────────────────────────────────────────────────────────────────────
def email_from_claims(claims: dict) -> str:
    """Email dell'utente. Entra ID spesso non manda `email` ma lo UPN in
    `preferred_username`/`upn`: si accettano come alternativa."""
    for field in ("email", "preferred_username", "upn"):
        value = str(claims.get(field) or "").strip()
        if "@" in value:
            return value.lower()
    raise SsoError("the id_token contains no email (nor UPN): the user cannot be identified", "no_email")


def identity_from_claims(claims: dict) -> tuple[str, str]:
    """Identità STABILE dell'utente presso l'IdP: la coppia (issuer, subject).

    È questa — non l'email — a dire CHI sta entrando. Il subject lo assegna
    l'IdP, è immutabile e l'utente non se lo può scegliere; email, UPN e
    `preferred_username` invece sono modificabili in molte directory, e chi può
    sceglierseli potrebbe altrimenti farsi riconoscere come qualcun altro.

    `validate_id_token` esige già `iss` e `sub`, quindi nel flusso reale ci sono
    sempre: qui si rifiuta comunque un token che ne sia privo.
    """
    issuer = str(claims.get("iss") or "").strip()
    subject = str(claims.get("sub") or "").strip()
    if not issuer or not subject:
        raise SsoError(
            "the id_token contains no iss/sub: it does not identify anyone stably", "no_identity"
        )
    return issuer, subject


def claim_is_true(value: Any) -> bool:
    """Una claim booleana è vera SOLO se lo afferma esplicitamente.

    Gli IdP mandano indifferentemente booleani o stringhe, e la vecchia guardia
    controllava `is False`: bastava che la claim mancasse, o valesse `"false"`
    o `0`, per superarla. Qui l'assenza vale NON confermato, che è l'unico
    default sicuro per una verifica.
    """
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"true", "1", "yes"}


def ensure_admitted(cfg: OidcSettings, email: str, idp_groups: set[str]) -> None:
    """Chi può ENTRARE, che è cosa diversa da quali gruppi riceve.

    Senza questi due cancelli un account attivo nasce per chiunque l'IdP
    autentichi: puntato alla directory aziendale, significa tutta l'azienda.
    Entrambi sono spenti di default, quindi non cambiano nulla a chi già usa
    l'SSO; chi li accende decide esplicitamente il perimetro.
    """
    if cfg.allowed_domains:
        dominio = email.rsplit("@", 1)[-1].lower()
        if dominio not in cfg.allowed_domains:
            raise SsoError(f"domain {dominio} is not allowed to sign in", "not_allowed")

    if cfg.require_allowlisted_group:
        ammessi = set(cfg.allowlist)
        if cfg.superuser_group:
            ammessi.add(cfg.superuser_group)
        if not ammessi:
            # configurazione contraddittoria: si richiede un gruppo ammesso ma
            # non ne è elencato nessuno → nessuno entrerebbe mai, in silenzio
            raise SsoError(
                "OIDC__REQUIRE_ALLOWLISTED_GROUP is on but OIDC__GROUP_ALLOWLIST is empty",
                "config_error",
            )
        if not (idp_groups & ammessi):
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
    """Nomi dei gruppi dalle claim configurate, normalizzati.

    Each source is read from the id_token's claims, else from the first of `more` that
    has it (the access token's claims, userinfo: see `more_group_claims`). Keycloak può
    emettere i path completi (`/analytics`): si toglie lo slash iniziale. A claim may be
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
    """Id dei gruppi Tabularia corrispondenti ai nomi. Con `auto_create` i nomi
    mancanti diventano gruppi nuovi; altrimenti vengono semplicemente ignorati
    (l'admin decide quali gruppi esistono)."""
    if not names:
        return set()
    existing = {g.name: g for g in session.exec(select(Group).where(Group.name.in_(names))).all()}
    ids = {g.id for g in existing.values()}
    if auto_create:
        for missing in names - set(existing):
            group = Group(name=missing, description="creato dall'SSO al primo login")
            session.add(group)
            session.flush()
            ids.add(group.id)
    return ids


def provision_and_sync(session: Session, claims: dict, cfg: Optional[OidcSettings] = None,
                       groups: Optional[set[str]] = None) -> tuple[User, dict]:
    """Crea/aggiorna l'utente dalle claim e riconcilia i suoi gruppi.

    Ritorna `(utente, dettaglio)`; il dettaglio finisce nell'audit e dice cosa è
    cambiato (utente creato, gruppi aggiunti/rimossi, superuser).
    """
    cfg = cfg or get_settings().oidc
    issuer, subject = identity_from_claims(claims)
    email = email_from_claims(claims)
    # the groups the provider asserts: given by login_user (which also reads the access
    # token and userinfo), or from the claims alone
    asserted = set(groups) if groups is not None else normalize_groups(claims, cfg)
    # 0) ha il permesso di entrare? Prima di creare o collegare qualsiasi cosa.
    ensure_admitted(cfg, email, set(asserted))

    # 1) CHI sta entrando: sempre dalla coppia stabile dell'IdP, mai dall'email.
    user = session.exec(
        select(User).where(User.oidc_issuer == issuer, User.oidc_subject == subject)
    ).first()
    created = False
    linked = False

    if user is None:
        # 2) Identità mai vista. Esiste già un account con quell'email?
        omonimo = session.exec(select(User).where(User.email == email)).first()
        if omonimo is None:
            # utente solo-SSO: nessuna password locale (colonna nullable)
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
        elif not omonimo.is_active:
            raise SsoError(f"user {email} is deactivated in Tabularia", "user_disabled")
        elif omonimo.is_service:
            # un account di servizio entra solo con un token, mai da un IdP
            raise SsoError(f"{email} is a service account", "identity_conflict")
        elif omonimo.oidc_subject:
            # quell'account appartiene già a un'ALTRA identità dell'IdP: due
            # persone non possono rivendicare lo stesso utente
            raise SsoError(
                f"the account {email} is already linked to another IdP identity", "identity_conflict"
            )
        elif not claim_is_true(claims.get("email_verified")):
            # QUI stava il takeover: un account esistente veniva rivendicato solo
            # perché il token portava la sua email. Il collegamento avviene una
            # volta sola e richiede che l'IdP dichiari l'email VERIFICATA.
            raise SsoError(
                f"the IdP does not declare the email {email} verified: linking to the "
                "existing account refused",
                "email_unverified",
            )
        else:
            omonimo.oidc_issuer = issuer
            omonimo.oidc_subject = subject
            session.add(omonimo)
            user = omonimo
            linked = True

    if not user.is_active:
        raise SsoError(f"user {user.email} is deactivated in Tabularia", "user_disabled")

    # L'email presso l'IdP può cambiare: si aggiorna, l'identità resta la stessa.
    # Non si tocca se l'indirizzo è già di qualcun altro (vincolo UNIQUE).
    if user.email != email:
        occupata = session.exec(select(User).where(User.email == email, User.id != user.id)).first()
        if occupata is None:
            user.email = email
            session.add(user)

    if claims.get("name") and not user.full_name:
        user.full_name = str(claims["name"]).strip()
        session.add(user)

    idp_groups = set(asserted)
    # il gruppo che concede i privilegi admin non è un gruppo di progetto: si
    # valuta PRIMA dell'allowlist, così non serve elencarlo anche lì
    superuser_before = user.is_superuser
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
    gestiti_qui = {
        g.id for g in session.exec(select(Group).where(or_(Group.is_admin, Group.is_observer))).all()
        if g.is_admin or g.name not in cfg.allowlist
    }
    wanted -= gestiti_qui
    current = user_group_ids(session, user) - gestiti_qui

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
        # collegamento di un account PREESISTENTE a un'identità dell'IdP: avviene
        # una volta sola e va lasciato in chiaro nell'audit
        "linked": linked,
        "groups_added": _names(wanted - current),
        "groups_removed": _names(removed_ids),
        "groups": sorted(idp_groups),
        "is_superuser": user.is_superuser,
    }
    if user.is_superuser != superuser_before:
        detail["superuser_changed"] = True
    return user, detail


def login_user(session: Session, code: str, tx: dict, cfg: Optional[OidcSettings] = None) -> tuple[User, dict]:
    """Callback completo: code → token → validazione → utente sincronizzato."""
    cfg = cfg or get_settings().oidc
    tokens = exchange_code(cfg, code, tx["cv"])
    claims = validate_id_token(cfg, tokens["id_token"], tx["nonce"])
    if claims.get("email_verified") is False:
        # se l'IdP dichiara l'email NON verificata non ci si fida: sarebbe una
        # via di takeover. Se la claim manca (tipico di Entra) si procede.
        raise SsoError("email not verified by the IdP", "email_unverified")
    groups = normalize_groups(claims, cfg, more_group_claims(cfg, claims, tokens))
    return provision_and_sync(session, claims, cfg, groups=groups)


def public_config() -> dict[str, Any]:
    """Ciò che la pagina di login può sapere SENZA essere autenticata: se l'SSO
    esiste e come si chiama il pulsante. Mai issuer, client id o segreti."""
    cfg = get_settings().oidc
    return {"enabled": cfg.enabled, "button_label": cfg.button_label}
