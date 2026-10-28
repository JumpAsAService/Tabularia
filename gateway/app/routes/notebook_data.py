"""What a notebook's pod reads: the datasources, on demand. docs/specs/notebooks.md
(«Data»); the module that calls these in the pod is app/notebook_pod/tabularia.py.

- Who: the pod, with its notebook token (`Authorization: Bearer nbk_…`), which holds only
  while its session lives, only from the pod's own address and only here — no other
  route takes it, and these take nothing else. The rights are those of the session's person: the author of the version
  the app runs, or whoever is editing.
- What: the datasources that person may read (VIEW on their folder), checked again at
  every call: a right taken away stops the next read.
- How: a link to the datasource's own parquet, valid for a few minutes, made by the
  engine (it holds the storage's keys, the gateway does not); the pod downloads it.
  Nothing is copied. Every read is audited (`notebook.read`).
- Lineage: the first read of a datasource in a session is recorded
  (services/notebook_reads.py), for the Lineage page, and told to OpenLineage as an
  input of the session's run.
- Reports: the ones that person sees (VIEW on their folder), rendered with their rights
  as the API renders them — every dataset checked, the parameters converted — waited for
  and sent back as the file (`tabularia.report()`); audited as `report.render` from the
  notebook.
"""
from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any, Literal, Optional, Union

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlmodel import Session, select

from app.core.config import get_settings
from app.core.engine_client import get_engine_client
from app.core.routing import ReleasingRoute
from app.db.session import get_session
from app.models import Datasource, Notebook, NotebookSession, User
from app.services import audit, notebook_reads, openlineage, permissions as perm_service
from app.services import notebook_runtime as runtime
from app.services.objects import ensure_can_read_keys, ensure_reads_pinned

router = APIRouter(route_class=ReleasingRoute, tags=["notebooks"], include_in_schema=False)
logger = logging.getLogger(__name__)

LINK_SECONDS = 600  # how long a download link holds

_ended = HTTPException(status_code=401, detail="This notebook's session has ended: open the notebook again",
                       headers={"WWW-Authenticate": "Bearer"})


class Caller:
    def __init__(self, row: NotebookSession, user: User, notebook: Notebook):
        self.row, self.user, self.notebook = row, user, notebook


def notebook_caller(request: Request, session: Session = Depends(get_session)) -> Caller:
    """The pod calling, by its notebook token: its session still live, its person still
    active, its notebook still there."""
    scheme, _, token = (request.headers.get("authorization") or "").partition(" ")
    token = token.strip()
    if scheme.lower() != "bearer" or not token.startswith(runtime.TOKEN_PREFIX):
        raise _ended
    row = session.exec(select(NotebookSession).where(NotebookSession.token_fp == runtime.fingerprint(token),
                                                     NotebookSession.status.in_(runtime.LIVE))).first()  # type: ignore[attr-defined]
    user = session.get(User, row.user_id) if row is not None else None
    notebook = session.get(Notebook, row.notebook_id) if row is not None else None
    if row is None or notebook is None or user is None:
        raise _ended
    # from its own pod only: a token the notebook's code printed in the app, for its
    # viewers to see, is worth nothing anywhere else (the pods call the gateway's
    # Service directly, so the address is the pod's). The pods never come through a
    # proxy, so a forwarding header is refused outright: uvicorn trusts them from the
    # private ranges and would otherwise take the address from the header, letting any
    # pod of the cluster claim to be the notebook's.
    if get_settings().notebooks.pod_ip_check:
        forwarded = any(h in request.headers for h in ("x-forwarded-for", "forwarded", "x-real-ip"))
        if forwarded or not row.pod_ip or (request.client.host if request.client else None) != row.pod_ip:
            raise HTTPException(status_code=401, detail="This notebook token holds only from its own pod",
                                headers={"WWW-Authenticate": "Bearer"})
    if not user.is_active:
        raise HTTPException(status_code=403, detail=f"The account this notebook reads with, {user.email}, is deactivated")
    return Caller(row, user, notebook)


