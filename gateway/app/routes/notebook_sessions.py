"""The live pods of a notebook (its sessions) and the editor's «Save version».
docs/specs/notebooks.md («Runtime»); the rules in services/notebook_runtime.py.

- starting the app: whoever sees the notebook (VIEW on its folder and the use of its
  environment); the pod reads with the rights of the author of the version it runs;
- starting the editor: EDIT on the folder; the pod reads with the editor's rights, and
  one person at a time edits a notebook (another gets 409, saying who);
- stopping: the app, whoever may edit; the editor, the editor or whoever may edit (the
  editor's file becomes the draft first);
- saving a version from the editor: only the editor, who becomes its author.

The app's session says how many people have it open (`visitors`, on every replica) and
how many it takes at once (`max_visitors`); the editor's says neither.

The frame of the app or of the editor opens on the notebooks' own address with a ticket
(`POST /notebooks/{id}/ticket`, services/notebook_frames.py): asked by Tabularia's page,
signed in in the browser, with the rights that start the session.
"""
from __future__ import annotations

from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlmodel import Session, select

from app.core.routing import ReleasingRoute
from app.db.session import get_session
from app.deps.auth import get_current_user, same_origin
from app.deps.permissions import ensure_can
from app.models import Notebook, NotebookSession, User
from app.models.permission import Capability
from app.routes.notebooks import NotebookDetail, _detail
from app.schemas.models import UtcDateTime
from app.services import notebook_frames as frames
from app.services import notebook_runtime as runtime
from app.services import notebooks
from app.services import permissions as perm_service

router = APIRouter(route_class=ReleasingRoute, tags=["notebooks"])


class Person(BaseModel):
    id: int
    email: str
    full_name: str = ""


class SessionOut(BaseModel):
    mode: str
    status: str  # starting | ready | failed
    error: Optional[str] = None
    user: Optional[Person] = None  # whose rights the pod reads with
    started_at: UtcDateTime
    last_seen_at: UtcDateTime
    # the app's: the people with it open now, and the most it takes at once
    visitors: Optional[int] = None
    max_visitors: Optional[int] = None


class SessionStart(BaseModel):
    mode: Literal["app", "edit"]


class SaveIn(BaseModel):
    note: Optional[str] = Field(default=None, max_length=500)


class TicketIn(BaseModel):
    mode: Literal["app", "edit"]


class TicketOut(BaseModel):
    # where the frame opens: the notebooks' own address with the ticket; None when they
    # are served on the interface's (NOTEBOOKS__ALLOW_SAME_ORIGIN), at the usual path
    url: Optional[str] = None
    # NOTEBOOKS__ALLOW_SAME_ORIGIN: the page warns that a notebook's code acts as you
    same_origin: bool = False
    expires_in: int = 0  # seconds left to use the ticket


def person(user: Optional[User]) -> Optional[Person]:
    return Person(id=user.id, email=user.email, full_name=user.full_name or "") if user else None


def _out(session: Session, row: NotebookSession) -> SessionOut:
    out = SessionOut(mode=row.mode, status=row.status, error=row.error, user=person(session.get(User, row.user_id)),
                     started_at=row.started_at, last_seen_at=row.last_seen_at)
    notebook = session.get(Notebook, row.notebook_id) if row.mode == "app" else None
    if notebook is not None:
        out.visitors = runtime.visitors(session, notebook.id)
        out.max_visitors = runtime.visitors_cap(notebook)
    return out


def being_edited(e: runtime.BeingEdited) -> JSONResponse:
    """409 with the editor beside the message, as the interface reads it."""
    editor = person(e.editor)
    return JSONResponse(status_code=409, content={"detail": str(e), "editor": editor.model_dump() if editor else None})


