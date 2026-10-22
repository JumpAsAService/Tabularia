"""Rotte SSO OIDC (opzionali): `/auth/sso/config`, `/auth/sso/login`, `/auth/sso/callback`.

Se l'SSO non è configurato le rotte esistono comunque ma rispondono "spento":
`/auth/sso/config` torna `{"enabled": false}` e il frontend non mostra il
pulsante. Il login locale non viene toccato in nessun caso (break-glass se l'IdP
è irraggiungibile).

Consegna della sessione al browser: lo stesso cookie `HttpOnly` del login locale,
scritto sulla risposta che rimanda a `OIDC__POST_LOGIN_URL`. Il token non passa
mai dall'URL né dal JavaScript; la pagina di callback chiede solo «chi sono?».
Un errore torna invece nel frammento (`#error=<codice>`), che non contiene segreti.
"""
import logging
import secrets
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import RedirectResponse
from sqlmodel import Session

from app.core.config import get_settings
from app.db.session import get_session
from app.services import audit, sessions, sso
from app.core.routing import RottaCheRilascia

logger = logging.getLogger(__name__)

router = APIRouter(route_class=RottaCheRilascia, prefix="/auth/sso", tags=["auth"])


def _cookie_path() -> str:
    """Il percorso del cookie di transazione COME LO VEDE IL BROWSER: dietro un
    proxy che pubblica il gateway sotto un prefisso (APP__ROOT_PATH) le rotte
    del flusso stanno sotto quel prefisso, e un cookie scritto senza non
    tornerebbe mai indietro. Scrittura e cancellazione devono coincidere."""
    return f"{get_settings().app.root_path}/auth/sso"


def _redirect_with(fragment: str) -> RedirectResponse:
    base = get_settings().oidc.post_login_url
    sep = "&" if "#" in base else "#"
    return RedirectResponse(f"{base}{sep}{fragment}", status_code=status.HTTP_303_SEE_OTHER)


def _seen_from(request: Request) -> str:
    """The address the browser used, as far as the gateway can tell (behind a proxy, what
    the proxy declares)."""
    proto = request.headers.get("x-forwarded-proto", "").split(",")[0].strip() or request.url.scheme
    host = request.headers.get("x-forwarded-host", "").split(",")[0].strip() or request.headers.get("host", "")
    return f"{proto}://{host}"


def _expected() -> str:
    u = urlsplit(get_settings().oidc.redirect_uri)
    return f"{u.scheme}://{u.netloc}"


def _why_missing(request: Request) -> str:
    """What to look at when the transaction cookie did not come back: almost always the
    sign-in started at an address other than OIDC__REDIRECT_URI's (another host name, an
    IP, http against https), whose cookies the browser keeps apart."""
    others = sorted(n for n in request.cookies if n.startswith(sso.TX_COOKIE))
    return (f"no transaction cookie came back with the callback (at {_seen_from(request)}, other sign-in "
            f"cookies present: {len(others)}). The sign-in started at an address other than "
            f"OIDC__REDIRECT_URI's ({_expected()}) — another host name, an IP, http against https — whose "
            "cookies the browser keeps apart (the warning 'sign-in started at …' logged when it began names "
            "it), or it is older than OIDC__LOGIN_TX_TTL_SECONDS, or the browser blocks cookies")


def _fail(session: Session, request: Request, err: sso.SsoError, email: str | None = None,
          state: str = "") -> RedirectResponse:
    """Log completo lato server, codice generico all'utente."""
    logger.warning("SSO fallito (%s): %s", err.code, err)
    audit.record_audit(
        session, actor_label=email or "sso", action=audit.SSO_LOGIN_FAILED, outcome="failure",
        detail={"reason": err.code}, request=request,
    )
    response = _redirect_with(f"error={err.code}")
    if state:
        response.delete_cookie(sso.tx_cookie_name(state), path=_cookie_path())
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
    seen, expected = _seen_from(request), _expected()
    if seen.split("://", 1)[-1] != expected.split("://", 1)[-1]:
        # the cookie written here will not reach the callback: say so now, in the log
        logger.warning("SSO: sign-in started at %s but OIDC__REDIRECT_URI is on %s: the browser will not "
                       "bring the transaction cookie back (tx_missing). Open Tabularia at the redirect URI's "
                       "address, or register this one", seen, expected)
    response = RedirectResponse(url, status_code=status.HTTP_303_SEE_OTHER)
    response.set_cookie(
        sso.tx_cookie_name(state),     # one per sign-in: a second one does not overwrite it
        sso.issue_tx_token(state, nonce, verifier),
        max_age=cfg.login_tx_ttl_seconds,
        httponly=True,                 # mai leggibile da JavaScript
        samesite="lax",                # il ritorno dall'IdP è una navigazione GET
        # as the session's cookie: Secure when the browser is on https. Deciding it from
        # the redirect URI lost the cookie of a sign-in started on http (the browser
        # refuses a Secure cookie there), and the callback said tx_missing
        secure=sessions._in_https(request),
        path=_cookie_path(),           # inviato solo alle rotte del flusso
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
    """Ritorno dall'IdP: valida, provisiona, sincronizza i gruppi e apre una
    sessione INTERNA (la stessa del login locale)."""
    cfg = get_settings().oidc
    if not cfg.enabled:
        return _redirect_with("error=sso_disabled")
    if error:
        logger.warning("SSO: l'IdP ha rifiutato il login: %s (%s)", error, error_description)
        return _fail(session, request, sso.SsoError(f"IdP: {error} {error_description}", "idp_denied"), state=state)
    if not code:
        return _fail(session, request, sso.SsoError("callback senza authorization code", "no_code"), state=state)

    # this sign-in's cookie; the single cookie of the previous version, for a sign-in
    # started on it during an upgrade
    tx_cookie = (request.cookies.get(sso.tx_cookie_name(state)) if state else None) or request.cookies.get(sso.TX_COOKIE)
    if not tx_cookie:
        return _fail(session, request, sso.SsoError(_why_missing(request), "tx_missing"), state=state)

    try:
        tx = sso.read_tx_token(tx_cookie, state)
        user, detail = sso.login_user(session, code, tx, cfg)
    except sso.SsoError as e:
        return _fail(session, request, e, state=state)
    except Exception as e:  # imprevisto: non deve mai esporre lo stack all'utente
        logger.exception("SSO: errore imprevisto nel callback")
        return _fail(session, request, sso.SsoError(str(e), "sso_failed"), state=state)

    audit.record_audit(
        session, actor=user, action=audit.SSO_LOGIN,
        detail={k: v for k, v in detail.items() if v not in ((), [], False)} or None,
        request=request,
    )
    sessions.chiudi_quella_del_cookie(session, request)
    try:
        riga, token = sessions.apri(session, user, request, "sso")
    except sessions.AccessoSuperato:  # disattivato mentre tornava dall'IdP
        return _fail(session, request, sso.SsoError("utente disattivato durante l'accesso", "sso_failed"), user.email,
                     state=state)
    response = RedirectResponse(get_settings().oidc.post_login_url, status_code=status.HTTP_303_SEE_OTHER)
    sessions.scrivi_cookie(response, request, token, riga)
    response.delete_cookie(sso.tx_cookie_name(state), path=_cookie_path())
    response.delete_cookie(sso.TX_COOKIE, path=_cookie_path())
    return response
