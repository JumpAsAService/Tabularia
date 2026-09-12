"""Rotte SSO OIDC (opzionali): `/auth/sso/config`, `/auth/sso/login`, `/auth/sso/callback`.

Se l'SSO non è configurato le rotte esistono comunque ma rispondono "spento":
`/auth/sso/config` torna `{"enabled": false}` e il frontend non mostra il
pulsante. Il login locale non viene toccato in nessun caso (break-glass se l'IdP
è irraggiungibile).

Consegna del token al frontend: redirect a `OIDC__POST_LOGIN_URL` con il token
nel FRAMMENTO dell'URL (`#token=…`). Il frammento non viene inviato al server,
quindi non finisce nei log di accesso né nell'header Referer; la pagina di
callback lo legge, lo salva nel cookie di sessione e ripulisce subito l'URL.
"""
import logging
import secrets

from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import RedirectResponse
from sqlmodel import Session

from app.core.config import get_settings
from app.core.security import create_access_token
from app.db.session import get_session
from app.services import audit, sso

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth/sso", tags=["auth"])


def _redirect_with(fragment: str) -> RedirectResponse:
    base = get_settings().oidc.post_login_url
    sep = "&" if "#" in base else "#"
    return RedirectResponse(f"{base}{sep}{fragment}", status_code=status.HTTP_303_SEE_OTHER)


def _fail(session: Session, request: Request, err: sso.SsoError, email: str | None = None) -> RedirectResponse:
    """Log completo lato server, codice generico all'utente."""
    logger.warning("SSO fallito (%s): %s", err.code, err)
    audit.record_audit(
        session, actor_label=email or "sso", action=audit.SSO_LOGIN_FAILED, outcome="failure",
        detail={"reason": err.code}, request=request,
    )
    response = _redirect_with(f"error={err.code}")
    response.delete_cookie(sso.TX_COOKIE, path="/auth/sso")
    return response


@router.get("/config")
def sso_config():
    """Pubblica (pre-login): dice al frontend se mostrare il pulsante SSO."""
    return sso.public_config()


@router.get("/login")
def sso_login(request: Request):
    """Avvia l'authorization code flow: genera state/nonce/PKCE, li mette in un
    cookie firmato di breve vita e manda l'utente sull'IdP."""
    cfg = get_settings().oidc
    if not cfg.enabled:
        return _redirect_with("error=sso_disabled")
    state, nonce = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    verifier, challenge = sso.new_pkce_pair()
    try:
        url = sso.build_authorize_url(cfg, state, nonce, challenge)
    except sso.SsoError as e:
        logger.warning("SSO: authorize non costruibile (%s): %s", e.code, e)
        return _redirect_with(f"error={e.code}")
    response = RedirectResponse(url, status_code=status.HTTP_303_SEE_OTHER)
    response.set_cookie(
        sso.TX_COOKIE,
        sso.issue_tx_token(state, nonce, verifier),
        max_age=cfg.login_tx_ttl_seconds,
        httponly=True,                 # mai leggibile da JavaScript
        samesite="lax",                # il ritorno dall'IdP è una navigazione GET
        secure=cfg.redirect_uri.startswith("https://"),
        path="/auth/sso",              # inviato solo alle rotte del flusso
    )
    return response


@router.get("/callback")
def sso_callback(
    request: Request,
    code: str = "",
    state: str = "",
    error: str = "",
    error_description: str = "",
    session: Session = Depends(get_session),
):
    """Ritorno dall'IdP: valida, provisiona, sincronizza i gruppi ed emette il
    TOKEN INTERNO (lo stesso del login locale)."""
    cfg = get_settings().oidc
    if not cfg.enabled:
        return _redirect_with("error=sso_disabled")
    if error:
        logger.warning("SSO: l'IdP ha rifiutato il login: %s (%s)", error, error_description)
        return _fail(session, request, sso.SsoError(f"IdP: {error} {error_description}", "idp_denied"))
    if not code:
        return _fail(session, request, sso.SsoError("callback senza authorization code", "no_code"))

    tx_cookie = request.cookies.get(sso.TX_COOKIE)
    if not tx_cookie:
        return _fail(session, request, sso.SsoError("cookie di transazione assente", "tx_missing"))

    try:
        tx = sso.read_tx_token(tx_cookie, state)
        user, detail = sso.login_user(session, code, tx, cfg)
    except sso.SsoError as e:
        return _fail(session, request, e)
    except Exception as e:  # imprevisto: non deve mai esporre lo stack all'utente
        logger.exception("SSO: errore imprevisto nel callback")
        return _fail(session, request, sso.SsoError(str(e), "sso_failed"))

    audit.record_audit(
        session, actor=user, action=audit.SSO_LOGIN,
        detail={k: v for k, v in detail.items() if v not in ((), [], False)} or None,
        request=request,
    )
    response = _redirect_with(f"token={create_access_token(user.id)}")
    response.delete_cookie(sso.TX_COOKIE, path="/auth/sso")
    return response
