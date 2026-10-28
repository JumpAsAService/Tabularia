"""marimo behind the gateway: `/notebooks/{id}/app/…` and `/notebooks/{id}/edit/…`, HTTP
and WebSocket, to the notebook's pod. docs/specs/notebooks.md («Proxy», «An origin of
their own»); the pods in services/notebook_runtime.py.

- Where: on the notebooks' own address (NOTEBOOKS__ORIGIN, services/notebook_frames.py
  says why), whose host reaches an application with these routes and nothing else
  (`install`); on the interface's address they answer 404. Without that address the
  app and the editor do not open, unless NOTEBOOKS__ALLOW_SAME_ORIGIN serves them on the
  interface's own (one machine only).
- Who: on the notebooks' address, the person of the notebook's cookie, which a ticket of
  Tabularia's page gives the frame (the root, `?ticket=`); never Tabularia's session
  cookie, never an API token. On the interface's address (ALLOW_SAME_ORIGIN), the person
  of Tabularia's session cookie. The app: whoever sees the notebook; the editor: whoever
  may edit it, and only the person editing it. Checked at every request, and every
  minute on an open WebSocket.
- marimo's requests carry no anti-CSRF header: a request that is not a GET, and every
  WebSocket, must come from a page of the address it is sent to instead (`Origin`).
- The root (`…/app/`) starts the app's pod when there is none and answers a small page
  that reloads itself until marimo answers; a failed start shows why. Anything else while
  the pod starts is a 503. An editor starts only from Tabularia's page there: a frame
  never starts one (a notebook's code could otherwise open another notebook's editor
  with the rights of whoever is looking).
- Every request, and every open WebSocket once a minute, keeps the pod alive
  (`last_seen_at`, throttled): the reaper stops the idle ones.
- An app takes a limited number of visitors at once (services/notebook_runtime.py,
  «Visitors»): marimo's WebSocket (`…/app/ws`) holds a place while it is open. Past the
  cap the root answers a page that says so and reloads itself until a place frees (one
  who has the app open already gets through: a reload, a second tab); a WebSocket that
  loses the race for the last place is closed with 1013 and the reason. The editor has
  no cap.
- marimo also opens a kernel for every `…/sse` stream (server-sent events, the
  alternative to its WebSocket that its page does not use here): an app's is refused, or
  it would be a visitor outside the cap. A path with dot segments is refused in either
  mode: httpx removes them, so `x/../sse` would reach `sse`, and browsers never send them.
- The answers may be framed only by the page of Tabularia that asked for the ticket
  (`frame-ancestors`), or, on the interface's address, by Tabularia's own pages
  (SAMEORIGIN); never by another site.
"""
from __future__ import annotations

import asyncio
import contextlib
import html
import logging
import re
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlencode

import anyio
import httpx
import websockets
from fastapi import APIRouter, Depends, FastAPI, Request, WebSocket
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from sqlmodel import Session
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool
from starlette.requests import HTTPConnection
from starlette.types import ASGIApp, Receive, Scope, Send
from starlette.websockets import WebSocketDisconnect

from app.core.body_limit import BodyLimitMiddleware
from app.core.routing import ReleasingRoute
from app.db.session import get_session
from app.deps.auth import cookie_user, same_origin, session_user
from app.models import Notebook, User
from app.models.permission import Capability
from app.services import notebook_frames as frames
from app.services import notebook_runtime as runtime
from app.services import notebooks
from app.services import permissions as perm_service

logger = logging.getLogger(__name__)

router = APIRouter(route_class=ReleasingRoute, tags=["notebooks"], include_in_schema=False)

_SAFE = {"GET", "HEAD", "OPTIONS"}
_METHODS = ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]
# hop-by-hop, and what is Tabularia's alone: the cookies (the session's, the notebook's),
# the anti-CSRF header. `origin` was checked here; marimo sees the request as the gateway's
_SKIP_REQUEST = {"host", "cookie", "authorization", "x-tabularia-csrf", "connection", "keep-alive", "te", "trailer",
                 "transfer-encoding", "upgrade", "proxy-authorization", "proxy-connection", "content-length", "origin"}
