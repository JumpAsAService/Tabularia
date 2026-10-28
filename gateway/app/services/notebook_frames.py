"""The notebooks' own address (NOTEBOOKS__ORIGIN): the tickets, the notebook's cookie, and
which host a request came to. docs/specs/notebooks.md («An origin of their own»);
routes/notebook_proxy.py serves the apps and the editors there.

Why. marimo shows whatever HTML a notebook's code writes (`mo.Html`, `mo.iframe`, the
files it serves under `@file/`), not sanitised in an app: the notebook's author runs
JavaScript in the browser of whoever opens it. On the interface's address that code
would call Tabularia's API with their session — someone who may edit a notebook would
become administrator the first time an administrator opens their app. On an address of
its own it cannot: the API does not answer there (routes/notebook_proxy.py, `install`),
the interface's address does not let it in (CORS), and Tabularia's cookie is not the
notebook's.

How.
- Tabularia's page, signed in on the interface's address, asks for a ticket (`POST
  /notebooks/{id}/ticket`, with the rights that start the session: VIEW for the app, EDIT
  for the editor). The ticket is random, kept as its SHA-256, for that person, notebook
  and mode, and used once within TICKET_SECONDS.
- The frame opens `<NOTEBOOKS__ORIGIN><root path>/notebooks/{id}/{mode}/?ticket=…`; the
  proxy uses the ticket up and redirects to the same address without it, setting the
  notebook's cookie: signed with a key of its own, valid only for that notebook, mode
  and person, and only while the Tabularia session that asked for the ticket holds
  (closed, expired or idle, the frame stops too). HttpOnly, Secure, SameSite=None and
  Partitioned (CHIPS): in an iframe of another site it is a third party's cookie, which
  the browsers that block those still keep, in the partition of the interface's site.
- On the notebooks' host the proxy reads only that cookie: never Tabularia's session
  cookie (which the browser sends there too when AUTH__COOKIE_DOMAIN covers both hosts),
  never an API token. Every request checks again what the person may do now.
- The cookie lasts COOKIE_SECONDS and is written again while it is used; an open
  WebSocket is checked every minute without its age.
- The pages there may be framed only by the page that asked for the ticket (CSP
  `frame-ancestors`).

What stays: all the notebooks share that one address, so a notebook's code can reach
another notebook the same person has open in the same browser (the docs say so). The
proxy never starts an editor there: only Tabularia's page does.
"""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import quote, urlsplit

import jwt
from sqlalchemy import delete
from sqlmodel import Session, select
from starlette.requests import HTTPConnection

from app.core.config import get_settings
from app.models import NotebookTicket

TICKET_SECONDS = 60
COOKIE = "tab_notebook"
COOKIE_SECONDS = 3600  # written again while it is used (RENEW_AFTER)
RENEW_AFTER = 600
_TYPE = "notebook-frame"
_ALGORITHM = "HS256"

NEEDS_ORIGIN = ("Notebooks need an address of their own (NOTEBOOKS__ORIGIN): an administrator must set it")
OPEN_FROM_TABULARIA = ("This address opens a notebook only from its page in Tabularia, once, within a minute. "
                       "Open the notebook again from Tabularia.")


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def fingerprint(ticket: str) -> str:
    return hashlib.sha256(ticket.encode()).hexdigest()


# ── which address ────────────────────────────────────────────────────────────
def origin() -> str:
    """NOTEBOOKS__ORIGIN, as the settings normalise it (scheme://host[:port]); empty: none."""
    return get_settings().notebooks.origin


def configured() -> bool:
    return bool(origin())


def same_origin_allowed() -> bool:
    """No address of their own, and NOTEBOOKS__ALLOW_SAME_ORIGIN: the notebooks on the
    interface's address, with Tabularia's session cookie, as before (one machine only)."""
    return not configured() and get_settings().notebooks.allow_same_origin


def is_notebooks_host(host: str) -> bool:
    """`host` (a request's Host header) is the notebooks' own: their origin's host and
    port, a default port written out or not."""
    mine = origin()
    if not mine or not host:
        return False
    host = host.strip().lower()
    default = ":443" if mine.startswith("https:") else ":80"
    if host.endswith(default):
        host = host[: -len(default)]
    return host == urlsplit(mine).netloc


def on_notebooks_host(conn: HTTPConnection) -> bool:
    return is_notebooks_host(conn.headers.get("host") or "")


def from_notebooks_page(conn: HTTPConnection) -> bool:
    """The request was sent by a page of the notebooks' own address (`Origin`): what a
    WebSocket or a request that changes something must show there. Browsers send `Origin`
    with both, and another site cannot forge it."""
    sent = (conn.headers.get("origin") or "").strip().rstrip("/").lower()
    return bool(sent) and sent == origin()


_ORIGIN = re.compile(r"https?://[a-z0-9.\-]+(:[0-9]{1,5})?|https?://\[[0-9a-f:.]+\](:[0-9]{1,5})?")


def page_origin(conn: HTTPConnection) -> Optional[str]:
    """The origin of the page that sent the request (`Origin`), when it is a plain one:
    it goes into the frame's `frame-ancestors`."""
    sent = (conn.headers.get("origin") or "").strip().rstrip("/").lower()
    return sent if _ORIGIN.fullmatch(sent) else None


