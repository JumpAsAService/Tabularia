"""Proxy towards the internal engine, with per-object RBAC on the data plane.

The routes that reference storage objects (preview/transform/export) read the JSON
body — small: it is the flow's IR — authorize EVERY managed key found (see
services/objects.py) and only then forward. The upload records the engine's answer
in the `uploads` register (owner = whoever uploaded), which is what makes the other
checks possible.

Left with plain authentication: /tasks/operations (metadata only) and
GET /tasks/{task_id} (a task's state: the ids are non-enumerable UUIDs) — a
documented debt, acceptable.
"""
import json
import re
import logging
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool
from sqlmodel import Session

from app.core.config import describe_bytes, get_settings
from app.core.engine_client import get_engine_client
from app.db.session import get_session
from app.deps.auth import get_current_user, oauth2_scheme
from app.models import Upload, User
from app.services import audit
from app.services.engine_policy import disabled_engines
from app.services.objects import collect_storage_keys, ensure_can_read_keys, ensure_can_run_keys, ensure_reads_pinned
from app.services.permissions import can_upload
from app.core.routing import ReleasingRoute

logger = logging.getLogger(__name__)


# hop-by-hop headers not to forward
# `authorization` and `cookie` carry the user's session: the engine does not need them
# (it trusts the gateway through the internal token) and they must not reach it
_SKIP_REQUEST_HEADERS = {"host", "content-length", "authorization", "cookie", "x-tabularia-csrf", "connection"}
# response headers to pass to the browser (content-disposition = the download's file name)
_PASS_RESPONSE_HEADERS = {"content-type", "content-disposition", "content-length"}


def _request_headers(request: Request) -> dict[str, str]:
    return {k: v for k, v in request.headers.items() if k.lower() not in _SKIP_REQUEST_HEADERS}


def _release_db(session: Session | None, user: User | None = None) -> None:
    """Hands the database connection back to the pool BEFORE waiting for the engine.

    Authentication and the permission checks read from the database, and the session
    keeps that transaction open — that is a pool connection — until the request ends.
    But a preview waits for the engine up to two minutes, an upload or an export for
    their whole length: the pool has fifteen connections, and fifteen slow waits
    together left every other request without a database, sign-in included (measured:
    4.8 seconds of wait for any request, and the previews themselves queued behind the
    pool).

    `close()` and not `commit()`: a commit would expire the user's attributes, and
    whoever reads them afterwards — on the event loop — would go and take a connection
    again right where waiting must not happen (see below). With `close()` the user stays
    readable without a database; that is why it is loaded here, if an earlier commit
    (`last_seen` is enough) had expired it already. The session stays usable: whoever
    uses it after the engine's answer opens a new, short transaction on it. There is
    nothing pending to lose: the checks only read and the audit commits on its own.
    """
    if session is None:
        return
    if user is not None:
        user.id  # noqa: B018 — reloads NOW if expired, while the session is there
    session.close()


# ── A connection is taken and handed back IN THE SAME step ───────────────────
# These routes are `async` (they forward streaming), the queries are synchronous, and
# FastAPI runs every synchronous dependency in a thread of its own. Hence two stalls,
# both seen live, both as long as the pool's timeout (30 s):
#
#  - queries run on the event loop: when the pool is exhausted the loop stays stuck
#    waiting for a connection that can free up only when IT goes back to moving the
#    other requests forward;
#  - a connection held between one step and the next: authentication takes it in a
#    thread and the request keeps it while waiting for a free thread for the checks;
#    meanwhile the threads are all busy with requests waiting for a connection. With
#    60 previews together: gateway stuck for 28 seconds.
#
# The rule that removes both: every stretch that uses the session runs in a thread
# (`run_in_threadpool`) and RELEASES before leaving it. So whoever waits for a
# connection waits only for threads that are working and will hand it back by
# themselves.
def _user(
    request: Request, token: str = Depends(oauth2_scheme), session: Session = Depends(get_session),
) -> User:
    """`get_current_user`, but without leaving the connection in the request's hands: it
    authenticates and releases in the same thread. The user returned is detached from
    the session and loaded already — it reads without a database."""
    user = get_current_user(request, token, session)
    _release_db(session, user)
    return user


