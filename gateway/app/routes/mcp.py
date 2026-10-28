"""The MCP server: `POST /mcp` (docs/specs/mcp-agent.md).

Agents outside Tabularia — Claude Desktop, an IDE, another company agent — use the same
tools as the chat (services/agent), signed in with an API token in the `Authorization`
header (a personal one or a service account's). A `read` token sees and calls only the
reading tools, a `write` one all of them; either way the permissions are its owner's,
checked at every call. The model is the client's: MCP needs no AI configured here.

Streamable HTTP WITHOUT SESSIONS, every answer a JSON body: the gateway runs several
replicas without affinity, and nothing here remembers a client between two requests
(drafts and plans live in Postgres). So there is no `Mcp-Session-Id`, no server-sent
stream (GET answers 405) and no elicitation: a change is a plan the client's human
confirms (`confirm_plan`, annotated as destructive) or the person confirms in Tabularia.

Written by hand, not with the `mcp` SDK: the SDK's stateless transport is an ASGI app
with a session manager to run in the application's lifespan, outside FastAPI's routes —
its dependencies (the token, the database session handed back before the engine is
awaited), the root path behind the proxy and the body limit would have to be rebuilt
around it, with ten more packages in the image. What this endpoint speaks is small:
JSON-RPC 2.0 with `initialize`, `ping`, `tools/list` and `tools/call`.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from sqlmodel import Session

from app.core.config import get_settings
from app.core.routing import ReleasingRoute
from app.db.session import get_session
from app.deps.auth import _token_user, same_origin
from app.models import User
from app.services import agent, api_tokens
from app.services.agent import common
from app.services.agent.registry import REGISTRY, ToolContext, ToolError, UnknownTool

logger = logging.getLogger(__name__)

router = APIRouter(route_class=ReleasingRoute, tags=["mcp"])

# newest first: the one offered when the client asks for another
PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26")
# a request without the version header, after the initialization (the specification)
ASSUMED_VERSION = "2025-03-26"

PARSE_ERROR, INVALID_REQUEST, METHOD_NOT_FOUND, INVALID_PARAMS = -32700, -32600, -32601, -32602

INSTRUCTIONS = """Tabularia prepares data: datasources (tables in its catalog), flows that transform them and write Outputs (a datasource, a database table, a file on S3 or SFTP, an email with the result attached), their schedules and runs. You act with the permissions of this token's owner.

To read: search, list_folders, list_datasources, describe_datasource, query_datasource (one SELECT on a datasource, the table is `self`), get_flow, list_runs, get_run, list_connections, list_environments, get_plan.

To change something (write tokens only), nothing changes directly:
1. build the flow in a draft — flow_draft_new, or flow_draft_open for a saved flow — with flow_add_source, flow_add_step, flow_add_output, checking each step with flow_preview;
2. put the changes in a plan: plan_save_flow, plan_run_flow, plan_schedule. Each returns the whole plan in words, its plan_id and a review link;
3. show the plan to the user and call confirm_plan only once they agree (they may also confirm it at the review link). A plan expires 30 minutes after its last change.