@router.post("/notebooks/{notebook_id}/sessions", response_model=SessionOut, status_code=202)
async def start_session(notebook_id: int, body: SessionStart, request: Request,
                        user: User = Depends(get_current_user), session: Session = Depends(get_session)):
    """Starts the notebook's app or editor, or returns the one running (idempotent)."""
    notebook = notebooks.get_visible(session, user, notebook_id)
    if body.mode == "edit":
        ensure_can(session, user, notebook.project_id, Capability.EDIT)
    try:
        row = await runtime.ensure_started(session, notebook, body.mode, user, request)
    except runtime.BeingEdited as e:
        return being_edited(e)
    return _out(session, row)


@router.get("/notebooks/{notebook_id}/sessions", response_model=list[SessionOut])
async def list_sessions(notebook_id: int, user: User = Depends(get_current_user),
                        session: Session = Depends(get_session)):
    """The notebook's live pods (and a failed one, saying why); the editor's only to
    whoever may edit."""
    notebook = notebooks.get_visible(session, user, notebook_id)
    editable = perm_service.has_capability(session, user, notebook.project_id, Capability.EDIT)
    rows = session.exec(select(NotebookSession).where(NotebookSession.notebook_id == notebook.id)
                        .order_by(NotebookSession.mode)).all()
    out = []
    for row in rows:
        if row.mode == "edit" and not editable:
            continue
        out.append(_out(session, await runtime.refresh(session, row)))
    return out


@router.delete("/notebooks/{notebook_id}/sessions/{mode}", status_code=204)
async def stop_session(notebook_id: int, mode: Literal["app", "edit"], request: Request,
                       user: User = Depends(get_current_user), session: Session = Depends(get_session)):
    notebook = notebooks.get_visible(session, user, notebook_id)
    row = runtime.row_of(session, notebook.id, mode)
    if not (mode == "edit" and row is not None and row.user_id == user.id):
        ensure_can(session, user, notebook.project_id, Capability.EDIT)
    if row is not None:
        await runtime.stop(session, row, actor=user, reason="stopped", request=request)
    return Response(status_code=204)


@router.post("/notebooks/{notebook_id}/edit/save", response_model=NotebookDetail)
async def save_from_editor(notebook_id: int, body: SaveIn, request: Request,
                           user: User = Depends(get_current_user), session: Session = Depends(get_session)):
    """A new version from what marimo saved in the editor's pod, by whoever edits; the
    notebook as GET /notebooks/{id} returns it."""
    notebook = notebooks.get_visible(session, user, notebook_id)
    ensure_can(session, user, notebook.project_id, Capability.EDIT)
    await runtime.save_version(session, notebook, user, body.note, request)
    session.refresh(notebook)
    return _detail(session, user, notebook)


@router.post("/notebooks/{notebook_id}/ticket", response_model=TicketOut)
def frame_ticket(notebook_id: int, body: TicketIn, request: Request, user: User = Depends(get_current_user),
                 session: Session = Depends(get_session)):
    """A ticket that opens the app (VIEW) or the editor (EDIT) in the page's frame, on the
    notebooks' own address: used once, within a minute, for this person, notebook and
    mode. Only from a page of Tabularia signed in in the browser (its session, its
    `Origin`): the frame holds while that session does, and only that page may frame it.
    Without an address of their own the notebooks do not open (409), unless
    NOTEBOOKS__ALLOW_SAME_ORIGIN."""
    notebook = notebooks.get_visible(session, user, notebook_id)
    if body.mode == "edit":
        ensure_can(session, user, notebook.project_id, Capability.EDIT)
    if not frames.configured():
        if not frames.same_origin_allowed():
            raise HTTPException(status_code=409, detail=frames.NEEDS_ORIGIN)
        return TicketOut(same_origin=True)
    sid = getattr(request.state, "sid", None)
    page = frames.page_origin(request)
    if not sid or page is None or not same_origin(request):
        raise HTTPException(status_code=403, detail="A notebook opens from its page in Tabularia, signed in in the "
                                                    "browser")
    ticket = frames.issue(session, notebook_id=notebook.id, mode=body.mode, user_id=user.id, sid=sid,
                          frame_origin=page)
    return TicketOut(url=frames.frame_url(notebook.id, body.mode, ticket), expires_in=frames.TICKET_SECONDS)
