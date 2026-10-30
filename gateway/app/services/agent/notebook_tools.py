"""The agent's marimo notebooks: reading one, and the plan's action that creates one or
changes its source.

The rules are the notebooks' own (routes/notebooks.py, services/notebooks.py): only where
the scripting environments are on (Kubernetes); a notebook is seen with VIEW on its folder
and the use of its environment, changed with EDIT; the app reads data with the rights of
whoever saved the version it runs — applying the plan makes this user that author; while
marimo's editor is open on a notebook its source changes only there. Checked when the
action is planned and again, through the same routes, when it is applied.
"""
from __future__ import annotations

import ast
from typing import Any, Optional

from fastapi import HTTPException
from pydantic import BaseModel, Field

from app.models import Notebook, Project, ScriptEnvironment, User
from app.models.permission import Capability
from app.services import notebooks
from app.services import permissions as perm_service
from app.services.agent import plans
from app.services.agent.common import folder_paths, in_thread
from app.services.agent.planning import change_lines, notebook_link, planned_save, text_diff
from app.services.agent.registry import ToolContext, ToolError, tool

MAX_SOURCE_CHARS = 200_000  # what a reading returns; a notebook is far smaller as a rule

_OFF = ("Notebooks need the scripting environments (Kubernetes), which this installation does not have: "
        "they cannot be read or made here.")


def _on() -> None:
    from app.routes.notebooks import _on as scripting_on

    if not scripting_on():
        raise ToolError(_OFF)


def _visible(session, user, notebook_id: int) -> Notebook:
    try:
        return notebooks.get_visible(session, user, notebook_id)
    except HTTPException as e:
        raise ToolError(f"No notebook {notebook_id} is available to this user.") from e


# ── reading ───────────────────────────────────────────────────────────────────
class NotebookIdArgs(BaseModel):
    notebook_id: int


@tool("get_notebook", title="Read a notebook", args=NotebookIdArgs, level="read", reads_data=False)
async def get_notebook(ctx: ToolContext, a: NotebookIdArgs) -> dict:
    """A marimo notebook: its folder, its Python environment, who saved the version its app runs (the app reads data with their rights) and its source, the marimo .py file. Unsaved work in marimo's editor is not shown: only that there is some, and whose. To change it, plan_notebook with its id."""
    from app.routes import notebooks as nb_routes

    _on()

    def work(session, user):
        n = _visible(session, user, a.notebook_id)
        d = nb_routes.get_notebook(n.id, user=user, session=session)
        source = d.content.get("source") or ""
        out: dict[str, Any] = {
            "id": d.id, "name": d.name, "description": d.description, "folder_id": d.project_id,
            "folder": folder_paths(session).get(d.project_id, ""), "environment_id": d.environment_id,
            "environment": d.environment_name, "version": d.current_version,
            "author": d.author.email if d.author else None, "can_edit": d.editable,
            "source": source[:MAX_SOURCE_CHARS], "link": notebook_link(d.id),
        }
        if len(source) > MAX_SOURCE_CHARS:
            out["note"] = f"The source is cut at {MAX_SOURCE_CHARS} characters of {len(source)}."
        if n.draft is not None and d.editable:
            by = session.get(User, n.draft_by) if n.draft_by else None
            out["unsaved_draft"] = {"by": by.email if by else None, "at": n.draft_at,
                                    "note": "Saving a new version drops it."}
        return out

    return await in_thread(ctx, work)


