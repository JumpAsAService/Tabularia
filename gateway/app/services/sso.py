"""SSO OIDC opzionale: porta d'ingresso verso un IdP esterno (Keycloak, Entra
ID/MSAL, Auth0, Okta…) e sincronizzazione dei gruppi.

Principio: l'IdP dimostra CHI è l'utente, poi si emette il SOLITO token interno
(`create_access_token`). `get_current_user`, l'RBAC e l'audit restano identici —
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
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from urllib.parse import urlencode

import httpx
import jwt
from sqlmodel import Session, select

from app.core.config import OidcSettings, get_settings
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


# ─────────────────────────────────────────────────────────────────────────────
# Discovery (.well-known) e chiavi di firma, con cache
# ─────────────────────────────────────────────────────────────────────────────
_discovery_cache: dict[str, tuple[float, dict]] = {}
_jwks_clients: dict[str, jwt.PyJWKClient] = {}


def discovery(cfg: Optional[OidcSettings] = None, *, force: bool = False) -> dict:
    """Documento di discovery dell'IdP (authorize/token/JWKS), cache con TTL."""
    cfg = cfg or get_settings().oidc
    cached = _discovery_cache.get(cfg.discovery_url)
    if cached and not force and time.time() - cached[0] < cfg.discovery_ttl_seconds:
        return cached[1]
    try:
        res = httpx.get(cfg.discovery_url, timeout=cfg.timeout_seconds)
        res.raise_for_status()
        doc = res.json()
    except Exception as e:
        raise SsoError(f"discovery OIDC fallita su {cfg.discovery_url}: {e}", "idp_unreachable") from e
    for field in ("authorization_endpoint", "token_endpoint", "jwks_uri", "issuer"):
        if not doc.get(field):
            raise SsoError(f"documento di discovery senza '{field}'", "idp_invalid")
    _discovery_cache[cfg.discovery_url] = (time.time(), doc)
    return doc


def reset_discovery_cache() -> None:
    """Usata dai test e dopo un cambio di configurazione."""
    _discovery_cache.clear()
    _jwks_clients.clear()


def _jwks_client(jwks_uri: str, timeout: float) -> jwt.PyJWKClient:
    client = _jwks_clients.get(jwks_uri)
    if client is None:
        # PyJWKClient tiene in cache le chiavi e le ri-scarica al rollover
        client = jwt.PyJWKClient(jwks_uri, timeout=int(timeout) or 10)
        _jwks_clients[jwks_uri] = client
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
        payload = jwt.decode(token, s.jwt.secret.get_secret_value(), algorithms=[s.jwt.algorithm])
    except jwt.PyJWTError as e:
        raise SsoError(f"cookie di transazione non valido o scaduto: {e}", "tx_expired") from e
    if payload.get("typ") != TX_TOKEN_TYPE:
        raise SsoError("cookie di transazione di tipo errato", "tx_invalid")
    # confronto a tempo costante: lo state è un segreto anti-CSRF
    if not secrets.compare_digest(str(payload.get("state", "")), str(state_from_idp or "")):
        raise SsoError("state non corrispondente (possibile CSRF sul callback)", "state_mismatch")
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
        res = httpx.post(doc["token_endpoint"], data=data, timeout=cfg.timeout_seconds)
    except Exception as e:
        raise SsoError(f"token endpoint irraggiungibile: {e}", "idp_unreachable") from e
    if res.status_code != 200:
        # il corpo può contenere dettagli della configurazione: resta nei log
        raise SsoError(f"token exchange rifiutato ({res.status_code}): {res.text[:300]}", "code_rejected")
    payload = res.json()
    if not payload.get("id_token"):
        raise SsoError("risposta del token endpoint senza id_token", "idp_invalid")
    return payload