_SKIP_RESPONSE = {"connection", "keep-alive", "te", "trailer", "transfer-encoding", "upgrade", "proxy-authenticate",
                  "proxy-connection", "set-cookie", "server", "date", "x-frame-options"}
_FRAME_ANCESTORS = re.compile(r"frame-ancestors[^;]*", re.I)
_DOT_SEGMENT = re.compile(r"(^|/)\.{1,2}(/|$)")
_TIMEOUT = httpx.Timeout(connect=5.0, read=300.0, write=300.0, pool=10.0)
WS_CHECK_SECONDS = 60  # an open WebSocket keeps the pod alive, and is checked again, this often
VISITOR_SOCKET = "ws"  # marimo's session socket: the one a visitor holds (a kernel each)
EVENT_STREAM = "sse"  # marimo's other way to a kernel: refused in an app (see the docstring)
# a message of the pod is held whole before it goes on to the browser: marimo's outputs
# stay under 8 MB (its output_max_bytes); past this the socket closes (1009)
MAX_MESSAGE_BYTES = 64 * 1024 * 1024
FULL_RELOAD_SECONDS = 10  # the «full» page asks again this often
FULL_CLOSE_CODE = 1013  # Try Again Later
# a reload closes the old socket a moment after the new page opens its own: the new one
# waits this long for the place before it is refused
TAKE_TRIES, TAKE_PAUSE = 8, 0.25

# how a request may reach a notebook (`_where`)
FRAME = "frame"  # the notebooks' own address, with the notebook's cookie
SAME = "same"  # the interface's address, with Tabularia's session (NOTEBOOKS__ALLOW_SAME_ORIGIN)
ELSEWHERE = "elsewhere"  # the interface's address while the notebooks have their own: nothing
OFF = "off"  # no address of their own, and none allowed
# who may frame an answer: Tabularia's own pages, on the interface's address
SELF = "'self'"


@dataclass
class Found:
    """What a request may reach: an answer to give instead (`answer`), or the pod."""
    answer: Optional[Response] = None
    user_id: Optional[int] = None
    row_status: Optional[str] = None  # None: no session
    row_error: Optional[str] = None
    pod_ip: Optional[str] = None
    stale_app: bool = False  # the app runs a version that is no longer the current one
    framing: Optional[str] = None  # who may frame the answers (frame-ancestors)
    claims: Optional[frames.Claims] = None  # the notebook's cookie, on its own address


def _where(conn: HTTPConnection) -> str:
    if frames.configured():
        return FRAME if frames.on_notebooks_host(conn) else ELSEWHERE
    return SAME if frames.same_origin_allowed() else OFF


def _from_its_page(conn: HTTPConnection, where: str) -> bool:
    """Sent by a page of the address it was sent to (`Origin`)."""
    return frames.from_notebooks_page(conn) if where == FRAME else same_origin(conn)


# ── pages ────────────────────────────────────────────────────────────────────
def _frame_headers(framing: Optional[str]) -> dict[str, str]:
    """Who may frame an answer. None: a page of words alone, nothing on it to lure a click
    onto (no ticket, no cookie: the page that asked is not known)."""
    if framing is None:
        return {}
    headers = {"Content-Security-Policy": f"frame-ancestors {framing}"}
    if framing == SELF:
        headers["X-Frame-Options"] = "SAMEORIGIN"
    return headers


def _page(title: str, message: str, status: int, framing: Optional[str], refresh: bool = False, every: int = 2,
          headers: Optional[dict[str, str]] = None) -> HTMLResponse:
    """A small page in the notebook's frame, in the browser's light or dark scheme;
    `refresh` reloads it every `every` seconds."""
    meta = f'<meta http-equiv="refresh" content="{every}">' if refresh else ""
    body = (f'<!doctype html><html lang="en"><head><meta charset="utf-8">{meta}'
            f'<meta name="viewport" content="width=device-width, initial-scale=1"><title>{html.escape(title)}</title>'
            '<style>:root{color-scheme:light dark}body{font:15px/1.5 system-ui,sans-serif;margin:0;display:grid;'
            'place-items:center;min-height:100vh;padding:16px;box-sizing:border-box}main{max-width:36rem}'
            'p{white-space:pre-wrap}</style></head><body><main>'
            f'<h1 style="font-size:1.15rem">{html.escape(title)}</h1>'
            f'<p role="{"status" if refresh else "alert"}" aria-live="polite">{html.escape(message)}</p>'
            '</main></body></html>')
    return HTMLResponse(body, status_code=status, headers={"Cache-Control": "no-store", **_frame_headers(framing),
                                                           **(headers or {})})


