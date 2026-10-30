"""The agent's flow groups: reading one, and the plan's actions that create or change a
group, run it and schedule it.

A group is a grid: steps run one after another, the cells of a step at the same time.
A cell runs a flow, refreshes a datasource, runs a saved script or a SQL script (writing
its datasource), runs a container image, or emails datasources (Pushmail). The rules are
the groups' own (routes/flow_groups.py, `_checked`): every cell must be something the
user may run — RUN on its folder, the registry and the image theirs to use, an SMTP
connection with CONNECT and recipients within its allowed domains for a Pushmail —
checked when the action is planned and again, through the same route, when it is applied.
"""
from __future__ import annotations

import shlex
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, ValidationError
from sqlmodel import select

from app.core.config import get_settings
from app.models import FlowGroup, Project, Run
from app.models.permission import Capability
from app.schemas.models import FlowGroupCell, FlowGroupDefinition, PushmailCell
from app.services import metadata_only
from app.services import permissions as perm_service
from app.services.agent import plans
from app.services.agent.common import fingerprint, folder_paths, in_thread
from app.services.agent.planning import (
    checked_cron, cron_words, flow_group_link, flows_only, made_by, planned_creation, planned_save,
)
from app.services.agent.registry import ToolContext, ToolError, _words, tool
from app.services.flow_groups import definition_of

_OBJECT_KINDS = ("flow", "refresh", "script", "sql")
_KIND_WORDS = {"flow": "run the flow", "refresh": "refresh the datasource", "script": "run the script",
               "sql": "write the datasource of the SQL script"}


def _group(session, user, group_id: int) -> FlowGroup:
    group = session.get(FlowGroup, group_id)
    if group is None or not perm_service.has_capability(session, user, group.project_id, Capability.VIEW):
        raise ToolError(f"No flow group {group_id} is available to this user.")
    return group


def _cell_lines(session, user, definition: FlowGroupDefinition) -> list[str]:
    """The grid in words, a line per cell: «step 1: run the flow «Orders» (flow 4)»."""
    from app.routes.flow_groups import _labels

    labels = _labels(session, user, definition)
    lines = []
    for n, (row, names) in enumerate(zip(definition.steps, labels), 1):
        for cell, label in zip(row, names):
            if cell is None:
                continue
            if cell.kind in _OBJECT_KINDS:
                what = (f"«{label.name}»" if label and label.name else "(hidden)" if label and label.hidden
                        else "(missing)")
                text = f"{_KIND_WORDS[cell.kind]} {what} ({cell.kind} {cell.id})"
            elif cell.kind == "container":
                text = _container_words(cell)
            else:
                p = cell.pushmail or PushmailCell()
                files = ", ".join([f"«{f.fileName}» (datasource {f.datasourceId})" for f in p.attachments]
                                  + [f"«{r.fileName}» (report {r.reportId}, {r.format}"
                                     + (", summary in the body" if r.inBody else "") + ")" for r in p.reports])
                text = (f"email «{p.label or 'Pushmail'}» to {', '.join(p.to)}" + (f" (cc {', '.join(p.cc)})" if p.cc else "")
                        + f", subject «{p.subject}», files {files} (pushmail {cell.id})")
            lines.append(f"step {n}: {text}")
    return lines


def _container_words(cell: FlowGroupCell) -> str:
    """What a container cell runs: the image, and the command and arguments it is started
    with (the variables by name: their values may hold what someone typed for the image)."""
    d = cell.container or {}
    command = [str(x) for x in d.get("command") or []]
    args = [str(x) for x in d.get("args") or []]
    text = f"run the container «{d.get('label') or d.get('repository')}» ({d.get('repository') or '?'}:{d.get('tag') or 'latest'})"
    if command:
        text += f" running `{shlex.join(command + args)}`"
    elif args:
        text += f" with the arguments `{shlex.join(args)}`"
    names = [str(v.get("name")) for v in d.get("variables") or [] if isinstance(v, dict) and v.get("name")]
    if names:
        text += f", variables {', '.join(names)}"
    return text + f" (container {cell.id})"