router = APIRouter(route_class=ReleasingRoute, tags=["engine"], dependencies=[Depends(_user)])


async def _release(session: Session | None, user: User | None = None) -> None:
    """A safety net before a wait: when the session still has an open transaction, it
    closes it (in a thread). Normally there is no need, because every stretch releases
    by itself."""
    if session is not None and session.in_transaction():
        await run_in_threadpool(_release_db, session, user)


async def _forward(
    request: Request, method: str, path: str, content: Any = None,
    session: Session | None = None, user: User | None = None, params: Any = None,
) -> StreamingResponse:
    """Forwards the request to the engine streaming in BOTH directions: the request's body
    (GB-scale upload) and the response's (csv/xlsx download). The gateway never buffers
    a whole file in RAM.
    `content` set = body read already (for the routes that inspect it).
    `session` (and `user`): the request's session, to release before the wait (see
    `_release_db`) — every route passes it. `params`: the query
    string to send instead of the browser's."""
    await _release(session, user)
    client = get_engine_client()
    if content is None and method in ("POST", "PUT", "PATCH"):
        content = request.stream()
    engine_req = client.build_request(
        method,
        path,
        params=request.query_params if params is None else params,
        headers=_request_headers(request),
        content=content,
    )
    engine_resp = await client.send(engine_req, stream=True)
    return StreamingResponse(
        engine_resp.aiter_raw(),
        status_code=engine_resp.status_code,
        headers={k: v for k, v in engine_resp.headers.items() if k.lower() in _PASS_RESPONSE_HEADERS},
        background=BackgroundTask(engine_resp.aclose),
    )


# the body of these routes is a flow's IR: small by nature. A cap so that a giant body
# (multi-GB) does not OOM the gateway with await body().
MAX_JSON_BODY_BYTES = 8 * 1024 * 1024  # 8 MB


async def _read_json(request: Request) -> tuple[bytes, Any]:
    # dropped at once when the declared Content-Length goes over the cap…
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            if int(declared) > MAX_JSON_BODY_BYTES:
                raise HTTPException(status_code=413, detail="The request body is too large (limit 8 MB)")
        except ValueError:
            pass
    # …and read in chunks with a real cap anyway (Content-Length may lie or be missing,
    # e.g. Transfer-Encoding: chunked)
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_JSON_BODY_BYTES:
            raise HTTPException(status_code=413, detail="The request body is too large (limit 8 MB)")
        chunks.append(chunk)
    raw = b"".join(chunks)
    try:
        # NaN and Infinity: Python's parser accepts them, the engine then cannot write them
        # back and answered 500. They are not JSON: they stop here
        return raw, (json.loads(raw, parse_constant=_not_json) if raw else {})
    except ValueError:  # JSONDecodeError included
        raise HTTPException(status_code=422, detail="The request body is not valid JSON")


def _not_json(constant: str):
    raise ValueError(f"{constant} is not a JSON value")


# ── Data routes forwarded to the engine ───────────────────────────────────────
@router.get("/tasks/operations")
async def operations(request: Request, session: Session = Depends(get_session)):
    return await _forward(request, "GET", "/tasks/operations", session=session)


