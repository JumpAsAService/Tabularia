"""Notebooks: marimo notebooks kept as objects of a folder, with their versions.
docs/specs/notebooks.md; the rules in services/notebooks.py.

Permissions:
- seeing one (lists, search, opening it, its versions): VIEW on its folder **and** the
  use of its environment; whoever lacks either gets 404, as if it did not exist;
- creating, changing, moving, renaming, duplicating, deleting, restoring a version,
  discarding the draft: EDIT on the folder (both folders for a move), and the use of
  the environment;
- the app reads with the rights of whoever saved the version it runs: saving or
  restoring makes the person who does it that author;
- while marimo's editor is open on a notebook its source changes only there
  (routes/notebook_sessions.py, «Save version»): changing it here or restoring a
  version is refused with 409 — it would overwrite the editor's work, or be overwritten
  by its next save;
- administrators see and change every notebook;
- how many people may have the app open at once (`max_visitors`) is set with EDIT,
  from 1 to NOTEBOOKS__MAX_VISITORS_CEILING; null = the installation's default
  (NOTEBOOKS__MAX_VISITORS_PER_APP). services/notebook_runtime.py, «Visitors».

On an installation without scripting (the Docker Compose stack) the lists are empty
and nothing can be saved, as for saved scripts.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlmodel import Session, select

from app.core.config import get_settings
from app.core.routing import ReleasingRoute
from app.db.session import get_session
from app.deps.auth import get_current_user
from app.deps.permissions import ensure_can
from app.models import Notebook, NotebookSession, NotebookVersion, Project, ScriptEnvironment, User
from app.models.permission import Capability
from app.schemas.models import UtcDateTime
from app.services import audit, notebooks, script_builds, script_nodes
from app.services import permissions as perm_service
from app.services.copy_names import commit_copy, free_copy_name

router = APIRouter(route_class=ReleasingRoute, tags=["notebooks"])


class Author(BaseModel):
    """Whoever saved a version: the app reads with their rights."""
    id: int
    email: str
    full_name: str = ""


class NotebookOut(BaseModel):
    id: int
    name: str
    description: str
    project_id: int
    environment_id: int
    environment_name: Optional[str]
    language: Optional[str]
    current_version: int
    created_at: UtcDateTime
    updated_at: UtcDateTime
    editable: bool  # EDIT on its folder: the page offers saving, moving, deleting


class NotebookDetail(NotebookOut):
    content: dict[str, Any]
    # who saved the current version (None: an account deleted since): the app runs with their rights
    author: Optional[Author] = None
    # the editor's unsaved work, shown only to whoever may edit
    draft: Optional[str] = None
    draft_by: Optional[str] = None
    draft_at: Optional[UtcDateTime] = None
    # people with the app open at once: the notebook's own number (None: the default),
    # the installation's default, and the most a notebook may set
    max_visitors: Optional[int] = None
    visitors_default: int
    visitors_ceiling: int


class NotebookCreate(BaseModel):
    name: str = Field(max_length=200)
    description: str = Field(default="", max_length=2000)
    environment_id: int
    content: Optional[dict[str, Any]] = None  # None: a starting notebook
    max_visitors: Optional[int] = Field(default=None, ge=1)  # None: the installation's default


class NotebookUpdate(BaseModel):
    name: Optional[str] = Field(default=None, max_length=200)
    description: Optional[str] = Field(default=None, max_length=2000)
    project_id: Optional[int] = None  # a move
    environment_id: Optional[int] = None
    content: Optional[dict[str, Any]] = None
    # sent as null: back to the installation's default; left out: unchanged
    max_visitors: Optional[int] = Field(default=None, ge=1)


class VersionOut(BaseModel):
    version: int
    note: str
    created_at: UtcDateTime
    created_by: Optional[str]
    current: bool


# ── helpers ───────────────────────────────────────────────────────────────────
def _on() -> bool:
    return script_builds.disabled_reason() is None


def _need_on() -> None:
    if not _on():
        raise HTTPException(status_code=409, detail="Notebooks need the scripting environments (Kubernetes)")


def _environment(session: Session, user: User, environment_id: int) -> ScriptEnvironment:
    env = session.get(ScriptEnvironment, environment_id)
    if env is None or not script_nodes.can_use(session, user, env):
        raise HTTPException(status_code=422, detail="Choose a scripting environment you may use")
    notebooks.check_environment(env)
    return env


def _name(session: Session, project_id: int, name: str, exclude_id: Optional[int] = None) -> str:
    name = (name or "").strip()
    if not name:
        raise HTTPException(status_code=422, detail="The notebook needs a name")
    clash = session.exec(select(Notebook.id).where(Notebook.project_id == project_id, Notebook.name == name)).first()
    if clash is not None and clash != exclude_id:
        raise HTTPException(status_code=409, detail=f"There is already a notebook '{name}' in this folder")
    return name


def _max_visitors(value: Optional[int]) -> Optional[int]:
    """A notebook's own number of visitors at once, within the installation's ceiling."""
    ceiling = get_settings().notebooks.max_visitors_ceiling
    if value is not None and value > ceiling:
        raise HTTPException(status_code=422, detail=f"At most {ceiling} people at once: the most this installation "
                                                    "allows (NOTEBOOKS__MAX_VISITORS_CEILING)")
    return value


def _not_being_edited(session: Session, notebook: Notebook) -> None:
    """409 while marimo's editor is open on the notebook (starting or ready), whoever
    holds it: its source changes there until it is saved or closed."""
    from app.services.notebook_runtime import LIVE

    row = session.exec(select(NotebookSession).where(
        NotebookSession.notebook_id == notebook.id, NotebookSession.mode == "edit",
        NotebookSession.status.in_(LIVE))).first()  # type: ignore[attr-defined]
    if row is None:
        return
    editor = session.get(User, row.user_id)
    raise HTTPException(status_code=409, detail=f"«{notebook.name}» is open in marimo's editor by "
                                                f"{editor.email if editor else 'someone'}: save or close it there first")


def _out(session: Session, user: User, n: Notebook, envs: Optional[dict[int, ScriptEnvironment]] = None) -> NotebookOut:
    env = (envs or {}).get(n.environment_id) or session.get(ScriptEnvironment, n.environment_id)
    return NotebookOut(id=n.id, name=n.name, description=n.description, project_id=n.project_id,
                       environment_id=n.environment_id, environment_name=env.name if env else None,
                       language=env.language if env else None, current_version=n.current_version,
                       created_at=n.created_at, updated_at=n.updated_at,
                       editable=perm_service.has_capability(session, user, n.project_id, Capability.EDIT))


def _many(session: Session, user: User, stmt) -> list[NotebookOut]:
    if stmt is None:
        return []
    rows = list(session.exec(stmt).all())
    ids = {r.environment_id for r in rows}
    envs = {e.id: e for e in session.exec(select(ScriptEnvironment).where(ScriptEnvironment.id.in_(ids))).all()} if ids else {}  # type: ignore[attr-defined]
    return [_out(session, user, r, envs) for r in rows]


def _detail(session: Session, user: User, n: Notebook) -> NotebookDetail:
    out = _out(session, user, n)
    row = notebooks.version_row(session, n)
    author = session.get(User, row.created_by) if row is not None and row.created_by else None
    limits = get_settings().notebooks
    detail = NotebookDetail(**out.model_dump(), content=notebooks.content_of(session, n),
                            author=Author(id=author.id, email=author.email, full_name=author.full_name) if author else None,
                            max_visitors=n.max_visitors, visitors_default=min(limits.max_visitors_per_app,
                                                                              limits.max_visitors_ceiling),
                            visitors_ceiling=limits.max_visitors_ceiling)
    if out.editable and n.draft is not None:
        by = session.get(User, n.draft_by) if n.draft_by else None
        detail.draft, detail.draft_by, detail.draft_at = n.draft, by.email if by else None, n.draft_at
    return detail


# ── lists ─────────────────────────────────────────────────────────────────────
@router.get("/notebooks", response_model=list[NotebookOut])
def list_notebooks(user: User = Depends(get_current_user), session: Session = Depends(get_session)):
    """Every notebook this person sees."""
    if not _on():
        return []
    return _many(session, user, notebooks.visible_query(session, user))


@router.get("/projects/{project_id}/notebooks", response_model=list[NotebookOut])
def list_project_notebooks(project_id: int, user: User = Depends(get_current_user),
                           session: Session = Depends(get_session)):
    if session.get(Project, project_id) is None:
        raise HTTPException(status_code=404, detail="Folder not found")
    ensure_can(session, user, project_id, Capability.VIEW)
    if not _on():
        return []
    return _many(session, user, notebooks.visible_query(session, user, {project_id}))


@router.get("/notebooks/{notebook_id}", response_model=NotebookDetail)
def get_notebook(notebook_id: int, user: User = Depends(get_current_user), session: Session = Depends(get_session)):
    if not _on():
        raise HTTPException(status_code=404, detail="Notebook not found")
    return _detail(session, user, notebooks.get_visible(session, user, notebook_id))


# ── create, change, copy, delete ──────────────────────────────────────────────
@router.post("/projects/{project_id}/notebooks", response_model=NotebookDetail, status_code=status.HTTP_201_CREATED)
def create_notebook(project_id: int, body: NotebookCreate, request: Request, user: User = Depends(get_current_user),
                    session: Session = Depends(get_session)):
    _need_on()
    if session.get(Project, project_id) is None:
        raise HTTPException(status_code=404, detail="Folder not found")
    ensure_can(session, user, project_id, Capability.EDIT)
    env = _environment(session, user, body.environment_id)
    name = _name(session, project_id, body.name)
    max_visitors = _max_visitors(body.max_visitors)
    content = notebooks.check_content(
        name, body.content if body.content is not None else {"source": notebooks.starter_source(name)})
    notebook = Notebook(name=name, description=body.description.strip(), project_id=project_id, owner_id=user.id,
                        environment_id=env.id, current_version=0, max_visitors=max_visitors)
    session.add(notebook)
    session.flush()
    notebooks.add_version(session, notebook, content, "created", user)
    session.commit()
    session.refresh(notebook)
    audit.record_audit(session, actor=user, action=audit.NOTEBOOK_CREATE, target_type="notebook",
                       target_id=notebook.id, target_label=notebook.name, request=request,
                       detail={"project_id": project_id, "environment": env.name})
    return _detail(session, user, notebook)


@router.patch("/notebooks/{notebook_id}", response_model=NotebookDetail)
def update_notebook(notebook_id: int, body: NotebookUpdate, request: Request, user: User = Depends(get_current_user),
                    session: Session = Depends(get_session)):
    """Name, description, folder (a move), environment, content. A change of the content
    is a new version, which becomes the one the app runs, with the rights of whoever
    saves it; the editor's draft is then behind it, and goes."""
    _need_on()
    notebook = notebooks.get_visible(session, user, notebook_id)
    ensure_can(session, user, notebook.project_id, Capability.EDIT)
    changes: dict[str, Any] = {}
    target = notebook.project_id
    if body.project_id is not None and body.project_id != notebook.project_id:
        if session.get(Project, body.project_id) is None:
            raise HTTPException(status_code=404, detail="Destination folder not found")
        ensure_can(session, user, body.project_id, Capability.EDIT)
        target = body.project_id
    name = _name(session, target, body.name if body.name is not None else notebook.name, exclude_id=notebook.id)
    if name != notebook.name:
        changes["name"] = {"from": notebook.name, "to": name}
    if target != notebook.project_id:
        changes["project_id"] = {"from": notebook.project_id, "to": target}
    if body.environment_id is not None and body.environment_id != notebook.environment_id:
        env = _environment(session, user, body.environment_id)
        changes["environment"] = env.name
        notebook.environment_id = env.id
    elif session.get(ScriptEnvironment, notebook.environment_id) is None:
        raise HTTPException(status_code=422, detail="Its scripting environment no longer exists: choose another")
    new_content = None
    if body.content is not None:
        checked = notebooks.check_content(name, body.content)
        if checked != notebooks.content_of(session, notebook):
            _not_being_edited(session, notebook)
            new_content = checked
    notebook.name, notebook.project_id = name, target
    if body.description is not None and body.description.strip() != notebook.description:
        notebook.description = body.description.strip()
        changes["description"] = True
    if "max_visitors" in body.model_fields_set and body.max_visitors != notebook.max_visitors:
        changes["max_visitors"] = {"from": notebook.max_visitors, "to": _max_visitors(body.max_visitors)}
        notebook.max_visitors = body.max_visitors
    if new_content is not None:
        changes["version"] = notebooks.add_version(session, notebook, new_content, "edited", user).version
        notebook.draft = notebook.draft_by = notebook.draft_at = None
    if not changes:
        return _detail(session, user, notebook)
    notebook.updated_at = datetime.now(timezone.utc)
    session.add(notebook)
    session.commit()
    session.refresh(notebook)
    audit.record_audit(session, actor=user, action=audit.NOTEBOOK_UPDATE, target_type="notebook",
                       target_id=notebook.id, target_label=notebook.name, request=request, detail=changes)
    return _detail(session, user, notebook)