def _settings(cell: FlowGroupCell) -> dict[str, Any]:
    if cell.kind == "container":
        return dict(cell.container or {})
    return cell.pushmail.model_dump() if cell.pushmail else {}


def settings_changes(before: FlowGroupDefinition, after: FlowGroupDefinition) -> list[str]:
    """The container and Pushmail cells kept by their number whose settings change, and
    which settings: «~ container 2: its settings change (command, variables)»."""
    old = {(c.kind, c.id): _settings(c) for row in before.steps for c in row
           if c is not None and c.kind in ("container", "pushmail")}
    lines = []
    for row in after.steps:
        for c in row:
            if c is None or (c.kind, c.id) not in old:
                continue
            was, now_ = old[(c.kind, c.id)], _settings(c)
            changed = sorted(k for k in set(was) | set(now_) if was.get(k) != now_.get(k))
            if changed:
                lines.append(f"~ {c.kind} {c.id}: its settings change ({', '.join(changed)})")
    return lines


# ── reading ───────────────────────────────────────────────────────────────────
class GroupIdArgs(BaseModel):
    flow_group_id: int


def _cell_view(cell: Optional[FlowGroupCell], label: Any, paths: dict[int, str]) -> Optional[dict]:
    if cell is None:
        return None
    out: dict[str, Any] = {"kind": cell.kind, "id": cell.id}
    if cell.kind in _OBJECT_KINDS:
        if label is None or label.missing:
            out["missing"] = True
        elif label.hidden:
            out["hidden"] = True
        else:
            out.update(name=label.name, folder=paths.get(label.project_id, ""))
    elif cell.kind == "container":
        d = cell.container or {}
        # the variables' values may hold what someone typed for the image: their names only
        out.update(label=d.get("label") or "", registry_id=d.get("registryId"), repository=d.get("repository"),
                   tag=d.get("tag") or "latest", command=d.get("command") or [], args=d.get("args") or [],
                   variables=[v.get("name") for v in d.get("variables") or [] if isinstance(v, dict)],
                   cpu=d.get("cpu"), memory=d.get("memory"))
    else:
        p = cell.pushmail or PushmailCell()
        out.update(label=p.label, connection_id=p.connectionId, to=p.to, cc=p.cc, subject=p.subject,
                   body=p.body[:2000], body_is_html=p.bodyIsHtml, timezone=p.timezone,
                   attachments=[{"datasource_id": f.datasourceId, "file_name": f.fileName, "max_rows": f.maxRows}
                                for f in p.attachments],
                   reports=[{"report_id": r.reportId, "format": r.format, "params": r.params, "file_name": r.fileName,
                             "in_body": r.inBody} for r in p.reports])
    return out


@tool("get_flow_group", title="Read a flow group", args=GroupIdArgs, level="read", reads_data=False,
      withholds="it returns no error text of its last run")
async def get_flow_group(ctx: ToolContext, a: GroupIdArgs) -> dict:
    """A flow group: its folder, schedule, last run and its grid — steps run one after another, the cells of a step at the same time — with what each cell runs: a flow, a datasource's refresh, a saved script, a SQL script, a container image, a Pushmail. To change it, plan_flow_group with its id and the whole new grid."""
    from app.routes import flow_groups as fg_routes

    withheld = ctx.withholds_data

    def work(session, user):
        _group(session, user, a.flow_group_id)
        d = fg_routes.get_flow_group(a.flow_group_id, user=user, session=session)
        paths = folder_paths(session)
        steps = [[_cell_view(c, label, paths) for c, label in zip(row, labels)]
                 for row, labels in zip(d.definition.steps, d.cells)]
        last = session.exec(select(Run).where(Run.flow_group_id == d.id).order_by(Run.id.desc())).first()  # type: ignore[union-attr]
        return {
            "id": d.id, "name": d.name, "description": d.description, "folder_id": d.project_id,
            "folder": paths.get(d.project_id, ""), "schedule": d.run_schedule, "next_run_at": d.next_run_at,
            "last_run": {"run_id": last.id, "status": last.status, "at": last.started_at,
                         **({"error": metadata_only.ERROR_WITHHELD if withheld else last.error[:500]}
                            if last.error else {})} if last else None,
            "can_edit": d.can_edit, "can_run": d.can_run, "steps": steps, "link": flow_group_link(d.id),
        }

    return await in_thread(ctx, work)