@router.get("/engines")
async def engines(request: Request, session: Session = Depends(get_session)):
    """The engines' catalog, with the installation's policy applied already.

    It is NOT a blind forward, and it is the only point where the engine's catalog is
    rewritten: the engine knows what is technically available, the gateway knows what
    the administrator allows. A disabled engine comes back `available: false` with
    `disabled_by_admin: true`, because the selector must be able to say WHY — «not
    allowed here» is not «not configured», and showing them alike would send the user
    looking for an environment variable that has nothing to do with it.
    """
    await _release(session)  # the policy is read AFTER the engine's answer
    client = get_engine_client()
    resp = await client.get("/engines")
    if resp.status_code >= 400:  # an engine error: passed on as it is
        return Response(
            content=resp.content,
            status_code=resp.status_code,
            media_type=resp.headers.get("content-type"),
        )
    catalog = resp.json()

    def policy() -> set[str]:
        try:
            return set(disabled_engines(session))
        finally:
            _release_db(session)

    forbidden = await run_in_threadpool(policy)
    if forbidden and isinstance(catalog, list):
        for e in catalog:
            if isinstance(e, dict) and e.get("id") in forbidden:
                e["available"] = False
                e["disabled_by_admin"] = True
    return catalog


_SLOT_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def scope_preview_slot(raw: bytes, payload: Any, user_id: int) -> bytes:
    """The browser declares a preview's slot ("only the last one counts in this panel"),
    but a new preview on the same slot KNOCKS DOWN the previous one: without a fence,
    whoever guessed someone else's slot would cancel their previews. The gateway prefixes
    it with the authenticated user, so a slot can hit only the previews of whoever sends
    it. A malformed slot is removed (the preview runs, simply without a slot); without a
    slot the body passes UNTOUCHED, byte for byte, as before."""
    if not isinstance(payload, dict) or "slot" not in payload:
        return raw
    slot = payload.get("slot")
    scoped = dict(payload)
    if isinstance(slot, str) and _SLOT_RE.match(slot):
        scoped["slot"] = f"u{user_id}:{slot}"
    else:
        scoped.pop("slot", None)
    return json.dumps(scoped).encode()


@router.post("/tasks/preview")
async def preview(
    request: Request,
    user: User = Depends(_user),
    session: Session = Depends(get_session),
):
    raw, payload = await _read_json(request)

    def checks() -> int:
        try:
            ensure_reads_pinned(session, user, payload, get_settings().engine.bucket)
            ensure_can_read_keys(session, user, collect_storage_keys(payload))
            return user.id
        finally:
            _release_db(session, user)

    user_id = await run_in_threadpool(checks)
    raw = scope_preview_slot(raw, payload, user_id)
    return await _forward(request, "POST", "/tasks/preview", content=raw, session=session, user=user)


@router.post("/tasks/cache-status")
async def cache_status(
    request: Request,
    user: User = Depends(_user),
    session: Session = Depends(get_session),
):
    """Which nodes of a flow have their result in the step cache (the editor's
    halo). The chains are the ones previews send, so they are checked like a
    preview's: sources pinned to the engine's bucket and readable by the user."""
    raw, payload = await _read_json(request)

    def checks() -> None:
        try:
            ensure_reads_pinned(session, user, payload, get_settings().engine.bucket)
            ensure_can_read_keys(session, user, collect_storage_keys(payload))
        finally:
            _release_db(session, user)

    await run_in_threadpool(checks)
    return await _forward(request, "POST", "/tasks/cache-status", content=raw, session=session, user=user)