@router.delete("/notebooks/{notebook_id}/draft", response_model=NotebookDetail)
def discard_draft(notebook_id: int, request: Request, user: User = Depends(get_current_user),
                  session: Session = Depends(get_session)):
    """Throws away the editor's unsaved work: the notebook is its current version again."""
    notebook = notebooks.get_visible(session, user, notebook_id)
    ensure_can(session, user, notebook.project_id, Capability.EDIT)
    if notebook.draft is None:
        return _detail(session, user, notebook)
    notebook.draft = notebook.draft_by = notebook.draft_at = None
    session.add(notebook)
    session.commit()
    session.refresh(notebook)
    audit.record_audit(session, actor=user, action=audit.NOTEBOOK_UPDATE, target_type="notebook",
                       target_id=notebook.id, target_label=notebook.name, request=request,
                       detail={"draft": "discarded"})
    return _detail(session, user, notebook)


@router.post("/notebooks/{notebook_id}/duplicate", response_model=NotebookDetail, status_code=status.HTTP_201_CREATED)
def duplicate_notebook(notebook_id: int, request: Request, user: User = Depends(get_current_user),
                       session: Session = Depends(get_session)):
    """A copy in its folder, as «name (i)», from the version the app runs; whoever copies
    it is the author of the copy's first version."""
    _need_on()
    src = notebooks.get_visible(session, user, notebook_id)
    ensure_can(session, user, src.project_id, Capability.EDIT)
    copy = Notebook(name=free_copy_name(session, Notebook, src.project_id, src.name), description=src.description,
                    project_id=src.project_id, owner_id=user.id, environment_id=src.environment_id, current_version=0,
                    max_visitors=src.max_visitors)
    session.add(copy)
    session.flush()
    notebooks.add_version(session, copy, notebooks.content_of(session, src), "created", user)
    commit_copy(session)
    session.refresh(copy)
    audit.record_audit(session, actor=user, action=audit.NOTEBOOK_CREATE, target_type="notebook",
                       target_id=copy.id, target_label=copy.name, request=request,
                       detail={"project_id": copy.project_id, "duplicate_of": src.id})
    return _detail(session, user, copy)