# ── plan: create or change ────────────────────────────────────────────────────
class GroupCellArgs(BaseModel):
    kind: Literal["flow", "refresh", "script", "container", "pushmail", "sql"] = Field(
        description="flow: run a flow; refresh: refresh a datasource imported from a connection; script: run a saved "
                    "Python/R script; sql: write a SQL script's datasource; container: run an image; pushmail: email "
                    "datasources as .xlsx files and reports as PDF or Excel.")
    id: Optional[int] = Field(None, description="flow, refresh, script, sql: the object's id. container, pushmail: the "
                                                "cell's number in the group (get_flow_group) to keep it as it is, or to "
                                                "change it with new settings; leave it out for a new one.")
    container: Optional[dict[str, Any]] = Field(None, description="container: the settings of a Container node — "
                                                                  "registryId, repository, tag, command, args, "
                                                                  "variables [{name, value}], cpu, memory, timeoutSeconds, retries.")
    pushmail: Optional[PushmailCell] = Field(None, description="pushmail: connectionId (an SMTP connection, see "
                                                               "list_connections and its allowed_domains), to, cc, "
                                                               "subject, body, attachments [{datasourceId, fileName, maxRows}], "
                                                               "reports [{reportId, format pdf|xlsx, params {name: value}, "
                                                               "fileName, inBody}].")


class PlanFlowGroupArgs(BaseModel):
    flow_group_id: Optional[int] = Field(None, description="A saved group to change; leave it out to create one.")
    folder_id: Optional[int] = Field(None, description="A new group: its folder (list_folders: can_edit).")
    name: Optional[str] = Field(None, max_length=200, description="A new group: its name (required). A saved one: a new name.")
    description: Optional[str] = Field(None, max_length=4000)
    steps: Optional[list[list[Optional[GroupCellArgs]]]] = Field(
        None, description="The WHOLE grid: a list of steps, each a list of cells run at the same time. A saved group: "
                          "the new grid (leave it out to keep it).")


def _definition(steps: list[list[Optional[GroupCellArgs]]], before: FlowGroupDefinition) -> FlowGroupDefinition:
    """The grid the route takes. A container or Pushmail cell given by its number alone
    is the one already in the group, kept as it is; a new one gets the next number."""
    kept = {(c.kind, c.id): c for row in before.steps for c in row if c is not None and c.kind in ("container", "pushmail")}
    used = {k: {c.id for row in before.steps for c in row if c is not None and c.kind == k} for k in ("container", "pushmail")}
    for row in steps:
        for c in row:
            if c is not None and c.kind in used and c.id is not None:
                used[c.kind].add(c.id)
    rows: list[list[Optional[dict]]] = []
    for n, row in enumerate(steps, 1):
        out: list[Optional[dict]] = []
        for c in row:
            if c is None:
                out.append(None)
            elif c.kind in _OBJECT_KINDS:
                if c.id is None:
                    raise ToolError(f"step {n}: a {c.kind} cell needs the id of what it runs.")
                out.append({"kind": c.kind, "id": c.id})
            else:
                settings = c.container if c.kind == "container" else (c.pushmail.model_dump() if c.pushmail else None)
                if settings is None:
                    old = kept.get((c.kind, c.id))
                    if old is None:
                        raise ToolError(f"step {n}: a new {c.kind} cell needs its settings (`{c.kind}`)"
                                        + (f": there is no {c.kind} cell {c.id} in the group." if c.id is not None else "."))
                    out.append(old.model_dump())
                    continue
                cell_id = c.id
                if cell_id is None:
                    cell_id = max(used[c.kind], default=0) + 1
                    used[c.kind].add(cell_id)
                out.append({"kind": c.kind, "id": cell_id, c.kind: settings})
        rows.append(out)
    try:
        return FlowGroupDefinition.model_validate({"steps": rows})
    except ValidationError as e:
        raise ToolError(_words(e)) from e


