"""What the agent's tools share: sessions, folder paths, links, the clock."""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Callable, Optional, TypeVar

from fastapi import Request
from sqlmodel import Session
from starlette.concurrency import run_in_threadpool

from app.core.config import get_settings
from app.models import User
from app.services import permissions as perm_service
from app.services.agent.registry import ToolContext

T = TypeVar("T")

MAX_CELL_CHARS = 300


def new_session() -> Session:
    """A DB session for one use of a tool: the MCP request's own is handed back before the
    tools wait for the engine. The tests replace it."""
    from app.db.session import engine

    return Session(engine)


def now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)  # naive UTC, as the columns


def fingerprint(text: Optional[str]) -> str:
    return hashlib.sha256((text or "").encode()).hexdigest()


async def in_thread(ctx: ToolContext, work: Callable[[Session, User], T]) -> T:
    """`work(session, user)` in a worker thread with a session of its own: the routes called
    as functions are synchronous and commit. The user is read again in that session, so
    the permissions are those of now."""

    def run() -> T:
        with ctx.session_factory() as session:
            user = session.get(User, ctx.user.id) or ctx.user
            return work(session, user)

    return await run_in_threadpool(run)


def folder_paths(session: Session) -> dict[int, str]:
    """Every folder's path, «Sales/Reports»."""
    projects = perm_service._all_projects(session)
    parents = {i: p.parent_id for i, p in projects.items()}
    return {pid: "/".join(projects[i].name for i in reversed(perm_service.ancestor_ids(parents, pid)) if i in projects)
            for pid in projects}


# ── links to the interface ────────────────────────────────────────────────────
def flow_link(flow_id: int) -> str:
    return f"/editor?flow={flow_id}"


def plan_link(plan_id: int) -> str:
    return f"/agent-plan?id={plan_id}"


def datasource_link(ds_id: int, name: str) -> str:
    from urllib.parse import quote

    return f"/datasources?q={quote(name)}&ds={ds_id}"


def absolute(request: Optional[Request], path: str) -> str:
    """A link of the interface for whoever reads it outside Tabularia (an MCP client).

    Behind a proxy that publishes the gateway under a prefix (APP__ROOT_PATH) the interface
    is on the same host: the request's own origin. Otherwise the interface is elsewhere
    (another host, or :3000 in development): the first of APP__CORS_ORIGINS, its origin."""
    app = get_settings().app
    if request is not None and (app.root_path or not app.cors_origins):
        return f"{request.url.scheme}://{request.url.netloc}{path}"
    if app.cors_origins:
        return app.cors_origins[0].rstrip("/") + path
    return path


def trim_rows(rows: list[dict], n: int) -> list[dict]:
    out = []
    for r in rows[:n]:
        out.append({k: (v[:MAX_CELL_CHARS] + "…" if isinstance(v, str) and len(v) > MAX_CELL_CHARS else v)
                    for k, v in r.items()})
    return out
