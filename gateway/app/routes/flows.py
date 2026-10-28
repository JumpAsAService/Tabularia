"""Saved flows (the editor's DAGs), held in the projects.

Permissions (inherited down the tree as always):
- list/open: VIEW on the flow's project;
- create/save/delete: EDIT;
- move: EDIT on both the origin and the destination project.
"""
import json
import unicodedata
from datetime import datetime, timezone
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, status, Request, Response
from sqlalchemy import func, or_
from sqlmodel import Session, select

from app.core.config import get_settings
from app.db.session import get_session
from app.deps.auth import get_current_user
from app.deps.permissions import ensure_can
from app.models import Connection, Datasource, Flow, FlowVersion, Project, Run, User
from app.services.dbt_export import DbtOptions
from app.core.language import request_language
from app.models.permission import Capability
from app.services import ai_chats, audit, flow_presence, openlineage, run_history
from pydantic import BaseModel, Field
from app.schemas.models import UtcDateTime
from app.schemas.models import (
    FlowCreate,
    FlowDetail,
    FlowOut,
    FlowScheduleUpdate,
    FlowStatsOut,
    FlowUpdate,
    FlowVersionOut,
    Page,
)
from app.services.engine_policy import KNOWN_ENGINES, disabled_engines
from app.services.pagination import paginate
from app.services import permissions as perm_service
from app.services import flow_access, registry_access, schedule_authority, script_nodes
from app.services.objects import collect_storage_keys, ensure_can_read_keys
from app.services.schedule import ScheduleError, next_fire, validate_schedule
from app.services.copy_names import free_copy_name
from app.core.routing import ReleasingRoute

router = APIRouter(route_class=ReleasingRoute, tags=["flows"])

# the engines SELECTABLE at creation (in sync with the engine's catalog: only those
# `available=True`).
# `clickhouse` (external server) is optional on the engine: when not configured, the
# run fails with a clear error from the engine.
_AVAILABLE_ENGINES = set(KNOWN_ENGINES)


def _validate_engine(engine: str | None, session: Session | None = None) -> str:
    """The engine exists in the catalog and — when the session is passed — is among those
    the administrator still allows to choose.

    `session` is optional on purpose: the check on the CATALOG is pure and stays usable
    without a database (the validation tests use it). The administrative criterion
    applies where there is a session, that is in all the routes — creation, engine
    change and production engine, the three points where an engine is chosen."""
    e = (engine or "polars").strip().lower()
    if e not in _AVAILABLE_ENGINES:
        raise HTTPException(
            status_code=422,
            detail=f"Engine not available: '{engine}'. Choose from: {', '.join(sorted(_AVAILABLE_ENGINES))}.",
        )
    forbidden = disabled_engines(session) if session is not None else set()
    if e in forbidden:
        raise HTTPException(
            status_code=422,
            detail=f"The engine '{e}' has been disabled by the administrator. "
            f"Choose from: {', '.join(sorted(_AVAILABLE_ENGINES - forbidden))}.",
        )
    return e


def _validate_production_engine(engine: str | None, session: Session | None = None) -> str | None:
    """Production engine: empty = None (the same as development)."""
    if engine is None or not engine.strip():
        return None
    return _validate_engine(engine, session)


# nodes that give "something to run": Output, or the control nodes (refresh of a
# datasource, run of another flow)
_RUNNABLE_NODE_TYPES = {"output", "refresh", "runflow", "script", "container"}


def _has_runnable_nodes(definition: str | None) -> bool:
    try:
        parsed = json.loads(definition or "{}")
    except json.JSONDecodeError:
        return False
    return any((n or {}).get("type") in _RUNNABLE_NODE_TYPES for n in parsed.get("nodes") or [])


def _authorize_definition_keys(session: Session, user: User, definition: str | None) -> None:
    """The storage keys named in a flow's definition must be READABLE by whoever saves.
    Without this check the per-object read rule (a key in a readable flow is readable)
    would authorize itself: writing someone else's key in one's own flow was enough to
    gain access to it.
    """
    if not definition:
        return
    try:
        parsed = json.loads(definition)
    except json.JSONDecodeError:
        raise HTTPException(status_code=422, detail="The flow definition is not valid JSON")
    ensure_can_read_keys(session, user, collect_storage_keys(parsed))
    # A node can name a datasource by id alone: the run resolves the id to its file
    # with the authority of whoever runs it, which for a schedule is its author.
    # Without this check, anyone with EDIT on a folder could add a datasource they
    # cannot read to someone else's scheduled flow, and the next scheduled run would
    # copy it into a folder they can read.
    ids = _datasource_ids(parsed)
    if ids:
        readable = perm_service.readable_project_ids(session, user)
        hidden = [ds_id for ds_id, project_id in session.exec(
            select(Datasource.id, Datasource.project_id).where(Datasource.id.in_(ids))
        ).all() if project_id not in readable]
        if hidden:
            raise HTTPException(status_code=403, detail="The flow reads a datasource you cannot read")


