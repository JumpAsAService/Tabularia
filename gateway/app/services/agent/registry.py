"""The registry of the agent's tools: one set for the chat and for MCP (docs/specs/mcp-agent.md).

A tool is a name, a description for the model, a pydantic model of its arguments, a level
(`read` or `write`) and an async handler `(ctx, args) -> dict`. The same entry serves:

- the MCP endpoint (routes/mcp.py): `tools/list` shows the tools the token's level allows,
  `tools/call` runs one through `call`, and a refusal becomes a result with `isError`;
- the chat (the Assistant page): each tool becomes a pydantic-ai tool, and a refusal a
  `ModelRetry` the model reads and corrects itself with.

A refusal is a `ToolError` with a message in words, for the model: what is wrong and what
to do instead. The handlers reuse the product's own functions (the routes called as
functions, the data and flow assistants' tools): those raise `ModelRetry` or
`HTTPException`, and `call` turns both into a `ToolError`, so a rule holds the same here
as in the interface.

Every tool checks the permissions of the person it runs for (`ctx.user`), at every call:
the model is never trusted with an id.
"""
from __future__ import annotations

import inspect
import logging
import uuid
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Literal, Optional

import httpx
from fastapi import HTTPException, Request
from pydantic import BaseModel, ValidationError
from pydantic_ai import ModelRetry
from sqlmodel import Session

from app.models import User

logger = logging.getLogger(__name__)

Level = Literal["read", "write"]
Origin = Literal["chat", "mcp"]


class ToolError(Exception):
    """A tool's refusal, in words the model can act on."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class UnknownTool(Exception):
    """No such tool for this caller (MCP: a JSON-RPC error, not a tool's result)."""


@dataclass
class ToolContext:
    """Who a tool runs for and from where.

    `session_factory` gives a DB session per use (a context manager): a tool waits for the
    engine, and must not hold a pool connection meanwhile. `request` is for the audit (the
    address, the token, `via`). `model_id` is the chat's model, or "mcp" (the model is the
    client's)."""

    user: User
    session_factory: Callable[[], AbstractContextManager[Session]]
    request: Optional[Request]
    origin: Origin
    token_name: Optional[str] = None
    chat_id: Optional[int] = None
    locale: str = "en"
    model_id: str = "mcp"
    # the engine of the chat's queries on datasources (None = the installation's default)
    engine: Optional[str] = None
    # previews of one call supersede each other only within it
    call_key: str = field(default_factory=lambda: uuid.uuid4().hex[:10])

    def chat_deps(self):
        """The data assistant's dependencies, for its tools reused here."""
        from app.services.ai_agent import ChatDeps

        return ChatDeps(user=self.user, session_factory=self.session_factory, engine=self.engine,
                        model_id=self.model_id, locale=self.locale, request=self.request,
                        chat_id=self.chat_id if self.origin == "chat" else None)


@dataclass(frozen=True)
class Tool:
    name: str
    title: str
    description: str
    args: type[BaseModel]
    level: Level
    handler: Callable[[ToolContext, Any], Awaitable[dict]]
    # "mcp": only over MCP (confirm_plan); "chat": only in the chat (ask_user)
    only: Optional[Origin] = None
    # it changes saved objects (confirm_plan): MCP clients ask their human first
    destructive: bool = False
    # it ends the chat's turn (ask_user): the chat makes it the agent's output, not a call
    # whose result goes back to the model (services/agent/chat.py)
    ends_turn: bool = False

    def input_schema(self) -> dict:
        schema = self.args.model_json_schema()
        schema.pop("title", None)
        for prop in (schema.get("properties") or {}).values():
            prop.pop("title", None)
        schema.setdefault("properties", {})
        return schema

    def annotations(self) -> dict:
        """MCP's hints (they are hints: the permissions are checked anyway)."""
        out: dict[str, Any] = {"title": self.title, "readOnlyHint": self.level == "read", "openWorldHint": False}
        if self.level == "write":
            out["destructiveHint"] = self.destructive
            out["idempotentHint"] = False
        return out


REGISTRY: dict[str, Tool] = {}


def tool(name: str, *, title: str, args: type[BaseModel], level: Level,
         only: Optional[Origin] = None, destructive: bool = False, ends_turn: bool = False):
    """Registers the decorated handler; its docstring is the description the model reads."""

    def register(fn: Callable[[ToolContext, Any], Awaitable[dict]]):
        if name in REGISTRY:
            raise RuntimeError(f"agent tool {name} registered twice")
        REGISTRY[name] = Tool(name=name, title=title, description=inspect.getdoc(fn) or "", args=args, level=level,
                              handler=fn, only=only, destructive=destructive, ends_turn=ends_turn)
        return fn

    return register


def _rank(t: Tool) -> int:
    """The order of a listing: reading, then building drafts, then the plan."""
    if t.level == "read":
        return 0
    return 3 if t.name in ("cancel_plan", "confirm_plan") else 2 if t.name.startswith("plan_") else 1


def available(origin: Origin, level: Level = "write") -> list[Tool]:
    """The tools for a caller: its origin's, and only the reading ones for a `read` level."""
    return sorted((t for t in REGISTRY.values()
                   if (t.only is None or t.only == origin) and (level == "write" or t.level == "read")), key=_rank)


def _words(e: ValidationError) -> str:
    parts = []
    for err in e.errors()[:6]:
        where = ".".join(str(x) for x in err.get("loc") or ()) or "arguments"
        parts.append(f"{where}: {err.get('msg')}")
    return "Invalid arguments — " + "; ".join(parts)


def _detail(e: HTTPException) -> str:
    d = e.detail
    if isinstance(d, str):
        return d
    if isinstance(d, list):
        return "; ".join(str(x.get("msg", x)) if isinstance(x, dict) else str(x) for x in d)
    return str(d)


async def call(ctx: ToolContext, name: str, arguments: Optional[dict], level: Level = "write") -> dict:
    """Runs a tool for `ctx`. Raises UnknownTool (no such tool for this origin), or
    ToolError (refused, in words)."""
    t = REGISTRY.get(name)
    if t is None or (t.only is not None and t.only != ctx.origin):
        raise UnknownTool(name)
    if t.level == "write" and level != "write":
        raise ToolError(f"{name} changes things: it needs a write token. This token reads only.")
    try:
        args = t.args.model_validate(arguments or {})
    except ValidationError as e:
        raise ToolError(_words(e)) from e
    try:
        return await t.handler(ctx, args)
    except ToolError:
        raise
    except ModelRetry as e:
        raise ToolError(str(e.message if hasattr(e, "message") else e)) from e
    except HTTPException as e:
        raise ToolError(_detail(e)) from e
    except httpx.TransportError as e:
        raise ToolError(f"A service Tabularia calls is not answering ({type(e).__name__}): try again in a moment.") from e