def frame_path(notebook_id: int, mode: str) -> str:
    """The app's or the editor's root as the browser sees it (behind the ingress's prefix),
    which is also the notebook's cookie's path."""
    return f"{get_settings().app.root_path}/notebooks/{notebook_id}/{mode}/"


def frame_url(notebook_id: int, mode: str, ticket: str) -> str:
    return f"{origin()}{frame_path(notebook_id, mode)}?ticket={quote(ticket, safe='')}"


# ── tickets ──────────────────────────────────────────────────────────────────
def issue(session: Session, *, notebook_id: int, mode: str, user_id: int, sid: str, frame_origin: str) -> str:
    """A new ticket (the caller checked the rights). The expired ones go now: nothing else
    deletes them, and they are a minute's worth."""
    session.exec(delete(NotebookTicket).where(NotebookTicket.expires_at < _now()))  # type: ignore[arg-type]
    ticket = secrets.token_urlsafe(32)
    session.add(NotebookTicket(token_fp=fingerprint(ticket), notebook_id=notebook_id, mode=mode, user_id=user_id,
                               sid=sid, frame_origin=frame_origin,
                               expires_at=_now() + timedelta(seconds=TICKET_SECONDS)))
    session.commit()
    return ticket


def redeem(session: Session, ticket: str, notebook_id: int, mode: str) -> Optional[NotebookTicket]:
    """The ticket, used up — when it exists, is not expired, and is for this notebook and
    mode; otherwise None. Two replicas using the same one: the delete decides, one wins."""
    if not ticket or len(ticket) > 200:
        return None
    row = session.exec(select(NotebookTicket).where(
        NotebookTicket.token_fp == fingerprint(ticket), NotebookTicket.notebook_id == notebook_id,
        NotebookTicket.mode == mode, NotebookTicket.expires_at > _now())).first()
    if row is None:
        return None
    gone = session.exec(delete(NotebookTicket).where(NotebookTicket.id == row.id))  # type: ignore[arg-type]
    session.commit()
    return row if gone.rowcount == 1 else None


def forget_notebook(session: Session, notebook_id: int) -> None:
    """A notebook being deleted: its tickets (the database's cascade does it too)."""
    session.exec(delete(NotebookTicket).where(NotebookTicket.notebook_id == notebook_id))  # type: ignore[arg-type]


# ── the notebook's cookie ────────────────────────────────────────────────────
@dataclass
class Claims:
    """What the notebook's cookie says: who, through which Tabularia session, which page
    frames it, since when."""
    user_id: int
    sid: str
    notebook_id: int
    mode: str
    frame_origin: str
    issued_at: int  # epoch seconds


def _key() -> bytes:
    """A key of its own, derived from the JWT secret: a notebook's cookie is never a
    session's token, nor the other way round."""
    return hmac.new(get_settings().jwt.secret.get_secret_value().encode(), b"tabularia notebook frames",
                    hashlib.sha256).digest()


def cookie_value(*, user_id: int, sid: str, notebook_id: int, mode: str, frame_origin: str) -> str:
    now = datetime.now(timezone.utc)
    return jwt.encode({"typ": _TYPE, "sub": str(user_id), "sid": sid, "nb": notebook_id, "mode": mode,
                       "anc": frame_origin, "iat": now, "exp": now + timedelta(seconds=COOKIE_SECONDS)},
                      _key(), algorithm=_ALGORITHM)


def cookie_header(value: str, notebook_id: int, mode: str) -> str:
    """The `Set-Cookie` of a notebook's frame. Written by hand: Python's cookies do not
    know `Partitioned` yet."""
    return (f"{COOKIE}={value}; Path={frame_path(notebook_id, mode)}; Max-Age={COOKIE_SECONDS}; HttpOnly; Secure; "
            "SameSite=None; Partitioned")


def renewed(claims: Claims) -> Optional[str]:
    """A new `Set-Cookie` for a cookie in use that is getting old; None while it is young."""
    if datetime.now(timezone.utc).timestamp() - claims.issued_at < RENEW_AFTER:
        return None
    value = cookie_value(user_id=claims.user_id, sid=claims.sid, notebook_id=claims.notebook_id, mode=claims.mode,
                         frame_origin=claims.frame_origin)
    return cookie_header(value, claims.notebook_id, claims.mode)


def read_cookie(conn: HTTPConnection, notebook_id: int, mode: str, any_age: bool = False) -> Optional[Claims]:
    """The notebook's cookie of this request, when it is good and for this notebook and
    mode; None otherwise. `any_age`: an expired one too (an open WebSocket's minute check:
    its socket was let in with a good one)."""
    value = conn.cookies.get(COOKIE)
    if not value or len(value) > 4096:
        return None
    try:
        data = jwt.decode(value, _key(), algorithms=[_ALGORITHM], options={"verify_exp": not any_age},
                          leeway=60)
        claims = Claims(user_id=int(data["sub"]), sid=str(data["sid"]), notebook_id=int(data["nb"]),
                        mode=str(data["mode"]), frame_origin=str(data["anc"]), issued_at=int(data["iat"]))
    except (jwt.PyJWTError, KeyError, ValueError, TypeError):
        return None
    if data.get("typ") != _TYPE or claims.notebook_id != notebook_id or claims.mode != mode:
        return None
    return claims
