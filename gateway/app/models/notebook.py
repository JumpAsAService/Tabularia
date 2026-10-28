"""Notebooks: marimo notebooks kept as objects of a folder, with their versions, and
published as web apps. docs/specs/notebooks.md; the rules in services/notebooks.py.

- `Notebook`: the object — name, folder, scripting environment (a Python one), the
  version the app runs, and the working draft the editor last saved. Who sees it: VIEW
  on its folder AND the use of its environment, as for saved scripts.
- `NotebookVisitor`: one open app in a browser (marimo's WebSocket through the proxy),
  so that every gateway replica counts the same visitors (services/notebook_runtime.py,
  «Visitors»).
- `NotebookTicket`: a one-minute, one-use ticket that opens a notebook's app or editor on
  the notebooks' own address (services/notebook_frames.py).
- `NotebookVersion`: every save that changes the notebook is a version; an older one can
  be restored (it becomes the newest). `content` is JSON: {"source": "<the marimo .py
  file>"}. The app reads with the rights of the version's `created_by`: whoever saved
  the code it runs.
"""
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import Column, Text, UniqueConstraint
from sqlmodel import Field, SQLModel


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Notebook(SQLModel, table=True):
    __tablename__ = "notebooks"
    __table_args__ = (UniqueConstraint("project_id", "name", name="uq_notebook_project_name"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    name: str
    description: str = ""
    project_id: int = Field(foreign_key="projects.id", index=True)
    owner_id: Optional[int] = Field(default=None, foreign_key="users.id")
    # no cascade: an environment in use by a notebook cannot be deleted
    environment_id: int = Field(foreign_key="script_environments.id", index=True)
    current_version: int = 1  # the one the app runs
    # what the editor last saved and is not a version yet, kept so that nothing is lost
    # when the editor's pod stops; None = nothing beyond the current version
    draft: Optional[str] = Field(default=None, sa_column=Column(Text, nullable=True))
    draft_by: Optional[int] = Field(default=None, foreign_key="users.id")
    draft_at: Optional[datetime] = None
    # how many people may have the app open at once (each has a kernel of their own, with
    # its copy of the data, in the app's pod); None = NOTEBOOKS__MAX_VISITORS_PER_APP
    max_visitors: Optional[int] = None
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)


class NotebookVersion(SQLModel, table=True):
    __tablename__ = "notebook_versions"
    __table_args__ = (UniqueConstraint("notebook_id", "version", name="uq_notebook_version"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    notebook_id: int = Field(foreign_key="notebooks.id", index=True)
    version: int  # 1, 2, 3… per notebook; the notebook's current_version is the one the app runs
    content: str = Field(sa_column=Column(Text, nullable=False))
    note: str = ""  # a stable English code the interface translates: "created", "edited", "restored from v2"
    created_at: datetime = Field(default_factory=_now)
    created_by: Optional[int] = Field(default=None, foreign_key="users.id")


# a live pod: starting → ready, or failed (the error says why, in words)
SESSION_MODES = ("app", "edit")
SESSION_STATES = ("starting", "ready", "failed")


def _naive_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class NotebookSession(SQLModel, table=True):
    """A notebook's pod, in the scripts namespace. Naive UTC times, as the scripting tables."""
    __tablename__ = "notebook_sessions"
    __table_args__ = (UniqueConstraint("notebook_id", "mode", name="uq_notebook_session_mode"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    notebook_id: int = Field(foreign_key="notebooks.id", index=True, ondelete="CASCADE")
    mode: str = Field(max_length=8)  # app | edit
    # whose rights the pod reads with: the author of the version (app), the editor (edit)
    user_id: int = Field(foreign_key="users.id", index=True)
    version: int  # the version it started from
    namespace: str = Field(max_length=63)
    job_name: str = Field(max_length=63)
    pod_ip: Optional[str] = Field(default=None, max_length=64)
    status: str = Field(default="starting", max_length=16)
    error: Optional[str] = Field(default=None, sa_column=Column(Text, nullable=True))
    # SHA-256 of the pod's notebook token: the token itself is only in the pod's Secret
    token_fp: str = Field(max_length=64, index=True)
    started_at: datetime = Field(default_factory=_naive_now)
    ready_at: Optional[datetime] = None
    last_seen_at: datetime = Field(default_factory=_naive_now)


class NotebookVisitor(SQLModel, table=True):
    """An app open in a browser: marimo's WebSocket, carried by one gateway replica. The
    replica keeps `last_seen_at` fresh while it is open and deletes the row when it
    closes; a row nobody refreshed (a replica that died) no longer counts, and the
    reaper clears it. Naive UTC times, as the sessions."""
    __tablename__ = "notebook_visitors"

    id: Optional[int] = Field(default=None, primary_key=True)
    notebook_id: int = Field(foreign_key="notebooks.id", index=True, ondelete="CASCADE")
    user_id: int = Field(foreign_key="users.id", index=True, ondelete="CASCADE")
    opened_at: datetime = Field(default_factory=_naive_now)
    last_seen_at: datetime = Field(default_factory=_naive_now)


class NotebookTicket(SQLModel, table=True):
    """A ticket to open a notebook's app or editor on the notebooks' own address
    (NOTEBOOKS__ORIGIN; services/notebook_frames.py): Tabularia's page asks for it, the
    frame uses it once, within a minute, and gets the notebook's cookie for it. In the
    database because the gateway's replicas share nothing else. Naive UTC times, as the
    sessions."""
    __tablename__ = "notebook_tickets"

    id: Optional[int] = Field(default=None, primary_key=True)
    # SHA-256 of the ticket: the ticket itself is only in the frame's address
    token_fp: str = Field(max_length=64, unique=True, index=True)
    notebook_id: int = Field(foreign_key="notebooks.id", index=True, ondelete="CASCADE")
    mode: str = Field(max_length=8)  # app | edit
    user_id: int = Field(foreign_key="users.id", index=True, ondelete="CASCADE")
    # the Tabularia session that asked for it: the notebook's cookie holds while it does
    sid: str = Field(max_length=64)
    # the origin of the page that asked, the only one that may frame the notebook
    frame_origin: str = Field(max_length=255)
    expires_at: datetime = Field(index=True)