@tool("plan_flow_group", title="Plan a flow group", args=PlanFlowGroupArgs, level="write", reads_data=False)
async def plan_flow_group(ctx: ToolContext, a: PlanFlowGroupArgs) -> dict:
    """Add to the plan: create a flow group (folder_id, name, steps) or change a saved one (flow_group_id and what changes: the plan shows which cells come and go). Every cell is checked as the interface checks it: what it runs must be something the user may run (RUN on its folder), a container's image one they may use, a Pushmail's SMTP connection one they may use (CONNECT) with every recipient within its allowed domains. plan_run_flow_group runs it, plan_schedule (kind flow_group) schedules it. Nothing changes until the plan is confirmed."""
    from app.routes import flow_groups as fg_routes

    new = a.flow_group_id is None
    if new and (a.folder_id is None or not (a.name or "").strip()):
        raise ToolError("A new flow group needs folder_id and name (or give flow_group_id to change a saved one).")
    # a change planned already for this group: the new one goes on from it, in its place
    pending = None if new else planned_save(ctx, "save_flow_group", a.flow_group_id)

    def check(session, user):
        paths = folder_paths(session)
        if new:
            if session.get(Project, a.folder_id) is None \
                    or not perm_service.has_capability(session, user, a.folder_id, Capability.EDIT):
                raise ToolError(f"The user cannot create flow groups in folder {a.folder_id}: see list_folders (can_edit).")
            group, before, folder_id, name = None, FlowGroupDefinition(), a.folder_id, a.name.strip()
            if fg_routes._name_taken(session, folder_id, name):
                raise ToolError(f"There is already a flow group '{name}' in that folder: choose another name, or change that one.")
        else:
            group = _group(session, user, a.flow_group_id)
            if not perm_service.has_capability(session, user, group.project_id, Capability.EDIT):
                raise ToolError(f"The user can read the flow group «{group.name}» but not change it (it needs EDIT on its folder).")
            before, folder_id = definition_of(group), group.project_id
            name = a.name.strip() if a.name is not None else pending["name"] if pending else group.name
            if not name:
                raise ToolError("The flow group needs a name.")
            if fg_routes._name_taken(session, folder_id, name, exclude_id=group.id):
                raise ToolError(f"There is already a flow group '{name}' in that folder.")
        definition = _definition(a.steps, before) if a.steps is not None else None
        if definition is None and pending and pending.get("definition") is not None:
            definition = FlowGroupDefinition.model_validate(pending["definition"])
        if definition is not None:
            fg_routes._checked(session, user, definition, before=None if new else before)
        info: dict[str, Any] = {"folder": paths.get(folder_id, ""), "folder_id": folder_id, "name": name,
                                "definition": definition.model_dump(mode="json") if definition is not None else None}
        if new:
            info["lines"] = _cell_lines(session, user, definition or FlowGroupDefinition())
            info["cells"] = sum(1 for row in (definition.steps if definition else []) for c in row if c is not None)
            info["steps"] = len(definition.steps) if definition else 0
            return info
        description = (a.description.strip() if a.description is not None
                       else pending["description"] if pending and pending.get("description") is not None
                       else group.description)
        lines: list[str] = []
        if definition is not None:
            old, now_ = _cell_lines(session, user, before), _cell_lines(session, user, definition)
            lines += [f"- {x}" for x in old if x not in now_] + [f"+ {x}" for x in now_ if x not in old]
            lines += settings_changes(before, definition)
            new_cells = {fg_routes._cell_key(c) for row in definition.steps for c in row if c is not None} \
                - {fg_routes._cell_key(c) for row in before.steps for c in row if c is not None}
            if new_cells and group.run_schedule and group.run_scheduled_by not in (None, user.id):
                info["note"] = "its schedule, set by someone else, is turned off (it would run cells its author never chose)"
        if name != group.name:
            lines.append(f"~ name: {group.name} → {name}")
        if description != group.description:
            lines.append("~ description changed")
        if not lines:
            raise ToolError(f"The plan would change nothing in the flow group «{group.name}».")
        info.update(old_name=group.name, base=fingerprint(group.definition), diff_lines=lines,
                    description=description if description != group.description else None)
        return info

    info = await in_thread(ctx, check)
    if new:
        words = (f"Create the flow group «{info['name']}» in {info['folder']}: {info['steps']} step"
                 f"{'s' if info['steps'] != 1 else ''}, {info['cells']} cell{'s' if info['cells'] != 1 else ''}")
    else:
        n = len(info["diff_lines"])
        words = f"Change the flow group «{info['old_name']}» in {info['folder']} ({n} change{'s' if n != 1 else ''})"
    if info.get("note"):
        words += "; " + info["note"]
    action: dict[str, Any] = {"kind": "save_flow_group", "id": a.flow_group_id, "folder_id": info["folder_id"],
                              "folder": info["folder"], "name": info["name"], "definition": info["definition"],
                              "words": words + "."}
    if new:
        action.update(description=(a.description or "").strip(), node_lines=info["lines"])
    else:
        action.update(base_hash=info["base"], diff_lines=info["diff_lines"], description=info["description"])
    key = f"save_flow_group:{a.flow_group_id}" if not new else f"save_flow_group:new:{info['folder_id']}:{info['name'].lower()}"
    return plans.add_action(ctx, action, key=key)


