"""Notebooks: who sees them, what they may hold, their versions.
docs/specs/notebooks.md; the object in models/notebook.py, the routes in
routes/notebooks.py.

Who sees a notebook: VIEW on its folder **and** the use of its environment, as for saved
scripts (services/saved_scripts.py): whoever lacks either does not see it at all — the
routes answer 404, the lists and the search leave it out. Administrators see every
notebook. EDIT on the folder changes, moves and deletes it.

A version's `content`: {"source": "<the marimo .py file>"}. A notebook declares no
inputs: it reads datasources by name while it runs, with the rights of whoever saved the
version it runs (piece 2), checked at every read.
"""
from __future__ import annotations

import json
from typing import Optional

from fastapi import HTTPException
from sqlmodel import Session, select

from app.models import Notebook, NotebookVersion, ScriptEnvironment, User
from app.models.permission import Capability
from app.services import permissions as perm_service
from app.services import script_nodes
from app.services.permissions import is_admin
from app.services.saved_scripts import usable_environment_ids

# one file, as large as the whole code of a saved script may be
MAX_SOURCE_BYTES = script_nodes.MAX_CODE_BYTES


def empty_content() -> dict:
    return {"source": ""}


def starter_source(name: str) -> str:
    """A new notebook: a valid marimo file with a title and a cell that shows how data is
    read. `tabularia` is commented out: the module reaches the pods with the runtime."""
    title = repr(f"# {name.strip() or 'Notebook'}")
    return (
        "import marimo\n"
        "\n"
        "app = marimo.App()\n"
        "\n"
        "\n"
        "@app.cell\n"
        "def _():\n"
        "    import marimo as mo\n"
        "    return (mo,)\n"
        "\n"
        "\n"
        "@app.cell\n"
        "def _(mo):\n"
        f"    mo.md({title})\n"
        "    return\n"
        "\n"
        "\n"
        "@app.cell\n"
        "def _():\n"
        "    # import tabularia\n"
        '    # df = tabularia.read("Sample/orders")  # any datasource the app\'s author can read\n'
        "    # df\n"
        "    return\n"
        "\n"
        "\n"
        'if __name__ == "__main__":\n'
        "    app.run()\n"
    )


# ── who sees what ────────────────────────────────────────────────────────────
def can_see(session: Session, user: User, notebook: Notebook) -> bool:
    if is_admin(session, user):
        return True
    envs = usable_environment_ids(session, user)
    return (notebook.environment_id in envs
            and perm_service.has_capability(session, user, notebook.project_id, Capability.VIEW))


def visible_query(session: Session, user: User, project_ids: Optional[set[int]] = None):
    """A select of the notebooks this person sees (in `project_ids` when given), or None
    when there are none."""
    stmt = select(Notebook)
    if not is_admin(session, user):
        envs = usable_environment_ids(session, user)
        readable = perm_service.readable_project_ids(session, user)
        if project_ids is not None:
            readable = set(readable) & set(project_ids)
        if not envs or not readable:
            return None
        stmt = stmt.where(Notebook.environment_id.in_(envs), Notebook.project_id.in_(readable))  # type: ignore[attr-defined]
    elif project_ids is not None:
        stmt = stmt.where(Notebook.project_id.in_(project_ids))  # type: ignore[attr-defined]
    return stmt.order_by(Notebook.name)


def get_visible(session: Session, user: User, notebook_id: int) -> Notebook:
    """The notebook, or 404: one that cannot be seen does not exist for this person."""
    notebook = session.get(Notebook, notebook_id)
    if notebook is None or not can_see(session, user, notebook):
        raise HTTPException(status_code=404, detail="Notebook not found")
    return notebook


# ── what a notebook may hold ─────────────────────────────────────────────────
def _refuse(name: str, message: str) -> HTTPException:
    return HTTPException(status_code=422, detail=f"Notebook «{name}»: {message}")


def check_environment(env: ScriptEnvironment) -> None:
    """marimo is Python: a notebook cannot run in an R environment."""
    if env.language != "python":
        raise HTTPException(status_code=422,
                            detail=f"A notebook needs a Python environment: «{env.name}» is for {env.language.upper()}")


def check_content(name: str, content: dict) -> dict:
    """The content as it will be saved, or a 422 saying what is wrong."""
    if not isinstance(content, dict):
        raise _refuse(name, "its content is not an object")
    source = content.get("source")
    if not isinstance(source, str):
        raise _refuse(name, "its source is the text of a marimo file")
    if not source.strip():
        raise _refuse(name, "its source is empty")
    if len(source.encode()) > MAX_SOURCE_BYTES:
        raise _refuse(name, f"its source is larger than {MAX_SOURCE_BYTES // 1024} KB")
    return {"source": source}


# ── versions ─────────────────────────────────────────────────────────────────
def version_row(session: Session, notebook: Notebook, version: Optional[int] = None) -> Optional[NotebookVersion]:
    return session.exec(select(NotebookVersion).where(
        NotebookVersion.notebook_id == notebook.id,
        NotebookVersion.version == (version or notebook.current_version))).first()


def content_of(session: Session, notebook: Notebook, version: Optional[int] = None) -> dict:
    row = version_row(session, notebook, version)
    if row is None:
        return empty_content()
    try:
        return {**empty_content(), **json.loads(row.content)}
    except ValueError:
        return empty_content()


def add_version(session: Session, notebook: Notebook, content: dict, note: str, user: User) -> NotebookVersion:
    """The next version, which becomes the one the app runs — with the rights of `user`,
    who saved it."""
    last = session.exec(select(NotebookVersion.version).where(NotebookVersion.notebook_id == notebook.id)
                        .order_by(NotebookVersion.version.desc())).first() or 0  # type: ignore[attr-defined]
    row = NotebookVersion(notebook_id=notebook.id, version=last + 1, content=json.dumps(content), note=note,
                          created_by=user.id)
    session.add(row)
    notebook.current_version = row.version
    session.add(notebook)
    return row


def notebooks_using_environment(session: Session, environment_id: int) -> list[str]:
    return list(session.exec(select(Notebook.name).where(Notebook.environment_id == environment_id)
                             .order_by(Notebook.name).limit(10)).all())
