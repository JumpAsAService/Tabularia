"""marimo behind the gateway: `/notebooks/{id}/app/…` and `/notebooks/{id}/edit/…`, HTTP
and WebSocket, to the notebook's pod. docs/specs/notebooks.md («Proxy»); the pods in
services/notebook_runtime.py.

- Who: the person of Tabularia's session cookie (the page shows the notebook in an
  iframe of the same origin). The app: whoever sees the notebook; the editor: whoever
  may edit it, and only the person editing it. Checked at every request, and every
  minute on an open WebSocket.
- marimo's requests carry no anti-CSRF header: a request that is not a GET, and every
  WebSocket, must come from Tabularia's own origin instead (`Origin`).
- The root (`…/app/`) starts the pod when there is none and answers a small page that
  reloads itself until marimo answers; a failed start shows why. Anything else while the
  pod starts is a 503.
- Every request, and every open WebSocket once a minute, keeps the pod alive
  (`last_seen_at`, throttled): the reaper stops the idle ones.
- An app takes a limited number of visitors at once (services/notebook_runtime.py,
  «Visitors»): marimo's WebSocket (`…/app/ws`) holds a place while it is open. Past the
  cap the root answers a page that says so and reloads itself until a place frees (one
  who has the app open already gets through: a reload, a second tab); a WebSocket that
  loses the race for the last place is closed with 1013 and the reason. The editor has
  no cap.
- The pod's answers may be framed by Tabularia's pages only (SAMEORIGIN), never by
  another site.
"""
from __future__ import annotations

import asyncio
import contextlib
import html
import logging
import re
from dataclasses import dataclass
from typing import Optional

import anyio
import httpx
import websockets
from fastapi import APIRouter, Depends, Request, WebSocket
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from sqlmodel import Session
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool
from starlette.requests import HTTPConnection
from starlette.websockets import WebSocketDisconnect

from app.core.routing import ReleasingRoute
from app.db.session import get_session
from app.deps.auth import cookie_user, same_origin
from app.models import Notebook, User
from app.models.permission import Capability
from app.services import notebook_runtime as runtime
from app.services import notebooks
from app.services import permissions as perm_service

logger = logging.getLogger(__name__)

router = APIRouter(route_class=ReleasingRoute, tags=["notebooks"], include_in_schema=False)

_SAFE = {"GET", "HEAD", "OPTIONS"}
_METHODS = ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]
# hop-by-hop, and what is Tabularia's alone: the session cookie, the anti-CSRF header.
# `origin` was checked here; marimo sees the request as the gateway's
_SKIP_REQUEST = {"host", "cookie", "authorization", "x-tabularia-csrf", "connection", "keep-alive", "te", "trailer",
                 "transfer-encoding", "upgrade", "proxy-authorization", "proxy-connection", "content-length", "origin"}
_SKIP_RESPONSE = {"connection", "keep-alive", "te", "trailer", "transfer-encoding", "upgrade", "proxy-authenticate",
                  "proxy-connection", "set-cookie", "server", "date", "x-frame-options"}
_FRAME_ANCESTORS = re.compile(r"frame-ancestors[^;]*", re.I)
_TIMEOUT = httpx.Timeout(connect=5.0, read=300.0, write=300.0, pool=10.0)
WS_CHECK_SECONDS = 60  # an open WebSocket keeps the pod alive, and is checked again, this often
VISITOR_SOCKET = "ws"  # marimo's session socket: the one a visitor holds (a kernel each)
FULL_RELOAD_SECONDS = 10  # the «full» page asks again this often
FULL_CLOSE_CODE = 1013  # Try Again Later
# a reload closes the old socket a moment after the new page opens its own: the new one
# waits this long for the place before it is refused
TAKE_TRIES, TAKE_PAUSE = 8, 0.25