def _starting(framing: Optional[str]) -> HTMLResponse:
    return _page("Starting…", "Starting the notebook. This page reloads by itself until it is ready.", 200, framing,
                 refresh=True)


def _people(n: int) -> str:
    return f"{n} {'person' if n == 1 else 'people'} connected"


def full_reason(count: int) -> str:
    """Why an app turns a visitor away; also the WebSocket's close reason (≤ 123 bytes)."""
    return f"This app already has {_people(count)}, the most it allows"


def _full(count: int, framing: Optional[str]) -> HTMLResponse:
    return _page("This app is full", f"{full_reason(count)}. It will open by itself as soon as someone leaves: "
                 f"this page checks again every {FULL_RELOAD_SECONDS} seconds.", 503, framing, refresh=True,
                 every=FULL_RELOAD_SECONDS, headers={"Retry-After": str(FULL_RELOAD_SECONDS)})


def _failed(error: Optional[str], framing: Optional[str]) -> HTMLResponse:
    return _page("The notebook could not start", (error or "It stopped before answering.")
                 + "\n\nClose this page and open the notebook again to try once more.", 502, framing)


def _off(root: bool) -> Response:
    """No address of their own for the notebooks, and the interface's not allowed."""
    if root:
        return _page("Notebooks are not set up", f"{frames.NEEDS_ORIGIN}.", 409, SELF)
    return JSONResponse({"detail": frames.NEEDS_ORIGIN}, status_code=409)


# ── who, and which pod ───────────────────────────────────────────────────────
def _person(conn: HTTPConnection, session: Session, notebook_id: int, mode: str, where: str,
            any_age: bool) -> tuple[Optional[User], Optional[str], Optional[frames.Claims]]:
    """Who asks, who may frame the answers, and the notebook's cookie (its own address)."""
    if where == FRAME:
        claims = frames.read_cookie(conn, notebook_id, mode, any_age=any_age)
        if claims is None:
            return None, None, None
        return session_user(conn, session, claims.user_id, claims.sid), claims.frame_origin, claims
    return cookie_user(conn, session), SELF, None


def _lookup(conn: HTTPConnection, session: Session, notebook_id: int, mode: str, root: bool, where: str,
            any_age: bool = False) -> Found:
    """In a thread, and the database handed back before leaving it (routes/proxy.py says why)."""
    try:
        user, framing, claims = _person(conn, session, notebook_id, mode, where, any_age)
        if user is None:
            if not root:
                return Found(answer=JSONResponse({"detail": "Invalid credentials"}, status_code=401))
            if where == FRAME:
                return Found(answer=_page("Open it from Tabularia", frames.OPEN_FROM_TABULARIA, 401, framing))
            return Found(answer=_page("Sign in", "Sign in to Tabularia to open this notebook.", 401, framing))
        notebook = session.get(Notebook, notebook_id)
        if notebook is None or not notebooks.can_see(session, user, notebook) or (
                mode == "edit" and not perm_service.has_capability(session, user, notebook.project_id, Capability.EDIT)):
            return Found(answer=_page("Not found", "There is no such notebook, or you may not open it.", 404, framing)
                         if root else JSONResponse({"detail": "Notebook not found"}, status_code=404))
        row = runtime.row_of(session, notebook.id, mode)
        found = Found(user_id=user.id, framing=framing, claims=claims)
        if row is None:
            return found
        if mode == "edit" and row.user_id != user.id and row.status != "failed":
            editor = session.get(User, row.user_id)
            message = f"«{notebook.name}» is being edited by {editor.email if editor else 'someone else'}."
            return Found(answer=_page("Being edited", message, 409, framing) if root
                         else JSONResponse({"detail": message}, status_code=409))
        found.row_status, found.row_error, found.pod_ip = row.status, row.error, row.pod_ip
        found.stale_app = mode == "app" and row.version != notebook.current_version
        if root and mode == "app" and row.status == "ready" and not found.stale_app:
            count = runtime.visitors(session, notebook.id)
            if count >= runtime.visitors_cap(notebook) and not runtime.is_visiting(session, notebook.id, user.id):
                return Found(answer=_full(count, framing))
        if row.status == "ready":
            runtime.touch(session, row)
        return found
    finally:
        session.close()