def validate_id_token(cfg: OidcSettings, id_token: str, nonce: str) -> dict:
    """Verifica firma (JWKS), `iss`, `aud`, `exp` e `nonce`. Ritorna le claim."""
    doc = discovery(cfg)
    algs = [a for a in (doc.get("id_token_signing_alg_values_supported") or []) if a in ALLOWED_ALGS]
    algs = algs or list(ALLOWED_ALGS)
    try:
        key = _jwks_client(doc["jwks_uri"], cfg.timeout_seconds).get_signing_key_from_jwt(id_token)
    except Exception as e:
        raise SsoError(f"chiave di firma non recuperabile dal JWKS: {e}", "jwks_error") from e
    try:
        claims = jwt.decode(
            id_token,
            key.key,
            algorithms=algs,
            audience=cfg.client_id,
            issuer=doc["issuer"],
            options={"require": ["exp", "iat", "iss", "aud", "sub"]},
        )
    except jwt.PyJWTError as e:
        raise SsoError(f"id_token non valido: {e}", "token_invalid") from e
    # il nonce lega il token a QUESTA richiesta di login (replay protection)
    if not secrets.compare_digest(str(claims.get("nonce", "")), str(nonce or "")):
        raise SsoError("nonce non corrispondente nell'id_token", "nonce_mismatch")
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
    raise SsoError("l'id_token non contiene un'email (né UPN): impossibile identificare l'utente", "no_email")


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
            "l'id_token non contiene iss/sub: non identifica stabilmente nessuno", "no_identity"
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
            raise SsoError(f"dominio {dominio} non ammesso all'accesso", "not_allowed")

    if cfg.require_allowlisted_group:
        ammessi = set(cfg.allowlist)
        if cfg.superuser_group:
            ammessi.add(cfg.superuser_group)
        if not ammessi:
            # configurazione contraddittoria: si richiede un gruppo ammesso ma
            # non ne è elencato nessuno → nessuno entrerebbe mai, in silenzio
            raise SsoError(
                "OIDC__REQUIRE_ALLOWLISTED_GROUP è attivo ma OIDC__GROUP_ALLOWLIST è vuoto",
                "config_error",
            )
        if not (idp_groups & ammessi):
            raise SsoError(f"{email} non appartiene a nessun gruppo ammesso", "not_allowed")


def normalize_groups(claims: dict, cfg: OidcSettings) -> set[str]:
    """Nomi dei gruppi dalla claim configurata, normalizzati.

    Keycloak può emettere i path completi (`/analytics`): si toglie lo slash
    iniziale. La claim può essere una lista o una stringa singola.
    """
    raw = claims.get(cfg.groups_claim)
    if raw is None:
        return set()
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple, set)):
        return set()
    names = {str(g).strip().lstrip("/").strip() for g in raw}
    return {n for n in names if n}


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


def provision_and_sync(session: Session, claims: dict, cfg: Optional[OidcSettings] = None) -> tuple[User, dict]:
    """Crea/aggiorna l'utente dalle claim e riconcilia i suoi gruppi.

    Ritorna `(utente, dettaglio)`; il dettaglio finisce nell'audit e dice cosa è
    cambiato (utente creato, gruppi aggiunti/rimossi, superuser).
    """
    cfg = cfg or get_settings().oidc
    issuer, subject = identity_from_claims(claims)
    email = email_from_claims(claims)
    # 0) ha il permesso di entrare? Prima di creare o collegare qualsiasi cosa.
    ensure_admitted(cfg, email, normalize_groups(claims, cfg))

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
            raise SsoError(f"utente {email} disattivato in Tabularia", "user_disabled")
        elif omonimo.oidc_subject:
            # quell'account appartiene già a un'ALTRA identità dell'IdP: due
            # persone non possono rivendicare lo stesso utente
            raise SsoError(
                f"l'account {email} è già collegato a un'altra identità dell'IdP", "identity_conflict"
            )
        elif not claim_is_true(claims.get("email_verified")):
            # QUI stava il takeover: un account esistente veniva rivendicato solo
            # perché il token portava la sua email. Il collegamento avviene una
            # volta sola e richiede che l'IdP dichiari l'email VERIFICATA.
            raise SsoError(
                f"l'IdP non dichiara verificata l'email {email}: collegamento all'account "
                "esistente rifiutato",
                "email_unverified",
            )
        else:
            omonimo.oidc_issuer = issuer
            omonimo.oidc_subject = subject
            session.add(omonimo)
            user = omonimo
            linked = True

    if not user.is_active:
        raise SsoError(f"utente {user.email} disattivato in Tabularia", "user_disabled")

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

    idp_groups = normalize_groups(claims, cfg)
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
    current = user_group_ids(session, user)

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
        raise SsoError("email non verificata presso l'IdP", "email_unverified")
    return provision_and_sync(session, claims, cfg)


def public_config() -> dict[str, Any]:
    """Ciò che la pagina di login può sapere SENZA essere autenticata: se l'SSO
    esiste e come si chiama il pulsante. Mai issuer, client id o segreti."""
    cfg = get_settings().oidc
    return {"enabled": cfg.enabled, "button_label": cfg.button_label}