def _datasource_ids(payload) -> set[int]:
    """Every `datasourceId` in a flow definition, however deep (nested operations too)."""
    found: set[int] = set()

    def walk(node) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "datasourceId" and isinstance(value, (int, str)) and str(value).isdigit():
                    found.add(int(value))
                else:
                    walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(payload)
    return found


@router.get("/flows", response_model=list[FlowOut])
def list_all_flows(user: User = Depends(get_current_user), session: Session = Depends(get_session)):
    """All the flows in the READABLE projects (for the global Flows page).

    `readable_project_ids`, not `visible_project_ids`: the ancestors shown for
    navigation do not give access to their content.
    """
    readable = perm_service.readable_project_ids(session, user)
    if not readable:
        return []
    return _with_locks(session, user, session.exec(
        select(Flow).where(Flow.project_id.in_(readable)).order_by(Flow.name)
    ).all())


# NB: registered BEFORE /flows/{flow_id} so that "search" is not taken for an id
@router.get("/flows/search", response_model=Page[FlowOut])
def search_flows(
    q: str | None = Query(None, description="searches name/description, over the WHOLE dataset"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """Flows in the readable projects, PAGINATED, with a server-side search over the whole
    dataset. For the global Flows page."""
    readable = perm_service.readable_project_ids(session, user)
    if not readable:
        return Page(items=[], total=0)
    base = select(Flow).where(Flow.project_id.in_(readable))
    if q:
        like = f"%{q}%"
        base = base.where(or_(Flow.name.ilike(like), Flow.description.ilike(like)))
    flows, total = paginate(session, base, Flow.name, limit, offset)
    return Page(items=_with_locks(session, user, flows, _with_owner_names(session, flows)), total=total)


def _with_owner_names(session: Session, flows: list[Flow]) -> list[FlowOut]:
    """Serializes the flows resolving `owner_name` (the name of whoever created them) and
    the LAST run's state, each with one aggregated query over the page's flows."""
    owner_ids = {f.owner_id for f in flows if f.owner_id is not None}
    names: dict[int, str] = {}
    if owner_ids:
        for uid, full_name, email in session.exec(
            select(User.id, User.full_name, User.email).where(User.id.in_(owner_ids))
        ).all():
            names[uid] = full_name or email

    # the latest run of each flow on the page, from the index: reading all their runs
    # sorted the whole history of the page's flows on every page open
    last_run = run_history.latest_flow_runs(session, (f.id for f in flows if f.id is not None))

    out: list[FlowOut] = []
    for f in flows:
        item = FlowOut.model_validate(f, from_attributes=True)
        item.owner_name = names.get(f.owner_id) if f.owner_id is not None else None
        lr = last_run.get(f.id)
        if lr:
            item.last_run_status, item.last_run_at = lr
        out.append(item)
    return out


def _with_locks(session: Session, user: User, flows: list[Flow], out: list[FlowOut] | None = None) -> list[FlowOut]:
    """The list items, each saying whether this person can open it (a flow that runs
    images of a container registry they have no access to is listed, greyed out)."""
    items = out if out is not None else [FlowOut.model_validate(f, from_attributes=True) for f in flows]
    for flow, item in zip(flows, items):
        item.locked_reason = flow_access.blocked(session, user, flow.definition)
    return items


def _get_flow(session: Session, flow_id: int) -> Flow:
    flow = session.get(Flow, flow_id)
    if flow is None:
        raise HTTPException(status_code=404, detail="Flow not found")
    return flow


@router.get("/projects/{project_id}/flows", response_model=list[FlowOut])
def list_flows(project_id: int, user: User = Depends(get_current_user), session: Session = Depends(get_session)):
    if session.get(Project, project_id) is None:
        raise HTTPException(status_code=404, detail="Folder not found")
    ensure_can(session, user, project_id, Capability.VIEW)
    return _with_locks(session, user, session.exec(
        select(Flow).where(Flow.project_id == project_id).order_by(Flow.name)).all())


def _check_flow_name(name: str) -> None:
    """A flow's name ends up in the Outputs' templates ({flow}: a subject, a file
    name): no control character (a line break, a tab) in a new or renamed one."""
    if any(ord(c) < 32 or ord(c) == 127 for c in name or ""):
        raise HTTPException(status_code=422, detail="The flow's name cannot hold a line break, a tab or another control character")


@router.post("/projects/{project_id}/flows", response_model=FlowDetail, status_code=status.HTTP_201_CREATED)
def create_flow(
    project_id: int,
    body: FlowCreate,
    request: Request,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    if session.get(Project, project_id) is None:
        raise HTTPException(status_code=404, detail="Folder not found")
    ensure_can(session, user, project_id, Capability.EDIT)
    _check_flow_name(body.name)
    _authorize_definition_keys(session, user, body.definition)
    script_nodes.check_definition(session, user, None, body.definition)
    registry_access.check_definition(session, user, None, body.definition)
    flow = Flow(
        name=body.name,
        description=body.description,
        definition=body.definition,
        project_id=project_id,
        owner_id=user.id,
        engine=_validate_engine(body.engine, session),
        production_engine=_validate_production_engine(body.production_engine, session),
    )
    session.add(flow)
    session.commit()
    session.refresh(flow)
    _snapshot_version(session, flow, user, note="created")
    audit.record_audit(
        session, actor=user, action=audit.FLOW_CREATE, target_type="flow",
        target_id=flow.id, target_label=flow.name,
        detail={"project_id": project_id, "engine": flow.engine}, request=request,
    )
    return flow


@router.post("/flows/{flow_id}/duplicate", response_model=FlowDetail, status_code=status.HTTP_201_CREATED)
def duplicate_flow(
    flow_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """A copy of the flow in its folder, as «name (i)», under the same rules as a new
    flow. The definition is copied as it is, Output nodes included: whoever made the
    copy renames its outputs before running it. Schedule and failure alerts stay
    with the original."""
    src = _get_flow(session, flow_id)
    ensure_can(session, user, src.project_id, Capability.EDIT)
    flow_access.ensure_can_open(session, user, src.definition)
    _authorize_definition_keys(session, user, src.definition)
    script_nodes.check_definition(session, user, None, src.definition)
    registry_access.check_definition(session, user, None, src.definition)
    flow = Flow(
        name=free_copy_name(session, Flow, src.project_id, src.name),
        description=src.description,
        definition=src.definition,
        project_id=src.project_id,
        owner_id=user.id,
        engine=_validate_engine(src.engine, session),
        production_engine=_validate_production_engine(src.production_engine, session),
    )
    session.add(flow)
    session.commit()
    session.refresh(flow)
    _snapshot_version(session, flow, user, note=f"copy of '{src.name}'")
    audit.record_audit(
        session, actor=user, action=audit.FLOW_CREATE, target_type="flow",
        target_id=flow.id, target_label=flow.name,
        detail={"project_id": flow.project_id, "engine": flow.engine, "duplicate_of": src.id}, request=request,
    )
    return flow


def _snapshot_version(session: Session, flow: Flow, user: User, note: str) -> None:
    """Auto-version: records the flow's CURRENT definition as a new version. Skips when the
    same as the last (a save that does not change the DAG makes no noise in the
    history). The flow is read again with its row locked until the commit: two saves
    at once take the next numbers in turn (not the same one, a 500 on the second), and
    the history ends with the definition the flow holds."""
    session.refresh(flow, with_for_update=True)
    last = session.exec(
        select(FlowVersion)
        .where(FlowVersion.flow_id == flow.id)
        .order_by(FlowVersion.version.desc())
    ).first()
    if last is not None and last.definition == flow.definition:
        return
    session.add(
        FlowVersion(
            flow_id=flow.id,
            version=(last.version + 1 if last else 1),
            definition=flow.definition,
            note=note,
            created_by=user.id,
        )
    )
    session.commit()


def _flow_for_dbt(session: Session, user: User, flow_id: int) -> Flow:
    """Administrators only (effective: personal flag or admin group; NOT the observers).
    The project is self-contained: it carries the upstream flows, the queries' text
    and, as seeds, the DATA of the sources that are not in a database — of folders whoever
    sees the flow does not see too. It is a tool of the data team, not of the common
    user (the user's choice, 2026-10-10)."""
    flow = _get_flow(session, flow_id)
    ensure_can(session, user, flow.project_id, Capability.VIEW)
    if not perm_service.is_admin(session, user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="The dbt export is for administrators only: the project carries data and definitions from other folders too",
        )
    return flow


@router.get("/flows/{flow_id}/export/dbt")
async def export_flow_dbt(
    flow_id: int,
    request: Request,
    target: str = Query("duckdb", pattern="^(duckdb|native|clickhouse)$"),
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """Exports the flow as a dbt project (zip). `target=duckdb` (federation), `native`
    (the origin warehouse, SQL translated through sqlglot) or `clickhouse` (the
    customer's ClickHouse, which reads the Postgres/MySQL sources live). The gateway
    resolves flow→operations and sources→DB tables, the engine compiles and zips.
    Without options: the complete project with the default values (see POST)."""
    return await _export_dbt(_flow_for_dbt(session, user, flow_id), request, user, session, target, None)


@router.post("/flows/{flow_id}/export/dbt")
async def export_flow_dbt_with_options(
    flow_id: int,
    options: DbtOptions,
    request: Request,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """The dbt export with the download dialog's choices: target, complete project or
    folder for an existing project, names, sources declared already, schema,
    materializations, tests, email (see `DbtOptions`)."""
    return await _export_dbt(_flow_for_dbt(session, user, flow_id), request, user, session, options.target, options)


@router.get("/flows/{flow_id}/export/dbt/plan")
def export_flow_dbt_plan(
    flow_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """What the download dialog needs: the possible targets (or why not, in the language of
    whoever asks), the sources of each with the default name, the outputs with the
    allowed materializations."""
    from app.services.dbt_export import export_plan

    from app.services.dbt_ai import ai_model, version_texts

    flow = _flow_for_dbt(session, user, flow_id)
    piano = export_plan(session, flow, get_settings().engine.bucket, request_language(request))
    model = ai_model(session)
    # `stored`: what the AI wrote for this version, which a re-export reuses without a call even with
    # the AI switched off: the dialog can still offer it, and the same version gives the same zip
    piano["ai"] = {"available": model is not None, "model": model, "stored": version_texts(session, flow)}
    return piano


async def _export_dbt(flow: Flow, request: Request, user: User, session: Session, target: str, options: DbtOptions | None):
    from app.services import dbt_ai
    from app.services.dbt_export import DbtExportError, _build, engine_export
    from app.services.dbt_messages import engine_message, error_message, translate

    language = request_language(request)
    deadline = dbt_ai.new_deadline()   # one budget for every AI call of this export: the ingress cuts it at 300 s
    try:
        payload, project = _build(session, flow, get_settings().engine.bucket, target, user.email, options)
        if options is not None and options.ai_descriptions:
            # descriptions where missing, written by the AI and remembered per version (services/dbt_ai.py)
            await dbt_ai.describe(session, user, flow, sorted(project.flows_seen), payload, deadline)
    except DbtExportError as e:
        raise HTTPException(status_code=422, detail=error_message(e, language))

    proposals: dict[str, str] = {}   # model → the key of the translation the AI proposed
    writers: dict[str, str] = {}  # model → the AI model that wrote its translation
    attempts: dict[str, int] = {}  # model → proposals asked for
    requests: dict[str, dict] = {}  # model → what the engine said the translation needs
    while True:
        if proposals:
            # checking an AI proposal is one more engine call: past the budget, the ingress would cut the
            # request with a bare 504 after the AI used most of its time
            try:
                dbt_ai.check_expiry(deadline)
            except DbtExportError as e:
                raise HTTPException(status_code=422, detail=error_message(e, language))
        try:
            resp = await engine_export(payload)   # bounded: ENGINE__DBT_EXPORT_TIMEOUT_SECONDS
        except DbtExportError as e:
            # the engine, upstream of the gateway, did not answer in time (504) or cannot be reached (502)
            raise HTTPException(status_code=504 if e.code == "engine_timeout" else 502, detail=error_message(e, language))
        if resp.status_code == 200:
            break
        try:
            raw = resp.json().get("detail")
        except Exception:
            raw = None
        prm = (raw.get("params") or {}) if isinstance(raw, dict) else {}
        model = prm.get("model")
        if (options is not None and options.ai_translations and isinstance(raw, dict) and raw.get("code") in dbt_ai.TRANSLATABLE
                and model and model not in proposals and len(proposals) < dbt_ai.MAX_TRANSLATIONS):
            # the engine does not translate this model: a proposal from the AI, which the engine then checks
            try:
                proposal, proposals[model], writers[model] = await dbt_ai.translate(session, user, flow, prm, deadline=deadline)
            except DbtExportError as e:
                raise HTTPException(status_code=422, detail=error_message(e, language))
            attempts[model] = 1
            requests[model] = prm
            payload.setdefault("overrides", {})[model] = proposal
            continue
        if isinstance(raw, dict) and raw.get("code") == "ai_translation_invalid" and prm.get("model") in proposals:
            model = prm["model"]
            dbt_ai.forget(session, flow, proposals[model], payload["overrides"][model])   # discarded: not reused
            if attempts.get(model, 0) < dbt_ai.ATTEMPTS:
                # a second attempt, with the reason in front (in the AI's language: English)
                try:
                    proposal, proposals[model], writers[model] = await dbt_ai.translate(
                        session, user, flow, requests[model],
                        discarded=(payload["overrides"][model], engine_message(raw, "en")), deadline=deadline)
                except DbtExportError as e:
                    raise HTTPException(status_code=422, detail=error_message(e, language))
                attempts[model] += 1
                payload["overrides"][model] = proposal
                continue
        detail = engine_message(raw, language) if raw is not None else translate("internal", {"detail": f"engine {resp.status_code}"}, language)
        raise HTTPException(status_code=resp.status_code if resp.status_code >= 400 else 502, detail=detail)

    fname = "".join(c if (c.isalnum() or c in "-_") else "_" for c in flow.name).strip("_") or "flow"
    # the dialog's own name when it cannot read this header: `_folder` for the folder package
    queue_ = f"_dbt_{target}{'_folder' if options is not None and options.package == 'folder' else ''}.zip"
    # the response first, the audit row after: a download is recorded only once it can be served
    answer = Response(
        content=resp.content, media_type="application/zip",
        headers={"Content-Disposition": _content_disposition(fname, queue_)},
    )
    # the AI models that wrote what the zip carries, not the one configured now
    written_by = {m for m in ((payload.get("ai") or {}).get("model") or "").split(", ") if m} | {m for m in writers.values() if m}
    audit.record_audit(
        session, actor=user, action=audit.EXPORT_DOWNLOAD,
        target_type="flow", target_id=flow.id, target_label=flow.name,
        detail={"format": "dbt", "target": target, **({"options": options.model_dump(by_alias=True)} if options else {}),
                **({"ai": {"model": ", ".join(sorted(written_by)) or None, "translated": sorted(proposals)}} if (payload.get("ai") or proposals) else {})},
        request=request,
    )
    return answer


def _content_disposition(base: str, queue_: str) -> str:
    """`attachment` with the file name twice: in ASCII for old clients (accents
    transliterated, other letters `_`), and as RFC 5987 `filename*` in UTF-8 with every
    letter. HTTP headers are Latin-1: a raw «Ł» or «销» there was a 500."""
    ascii_ = "".join(c if c.isascii() else ("" if unicodedata.combining(c) else "_")
                     for c in unicodedata.normalize("NFKD", base)).strip("_") or "flow"
    return f"attachment; filename=\"{ascii_}{queue_}\"; filename*=UTF-8''{quote(base + queue_, safe='')}"


# ── Presence in the editor ─────────────────────────────────────────────────────
class PresenceBeat(BaseModel):
    # the id of the editor's INSTANCE (one per tab), generated by the browser
    instance: str = Field(min_length=4, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")


class PresenceOther(BaseModel):
    user_id: int
    email: str
    full_name: str = ""
    since: UtcDateTime


class PresenceOut(BaseModel):
    others: list[PresenceOther]
    heartbeat_seconds: int


@router.post("/flows/{flow_id}/presence", response_model=PresenceOut)
def flow_presence_beat(
    flow_id: int,
    body: PresenceBeat,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """"I have this flow open": the editor's heartbeat. It answers with who ELSE has it
    open. It blocks and prevents nothing — it warns, because whoever saves last
    overwrites the other. Whoever sees the flow sees who is looking at it too; nobody
    else."""
    flow = _get_flow(session, flow_id)
    ensure_can(session, user, flow.project_id, Capability.VIEW)
    others = flow_presence.beat(session, flow_id, body.instance, user.id, user.email, user.full_name or "")
    if perm_service.is_observer_only(session, user):
        # an observer reads people masked everywhere else (audit, users): here too
        from app.services.masking import mask_email

        others = [{**o, "email": mask_email(o["email"]), "full_name": ""} for o in others]
    return PresenceOut(
        others=[PresenceOther(**{**o, "since": datetime.fromtimestamp(o["since"], timezone.utc)}) for o in others],
        heartbeat_seconds=flow_presence.HEARTBEAT_SECONDS,
    )


@router.delete("/flows/{flow_id}/presence/{instance}", status_code=status.HTTP_204_NO_CONTENT)
def flow_presence_leave(
    flow_id: int,
    instance: str,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    flow = _get_flow(session, flow_id)
    ensure_can(session, user, flow.project_id, Capability.VIEW)
    flow_presence.leave(session, flow_id, instance)


@router.get("/flows/{flow_id}", response_model=FlowDetail)
def get_flow(flow_id: int, user: User = Depends(get_current_user), session: Session = Depends(get_session)):
    flow = _get_flow(session, flow_id)
    ensure_can(session, user, flow.project_id, Capability.VIEW)
    # a flow that runs images of a registry this person may not use does not open
    flow_access.ensure_can_open(session, user, flow.definition)
    return flow


def _guard_someone_elses_schedule(flow: Flow, user: User, new_definition: str | None,
                                   moved: bool) -> dict | None:
    """A schedule carries its author's authority. An edit by someone else that makes
    the flow reach more (datasources, files, flows it runs, connections, publish
    folders), or a move to another folder, would spend that authority on something
    the author never chose: the schedule is switched off. Returns the audit detail
    for the caller to record once the change is committed (`_record_schedule_off`)."""
    if not flow.run_schedule or flow.run_scheduled_by in (None, user.id):
        return None
    reason = None
    if moved:
        reason = "the flow was moved to another folder"
    elif new_definition is not None:
        new_ones = schedule_authority.widens(flow.definition, new_definition)
        if new_ones:
            reason = f"the definition now reads or writes more ({len(new_ones)} new reference(s))"
    if reason is None:
        return None
    author = flow.run_scheduled_by
    flow.run_schedule = None
    flow.run_scheduled_by = None
    flow.next_run_at = None
    return {"turned_off": True, "reason": reason, "author": author}


def _record_schedule_off(session: Session, user: User, flow: Flow, detail: dict | None, request) -> None:
    if detail:
        audit.record_audit(session, actor=user, action=audit.FLOW_SCHEDULE, target_type="flow",
                           target_id=flow.id, target_label=flow.name, detail=detail, request=request)


@router.patch("/flows/{flow_id}", response_model=FlowDetail)
def update_flow(
    flow_id: int,
    body: FlowUpdate,
    request: Request,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    flow = _get_flow(session, flow_id)
    ensure_can(session, user, flow.project_id, Capability.EDIT)
    flow_access.ensure_can_open(session, user, flow.definition)
    if body.definition is not None:
        _authorize_definition_keys(session, user, body.definition)
        script_nodes.check_definition(session, user, flow.definition, body.definition)
        registry_access.check_definition(session, user, flow.definition, body.definition)
    if body.name is not None:
        body.name = body.name.strip()
        if not body.name:
            raise HTTPException(status_code=422, detail="The flow needs a name")
        if body.name != flow.name:  # a name saved before is left as it is
            _check_flow_name(body.name)
    changed = [k for k in ("name", "description", "definition", "project_id", "engine", "production_engine")
               if getattr(body, k, None) is not None]
    # the folder path is part of the flow's name in an external lineage catalog
    lineage_names = openlineage.catalog_names(session, flows=[flow]) if body.name is not None or body.project_id is not None else {}

    moved = body.project_id is not None and body.project_id != flow.project_id
    if moved:
        # a move: EDIT on the destination folder is needed too
        if session.get(Project, body.project_id) is None:
            raise HTTPException(status_code=404, detail="Destination folder not found")
        ensure_can(session, user, body.project_id, Capability.EDIT)
    schedule_off = _guard_someone_elses_schedule(flow, user, body.definition, moved)
    if moved:
        flow.project_id = body.project_id

    if body.name is not None:
        flow.name = body.name
    if body.description is not None:
        flow.description = body.description
    if body.definition is not None:
        flow.definition = body.definition
    if body.engine is not None:
        flow.engine = _validate_engine(body.engine, session)
    if body.production_engine is not None:
        flow.production_engine = _validate_production_engine(body.production_engine, session)

    flow.updated_at = datetime.now(timezone.utc)
    session.add(flow)
    session.commit()
    session.refresh(flow)
    if body.definition is not None:  # only DAG changes make a version
        _snapshot_version(session, flow, user, note="edited")
    openlineage.renamed(session, lineage_names)
    audit.record_audit(
        session, actor=user, action=audit.FLOW_UPDATE, target_type="flow",
        target_id=flow.id, target_label=flow.name, detail={"changed": changed}, request=request,
    )
    _record_schedule_off(session, user, flow, schedule_off, request)
    session.refresh(flow)
    return flow


@router.get("/flows/{flow_id}/versions", response_model=list[FlowVersionOut])
def list_flow_versions(
    flow_id: int, user: User = Depends(get_current_user), session: Session = Depends(get_session)
):
    """The flow's version history (the newest is the current one)."""
    flow = _get_flow(session, flow_id)
    ensure_can(session, user, flow.project_id, Capability.VIEW)
    flow_access.ensure_can_open(session, user, flow.definition)
    versions = session.exec(
        select(FlowVersion)
        .where(FlowVersion.flow_id == flow_id)
        .order_by(FlowVersion.version.desc())
    ).all()
    current = versions[0].version if versions else None
    # the names of the versions' authors (who released/edited)
    uids = {v.created_by for v in versions if v.created_by is not None}
    names: dict[int, str] = {}
    if uids:
        for u in session.exec(select(User).where(User.id.in_(uids))).all():
            names[u.id] = u.full_name or u.email
    return [
        FlowVersionOut(
            version=v.version,
            note=v.note,
            created_at=v.created_at,
            created_by=v.created_by,
            created_by_name=names.get(v.created_by),
            is_current=(v.version == current),
        )
        for v in versions
    ]


@router.post("/flows/{flow_id}/versions/{version}/promote", response_model=FlowDetail)
def promote_flow_version(
    flow_id: int,
    version: int,
    request: Request,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """Promotes an old version to current: its definition becomes the flow's and is
    recorded as a new version at the head of the history."""
    flow = _get_flow(session, flow_id)
    ensure_can(session, user, flow.project_id, Capability.EDIT)
    target = session.exec(
        select(FlowVersion).where(FlowVersion.flow_id == flow_id, FlowVersion.version == version)
    ).first()
    if target is None:
        raise HTTPException(status_code=404, detail="Version not found")
    _authorize_definition_keys(session, user, target.definition)  # RBAC as on save
    flow_access.ensure_can_open(session, user, flow.definition)
    flow_access.ensure_can_open(session, user, target.definition)
    script_nodes.check_definition(session, user, flow.definition, target.definition)
    registry_access.check_definition(session, user, flow.definition, target.definition)
    schedule_off = _guard_someone_elses_schedule(flow, user, target.definition, False)
    flow.definition = target.definition
    flow.updated_at = datetime.now(timezone.utc)
    session.add(flow)
    session.commit()
    session.refresh(flow)
    _snapshot_version(session, flow, user, note=f"promoted from v{version}")
    session.refresh(flow)
    audit.record_audit(
        session, actor=user, action=audit.FLOW_PROMOTE, target_type="flow",
        target_id=flow.id, target_label=flow.name, detail={"from_version": version}, request=request,
    )
    _record_schedule_off(session, user, flow, schedule_off, request)
    session.refresh(flow)
    return flow


@router.get("/flows/{flow_id}/stats", response_model=FlowStatsOut)
def flow_stats(
    flow_id: int, user: User = Depends(get_current_user), session: Session = Depends(get_session)
):
    """Run statistics from the flow's run history (kind flow/orchestration): how many,
    successes/failures, last run, average time."""
    flow = _get_flow(session, flow_id)
    ensure_can(session, user, flow.project_id, Capability.VIEW)

    def _count(*conds) -> int:
        return session.exec(select(func.count()).select_from(Run).where(*conds)).one()

    run_count = _count(Run.flow_id == flow_id)
    success = _count(Run.flow_id == flow_id, Run.status == "SUCCESS")
    failure = _count(Run.flow_id == flow_id, Run.status == "FAILURE")
    last_run_at = session.exec(select(func.max(Run.started_at)).where(Run.flow_id == flow_id)).one()

    # average time: over the last 500 FINISHED runs, computed in Python (portable between
    # Postgres and SQLite, avoids dialect-specific EXTRACT/julianday)
    finished = session.exec(
        select(Run.started_at, Run.finished_at)
        .where(Run.flow_id == flow_id, Run.finished_at.is_not(None))
        .order_by(Run.started_at.desc())
        .limit(500)
    ).all()
    durs = [(f - s).total_seconds() for s, f in finished if s and f and f >= s]
    avg = round(sum(durs) / len(durs), 2) if durs else None
    return FlowStatsOut(
        run_count=run_count,
        success_count=success,
        failure_count=failure,
        last_run_at=last_run_at,
        avg_duration_seconds=avg,
    )


@router.delete("/flows/{flow_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_flow(flow_id: int, request: Request, user: User = Depends(get_current_user), session: Session = Depends(get_session)):
    flow = _get_flow(session, flow_id)
    ensure_can(session, user, flow.project_id, Capability.EDIT)
    flow_name = flow.name  # snapshot before the deletion

    # FKs to handle: the run history dies with the flow; the published datasources
    # SURVIVE (they are catalog content) losing only their provenance (flow_id → NULL).
    # Bulk statements: run AT ONCE, so the order (the referencing first, then the flow) is
    # guaranteed.
    from sqlalchemy import delete as sa_delete, update as sa_update

    from app.models import Datasource

    session.exec(sa_update(Datasource).where(Datasource.flow_id == flow_id).values(flow_id=None))
    # runs this flow's runs started for other objects (the imports of its Refresh
    # nodes, the outputs of its Run-flow nodes) belong to those objects' history:
    # they stay, no longer attached, or their link would refuse the delete
    own_runs = select(Run.id).where(Run.flow_id == flow_id)
    session.exec(sa_update(Run).where(Run.parent_run_id.in_(own_runs)).values(parent_run_id=None))
    session.exec(sa_delete(Run).where(Run.flow_id == flow_id))
    session.exec(sa_delete(FlowVersion).where(FlowVersion.flow_id == flow_id))
    from app.models import DbtAiText

    session.exec(sa_delete(DbtAiText).where(DbtAiText.flow_id == flow_id))   # the AI texts of its dbt exports
    ai_chats.forget_flow(session, flow.id)  # the editor's assistant talked about this flow only
    session.delete(flow)
    session.commit()
    audit.record_audit(
        session, actor=user, action=audit.FLOW_DELETE, target_type="flow",
        target_id=flow_id, target_label=flow_name, request=request,
    )


# the 409 of «Run now», in the language of the asker's interface
_ALREADY_RUNNING = {
    "en": "The flow is already queued or running: it can be run again once it has finished.",
    "it": "Il flusso è già in coda o in esecuzione: si potrà rilanciare quando avrà finito.",
    "de": "Der Flow ist bereits in der Warteschlange oder läuft: Er kann erneut gestartet werden, wenn er fertig ist.",
    "es": "El flow ya está en cola o en ejecución: se podrá lanzar de nuevo cuando termine.",
    "fr": "Le flux est déjà en file d'attente ou en cours : il pourra être relancé une fois terminé.",
}


@router.post("/flows/{flow_id}/run-now", status_code=status.HTTP_202_ACCEPTED)
def run_flow_now(
    flow_id: int,
    request: Request,
    mode: str = Query("development", pattern="^(development|production)$"),
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """Queues the flow's ORCHESTRATION (refresh/output/runflow nodes in the edges' order),
    which an orchestrator runs with the user's authority. RUN on the project is needed;
    the RBAC of the single steps (CONNECT of the refreshes, EDIT/CONNECT of the outputs)
    is checked again during the orchestration.

    It creates a tracing 'orchestration run' at once and returns its id: the frontend
    polls it (`GET /runs/{id}`) until SUCCESS/FAILURE — so flows without an Output node
    (which make no runs of their own) have an observable state too.

    `mode`: "development" (default) uses the editor's engine; "production" uses the
    flow's production engine — as the scheduler would — to try it by hand before
    scheduling."""
    from app.services.orchestrator import create_orchestration_run, flow_running, in_line_for_the_flow, wake

    flow = _get_flow(session, flow_id)
    ensure_can(session, user, flow.project_id, Capability.RUN)
    flow_access.ensure_can_open(session, user, flow.definition)
    # One more launch would not start anyway (the orchestrator closes it as «already
    # running»), but a row in `runs` and a round of the queue each would stay: repeated, it
    # pushes the other flows' runs to the back.
    in_line_for_the_flow(session, flow.id)  # check and queueing in line, across replicas
    if flow_running(session, flow.id):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=_ALREADY_RUNNING.get(request_language(request), _ALREADY_RUNNING["en"]),
        )
    run = create_orchestration_run(session, user, flow, engine_mode=mode)
    wake()
    audit.record_audit(
        session, actor=user, action=audit.FLOW_RUN, target_type="flow",
        target_id=flow.id, target_label=flow.name, detail={"run_id": run.id, "trigger": "manual", "engine_mode": mode},
        request=request,
    )
    return {"status": "started", "flow_id": flow.id, "run_id": run.id}


def _set_failure_notice(session: Session, user: User, flow: Flow, body: FlowScheduleUpdate) -> None:
    """Whom to notify when a scheduled run fails.

    The addresses go through the same barrier as the email node: an SMTP connection may
    limit the domains it sends to, and the notice is not a shortcut around it. And
    CONNECT on the connection is needed, as for using it elsewhere: otherwise whoever
    has only RUN on a flow could make someone else's mail server send."""
    from app.services import notify_targets

    if body.notify_emails is not None:
        flow.notify_emails = ", ".join(notify_targets.parse_recipients(body.notify_emails)) or None

    if body.notify_connection_id is not None:
        if body.notify_connection_id in (0, -1):  # turns the notice off
            flow.notify_connection_id = None
        else:
            flow.notify_connection_id = notify_targets.smtp_connection(session, user, body.notify_connection_id).id

    if flow.notify_emails and flow.notify_connection_id:
        notify_targets.ensure_deliverable(session.get(Connection, flow.notify_connection_id), flow.notify_emails.split(", "))


@router.put("/flows/{flow_id}/schedule", response_model=FlowDetail)
def set_flow_schedule(
    flow_id: int,
    body: FlowScheduleUpdate,
    request: Request,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """Sets/disables the scheduled run (5-field cron; '' = off). RUN on the flow's project
    is needed, and the flow must have at least one Output node (without, a scheduled run
    would produce nothing). At fire time the gateway resolves the CURRENT definition
    again and launches the Outputs with the authority of whoever schedules (EDIT/CONNECT
    of the single outputs checked again then)."""
    flow = _get_flow(session, flow_id)
    ensure_can(session, user, flow.project_id, Capability.RUN)
    flow_access.ensure_can_open(session, user, flow.definition)

    cron = (body.cron or "").strip()
    if not cron:
        flow.run_schedule = None
        flow.run_scheduled_by = None
        flow.next_run_at = None
    else:
        if not _has_runnable_nodes(flow.definition):
            raise HTTPException(
                status_code=422,
                detail="The flow has nothing to run (Output, refresh or run flow): "
                "add at least one of these nodes before scheduling it",
            )
        try:
            cron = validate_schedule(cron)
        except ScheduleError as e:
            raise HTTPException(status_code=422, detail=str(e))
        flow.run_schedule = cron
        flow.run_scheduled_by = user.id  # the scheduled runs' authority
        flow.next_run_at = next_fire(cron, datetime.now(timezone.utc))
    if body.production_engine is not None:  # omitted = unchanged; "" = as development
        flow.production_engine = _validate_production_engine(body.production_engine, session)
    if body.notify_emails is not None or body.notify_connection_id is not None:
        _set_failure_notice(session, user, flow, body)
    flow.updated_at = datetime.now(timezone.utc)
    session.add(flow)
    session.commit()
    session.refresh(flow)
    audit.record_audit(
        session, actor=user, action=audit.FLOW_SCHEDULE, target_type="flow",
        target_id=flow.id, target_label=flow.name,
        detail={"cron": flow.run_schedule, "production_engine": flow.production_engine,
                "notify": bool(flow.notify_emails and flow.notify_connection_id)}, request=request,
    )
    return flow