@router.delete("/notebooks/{notebook_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_notebook(notebook_id: int, request: Request, user: User = Depends(get_current_user),
                    session: Session = Depends(get_session)):
    notebook = notebooks.get_visible(session, user, notebook_id)
    ensure_can(session, user, notebook.project_id, Capability.EDIT)
    label = notebook.name
    # its live pods: the sessions go now, their Jobs with the reaper's next round
    from app.services import notebook_runtime

    notebook_runtime.forget(session, notebook.id)
    for row in session.exec(select(NotebookVersion).where(NotebookVersion.notebook_id == notebook.id)).all():
        session.delete(row)
    session.flush()
    session.delete(notebook)
    session.commit()
    audit.record_audit(session, actor=user, action=audit.NOTEBOOK_DELETE, target_type="notebook",
                       target_id=notebook_id, target_label=label, request=request)


# ── versions ──────────────────────────────────────────────────────────────────
@router.get("/notebooks/{notebook_id}/versions", response_model=list[VersionOut])
def list_versions(notebook_id: int, user: User = Depends(get_current_user), session: Session = Depends(get_session)):
    notebook = notebooks.get_visible(session, user, notebook_id)
    rows = session.exec(select(NotebookVersion).where(NotebookVersion.notebook_id == notebook.id)
                        .order_by(NotebookVersion.version.desc())).all()  # type: ignore[attr-defined]
    authors = {u.id: u.email for u in session.exec(select(User).where(
        User.id.in_({r.created_by for r in rows if r.created_by}))).all()} if rows else {}  # type: ignore[attr-defined]
    return [VersionOut(version=r.version, note=r.note, created_at=r.created_at, created_by=authors.get(r.created_by),
                       current=r.version == notebook.current_version) for r in rows]