Ask the user when a choice is theirs (which datasource, which recipients, what time). Connections hold credentials: you never create or change one; when the needed one is missing, tell the user and point them to the connections page."""


def _error(msg_id: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def _result(msg_id: Any, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _switched_on() -> None:
    if not get_settings().mcp.enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="MCP is turned off on this installation")


def mcp_user(request: Request, session: Session = Depends(get_session)) -> User:
    """The token's owner. Only an API token in the header: the browser's session cookie
    does not open the MCP endpoint."""
    _switched_on()
    if request.headers.get("origin") and not same_origin(request):
        # a page of another site (DNS rebinding): the specification asks to refuse it
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Origin not allowed")
    scheme, _, credential = (request.headers.get("authorization") or "").partition(" ")
    credential = credential.strip()
    if scheme.lower() != "bearer" or not api_tokens.is_a_token(credential):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="An API token is needed: Authorization: Bearer tab_…",
                            headers={"WWW-Authenticate": "Bearer"})
    user = _token_user(request, credential, session, level_checked_by_caller=True)
    user.id  # noqa: B018 — loaded now: the tools read it without this session
    session.close()  # the tools take their own connections, one use at a time
    return user


def _tool_list(level: str) -> list[dict]:
    return [{"name": t.name, "title": t.title, "description": t.description, "inputSchema": t.input_schema(),
             "annotations": t.annotations()} for t in agent.available("mcp", level)]  # type: ignore[arg-type]


async def _handle(ctx: ToolContext, level: str, msg: Any) -> Optional[dict]:
    """One JSON-RPC message → its answer, or None for a notification (or a client's answer)."""
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
        return _error(msg.get("id") if isinstance(msg, dict) else None, INVALID_REQUEST, "Invalid Request")
    method, msg_id = msg.get("method"), msg.get("id")
    if not isinstance(method, str) or "id" not in msg:
        return None  # notifications/initialized, notifications/cancelled, …: nothing to answer
    params = msg.get("params") or {}
    if not isinstance(params, dict):
        return _error(msg_id, INVALID_PARAMS, "params must be an object")
    if method == "initialize":
        asked = params.get("protocolVersion")
        instructions = INSTRUCTIONS
        if get_settings().mcp.confirm_in_tabularia_only:
            instructions += "\n\nOn this installation the user confirms plans themselves, in Tabularia: give them the review link."
        if level != "write":
            instructions += "\n\nThis token reads only: the tools that change things are not available."
        return _result(msg_id, {
            "protocolVersion": asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "tabularia", "title": "Tabularia", "version": get_settings().app.version},
            "instructions": instructions,
        })
    if method == "ping":
        return _result(msg_id, {})
    if method == "tools/list":
        return _result(msg_id, {"tools": _tool_list(level)})
    if method == "tools/call":
        name, arguments = params.get("name"), params.get("arguments") or {}
        if not isinstance(name, str) or not isinstance(arguments, dict):
            return _error(msg_id, INVALID_PARAMS, "tools/call needs name and an object of arguments")
        known = REGISTRY.get(name)
        if known is not None and known.level == "write" and level != "write" and known.only in (None, "mcp"):
            return _error(msg_id, INVALID_PARAMS, f"Tool {name} changes things: it needs a write token, this one reads only")
        try:
            out = await agent.call(ctx, name, arguments, level)  # type: ignore[arg-type]
        except UnknownTool:
            return _error(msg_id, INVALID_PARAMS, f"Unknown tool: {name}")
        except ToolError as e:
            return _result(msg_id, {"content": [{"type": "text", "text": e.message}], "isError": True})
        except Exception:  # noqa: BLE001 — the client gets a result it can show; the log the details
            logger.exception("mcp: tool %s failed", name)
            return _result(msg_id, {"content": [{"type": "text", "text": "Internal error: see the gateway's log."}],
                                    "isError": True})
        return _result(msg_id, {"content": [{"type": "text", "text": json.dumps(out, ensure_ascii=False, default=str)}],
                                "isError": False})
    return _error(msg_id, METHOD_NOT_FOUND, f"Method not found: {method}")


@router.post("/mcp", summary="MCP server (Streamable HTTP, stateless, JSON answers)")
async def mcp_endpoint(request: Request, user: User = Depends(mcp_user)) -> Response:
    version = request.headers.get("mcp-protocol-version")
    if version and version not in PROTOCOL_VERSIONS:
        return JSONResponse(_error(None, INVALID_REQUEST, f"Unsupported MCP-Protocol-Version: {version}"), status_code=400)
    try:
        body = json.loads(await request.body() or b"null")
    except ValueError:
        return JSONResponse(_error(None, PARSE_ERROR, "Parse error"), status_code=400)
    request.state.via = "mcp"  # every audit row of this request says so (services/audit.py)
    ctx = ToolContext(user=user, session_factory=lambda: common.new_session(), request=request, origin="mcp",
                      token_name=getattr(request.state, "api_token", None), model_id="mcp",
                      locale="en")
    level = getattr(request.state, "api_token_scope", "read")
    if isinstance(body, list):  # a batch (protocol 2025-03-26)
        if not body:
            return JSONResponse(_error(None, INVALID_REQUEST, "Empty batch"), status_code=400)
        answers = [a for a in [await _handle(ctx, level, m) for m in body] if a is not None]
        return JSONResponse(answers) if answers else Response(status_code=status.HTTP_202_ACCEPTED)
    answer = await _handle(ctx, level, body)
    if answer is None:
        return Response(status_code=status.HTTP_202_ACCEPTED)
    return JSONResponse(answer)


@router.get("/mcp", include_in_schema=False)
@router.delete("/mcp", include_in_schema=False)
def mcp_no_stream() -> Response:
    """No server-sent stream to open and no session to close: every answer comes with its request."""
    _switched_on()
    return Response(status_code=status.HTTP_405_METHOD_NOT_ALLOWED, headers={"Allow": "POST"})