@plans.action_kind("save_flow_group")
async def _apply_save_flow_group(ctx: ToolContext, a: dict, results: list[dict]) -> dict:
    from app.routes import flow_groups as fg_routes
    from app.schemas.models import FlowGroupCreate, FlowGroupUpdate

    definition = FlowGroupDefinition.model_validate(a["definition"]) if a.get("definition") is not None else None

    def work(session, user):
        if a.get("id") is None:
            d = fg_routes.create_flow_group(a["folder_id"], FlowGroupCreate(
                name=a["name"], description=a.get("description") or "", definition=definition or FlowGroupDefinition()),
                ctx.request, user=user, session=session)
            return d.id, d.name, "created"
        group = session.get(FlowGroup, a["id"])
        if group is None:
            raise ToolError(f"Flow group {a['id']} no longer exists: nothing saved.")
        if fingerprint(group.definition) != a.get("base_hash"):
            raise ToolError(f"The flow group «{group.name}» was changed in Tabularia after the plan was made: nothing "
                            "saved. Read it again (get_flow_group) and make a new plan.")
        d = fg_routes.update_flow_group(a["id"], FlowGroupUpdate(name=a["name"], description=a.get("description"),
                                                                 definition=definition),
                                        ctx.request, user=user, session=session)
        return d.id, d.name, "changed"

    group_id, name, verb = await in_thread(ctx, work)
    return {"object": {"kind": "flow_group", "id": group_id, "name": name}, "link": flow_group_link(group_id),
            "words": f"Flow group «{name}» {verb}."}


# ── plan: run and schedule ────────────────────────────────────────────────────
def _target(ctx: ToolContext, group_id: Optional[int], planned_action: Optional[int]) -> dict:
    if (group_id is None) == (planned_action is None):
        raise ToolError("Give flow_group_id (a saved group) or planned_action (the n of this plan's plan_flow_group "
                        "that creates it).")
    if planned_action is not None:
        made = planned_creation(ctx, planned_action, "save_flow_group", "flow group")
        return {"id": None, "planned_action": planned_action, "name": made["name"], "folder_id": made["folder_id"],
                "definition": made.get("definition")}
    pending = planned_save(ctx, "save_flow_group", group_id)
    return {"id": group_id, "planned_action": None,
            "definition": pending["definition"] if pending and pending.get("definition") is not None else None}


def _check_target(session, user, t: dict, what: str, need_cells: bool) -> dict:
    if t["id"] is None:
        folder_id, name, definition, group = t["folder_id"], t["name"], t.get("definition"), None
    else:
        group = _group(session, user, t["id"])
        folder_id, name = group.project_id, group.name
        definition = t["definition"] if t.get("definition") is not None else definition_of(group).model_dump(mode="json")
    if not perm_service.has_capability(session, user, folder_id, Capability.RUN):
        raise ToolError(f"The user cannot {what} the flow group «{name}» (it needs RUN on its folder).")
    cells = sum(1 for row in (definition or {}).get("steps") or [] for c in row if c is not None)
    if need_cells and not cells:
        raise ToolError(f"The flow group «{name}» is empty: give it cells first (plan_flow_group).")
    return {**t, "name": name, "cells": cells, "schedule": group.run_schedule if group else None,
            "scheduled_by": group.run_scheduled_by if group else None}