def _use_ticket(request: Request, session: Session, notebook_id: int, mode: str) -> Response:
    """The frame's first request on the notebooks' address (`?ticket=`): the ticket is
    used up for the notebook's cookie, and the frame goes on to the same address without
    it. A ticket used already, with a good cookie, is a frame loaded again: it goes on."""
    try:
        ticket = frames.redeem(session, request.query_params.get("ticket", ""), notebook_id, mode)
    finally:
        session.close()
    rest = [(k, v) for k, v in request.query_params.multi_items() if k != "ticket"]
    headers = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}
    there = RedirectResponse("./" + (f"?{urlencode(rest)}" if rest else ""), status_code=303, headers=headers)
    if ticket is None:
        if frames.read_cookie(request, notebook_id, mode) is not None:
            return there
        return _page("Open it from Tabularia", frames.OPEN_FROM_TABULARIA, 403, None)
    there.headers.append("set-cookie", frames.cookie_header(frames.cookie_value(
        user_id=ticket.user_id, sid=ticket.sid, notebook_id=notebook_id, mode=mode, frame_origin=ticket.frame_origin),
        notebook_id, mode))
    return there


async def _start(session: Session, notebook_id: int, mode: str, user_id: int, request: Request,
                 framing: Optional[str]) -> Response | str:
    """The root of a notebook without a live pod: it starts (the pod's address when it is
    ready already), or says why it cannot."""
    from fastapi import HTTPException

    try:
        notebook, user = session.get(Notebook, notebook_id), session.get(User, user_id)
        if notebook is None or user is None:
            return _page("Not found", "There is no such notebook.", 404, framing)
        try:
            row = await runtime.ensure_started(session, notebook, mode, user, request)
        except runtime.BeingEdited as e:
            return _page("Being edited", f"{e}.", 409, framing)
        except HTTPException as e:
            return _page("The notebook cannot start", str(e.detail), e.status_code, framing)
        if row.status == "failed":
            return _failed(row.error, framing)
        if row.status != "ready" or not row.pod_ip:
            return _starting(framing)
        return row.pod_ip
    finally:
        session.close()


# ── HTTP ─────────────────────────────────────────────────────────────────────
def _with_ancestors(policy: str, framing: str) -> str:
    """marimo's Content-Security-Policy, framed by `framing` only."""
    if "frame-ancestors" in policy.lower():
        return _FRAME_ANCESTORS.sub(f"frame-ancestors {framing}", policy)
    return f"{policy.rstrip('; ')}; frame-ancestors {framing}" if policy.strip() else f"frame-ancestors {framing}"