@router.post("/tasks/transform-data")
async def transform(
    request: Request,
    user: User = Depends(_user),
    session: Session = Depends(get_session),
):
    raw, payload = await _read_json(request)
    if not isinstance(payload, dict):
        raise HTTPException(status_code=422, detail="The request body must be a JSON object")
    # the destinations (database or S3) go ONLY through /flows/{id}/runs: the gateway
    # builds the connection payload (RBAC CONNECT, secret never from the client).
    # "db_destination" is the historical name: refused too. "mirror" (copy to an external
    # S3) and "email" (sending the output as an attachment) belong to the SAME class: they
    # carry a connection and a destination. Without this filter any user could send them
    # here with their own endpoint and secret IN CLEAR and get the parquet delivered by
    # the worker, skipping catalog, CONNECT, encryption and Run row — that is every check
    # the /flows/{id}/runs path applies. For "email" it would skip the allowed domains'
    # validation too, the only barrier to sending data towards an arbitrary address.
    #
    # The family grows: when a member is added it must be added HERE, in the same commit.
    # Forgetting it has happened already.
    if any(payload.get(k) for k in ("destination", "db_destination", "mirror", "email")):
        raise HTTPException(
            status_code=422,
            detail="Destinations are not allowed here: save the flow and use an Output node",
        )
    engine_bucket = get_settings().engine.bucket
    # the SERVER chooses the OUTPUT key (out/<uuid> write-once), never the client: no
    # overwrite of another user's blob and no reuse of the key that would poison the step
    # cache (indexed by path). The client polls the task_id, it does not need to know or
    # choose the key.
    payload["output_key"] = f"out/{uuid4().hex}.parquet"
    payload["bucket"] = payload.get("bucket") or engine_bucket

    def checks() -> None:
        # sources bound to the engine's bucket + managed prefixes (no arbitrary reads)
        ensure_reads_pinned(session, user, payload, engine_bucket)
        # authorizes the READ keys only (the output is generated by the server, it is not authorized for reading)
        read_payload = {k: v for k, v in payload.items() if k != "output_key"}
        keys = collect_storage_keys(read_payload)
        ensure_can_read_keys(session, user, keys)
        # …and then the permission to RUN: this is not a preview, it is a run without a saved
        # flow. Reading is not enough — the same threshold as /flows/{id}/runs holds
        ensure_can_run_keys(session, user, keys)
        audit.record_audit(
            session, actor=user, action=audit.TRANSFORM_RUN,
            target_type="transform", target_label=payload.get("input_key"),
            detail={"engine": payload.get("engine"), "source_keys": sorted(keys),
                    "operations": len(payload.get("operations") or [])},
            request=request,
        )
        _release_db(session, user)

    await run_in_threadpool(checks)
    return await _forward(
        request, "POST", "/tasks/transform-data", content=json.dumps(payload).encode(), session=session, user=user,
    )


def _authorize_export(request: Request, session: Session, user: User, payload: Any) -> None:
    """Who exports must be able to read every source; the download is audited
    (who takes what: format, file, source, engine)."""
    try:
        ensure_reads_pinned(session, user, payload, get_settings().engine.bucket)
        keys = collect_storage_keys(payload)
        ensure_can_read_keys(session, user, keys)
        audit.record_audit(
            session, actor=user, action=audit.EXPORT_DOWNLOAD,
            target_type="export", target_label=payload.get("filename") or payload.get("input_key"),
            detail={
                "format": payload.get("format"),
                "filename": payload.get("filename"),
                "engine": payload.get("engine"),
                "source_keys": sorted(keys),
            },
            request=request,
        )
    finally:
        _release_db(session, user)


@router.post("/tasks/export")
async def export(
    request: Request,
    user: User = Depends(_user),
    session: Session = Depends(get_session),
):
    """The export for API callers: the file comes back in the response."""
    raw, payload = await _read_json(request)
    await run_in_threadpool(_authorize_export, request, session, user, payload)
    return await _forward(request, "POST", "/tasks/export", content=raw, session=session, user=user)


# ── The browser's export: started, followed, its file taken once ────────────
# Nothing waits on an open request while a worker writes a large file, and the
# browser saves the file as it arrives instead of holding it all in memory. The
# engine lets only the user who started an export see it: the gateway names them.
_TASK_ID = re.compile(r"^[0-9a-f-]{36}$")


def export_owner(user: User) -> str:
    """Who an export belongs to, as the engine knows it (SQL scripts start theirs too)."""
    return f"user:{user.id}"


@router.post("/tasks/exports")
async def start_export(
    request: Request,
    user: User = Depends(_user),
    session: Session = Depends(get_session),
):
    raw, payload = await _read_json(request)
    if not isinstance(payload, dict):
        raise HTTPException(status_code=422, detail="The request body must be a JSON object")
    await run_in_threadpool(_authorize_export, request, session, user, payload)
    payload["owner"] = export_owner(user)  # never the browser's word for it
    return await _forward(request, "POST", "/tasks/exports", content=json.dumps(payload).encode(), session=session, user=user)