class PlanRunGroupArgs(BaseModel):
    flow_group_id: Optional[int] = Field(None, description="A saved group.")
    planned_action: Optional[int] = Field(None, ge=1, description="A group this plan creates: the n of its plan_flow_group action.")


@tool("plan_run_flow_group", title="Plan running a flow group", args=PlanRunGroupArgs, level="write", reads_data=False)
async def plan_run_flow_group(ctx: ToolContext, a: PlanRunGroupArgs) -> dict:
    """Add to the plan: run a flow group once, now — its steps one after another, each cell checked again with the user's rights when it runs. Nothing runs until the plan is confirmed; then get_run follows the run."""
    target = _target(ctx, a.flow_group_id, a.planned_action)
    t = await in_thread(ctx, lambda session, user: _check_target(session, user, target, "run", True))
    action = {"kind": "run_flow_group", "flow_group_id": t["id"], "planned_action": t["planned_action"],
              "name": t["name"], "words": f"Run the flow group «{t['name']}» now ({t['cells']} cells)."}
    return plans.add_action(ctx, action, key=f"run_flow_group:{t['id'] if t['id'] is not None else 'a%s' % t['planned_action']}")


def _group_of(a: dict, results: list[dict]) -> int:
    if a.get("flow_group_id") is not None:
        return a["flow_group_id"]
    return made_by(results, a["planned_action"], "flow group")


@plans.action_kind("run_flow_group")
async def _apply_run_flow_group(ctx: ToolContext, a: dict, results: list[dict]) -> dict:
    from app.routes import flow_groups as fg_routes

    group_id = _group_of(a, results)
    res = await in_thread(ctx, lambda session, user: fg_routes.run_flow_group_now(group_id, ctx.request, user=user,
                                                                                    session=session))
    return {"object": {"kind": "run", "id": res["run_id"], "flow_group_id": group_id}, "link": flow_group_link(group_id),
            "words": f"Run {res['run_id']} of «{a['name']}» queued."}


@plans.scheduler("flow_group")
async def _plan_group_schedule(ctx: ToolContext, a: Any) -> dict:
    flows_only(a)
    cron = checked_cron(a.cron)
    target = _target(ctx, a.id, a.planned_action)
    t = await in_thread(ctx, lambda session, user: _check_target(session, user, target, "schedule", bool(cron)))
    if cron:
        words = f"Schedule the flow group «{t['name']}»: {cron_words(cron)}"
        if t.get("schedule") and t.get("scheduled_by") not in (None, ctx.user.id):
            words += f", replacing `{t['schedule']}` set by someone else: the runs will carry this user's authority"
    else:
        words = f"Turn off the schedule of the flow group «{t['name']}»"
    action = {"kind": "schedule_flow_group", "flow_group_id": t["id"], "planned_action": t["planned_action"],
              "name": t["name"], "cron": cron or None, "timezone": get_settings().app.timezone, "words": words + "."}
    return plans.add_action(ctx, action,
                            key=f"schedule_flow_group:{t['id'] if t['id'] is not None else 'a%s' % t['planned_action']}")


@plans.action_kind("schedule_flow_group")
async def _apply_schedule_flow_group(ctx: ToolContext, a: dict, results: list[dict]) -> dict:
    from app.routes import flow_groups as fg_routes
    from app.schemas.models import FlowGroupScheduleUpdate

    group_id = _group_of(a, results)

    def work(session, user):
        d = fg_routes.set_flow_group_schedule(group_id, FlowGroupScheduleUpdate(cron=a.get("cron") or ""), ctx.request,
                                              user=user, session=session)
        return d.name, d.run_schedule, d.next_run_at

    name, schedule, nxt = await in_thread(ctx, work)
    words = (f"«{name}» scheduled: `{schedule}` (next run {nxt:%Y-%m-%d %H:%M} UTC)." if schedule and nxt
             else f"The schedule of «{name}» is off.")
    return {"object": {"kind": "flow_group", "id": group_id, "name": name}, "link": flow_group_link(group_id),
            "words": words}