@router.get("/notebooks/{notebook_id}/versions/{version}", response_model=dict)
def get_version(notebook_id: int, version: int, user: User = Depends(get_current_user),
                session: Session = Depends(get_session)) -> dict:
    notebook = notebooks.get_visible(session, user, notebook_id)
    if notebooks.version_row(session, notebook, version) is None:
        raise HTTPException(status_code=404, detail="Version not found")
    return notebooks.content_of(session, notebook, version)


@router.post("/notebooks/{notebook_id}/versions/{version}/restore", response_model=NotebookDetail)
def restore_version(notebook_id: int, version: int, request: Request, user: User = Depends(get_current_user),
                    session: Session = Depends(get_session)):
    """An older version becomes the newest, and the one the app runs — with the rights of
    whoever restores it, not of whoever first wrote it."""
    _need_on()
    notebook = notebooks.get_visible(session, user, notebook_id)
    ensure_can(session, user, notebook.project_id, Capability.EDIT)
    if notebooks.version_row(session, notebook, version) is None:
        raise HTTPException(status_code=404, detail="Version not found")
    if version == notebook.current_version:
        return _detail(session, user, notebook)
    _not_being_edited(session, notebook)
    content = notebooks.check_content(notebook.name, notebooks.content_of(session, notebook, version))
    row = notebooks.add_version(session, notebook, content, f"restored from v{version}", user)
    notebook.updated_at = datetime.now(timezone.utc)
    session.add(notebook)
    session.commit()
    session.refresh(notebook)
    audit.record_audit(session, actor=user, action=audit.NOTEBOOK_RESTORE, target_type="notebook",
                       target_id=notebook.id, target_label=notebook.name, request=request,
                       detail={"from": version, "version": row.version})
    return _detail(session, user, notebook)