def _folders(session: Session) -> dict[int, str]:
    """Every folder's path, «Parent/Child»."""
    projects = perm_service._all_projects(session)
    parents = {i: p.parent_id for i, p in projects.items()}
    return {i: "/".join(projects[a].name for a in reversed(perm_service.ancestor_ids(parents, i)) if a in projects)
            for i in projects}


def _readable(session: Session, user: User) -> list[Datasource]:
    readable = perm_service.readable_project_ids(session, user)
    if not readable:
        return []
    return list(session.exec(select(Datasource).where(Datasource.project_id.in_(readable))  # type: ignore[attr-defined]
                             .order_by(Datasource.name)).all())


def _columns(ds: Datasource) -> list[str]:
    import json

    try:
        return [c.get("name") for c in json.loads(ds.columns or "[]") if isinstance(c, dict) and c.get("name")]
    except ValueError:
        return []


@router.get("/notebook-data/datasources")
def list_datasources(caller: Caller = Depends(notebook_caller), session: Session = Depends(get_session)) -> list[dict]:
    """What the notebook may read: id, name, folder, path, rows, columns."""
    folders = _folders(session)
    out = []
    for ds in _readable(session, caller.user):
        folder = folders.get(ds.project_id, "")
        out.append({"id": ds.id, "name": ds.name, "folder": folder, "path": f"{folder}/{ds.name}" if folder else ds.name,
                    "rows": ds.rows, "columns": _columns(ds)})
    return out


class ReadIn(BaseModel):
    datasource: Union[int, str]


def _norm(path: str) -> str:
    return "/".join(p.strip() for p in path.strip().strip("/").split("/"))


def _find(session: Session, user: User, wanted: Union[int, str]) -> Datasource:
    """By id, by path («Folder/Sub/Name») or by a name only one readable datasource has."""
    return _pick(session, _readable(session, user), wanted, "datasource")


def _pick(session: Session, readable: list, wanted: Union[int, str], noun: str):
    """One of `readable` (datasources, reports) by id, by path or by a name only one has."""
    if isinstance(wanted, int) or (isinstance(wanted, str) and wanted.strip().isdigit()):
        found = next((d for d in readable if d.id == int(wanted)), None)
        if found is None:
            raise HTTPException(status_code=404, detail=f"There is no {noun} {wanted} this notebook may read")
        return found
    name = _norm(wanted)
    if not name:
        raise HTTPException(status_code=422, detail=f"Name the {noun}: «Folder/Name», or its id")
    if "/" in name:
        folders = _folders(session)
        for d in readable:
            if _norm(f"{folders.get(d.project_id, '')}/{d.name}") == name:
                return d
        raise HTTPException(status_code=404, detail=f"There is no {noun} «{name}» this notebook may read")
    same = [d for d in readable if d.name.strip() == name]
    if len(same) == 1:
        return same[0]
    if not same:
        raise HTTPException(status_code=404, detail=f"There is no {noun} «{name}» this notebook may read")
    folders = _folders(session)
    paths = ", ".join(sorted(f"{folders.get(d.project_id, '')}/{d.name}" for d in same)[:5])
    raise HTTPException(status_code=409, detail=f"Several {noun}s are called «{name}»: name its folder too ({paths})")