@dataclass
class Found:
    """What a request may reach: an answer to give instead (`answer`), or the pod."""
    answer: Optional[Response] = None
    user_id: Optional[int] = None
    row_status: Optional[str] = None  # None: no session
    row_error: Optional[str] = None
    pod_ip: Optional[str] = None
    stale_app: bool = False  # the app runs a version that is no longer the current one


# ── pages ────────────────────────────────────────────────────────────────────
def _page(title: str, message: str, status: int, refresh: bool = False, every: int = 2,
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
    return HTMLResponse(body, status_code=status, headers={"Cache-Control": "no-store", "X-Frame-Options": "SAMEORIGIN",
                                                           **(headers or {})})


def _starting() -> HTMLResponse:
    return _page("Starting…", "Starting the notebook. This page reloads by itself until it is ready.", 200, refresh=True)


def _people(n: int) -> str:
    return f"{n} {'person' if n == 1 else 'people'} connected"


def full_reason(count: int) -> str:
    """Why an app turns a visitor away; also the WebSocket's close reason (≤ 123 bytes)."""
    return f"This app already has {_people(count)}, the most it allows"


def _full(count: int) -> HTMLResponse:
    return _page("This app is full", f"{full_reason(count)}. It will open by itself as soon as someone leaves: "
                 f"this page checks again every {FULL_RELOAD_SECONDS} seconds.", 503, refresh=True,
                 every=FULL_RELOAD_SECONDS, headers={"Retry-After": str(FULL_RELOAD_SECONDS)})


def _failed(error: Optional[str]) -> HTMLResponse:
    return _page("The notebook could not start", (error or "It stopped before answering.")
                 + "\n\nClose this page and open the notebook again to try once more.", 502)


# ── who, and which pod ───────────────────────────────────────────────────────
def _lookup(conn: HTTPConnection, session: Session, notebook_id: int, mode: str, root: bool) -> Found:
    """In a thread, and the database handed back before leaving it (routes/proxy.py says why)."""
    try:
        user = cookie_user(conn, session)
        if user is None:
            return Found(answer=_page("Sign in", "Sign in to Tabularia to open this notebook.", 401) if root
                         else JSONResponse({"detail": "Invalid credentials"}, status_code=401))
        notebook = session.get(Notebook, notebook_id)
        if notebook is None or not notebooks.can_see(session, user, notebook) or (
                mode == "edit" and not perm_service.has_capability(session, user, notebook.project_id, Capability.EDIT)):
            return Found(answer=_page("Not found", "There is no such notebook, or you may not open it.", 404) if root
                         else JSONResponse({"detail": "Notebook not found"}, status_code=404))
        row = runtime.row_of(session, notebook.id, mode)
        found = Found(user_id=user.id)
        if row is None:
            return found
        if mode == "edit" and row.user_id != user.id and row.status != "failed":
            editor = session.get(User, row.user_id)
            message = f"«{notebook.name}» is being edited by {editor.email if editor else 'someone else'}."
            return Found(answer=_page("Being edited", message, 409) if root
                         else JSONResponse({"detail": message}, status_code=409))
        found.row_status, found.row_error, found.pod_ip = row.status, row.error, row.pod_ip
        found.stale_app = mode == "app" and row.version != notebook.current_version
        if root and mode == "app" and row.status == "ready" and not found.stale_app:
            count = runtime.visitors(session, notebook.id)
            if count >= runtime.visitors_cap(notebook) and not runtime.is_visiting(session, notebook.id, user.id):
                return Found(answer=_full(count))
        if row.status == "ready":
            runtime.touch(session, row)
        return found
    finally:
        session.close()


async def _start(session: Session, notebook_id: int, mode: str, user_id: int, request: Request) -> Response | str:
    """The root of a notebook without a live pod: it starts (the pod's address when it is
    ready already), or says why it cannot."""
    from fastapi import HTTPException

    try:
        notebook, user = session.get(Notebook, notebook_id), session.get(User, user_id)
        if notebook is None or user is None:
            return _page("Not found", "There is no such notebook.", 404)
        try:
            row = await runtime.ensure_started(session, notebook, mode, user, request)
        except runtime.BeingEdited as e:
            return _page("Being edited", f"{e}.", 409)
        except HTTPException as e:
            return _page("The notebook cannot start", str(e.detail), e.status_code)
        if row.status == "failed":
            return _failed(row.error)
        if row.status != "ready" or not row.pod_ip:
            return _starting()
        return row.pod_ip
    finally:
        session.close()


# ── HTTP ─────────────────────────────────────────────────────────────────────
def _response_headers(upstream: httpx.Response) -> dict[str, str]:
    out: dict[str, str] = {}
    for k, v in upstream.headers.items():
        key = k.lower()
        if key in _SKIP_RESPONSE:
            continue
        if key == "content-security-policy" and "frame-ancestors" in v.lower():
            v = _FRAME_ANCESTORS.sub("frame-ancestors 'self'", v)
        out[k] = v
    out["X-Frame-Options"] = "SAMEORIGIN"
    return out


async def _forward(request: Request, pod_ip: str, notebook_id: int, mode: str, path: str) -> Response:
    url = f"http://{pod_ip}:{runtime.APP_PORT}{runtime.base_url(notebook_id, mode)}/{path}"
    if request.url.query:
        url += f"?{request.url.query}"
    headers = {k: v for k, v in request.headers.items() if k.lower() not in _SKIP_REQUEST}
    client = httpx.AsyncClient(timeout=_TIMEOUT)
    body = request.stream() if request.method not in ("GET", "HEAD", "OPTIONS") else None
    try:
        upstream = await client.send(client.build_request(request.method, url, headers=headers, content=body), stream=True)
    except httpx.HTTPError:
        await client.aclose()
        raise

    async def close() -> None:
        await upstream.aclose()
        await client.aclose()

    return StreamingResponse(upstream.aiter_raw(), status_code=upstream.status_code,
                             headers=_response_headers(upstream), background=BackgroundTask(close))


async def _proxy(request: Request, session: Session, notebook_id: int, mode: str, path: str) -> Response:
    if request.method not in _SAFE and not same_origin(request):
        return JSONResponse({"detail": "This request must come from a page of Tabularia"}, status_code=403)
    root = path == "" and request.method == "GET"
    found = await run_in_threadpool(_lookup, request, session, notebook_id, mode, root)
    if found.answer is not None:
        return found.answer
    pod_ip = found.pod_ip
    if found.row_status != "ready" or (root and found.stale_app):
        if not root:
            return JSONResponse({"detail": "The notebook is starting: try again in a moment"}, status_code=503,
                                headers={"Retry-After": "2"})
        if found.row_status == "failed":
            return _failed(found.row_error)
        started = await _start(session, notebook_id, mode, found.user_id, request)
        if isinstance(started, Response):
            return started
        pod_ip = started
    try:
        return await _forward(request, pod_ip or "", notebook_id, mode, path)
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
            return _page("The notebook is not answering", message, 502)
        return JSONResponse({"detail": message}, status_code=502)


@router.get("/notebooks/{notebook_id}/app")
@router.get("/notebooks/{notebook_id}/edit")
async def without_slash(request: Request):
    """marimo answers under the address with the slash: its pages' links are relative."""
    return RedirectResponse(request.url.path.rsplit("/", 1)[-1] + "/", status_code=307)


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
    kept."""
    if visitor_id is not None:
        runtime.keep_visitor(session, visitor_id)
    found = _lookup(conn, session, notebook_id, mode, root=False)
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
        return _close_code(pod.close_code)

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
    if not same_origin(browser):
        await browser.close(code=1008)
        return
    found = await run_in_threadpool(_lookup, browser, session, notebook_id, mode, False)
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
        pod = await websockets.connect(url, subprotocols=wanted or None, max_size=None, open_timeout=10,
                                       ping_interval=20, ping_timeout=20, compression=None)
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