@router.get("/tasks/exports/{task_id}")
async def export_status(request: Request, task_id: str, user: User = Depends(_user)):
    if not _TASK_ID.match(task_id):
        raise HTTPException(status_code=404, detail="Export not found")
    return await _forward(request, "GET", f"/tasks/exports/{task_id}", params={"owner": export_owner(user)})


@router.get("/tasks/exports/{task_id}/file")
async def export_file(request: Request, task_id: str, user: User = Depends(_user)):
    if not _TASK_ID.match(task_id):
        raise HTTPException(status_code=404, detail="Export not found")
    return await _forward(request, "GET", f"/tasks/exports/{task_id}/file", params={"owner": export_owner(user)})


@router.get("/tasks/{task_id}")
async def task_status(request: Request, task_id: str, session: Session = Depends(get_session)):
    return await _forward(request, "GET", f"/tasks/{task_id}", session=session)


class _UploadTooLarge(Exception):
    pass


@router.post("/files")
async def upload(
    request: Request,
    user: User = Depends(_user),
    session: Session = Depends(get_session),
):
    """Upload (streaming towards the engine) + recording the ownership.

    The engine's answer is a small JSON (IngestResult): it is buffered to record who owns
    the dataset just created — the basis of the read check on uploads not inside a saved
    flow yet.

    EDIT somewhere is needed: the upload has no folder to ask the permission on, and
    without this check a read-only account could write into the installation's bucket
    (see permissions.can_upload).
    """

    def checks() -> int:
        if not can_upload(session, user):
            raise HTTPException(
                status_code=403,
                detail="Uploading a file needs edit permission on at least one folder",
            )
        owner = user.id
        _release_db(session, user)  # the upload can last minutes: no connection in hand
        return owner

    limit = get_settings().upload.max_bytes
    too_much = HTTPException(
        status_code=413,
        detail=f"The file is too large: the limit is {describe_bytes(limit)} (UPLOAD__MAX_BYTES)",
    )
    # a declared size over the ceiling: refused before anything is read or sent
    declared = request.headers.get("content-length") or ""
    if limit and declared.isdigit() and int(declared) > limit:
        raise too_much
    owner_id = await run_in_threadpool(checks)

    overtaken = False

    async def counted():
        """The body as it arrives, cut at the ceiling: a chunked upload declares no
        size, and a declared one could be a lie."""
        nonlocal overtaken
        received = 0
        async for piece in request.stream():
            received += len(piece)
            if limit and received > limit:
                overtaken = True
                raise _UploadTooLarge()
            yield piece

    client = get_engine_client()
    engine_req = client.build_request(
        "POST",
        "/files",
        params=request.query_params,
        headers=_request_headers(request),
        content=counted(),
    )
    try:
        engine_resp = await client.send(engine_req)
    except Exception:
        # the engine sees a body that stops short and throws its spooled copy away
        if overtaken:
            raise too_much from None
        raise

    if engine_resp.status_code < 400:
        def record() -> None:
            try:
                data = engine_resp.json()
                session.add(
                    Upload(
                        dataset_id=str(data.get("dataset_id") or ""),
                        bucket=get_settings().engine.bucket,
                        parquet_key=str(data.get("parquet_key") or ""),
                        raw_key=str(data.get("raw_key") or ""),
                        owner_id=owner_id,
                    )
                )
                session.commit()
            except Exception as e:  # recording must not break the upload
                logger.warning("upload not recorded in the uploads register: %s", e)
                session.rollback()

        await run_in_threadpool(record)

    return Response(
        content=engine_resp.content,
        status_code=engine_resp.status_code,
        media_type=engine_resp.headers.get("content-type"),
    )