# ── plan: create or change ────────────────────────────────────────────────────
class PlanNotebookArgs(BaseModel):
    notebook_id: Optional[int] = Field(None, description="A saved notebook to change; leave it out to create one.")
    folder_id: Optional[int] = Field(None, description="A new notebook: its folder (list_folders: can_edit).")
    name: Optional[str] = Field(None, max_length=200, description="A new notebook: its name (required). A saved one: a new name.")
    description: Optional[str] = Field(None, max_length=2000)
    environment_id: Optional[int] = Field(None, description="A Python environment the user may use (list_environments). A new notebook: required.")
    source: Optional[str] = Field(None, max_length=notebooks.MAX_SOURCE_BYTES, description="The WHOLE marimo file (.py): `import marimo`, `app = marimo.App()`, cells as `@app.cell` functions. Data is read with `import tabularia` and `tabularia.read(\"Folder/datasource\")`. A new notebook: leave it out for a starting one; a saved one: leave it out to keep its source.")


def _syntax(name: str, source: str) -> None:
    """A source that is not Python would break the app: the plan refuses it (a help, the
    interface saves what it is given)."""
    try:
        ast.parse(source)
    except SyntaxError as e:
        raise ToolError(f"Notebook «{name}»: the source is not valid Python, line {e.lineno}: {e.msg}.") from e


@tool("plan_notebook", title="Plan a notebook", args=PlanNotebookArgs, level="write", reads_data=False)
async def plan_notebook(ctx: ToolContext, a: PlanNotebookArgs) -> dict:
    """Add to the plan: create a marimo notebook (folder_id, name, environment_id, source) or change a saved one (notebook_id and what changes: the plan shows the difference of the source). Its app will read data with this user's rights, as the author of the version it runs. Refused while someone has the notebook open in marimo's editor. Only where scripting (Kubernetes) is on. Nothing changes until the plan is confirmed."""
    from app.routes import notebooks as nb_routes

    _on()
    new = a.notebook_id is None
    if new and (a.folder_id is None or not (a.name or "").strip() or a.environment_id is None):
        raise ToolError("A new notebook needs folder_id, name and environment_id (or give notebook_id to change a saved one).")
    # a change planned already for this notebook: the new one goes on from it, in its place
    pending = None if new else planned_save(ctx, "save_notebook", a.notebook_id)

    def check(session, user):
        paths = folder_paths(session)
        if new:
            if session.get(Project, a.folder_id) is None \
                    or not perm_service.has_capability(session, user, a.folder_id, Capability.EDIT):
                raise ToolError(f"The user cannot create notebooks in folder {a.folder_id}: see list_folders (can_edit).")
            env = nb_routes._environment(session, user, a.environment_id)
            name = nb_routes._name(session, a.folder_id, a.name)
            content = notebooks.check_content(name, {"source": a.source if a.source is not None
                                                     else notebooks.starter_source(name)})
            _syntax(name, content["source"])
            return {"folder_id": a.folder_id, "folder": paths.get(a.folder_id, ""), "name": name,
                    "environment_id": env.id, "environment": env.name, "content": content}
        notebook = _visible(session, user, a.notebook_id)
        if not perm_service.has_capability(session, user, notebook.project_id, Capability.EDIT):
            raise ToolError(f"The user can read the notebook «{notebook.name}» but not change it (it needs EDIT on its folder).")
        name = nb_routes._name(session, notebook.project_id, a.name if a.name is not None
                               else pending["name"] if pending else notebook.name, exclude_id=notebook.id)
        old_env = session.get(ScriptEnvironment, notebook.environment_id)
        env = old_env
        env_id = a.environment_id if a.environment_id is not None else (pending or {}).get("environment_id")
        if env_id is not None and env_id != notebook.environment_id:
            env = nb_routes._environment(session, user, env_id)
        current = notebooks.content_of(session, notebook)
        content = None
        source = a.source if a.source is not None else ((pending or {}).get("definition") or {}).get("source")
        if source is not None:
            checked = notebooks.check_content(name, {"source": source})
            if checked != current:
                _syntax(name, checked["source"])
                nb_routes._not_being_edited(session, notebook)  # 409 while marimo's editor is open on it
                content = checked
        description = (a.description.strip() if a.description is not None
                       else pending["description"] if pending and pending.get("description") is not None
                       else notebook.description)
        lines = text_diff(current.get("source") or "", content["source"]) if content else []
        lines += change_lines([("name", notebook.name, name),
                               ("environment", old_env.name if old_env else None, env.name if env else None)])
        if description != notebook.description:
            lines.append("~ description changed")
        if not lines:
            raise ToolError(f"The plan would change nothing in the notebook «{notebook.name}».")
        draft_by = None
        if content is not None and notebook.draft is not None:
            by = session.get(User, notebook.draft_by) if notebook.draft_by else None
            draft_by = by.email if by else "someone"
        return {"folder_id": notebook.project_id, "folder": paths.get(notebook.project_id, ""), "name": name,
                "old_name": notebook.name, "environment_id": env.id if env and env.id != notebook.environment_id else None,
                "environment": env.name if env else None, "content": content, "base": f"v{notebook.current_version}",
                "diff_lines": lines, "draft_by": draft_by,
                "description": description if description != notebook.description else None}

    info = await in_thread(ctx, check)
    if new:
        words = (f"Create the notebook «{info['name']}» in {info['folder']}, environment «{info['environment']}»; "
                 "its app will read data with this user's rights")
    else:
        n = len([x for x in info["diff_lines"] if x[:1] in "+-~"])
        words = f"Change the notebook «{info['old_name']}» in {info['folder']} ({n} changed line{'s' if n != 1 else ''})"
        if info["content"] is not None:
            words += "; its app will run the new version with this user's rights"
        if info["draft_by"]:
            words += f"; the unsaved work in marimo's editor (by {info['draft_by']}) is dropped"
    action: dict[str, Any] = {"kind": "save_notebook", "id": a.notebook_id, "folder_id": info["folder_id"],
                              "folder": info["folder"], "name": info["name"], "environment": info["environment"],
                              "environment_id": info["environment_id"], "definition": info["content"],
                              "words": words + "."}
    if new:
        # the source whole: it is what the app will run, with this user's rights
        action.update(description=(a.description or "").strip(), code=info["content"]["source"])
    else:
        action.update(base_hash=info["base"], diff_lines=info["diff_lines"], description=info["description"])
    key = f"save_notebook:{a.notebook_id}" if not new else f"save_notebook:new:{info['folder_id']}:{info['name'].lower()}"
    return plans.add_action(ctx, action, key=key)