def _response_headers(upstream: httpx.Response, framing: Optional[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for k, v in upstream.headers.items():
        key = k.lower()
        if key in _SKIP_RESPONSE:
            continue
        if key == "content-security-policy" and framing is not None:
            v = _with_ancestors(v, framing)
        out[k] = v
    if framing == SELF:
        out["X-Frame-Options"] = "SAMEORIGIN"
    elif framing is not None and not any(k.lower() == "content-security-policy" for k in out):
        out["Content-Security-Policy"] = f"frame-ancestors {framing}"
    return out


def _out_of_bounds(mode: str, path: str) -> bool:
    """A path the proxy does not carry: dot segments, an app's event stream."""
    return bool(_DOT_SEGMENT.search(path)) or (mode == "app" and path.strip("/") == EVENT_STREAM)


async def _forward(request: Request, pod_ip: str, notebook_id: int, mode: str, path: str,
                   framing: Optional[str]) -> Response:
    url = f"http://{pod_ip}:{runtime.APP_PORT}{runtime.base_url(notebook_id, mode)}/{path}"
    if request.url.query:
        url += f"?{request.url.query}"
    headers = {k: v for k, v in request.headers.items() if k.lower() not in _SKIP_REQUEST}
    client = runtime.pod_client(_TIMEOUT)
    body = request.stream() if request.method not in ("GET", "HEAD", "OPTIONS") else None
    try:
        upstream = await client.send(client.build_request(request.method, url, headers=headers, content=body), stream=True)
    except httpx.HTTPError:
        await client.aclose()
        raise

    async def close() -> None:
        await upstream.aclose()
        await client.aclose()

    async def answer():
        try:
            async for chunk in upstream.aiter_raw():
                yield chunk
        except httpx.HTTPError:
            await close()  # the pod went away halfway: the background task does not run then
            raise

    return StreamingResponse(answer(), status_code=upstream.status_code,
                             headers=_response_headers(upstream, framing), background=BackgroundTask(close))


async def _proxy(request: Request, session: Session, notebook_id: int, mode: str, path: str) -> Response:
    where = _where(request)
    root = path == "" and request.method == "GET"
    if where == ELSEWHERE or _out_of_bounds(mode, path):
        return JSONResponse({"detail": "Not Found"}, status_code=404)
    if where == OFF:
        return _off(root)
    if where == FRAME and root and "ticket" in request.query_params:
        return await run_in_threadpool(_use_ticket, request, session, notebook_id, mode)
    if request.method not in _SAFE and not _from_its_page(request, where):
        return JSONResponse({"detail": "This request must come from the notebook's own page"}, status_code=403)
    found = await run_in_threadpool(_lookup, request, session, notebook_id, mode, root, where)
    if found.answer is not None:
        return found.answer
    pod_ip = found.pod_ip
    if found.row_status != "ready" or (root and found.stale_app):
        if not root:
            return JSONResponse({"detail": "The notebook is starting: try again in a moment"}, status_code=503,
                                headers={"Retry-After": "2"})
        if found.row_status == "failed":
            return _failed(found.row_error, found.framing)
        if where == FRAME and mode == "edit" and found.row_status is None:
            return _page("The editor is not running", "It stopped, or it was not started. Open it again from "
                         "Tabularia.", 409, found.framing)
        started = await _start(session, notebook_id, mode, found.user_id, request, found.framing)
        if isinstance(started, Response):
            return started
        pod_ip = started
    try:
        response = await _forward(request, pod_ip or "", notebook_id, mode, path, found.framing)
    except httpx.HTTPError as e:
        logger.info("notebooks: the pod of notebook %s (%s) not reached (%s)", notebook_id, mode, type(e).__name__)
        try:
            row = runtime.row_of(session, notebook_id, mode)
            gone = row is not None and not await runtime.alive(session, row)
        finally:
            session.close()
        message = ("The notebook stopped. Reload the page to start it again." if gone
                   else "The notebook is not answering: try again in a moment.")
        if root:
            return _page("The notebook is not answering", message, 502, found.framing)
        return JSONResponse({"detail": message}, status_code=502)
    # the notebook's cookie, written again while it is used: on the page and on what the
    # page does (never on marimo's files, which a cache may keep)
    if found.claims is not None and (root or request.method not in _SAFE):
        again = frames.renewed(found.claims)
        if again:
            response.headers.append("set-cookie", again)
    return response


@router.get("/notebooks/{notebook_id}/app")
@router.get("/notebooks/{notebook_id}/edit")
async def without_slash(request: Request):
    """marimo answers under the address with the slash: its pages' links are relative."""
    if _where(request) == ELSEWHERE:
        return JSONResponse({"detail": "Not Found"}, status_code=404)
    target = request.url.path.rsplit("/", 1)[-1] + "/"
    return RedirectResponse(target + (f"?{request.url.query}" if request.url.query else ""), status_code=307)


@router.api_route("/notebooks/{notebook_id}/app/{path:path}", methods=_METHODS)
async def app_http(notebook_id: int, path: str, request: Request, session: Session = Depends(get_session)):
    return await _proxy(request, session, notebook_id, "app", path)


@router.api_route("/notebooks/{notebook_id}/edit/{path:path}", methods=_METHODS)
async def edit_http(notebook_id: int, path: str, request: Request, session: Session = Depends(get_session)):
    return await _proxy(request, session, notebook_id, "edit", path)


# ── WebSocket ────────────────────────────────────────────────────────────────
def _still_open(conn: HTTPConnection, session: Session, notebook_id: int, mode: str,
                visitor_id: Optional[int] = None) -> bool:
    """Every minute of an open WebSocket: the person may still open the notebook, the
    session is still theirs; the pod's idle time starts again, the visitor's place is
    kept. The notebook's cookie of the handshake may have grown old meanwhile: its
    session is what counts."""
    if visitor_id is not None:
        runtime.keep_visitor(session, visitor_id)
    found = _lookup(conn, session, notebook_id, mode, root=False, where=_where(conn), any_age=True)
    return found.answer is None and found.row_status == "ready"


def _take(session: Session, notebook_id: int, user_id: int) -> tuple[Optional[int], int]:
    """A visitor's place: (its id, or None when the app is full; the visitors counted)."""
    try:
        notebook = session.get(Notebook, notebook_id)
        if notebook is None:
            return None, 0
        visitor_id, count, _ = runtime.take_visitor(session, notebook, user_id)
        return visitor_id, count
    finally:
        session.close()


def _drop(session: Session, visitor_id: int) -> None:
    try:
        runtime.drop_visitor(session, visitor_id)
    finally:
        session.close()


def _close_code(code: Optional[int]) -> int:
    """A code the browser may be sent (1006, 1005 are only observed, never sent)."""
    if code in (1000, 1001) or (code is not None and (1007 <= code <= 1011 or 3000 <= code <= 4999)):
        return code
    return 1011


async def _pump(browser: WebSocket, pod, check) -> int:
    async def to_pod() -> int:
        while True:
            message = await browser.receive()
            if message["type"] == "websocket.disconnect":
                await pod.close()
                return 1000
            if message.get("text") is not None:
                await pod.send(message["text"])
            elif message.get("bytes") is not None:
                await pod.send(message["bytes"])

    async def to_browser() -> int:
        with contextlib.suppress(websockets.ConnectionClosed):
            async for data in pod:
                if isinstance(data, str):
                    await browser.send_text(data)
                else:
                    await browser.send_bytes(data)
        # the pod's code; the gateway's own when it closed first (a message too large: 1009)
        frame = pod.protocol.close_rcvd or pod.protocol.close_sent
        return _close_code(frame.code if frame else None)

    async def keep() -> int:
        while True:
            await asyncio.sleep(WS_CHECK_SECONDS)
            if not await check():
                # the pod's socket is closed by the caller: closing it here would end
                # to_browser first, and the browser would get 1000 instead
                return 1008

    tasks = [asyncio.ensure_future(t()) for t in (to_pod, to_browser, keep)]
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        first = done.pop()
        return first.result() if not first.cancelled() and first.exception() is None else 1011
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def _websocket(browser: WebSocket, session: Session, notebook_id: int, mode: str, path: str) -> None:
    where = _where(browser)
    if where in (ELSEWHERE, OFF) or _out_of_bounds(mode, path) or not _from_its_page(browser, where):
        await browser.close(code=1008)
        return
    found = await run_in_threadpool(_lookup, browser, session, notebook_id, mode, False, where)
    if found.answer is not None or found.row_status != "ready" or not found.pod_ip:
        await browser.close(code=1008 if found.answer is not None else 1013)
        return
    visitor_id: Optional[int] = None
    if mode == "app" and path == VISITOR_SOCKET:
        count = 0
        for attempt in range(TAKE_TRIES):
            visitor_id, count = await run_in_threadpool(_take, session, notebook_id, found.user_id)
            if visitor_id is not None:
                break
            if attempt < TAKE_TRIES - 1:
                await asyncio.sleep(TAKE_PAUSE)
        if visitor_id is None:
            # accepted first: a refused handshake would reach the page without the reason
            await browser.accept()
            await browser.close(code=FULL_CLOSE_CODE, reason=full_reason(count))
            return
    try:
        await _carry(browser, session, notebook_id, mode, path, found.pod_ip, visitor_id)
    finally:
        if visitor_id is not None:
            # also when the handler is being cancelled (the server stopping): the place frees
            with anyio.CancelScope(shield=True):
                await run_in_threadpool(_drop, session, visitor_id)


async def _carry(browser: WebSocket, session: Session, notebook_id: int, mode: str, path: str, pod_ip: str,
                 visitor_id: Optional[int]) -> None:
    url = f"ws://{pod_ip}:{runtime.APP_PORT}{runtime.base_url(notebook_id, mode)}/{path}"
    if browser.url.query:
        url += f"?{browser.url.query}"
    wanted = [p.strip() for p in (browser.headers.get("sec-websocket-protocol") or "").split(",") if p.strip()]
    try:
        # at the pod's address, never through the environment's proxy (runtime.pod_client)
        pod = await websockets.connect(url, subprotocols=wanted or None, max_size=MAX_MESSAGE_BYTES, open_timeout=10,
                                       ping_interval=20, ping_timeout=20, compression=None, proxy=None)
    except (OSError, asyncio.TimeoutError, websockets.InvalidHandshake) as e:
        logger.info("notebooks: the WebSocket of notebook %s (%s) not opened (%s)", notebook_id, mode, type(e).__name__)
        await browser.close(code=1011)
        return

    async def check() -> bool:
        return await run_in_threadpool(_still_open, browser, session, notebook_id, mode, visitor_id)

    try:
        await browser.accept(subprotocol=pod.subprotocol)
        code = await _pump(browser, pod, check)
    except WebSocketDisconnect:
        code = 1000
    finally:
        await pod.close()
    with contextlib.suppress(RuntimeError, WebSocketDisconnect):
        await browser.close(code=code)


@router.websocket("/notebooks/{notebook_id}/app/{path:path}")
async def app_ws(browser: WebSocket, notebook_id: int, path: str, session: Session = Depends(get_session)):
    await _websocket(browser, session, notebook_id, "app", path)


@router.websocket("/notebooks/{notebook_id}/edit/{path:path}")
async def edit_ws(browser: WebSocket, notebook_id: int, path: str, session: Session = Depends(get_session)):
    await _websocket(browser, session, notebook_id, "edit", path)


# ── the notebooks' own host ──────────────────────────────────────────────────
class NotebooksHost:
    """ASGI: a request to the notebooks' own host (NOTEBOOKS__ORIGIN) goes to an
    application that has this module's routes and nothing else. There the rest of the
    API does not exist — not even the routes that share these paths, such as the editor's
    «Save version» — so a notebook's JavaScript finds nothing to call on its own address,
    whatever cookie the browser sends along."""

    def __init__(self, app: ASGIApp, notebooks: ASGIApp) -> None:
        self.app = app
        self.notebooks = notebooks

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] in ("http", "websocket") and frames.configured():
            host = next((v.decode("latin-1") for k, v in scope.get("headers") or [] if k == b"host"), "")
            if frames.is_notebooks_host(host):
                await self.notebooks(scope, receive, send)
                return
        await self.app(scope, receive, send)


def install(app: FastAPI) -> None:
    """Puts the notebooks' own host in front of `app` (main.py; the tests do the same).
    The two applications share the dependencies' overrides and the exception handlers;
    the bodies have the same ceiling. No CORS there: no other address calls it."""
    host = FastAPI(root_path=app.root_path, redirect_slashes=False, openapi_url=None, docs_url=None, redoc_url=None)
    host.dependency_overrides = app.dependency_overrides
    host.exception_handlers = app.exception_handlers
    host.include_router(router)
    host.add_middleware(BodyLimitMiddleware)
    app.add_middleware(NotebooksHost, notebooks=host)
