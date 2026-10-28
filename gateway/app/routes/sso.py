"""OIDC SSO routes (optional): `/auth/sso/config`, `/auth/sso/login`, `/auth/sso/callback`.

When SSO is not configured the routes exist anyway but answer "off":
`/auth/sso/config` returns `{"enabled": false}` and the frontend does not show the
button. The local login is never touched (break-glass when the IdP cannot be
reached).

Handing the session to the browser: the same `HttpOnly` cookie as the local login,
written on the response that redirects to `OIDC__POST_LOGIN_URL`. The token never goes
through the URL nor through JavaScript; the callback page asks only «who am I?». An
error comes back in the fragment instead (`#error=<code>`), which holds no secrets.
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
from app.core.routing import ReleasingRoute

logger = logging.getLogger(__name__)

router = APIRouter(route_class=ReleasingRoute, prefix="/auth/sso", tags=["auth"])


def _cookie_path() -> str:
    """The transaction cookie's path AS THE BROWSER SEES IT: behind a proxy that publishes
    the gateway under a prefix (APP__ROOT_PATH) the flow's routes are under that prefix,
    and a cookie written without it would never come back. Writing and deleting must
    match."""
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
    """The whole log on the server, a generic code to the user."""
    logger.warning("SSO failed (%s): %s", err.code, err)
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
    """Public (pre-login): tells the frontend whether to show the SSO button."""
    return sso.public_config()


@router.get("/login")
def sso_login(request: Request):
    """Starts the authorization code flow: generates state/nonce/PKCE, puts them in a signed,
    short-lived cookie and sends the user to the IdP."""
    cfg = get_settings().oidc
    if not cfg.enabled:
        return _redirect_with("error=sso_disabled")
    state, nonce = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    verifier, challenge = sso.new_pkce_pair()
    try:
        url = sso.build_authorize_url(cfg, state, nonce, challenge)
    except sso.SsoError as e:
        logger.warning("SSO: authorize cannot be built (%s): %s", e.code, e)
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
        httponly=True,                 # never readable by JavaScript
        samesite="lax",                # the return from the IdP is a GET navigation
        # as the session's cookie: Secure when the browser is on https. Deciding it from
        # the redirect URI lost the cookie of a sign-in started on http (the browser
        # refuses a Secure cookie there), and the callback said tx_missing
        secure=sessions._in_https(request),
        path=_cookie_path(),           # sent only to the flow's routes
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
    """Return from the IdP: validates, provisions, synchronizes the groups and opens an
    INTERNAL session (the same as the local login's)."""
    cfg = get_settings().oidc
    if not cfg.enabled:
        return _redirect_with("error=sso_disabled")
    if error:
        logger.warning("SSO: the IdP refused the login: %s (%s)", error, error_description)
        return _fail(session, request, sso.SsoError(f"IdP: {error} {error_description}", "idp_denied"), state=state)
    if not code:
        return _fail(session, request, sso.SsoError("callback without authorization code", "no_code"), state=state)

    # this sign-in's cookie; the single cookie of the previous version, for a sign-in
    # started on it during an upgrade
    tx_cookie = (request.cookies.get(sso.tx_cookie_name(state)) if state else None) or request.cookies.get(sso.TX_COOKIE)
    if not tx_cookie:
        return _fail(session, request, sso.SsoError(_why_missing(request), "tx_missing"), state=state)

    try:
        tx = sso.read_tx_token(tx_cookie, state)
        user, detail = sso.login_user(session, code, tx, cfg, request=request)
    except sso.SsoError as e:
        return _fail(session, request, e, state=state)
    except Exception as e:  # unexpected: must never expose the stack to the user
        logger.exception("SSO: unexpected error in the callback")
        return _fail(session, request, sso.SsoError(str(e), "sso_failed"), state=state)

    audit.record_audit(
        session, actor=user, action=audit.SSO_LOGIN,
        detail={k: v for k, v in detail.items() if v not in ((), [], False)} or None,
        request=request,
    )
    sessions.close_the_cookies_session(session, request)
    try:
        row, token = sessions.open_session(session, user, request, "sso")
    except sessions.SignInSuperseded:  # disabled while coming back from the IdP
        return _fail(session, request, sso.SsoError("user disabled during the sign-in", "sso_failed"), user.email,
                     state=state)
    response = RedirectResponse(get_settings().oidc.post_login_url, status_code=status.HTTP_303_SEE_OTHER)
    sessions.write_cookie(response, request, token, row)
    response.delete_cookie(sso.tx_cookie_name(state), path=_cookie_path())
    response.delete_cookie(sso.TX_COOKIE, path=_cookie_path())
    return response