@plans.action_kind("save_notebook")
async def _apply_save_notebook(ctx: ToolContext, a: dict, results: list[dict]) -> dict:
    from app.routes import notebooks as nb_routes

    def work(session, user):
        if a.get("id") is None:
            d = nb_routes.create_notebook(a["folder_id"], nb_routes.NotebookCreate(
                name=a["name"], description=a.get("description") or "", environment_id=a["environment_id"],
                content=a["definition"]), ctx.request, user=user, session=session)
            return d.id, d.name, "created"
        notebook = session.get(Notebook, a["id"])
        if notebook is None:
            raise ToolError(f"Notebook {a['id']} no longer exists: nothing saved.")
        if f"v{notebook.current_version}" != a.get("base_hash"):
            raise ToolError(f"The notebook «{notebook.name}» was changed in Tabularia after the plan was made: nothing "
                            "saved. Read it again (get_notebook) and make a new plan.")
        body: dict[str, Any] = {"name": a["name"]}
        if a.get("description") is not None:
            body["description"] = a["description"]
        if a.get("environment_id") is not None:
            body["environment_id"] = a["environment_id"]
        if a.get("definition") is not None:
            body["content"] = a["definition"]
        d = nb_routes.update_notebook(a["id"], nb_routes.NotebookUpdate(**body), ctx.request, user=user, session=session)
        return d.id, d.name, "changed"

    notebook_id, name, verb = await in_thread(ctx, work)
    return {"object": {"kind": "notebook", "id": notebook_id, "name": name}, "link": notebook_link(notebook_id),
            "words": f"Notebook «{name}» {verb}."}