@router.post("/notebook-data/read")
async def read_datasource(body: ReadIn, request: Request, caller: Caller = Depends(notebook_caller),
                          session: Session = Depends(get_session)) -> dict:
    """A link to download the datasource's parquet, valid for a few minutes."""
    ds = _find(session, caller.user, body.datasource)
    # the same fences as every read of the data plane: the engine's bucket, a managed key,
    # one this person may read
    ensure_reads_pinned(session, caller.user, {"bucket": ds.bucket, "key": ds.key}, get_settings().engine.bucket)
    ensure_can_read_keys(session, caller.user, [ds.key])
    found = {"id": ds.id, "name": ds.name, "rows": ds.rows}
    target = {"bucket": ds.bucket, "key": ds.key}
    notebook, row, user = caller.notebook, caller.row, caller.user
    detail = {"notebook": notebook.id, "notebook_name": notebook.name, "mode": row.mode}
    # the session as it is now: read again after the engine answers, its row may be gone
    pod = SimpleNamespace(notebook_id=row.notebook_id, mode=row.mode, version=row.version, job_name=row.job_name,
                          started_at=row.started_at)
    session.close()  # nothing more to read while the engine answers
    r = await get_engine_client().get("/files/presigned", params={**target, "expires": LINK_SECONDS})
    if r.status_code >= 400:
        try:
            why: Optional[str] = r.json().get("detail")
        except ValueError:
            why = None
        raise HTTPException(status_code=404 if r.status_code == 404 else 502,
                            detail=f"The data of «{ds.name}» cannot be read: {why or f'the engine answered {r.status_code}'}")
    audit.record_audit(session, actor=user, action=audit.NOTEBOOK_READ, target_type="datasource",
                       target_id=found["id"], target_label=found["name"], request=request, detail=detail)
    try:
        first = notebook_reads.record(session, pod.notebook_id, found["id"], pod.job_name, pod.mode)
    except Exception as e:  # noqa: BLE001 — the read is done: its lineage must not undo it
        session.rollback()
        logger.warning("notebooks: the read of datasource %s by notebook %s not recorded (%s)", found["id"],
                       pod.notebook_id, type(e).__name__)
        first = False
    if first:
        openlineage.notebook_session(session, pod, "RUNNING", datasource_id=found["id"])
    return {"url": r.json()["url"], **found}


# ── reports (docs/specs/reports.md, «API») ───────────────────────────────────
def _reports(session: Session, user: User) -> list:
    from app.services import reports

    stmt = reports.visible_query(session, user)
    return list(session.exec(stmt).all()) if stmt is not None else []


@router.get("/notebook-data/reports")
def list_reports(caller: Caller = Depends(notebook_caller), session: Session = Depends(get_session)) -> list[dict]:
    """The reports the notebook may render: id, name, folder, path, parameters."""
    from app.services import reports

    folders = _folders(session)
    found = _reports(session, caller.user)
    contents = reports.current_contents(session, found)
    out = []
    for r in found:
        folder = folders.get(r.project_id, "")
        params = [{"name": p.get("name"), "type": p.get("type"), "multi": bool(p.get("multi")),
                   "required": bool(p.get("required")), "default": p.get("default")}
                  for p in contents[r.id].get("parameters") or [] if isinstance(p, dict)]
        out.append({"id": r.id, "name": r.name, "folder": folder, "path": f"{folder}/{r.name}" if folder else r.name,
                    "parameters": params})
    return out


class ReportIn(BaseModel):
    report: Union[int, str]
    format: Literal["pdf", "xlsx", "html"] = "pdf"
    # a text, a number, true/false, or a list for a parameter that takes several
    params: dict[str, Any] = Field(default_factory=dict)


@router.post("/notebook-data/report")
async def render_report(body: ReportIn, request: Request, caller: Caller = Depends(notebook_caller),
                        session: Session = Depends(get_session)):
    """The report rendered with these parameters and the rights the notebook carries, as
    the API renders it (GET /reports/{id}/render): waited for up to
    REPORTS__API_WAIT_SECONDS, sent back as the file. Audited as `report.render` from
    the notebook."""
    from app.routes.proxy import _forward, _release_db, export_owner
    from app.routes.reports import _wait_for_file
    from app.services import reports

    user = caller.user
    report = _pick(session, _reports(session, user), body.report, "report")
    extra = {"notebook": caller.notebook.id, "notebook_name": caller.notebook.name, "mode": caller.row.mode}
    started = await reports.render(session, user, report, reports.content_of(session, report), body.format,
                                   reports.given_params(body.params), origin="notebook", request=request,
                                   audit_success=False, extra=extra)
    owner = export_owner(user)
    _release_db(session, user)  # nothing holds a database connection while the engine works
    try:
        await _wait_for_file(started.task_id, owner)
    except HTTPException as e:
        reports._audit_render(session, user, report, request, "failure", {**started.detail, "error": str(e.detail)[:500]})
        raise
    reports._audit_render(session, user, report, request, "success", started.detail)
    return await _forward(request, "GET", f"/tasks/exports/{started.task_id}/file", session=session, user=user,
                          params={"owner": owner})
