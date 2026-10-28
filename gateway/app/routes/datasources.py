"""Named datasources: the catalog of the datasets reusable as sources.

Permissions inherited down the tree as for flows and projects:
- list/use: VIEW on the datasource's project;
- rename/move/delete: EDIT (moving requires EDIT at the destination too).

The gateway NEVER touches the storage directly (the S3 credentials live only in the
engine): deleting the blob goes through the engine's internal endpoint.
"""
import json
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status, Request
from starlette.concurrency import run_in_threadpool
from sqlalchemy import or_, text
from sqlmodel import Session, select

import httpx

from app.core.config import get_settings
from app.core.engine_client import get_engine_client
from app.db.session import get_session
from app.deps.auth import get_current_user
from app.deps.permissions import ensure_can
from app.models import Connection, Datasource, Project, Run, SavedView, User
from app.services import audit, openlineage
from app.services import contracts as contract_service
from app.services.documented import count_described, is_ai_ready
from app.models.permission import Capability
from app.models.run import LIVE_INGEST, TERMINAL_STATES
from app.routes.runs import _reconcile, launch_ingest_run, run_out, snapshot_key, start_refresh_if_free
from app.routes.connections import NOT_DATABASES, _engine_inspect, engine_connection_payload
from app.schemas.models import (
    DatasourceOut,
    DatasourceUpdate,
    DbDatasourceCreate,
    SharePointDatasourceCreate,
    SftpDatasourceCreate,
    S3DatasourceCreate,
    ApiDatasourceCreate,
    Page,
    RunOut,
    ScheduleUpdate,
)
from app.services import permissions as perm_service
from app.services.blobgc import schedule_blob_deletion
from app.services.copy_names import commit_copy, free_copy_name
from app.services.pagination import paginate
from app.services.schedule import ScheduleError, next_fire, validate_schedule
from app.core.routing import ReleasingRoute

logger = logging.getLogger(__name__)

router = APIRouter(route_class=ReleasingRoute, tags=["datasources"])


MAX_COLUMN_DESCRIPTION = 2000
# the DATASOURCE's description: what it holds, one line = what, period, caveats. People
# and the AI assistant read it to choose the table.
MAX_DATASOURCE_DESCRIPTION = 4000
MAX_SORT_KEY = 255


def clean_sort_keys(keys: list[str]) -> list[str]:
    """Trimmed, without blanks or repeats, in the order given: the order is the ORDER BY."""
    cleaned: list[str] = []
    for key in keys:
        key = (key or "").strip()
        if key and key not in cleaned:
            if len(key) > MAX_SORT_KEY:
                raise HTTPException(status_code=422, detail=f"Sort key longer than {MAX_SORT_KEY} characters")
            cleaned.append(key)
    return cleaned


def _checked_sort_keys(ds: Datasource, keys: list[str]) -> list[str]:
    """Sort keys a datasource may take. Against the columns of its last import when
    there is one: a key that is not a column would fail every refresh from then on.
    Without columns (the first import never succeeded, often because of a mistyped
    key) they are taken as given, which is how such a datasource gets repaired."""
    if ds.kind == "flow":
        raise HTTPException(
            status_code=422,
            detail="This datasource is published by a flow: set its sort keys on the flow's Output node",
        )
    if ds.source_type == "sharepoint":
        raise HTTPException(status_code=422, detail="SharePoint imports keep the rows in file order: no sort keys")
    if ds.source_type == "sftp":
        raise HTTPException(status_code=422, detail="SFTP imports keep the rows in file order: no sort keys")
    if ds.source_type == "api":
        raise HTTPException(status_code=422, detail="API imports keep the rows in the order of the responses: no sort keys")
    if ds.source_type == "s3":
        raise HTTPException(status_code=422, detail="S3 imports keep the rows in file order: no sort keys")
    keys = clean_sort_keys(keys)
    try:
        known = [c.get("name") for c in json.loads(ds.columns or "[]") if isinstance(c, dict)]
    except json.JSONDecodeError:
        known = []
    unknown = [k for k in keys if known and k not in known]
    if unknown:
        raise HTTPException(status_code=422, detail=f"Not columns of this datasource: {', '.join(unknown)}")
    return keys


def _load_column_descriptions(ds: Datasource) -> dict[str, str]:
    try:
        raw = json.loads(ds.column_descriptions or "{}")
    except json.JSONDecodeError:
        return {}
    return {str(k): str(v) for k, v in raw.items()} if isinstance(raw, dict) else {}


def clean_column_descriptions(value: dict[str, str]) -> dict[str, str]:
    """Normalizes the {column: description} map: spaces stripped, empty items dropped,
    length limited (422 beyond)."""
    out: dict[str, str] = {}
    for col, text in value.items():
        col = str(col).strip()
        text = str(text or "").strip()
        if not col or not text:
            continue
        if len(text) > MAX_COLUMN_DESCRIPTION:
            raise HTTPException(
                status_code=422,
                detail=f"The description of field '{col}' is longer than {MAX_COLUMN_DESCRIPTION} characters",
            )
        out[col] = text
    return out


def _refreshing_ids(session: Session, rows: list[Datasource]) -> set[int]:
    """The datasources with an ingest IN PROGRESS, with one query for the whole list. The
    "importing" state lived only in the page's memory: with a five-minute refresh,
    changing tab and coming back — or the refresh having been started by the scheduler —
    was enough to see nothing any more. The server says it, so the spinner survives
    reloads and other people's refreshes."""
    ids = [d.id for d in rows if d.id is not None]
    if not ids:
        return set()
    # LIVE_INGEST written as the partial index's condition: only live rows are read
    found = session.exec(select(Run.datasource_id).where(Run.datasource_id.in_(ids), text(LIVE_INGEST))).all()
    return {i for i in found if i is not None}


def _to_out(ds: Datasource, refreshing: bool = False, contract: dict | None = None) -> DatasourceOut:
    try:
        cols = json.loads(ds.columns or "[]")
    except json.JSONDecodeError:
        cols = []
    descs = _load_column_descriptions(ds)
    # the description travels inside `columns` too (handy for editor/AI)
    cols = [
        {**c, "description": descs[c["name"]]} if isinstance(c, dict) and c.get("name") in descs else c
        for c in cols
    ]
    # attribute access (not model_dump): an instance "expired" after the audit's commit
    # would give an empty dict → 500 (see connections._to_out)
    try:
        skeys = json.loads(ds.sort_keys or "[]")
    except json.JSONDecodeError:
        skeys = []
    if not isinstance(skeys, list):
        skeys = []
    fields = {
        f: getattr(ds, f)
        for f in DatasourceOut.model_fields
        # `refreshing` is not a column: it is computed (see _refreshing_ids)
        # the COMPUTED ones are not on the model: `getattr` here would blow up
        if f not in ("columns", "column_descriptions", "sort_keys", "refreshing",
                     "described_columns", "total_columns", "ai_ready", "contract")
    }
    described, totals = count_described(cols, descs)
    return DatasourceOut(
        **fields, columns=cols, column_descriptions=descs, sort_keys=skeys, refreshing=refreshing,
        described_columns=described, total_columns=totals,
        ai_ready=is_ai_ready(ds.description, described, totals),
        contract=contract,
    )


def _to_out_many(session: Session, rows: list[Datasource]) -> list[DatasourceOut]:
    """A list of datasources with what is computed for all of them together: who is
    importing and the data contract's state — one query each, not one per row."""
    busy = _refreshing_ids(session, rows)
    state = contract_service.summaries(session, [d.id for d in rows])
    return [_to_out(d, d.id in busy, state.get(d.id)) for d in rows]


def _get_ds(session: Session, ds_id: int) -> Datasource:
    ds = session.get(Datasource, ds_id)
    if ds is None:
        raise HTTPException(status_code=404, detail="Datasource not found")
    return ds


@router.get("/datasources", response_model=list[DatasourceOut])
def list_all_datasources(user: User = Depends(get_current_user), session: Session = Depends(get_session)):
    """All the datasources in the READABLE projects (for the sources' picker).

    `readable_project_ids`, not `visible_project_ids`: the ancestors shown for navigation
    do not give access to their content.
    """
    readable = perm_service.readable_project_ids(session, user)
    if not readable:
        return []
    rows = session.exec(
        select(Datasource).where(Datasource.project_id.in_(readable)).order_by(Datasource.name)
    ).all()
    return _to_out_many(session, rows)


@router.get("/datasources/search", response_model=Page[DatasourceOut])
def search_datasources(
    q: str | None = Query(None, description="searches name/description, over the WHOLE dataset"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """Datasources in the readable projects, PAGINATED, with a server-side search over the
    whole dataset (not only the page). For the global Datasources page."""
    readable = perm_service.readable_project_ids(session, user)
    if not readable:
        return Page(items=[], total=0)
    base = select(Datasource).where(Datasource.project_id.in_(readable))
    if q:
        like = f"%{q}%"
        base = base.where(or_(Datasource.name.ilike(like), Datasource.description.ilike(like)))
    rows, total = paginate(session, base, Datasource.name, limit, offset)
    return Page(items=_to_out_many(session, rows), total=total)


@router.get("/projects/{project_id}/datasources", response_model=list[DatasourceOut])
def list_project_datasources(
    project_id: int,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    if session.get(Project, project_id) is None:
        raise HTTPException(status_code=404, detail="Folder not found")
    ensure_can(session, user, project_id, Capability.VIEW)
    rows = session.exec(
        select(Datasource).where(Datasource.project_id == project_id).order_by(Datasource.name)
    ).all()
    return _to_out_many(session, rows)


@router.post(
    "/projects/{project_id}/datasources/database",
    response_model=DatasourceOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_db_datasource(
    project_id: int,
    body: DbDatasourceCreate,
    request: Request,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """A datasource from a database: EDIT on the destination folder + CONNECT on the
    connection's folder (the barrier on the credentials). The first ingest starts at
    once; the parquet is a snapshot, the refresh replaces it.
    """
    if session.get(Project, project_id) is None:
        raise HTTPException(status_code=404, detail="Folder not found")
    ensure_can(session, user, project_id, Capability.EDIT)

    conn = session.get(Connection, body.connection_id)
    if conn is None:
        raise HTTPException(status_code=404, detail="Connection not found")
    ensure_can(session, user, conn.project_id, Capability.CONNECT)
    if conn.db_type == "s3":
        raise HTTPException(
            status_code=422,
            detail="An S3 connection is not a database: read its files with an «S3 folder» datasource",
        )
    if conn.db_type in NOT_DATABASES:
        raise HTTPException(status_code=422, detail=f"A {conn.db_type} connection is not a database")

    if body.source_type not in ("table", "sql"):
        raise HTTPException(status_code=422, detail="source_type must be 'table' or 'sql'")
    if not body.source_ref.strip():
        raise HTTPException(status_code=422, detail="The source (table or SQL) is empty")
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="The datasource needs a name")
    conflict = session.exec(
        select(Datasource).where(Datasource.project_id == project_id, Datasource.name == name)
    ).first()
    if conflict:
        raise HTTPException(status_code=409, detail=f"There is already a datasource '{name}' in this folder")

    ds = Datasource(
        name=name,
        description=body.description,
        project_id=project_id,
        owner_id=user.id,
        bucket=get_settings().engine.bucket,
        key="",  # no snapshot until the first ingest succeeds
        kind="database",
        connection_id=conn.id,
        source_type=body.source_type,
        source_ref=body.source_ref,
        sort_keys=json.dumps(clean_sort_keys(body.sort_keys)),
    )
    session.add(ds)
    session.commit()
    session.refresh(ds)

    try:
        await launch_ingest_run(session, user, ds, conn)
    except Exception:
        # engine down (a ConnectError too, not only an HTTPException) or request refused: no
        # half datasource, an atomic creation
        session.delete(ds)
        session.commit()
        raise
    audit.record_audit(
        session, actor=user, action=audit.DS_CREATE, target_type="datasource",
        target_id=ds.id, target_label=ds.name,
        detail={"project_id": project_id, "connection_id": conn.id,
                "source_type": body.source_type, "source_ref": body.source_ref},
        request=request,
    )
    return _to_out(ds)


@router.post(
    "/projects/{project_id}/datasources/sharepoint",
    response_model=DatasourceOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_sharepoint_datasource(
    project_id: int,
    body: SharePointDatasourceCreate,
    request: Request,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """A datasource from Excel files on SharePoint (path, globs too, + sheet).

    It is a `kind="database"` datasource in every respect — manual and scheduled refresh,
    import history, spinner, snapshot swap — with `source_type="sharepoint"` and
    `source_ref` = JSON {path, sheet, format, csv}: only WHO brings the data changes (see
    launch_ingest_run)."""
    if session.get(Project, project_id) is None:
        raise HTTPException(status_code=404, detail="Folder not found")
    ensure_can(session, user, project_id, Capability.EDIT)
    conn = session.get(Connection, body.connection_id)
    if conn is None:
        raise HTTPException(status_code=404, detail="Connection not found")
    ensure_can(session, user, conn.project_id, Capability.CONNECT)
    if conn.db_type != "sharepoint":
        raise HTTPException(status_code=422, detail="A SharePoint connection is required")
    name, path, sheet = body.name.strip(), body.path.strip(), body.sheet.strip()
    if not name or not path:
        raise HTTPException(status_code=422, detail="Name and path are required")
    if session.exec(select(Datasource).where(Datasource.project_id == project_id, Datasource.name == name)).first():
        raise HTTPException(status_code=409, detail=f"There is already a datasource '{name}' in this folder")

    ref = {"path": path, "sheet": sheet, "format": body.format, "csv": body.csv.model_dump(), **body.template_fields()}
    await validate_file_templates(conn, ref, ("path",))
    ds = Datasource(
        name=name, description=body.description, project_id=project_id, owner_id=user.id,
        bucket=get_settings().engine.bucket, key="",  # no snapshot until the first import succeeds
        kind="database", connection_id=conn.id,
        source_type="sharepoint", source_ref=json.dumps(ref),
    )
    session.add(ds)
    session.commit()
    session.refresh(ds)
    try:
        await launch_ingest_run(session, user, ds, conn)
    except Exception:
        session.delete(ds)  # engine down (ConnectError too) or refusal: no half datasource
        session.commit()
        raise
    audit.record_audit(
        session, actor=user, action=audit.DS_CREATE, target_type="datasource",
        target_id=ds.id, target_label=ds.name,
        detail={"project_id": project_id, "connection_id": conn.id, "source_type": "sharepoint", "path": path,
                "format": body.format, "sheet": sheet},
        request=request,
    )
    return _to_out(ds)


@router.post(
    "/projects/{project_id}/datasources/sftp",
    response_model=DatasourceOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_sftp_datasource(
    project_id: int,
    body: SftpDatasourceCreate,
    request: Request,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """Datasource from the files of a folder on an SFTP server (folder + glob).

    A `kind="database"` datasource like the SharePoint one — manual and scheduled
    refresh, import history, snapshot swap — with `source_type="sftp"` and
    `source_ref` = JSON {folder, pattern, format, sheet, csv}: only WHO brings the
    data changes (see launch_ingest_run). EDIT on the folder + CONNECT on the
    connection's folder, as for every database datasource."""
    if session.get(Project, project_id) is None:
        raise HTTPException(status_code=404, detail="Folder not found")
    ensure_can(session, user, project_id, Capability.EDIT)
    conn = session.get(Connection, body.connection_id)
    if conn is None:
        raise HTTPException(status_code=404, detail="Connection not found")
    ensure_can(session, user, conn.project_id, Capability.CONNECT)
    if conn.db_type != "sftp":
        raise HTTPException(status_code=422, detail="An SFTP connection is required")
    name, pattern = body.name.strip(), body.pattern.strip()
    if not name or not pattern:
        raise HTTPException(status_code=422, detail="Name and file pattern are required")
    if session.exec(select(Datasource).where(Datasource.project_id == project_id, Datasource.name == name)).first():
        raise HTTPException(status_code=409, detail=f"There is already a datasource '{name}' in this folder")
    ref = {"folder": body.folder.strip(), "pattern": pattern, "format": body.format, "sheet": body.sheet.strip(),
           "csv": body.csv.model_dump(), **body.template_fields()}
    await validate_file_templates(conn, ref, ("folder", "pattern"))
    ds = Datasource(
        name=name, description=body.description, project_id=project_id, owner_id=user.id,
        bucket=get_settings().engine.bucket, key="",  # no snapshot until the first import succeeds
        kind="database", connection_id=conn.id, source_type="sftp", source_ref=json.dumps(ref),
    )
    session.add(ds)
    session.commit()
    session.refresh(ds)
    try:
        await launch_ingest_run(session, user, ds, conn)
    except Exception:
        session.delete(ds)  # engine down (a ConnectError too) or refused: no half datasource
        session.commit()
        raise
    audit.record_audit(
        session, actor=user, action=audit.DS_CREATE, target_type="datasource",
        target_id=ds.id, target_label=ds.name,
        detail={"project_id": project_id, "connection_id": conn.id, "source_type": "sftp",
                "folder": ref["folder"], "pattern": pattern},
        request=request,
    )
    return _to_out(ds)


FILE_TEMPLATE_KEYS = ("timezone", "variables", "last_refresh_initial", "mode", "keys")


def is_dynamic_ref(ref: dict, fields: tuple[str, ...]) -> bool:
    """A file datasource with placeholders, variables or the append mode: what
    only the engine can check (the grammar of app.core.placeholders)."""
    return (any("{" in str(ref.get(f) or "") or "}" in str(ref.get(f) or "") for f in fields)
            or any(k in ref for k in FILE_TEMPLATE_KEYS))


async def validate_file_templates(conn: Connection, ref: dict, fields: tuple[str, ...]) -> None:
    """Before saving: the engine checks the placeholders, the variables (a range
    within its cap and not empty), the keys of the append — the messages the
    refresh would give, at save time. A datasource without any of them is saved
    as before, with no call."""
    if not is_dynamic_ref(ref, fields):
        return
    try:
        await _engine_inspect(engine_connection_payload(conn), "validate", source=ref)
    except HTTPException as e:
        if e.status_code == 400:  # the engine's message for the person who wrote the datasource
            raise HTTPException(status_code=422, detail=e.detail) from None
        raise


@router.post(
    "/projects/{project_id}/datasources/s3",
    response_model=DatasourceOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_s3_datasource(
    project_id: int,
    body: S3DatasourceCreate,
    request: Request,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """Datasource from the files of a folder of a bucket («S3 folder»): the bucket
    (empty = the connection's default), a folder and a glob, with placeholders,
    variables and the append mode as the SFTP datasource.

    A `kind="database"` datasource — refresh, schedule, history, snapshot swap —
    with `source_type="s3"` and the JSON of the folder in `source_ref`. EDIT on
    the folder + CONNECT on the connection's folder, as for every database
    datasource; the connection is the S3 one of the Output nodes."""
    if session.get(Project, project_id) is None:
        raise HTTPException(status_code=404, detail="Folder not found")
    ensure_can(session, user, project_id, Capability.EDIT)
    conn = session.get(Connection, body.connection_id)
    if conn is None:
        raise HTTPException(status_code=404, detail="Connection not found")
    ensure_can(session, user, conn.project_id, Capability.CONNECT)
    if conn.db_type != "s3":
        raise HTTPException(status_code=422, detail="An S3 connection is required")
    name = body.name.strip()
    if not name or not body.pattern.strip():
        raise HTTPException(status_code=422, detail="Name and file pattern are required")
    if not body.bucket.strip() and not (conn.database or "").strip():
        raise HTTPException(status_code=422, detail="No bucket: set one on the datasource or as the connection's default")
    if session.exec(select(Datasource).where(Datasource.project_id == project_id, Datasource.name == name)).first():
        raise HTTPException(status_code=409, detail=f"There is already a datasource '{name}' in this folder")
    ref = body.source_ref()
    await validate_file_templates(conn, ref, ("folder", "pattern"))
    ds = Datasource(
        name=name, description=body.description, project_id=project_id, owner_id=user.id,
        bucket=get_settings().engine.bucket, key="",  # no snapshot until the first import succeeds
        kind="database", connection_id=conn.id, source_type="s3", source_ref=json.dumps(ref),
    )
    session.add(ds)
    session.commit()
    session.refresh(ds)
    try:
        await launch_ingest_run(session, user, ds, conn)
    except Exception:
        session.delete(ds)  # engine down (a ConnectError too) or refused: no half datasource
        session.commit()
        raise
    audit.record_audit(
        session, actor=user, action=audit.DS_CREATE, target_type="datasource",
        target_id=ds.id, target_label=ds.name,
        detail={"project_id": project_id, "connection_id": conn.id, "source_type": "s3",
                "bucket": ref["bucket"] or conn.database, "folder": ref["folder"], "pattern": ref["pattern"]},
        request=request,
    )
    return _to_out(ds)


@router.post(
    "/projects/{project_id}/datasources/api",
    response_model=DatasourceOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_api_datasource(
    project_id: int,
    body: ApiDatasourceCreate,
    request: Request,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """Datasource from an HTTP API: the call (path, query, headers, body,
    variables, pagination, the shape of the answer, the refresh mode) is made at
    each refresh.

    A `kind="database"` datasource like the SharePoint and SFTP ones — manual and
    scheduled refresh, import history, snapshot swap — with `source_type="api"`
    and `source_ref` = the call as JSON. The engine checks the call FIRST (the
    placeholders, the variables, the JMESPath…): a call that cannot work is
    refused before anything is saved. EDIT on the folder + CONNECT on the
    connection's folder, as for every database datasource."""
    if session.get(Project, project_id) is None:
        raise HTTPException(status_code=404, detail="Folder not found")
    ensure_can(session, user, project_id, Capability.EDIT)
    conn = session.get(Connection, body.connection_id)
    if conn is None:
        raise HTTPException(status_code=404, detail="Connection not found")
    ensure_can(session, user, conn.project_id, Capability.CONNECT)
    if conn.db_type != "api":
        raise HTTPException(status_code=422, detail="An API connection is required")
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="The datasource needs a name")
    if session.exec(select(Datasource).where(Datasource.project_id == project_id, Datasource.name == name)).first():
        raise HTTPException(status_code=409, detail=f"There is already a datasource '{name}' in this folder")
    ref = body.model_dump(exclude={"name", "description", "connection_id"})
    ref["keys"] = [k.strip() for k in ref.get("keys") or [] if k.strip()]
    try:
        await _engine_inspect(engine_connection_payload(conn), "validate", source=ref)
    except HTTPException as e:
        if e.status_code == 400:  # the engine's message for the person who wrote the call
            raise HTTPException(status_code=422, detail=e.detail) from None
        raise
    ds = Datasource(
        name=name, description=body.description, project_id=project_id, owner_id=user.id,
        bucket=get_settings().engine.bucket, key="",  # no snapshot until the first import succeeds
        kind="database", connection_id=conn.id, source_type="api", source_ref=json.dumps(ref),
    )
    session.add(ds)
    session.commit()
    session.refresh(ds)
    try:
        await launch_ingest_run(session, user, ds, conn)
    except Exception:
        session.delete(ds)  # engine down (a ConnectError too) or refused: no half datasource
        session.commit()
        raise
    audit.record_audit(
        session, actor=user, action=audit.DS_CREATE, target_type="datasource",
        target_id=ds.id, target_label=ds.name,
        detail={"project_id": project_id, "connection_id": conn.id, "source_type": "api",
                "method": ref["method"], "path": ref["path"], "mode": ref["mode"]},
        request=request,
    )
    return _to_out(ds)


# a copy on the storage is one request however big the file: give it the time a few GB take
_COPY_TIMEOUT = httpx.Timeout(900.0, connect=5.0)


@router.post("/datasources/{ds_id}/duplicate", response_model=DatasourceOut, status_code=status.HTTP_201_CREATED)
async def duplicate_datasource(
    ds_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """A copy of the datasource in its folder, as «name (i)»: its definition and its
    data. The data file is copied on the storage to one of its own, since two
    datasources sharing a file would lose it when either is refreshed or deleted.

    A copy of a database datasource refreshes like the original, so it asks for
    CONNECT on the connection's folder, as creating one does. The data contract
    comes along, to be checked again; the refresh schedule, the contract's alerts
    and the flow that published the original stay with the original."""
    src = _get_ds(session, ds_id)
    ensure_can(session, user, src.project_id, Capability.EDIT)
    conn = session.get(Connection, src.connection_id) if src.connection_id is not None else None
    if conn is not None:
        ensure_can(session, user, conn.project_id, Capability.CONNECT)
    contract = contract_service.get(session, src.id)
    contract_doc = (json.loads(contract.document), contract.enabled) if contract is not None else None
    src_id, bucket, src_key = src.id, src.bucket, src.key
    ds = Datasource(
        name=free_copy_name(session, Datasource, src.project_id, src.name),
        description=src.description,
        project_id=src.project_id,
        owner_id=user.id,
        bucket=bucket,
        key="",  # set once the file is copied
        rows=src.rows,
        columns=src.columns,
        sort_keys=src.sort_keys,
        column_descriptions=src.column_descriptions,
        kind=src.kind,
        connection_id=src.connection_id,
        source_type=src.source_type,
        source_ref=src.source_ref,
        refreshed_at=src.refreshed_at,
    )
    session.add(ds)
    commit_copy(session)  # the commit also gives the connection back while the engine copies

    if src_key:
        key = snapshot_key(ds.id)
        failure = None
        try:
            resp = await get_engine_client().post(
                "/files/copy", params={"bucket": bucket, "source_key": src_key, "target_key": key}, timeout=_COPY_TIMEOUT,
            )
            if resp.status_code >= 400:
                failure = "the data file is missing" if resp.status_code == 404 else f"engine status {resp.status_code}"
        except httpx.HTTPError as e:
            failure = f"engine unreachable ({type(e).__name__})"
        if failure is not None:
            # nothing half made: no datasource without its data
            session.delete(ds)
            session.commit()
            logger.warning("duplicate of datasource %s: copy of %s failed: %s", src_id, src_key, failure)
            raise HTTPException(status_code=502, detail=f"The data could not be copied, nothing was created: {failure}")
        ds.key = key
    contract_summary = None
    if contract_doc is not None:
        contract_summary = contract_service.summary(
            contract_service.save(session, ds, contract_doc[0], contract_doc[1], user.id))
        if not contract_doc[1]:
            contract_summary = None  # lists show only contracts that are on
    session.add(ds)
    session.commit()
    audit.record_audit(
        session, actor=user, action=audit.DS_CREATE, target_type="datasource",
        target_id=ds.id, target_label=ds.name,
        detail={"project_id": ds.project_id, "kind": ds.kind, "duplicate_of": src_id}, request=request,
    )
    return _to_out(ds, contract=contract_summary)


@router.post("/datasources/{ds_id}/refresh", response_model=RunOut, status_code=status.HTTP_202_ACCEPTED)
async def refresh_datasource(
    ds_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """Runs the source again and replaces the snapshot: RUN on the datasource's folder +
    CONNECT on the connection's. One refresh at a time."""
    ds = _get_ds(session, ds_id)
    if ds.kind != "database":
        raise HTTPException(status_code=422, detail="Only database datasources can be refreshed")
    ensure_can(session, user, ds.project_id, Capability.RUN)
    conn = session.get(Connection, ds.connection_id) if ds.connection_id else None
    if conn is None:
        raise HTTPException(status_code=409, detail="The connection of this datasource no longer exists")
    ensure_can(session, user, conn.project_id, Capability.CONNECT)

    # in line with every other starter (replicas, scheduler, flows): one at a time
    run = await start_refresh_if_free(session, user, ds, conn, launch=launch_ingest_run)
    if run is None:
        raise HTTPException(status_code=409, detail="A refresh is already running for this datasource")
    audit.record_audit(
        session, actor=user, action=audit.DS_REFRESH, target_type="datasource",
        target_id=ds.id, target_label=ds.name, detail={"run_id": getattr(run, "id", None)},
        request=request,
    )
    return run


@router.put("/datasources/{ds_id}/schedule", response_model=DatasourceOut)
def set_schedule(
    ds_id: int,
    body: ScheduleUpdate,
    request: Request,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """Sets/disables the scheduled refresh (5-field cron). An empty `cron` = disable. RUN on
    the datasource + CONNECT on the connection are needed: scheduling authorizes REPEATED
    refreshes, so the same authority as the manual refresh. The scheduled refreshes will
    run with the authority of WHOEVER sets the schedule (captured here in
    `refresh_scheduled_by`)."""
    ds = _get_ds(session, ds_id)
    if ds.kind != "database":
        raise HTTPException(status_code=422, detail="Only database datasources can be scheduled")
    ensure_can(session, user, ds.project_id, Capability.RUN)
    conn = session.get(Connection, ds.connection_id) if ds.connection_id else None
    if conn is None:
        raise HTTPException(status_code=409, detail="The connection of this datasource no longer exists")
    ensure_can(session, user, conn.project_id, Capability.CONNECT)

    cron = (body.cron or "").strip()
    if not cron:
        ds.refresh_schedule = None
        ds.refresh_scheduled_by = None
        ds.next_refresh_at = None
    else:
        try:
            cron = validate_schedule(cron)
        except ScheduleError as e:
            raise HTTPException(status_code=422, detail=str(e))
        ds.refresh_schedule = cron
        ds.refresh_scheduled_by = user.id  # the scheduled refreshes' authority
        ds.next_refresh_at = next_fire(cron, datetime.now(timezone.utc))
    ds.updated_at = datetime.now(timezone.utc)
    session.add(ds)
    session.commit()
    session.refresh(ds)
    audit.record_audit(
        session, actor=user, action=audit.DS_SCHEDULE, target_type="datasource",
        target_id=ds.id, target_label=ds.name,
        detail={"cron": ds.refresh_schedule}, request=request,
    )
    return _to_out(ds)


@router.get("/datasources/{ds_id}/runs", response_model=list[RunOut])
async def list_datasource_runs(
    ds_id: int,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """The datasource's ingest (refresh) history, reconciled on reading."""
    def read() -> list[Run]:  # in a worker thread: the event loop never waits for the database
        ds = _get_ds(session, ds_id)
        ensure_can(session, user, ds.project_id, Capability.VIEW)
        return list(session.exec(
            select(Run)
            .where(Run.datasource_id == ds.id, Run.kind == "ingest")
            .order_by(Run.started_at.desc())
            .limit(50)
        ).all())

    runs = await run_in_threadpool(read)
    return [run_out(session, await _reconcile(session, r)) for r in runs]


@router.patch("/datasources/{ds_id}", response_model=DatasourceOut)
def update_datasource(
    ds_id: int,
    body: DatasourceUpdate,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
    request: Request = None,  # type: ignore[assignment]
):
    ds = _get_ds(session, ds_id)
    ensure_can(session, user, ds.project_id, Capability.EDIT)
    from_folder, from_name, schedule_before = ds.project_id, ds.name, ds.refresh_schedule
    # the folder path is part of the datasource's name in an external lineage catalog
    lineage_names = openlineage.catalog_names(session, datasources=[ds]) if body.name is not None or body.project_id is not None else {}

    target_project = ds.project_id
    if body.project_id is not None and body.project_id != ds.project_id:
        if session.get(Project, body.project_id) is None:
            raise HTTPException(status_code=404, detail="Destination folder not found")
        ensure_can(session, user, body.project_id, Capability.EDIT)
        target_project = body.project_id
        # The schedule carries the AUTHORITY of whoever created it: when the datasource changes
        # folder, that authority no longer holds here. It is turned off, and whoever has the
        # permissions in the new place sets it again.
        if ds.refresh_schedule and ds.refresh_scheduled_by != user.id:
            ds.refresh_schedule = None
            ds.refresh_scheduled_by = None
            ds.next_refresh_at = None

    new_name = body.name.strip() if body.name is not None else ds.name
    if not new_name:
        raise HTTPException(status_code=422, detail="The datasource needs a name")
    conflict = session.exec(
        select(Datasource).where(
            Datasource.project_id == target_project,
            Datasource.name == new_name,
            Datasource.id != ds.id,
        )
    ).first()
    if conflict:
        raise HTTPException(status_code=409, detail=f"There is already a datasource '{new_name}' in this folder")

    ds.name = new_name
    ds.project_id = target_project
    if body.description is not None:
        text = body.description.strip()
        if len(text) > MAX_DATASOURCE_DESCRIPTION:
            raise HTTPException(status_code=422, detail=f"The description is longer than {MAX_DATASOURCE_DESCRIPTION} characters")
        ds.description = text
    if body.column_descriptions is not None:
        ds.column_descriptions = json.dumps(clean_column_descriptions(body.column_descriptions), ensure_ascii=False)
    if body.sort_keys is not None:
        # takes effect at the next import or refresh, which reads them from here
        ds.sort_keys = json.dumps(_checked_sort_keys(ds, body.sort_keys))
    ds.updated_at = datetime.now(timezone.utc)
    session.add(ds)
    session.commit()
    session.refresh(ds)
    openlineage.renamed(session, lineage_names)
    detail = {}
    if ds.project_id != from_folder:
        detail.update(moved_to=ds.project_id, from_project=from_folder,
                         schedule_cleared=bool(schedule_before) and not ds.refresh_schedule)
    if ds.name != from_name:
        detail["name"] = {"from": from_name, "to": ds.name}
    if detail:
        audit.record_audit(session, actor=user, action=audit.DS_UPDATE, request=request, target_type="datasource",
                           target_id=ds.id, target_label=ds.name, detail=detail)
        session.refresh(ds)
    return _to_out(ds)


@router.delete("/datasources/{ds_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_datasource(
    ds_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """Deletes the catalog item AND (deferred) the parquet blob.

    A deliberate order: the catalog FIRST (detaching the runs that reference it), and in
    the SAME transaction the blob is marked for DEFERRED deletion: a run/preview in
    progress may have resolved its key already and be about to read it, so it is not
    deleted at once (avoids the 404 while reading). The scheduler's sweep deletes it
    after the grace. The flows referencing it will fail in preview with the standard
    missing-source error — consistent with how datasets are deleted.
    """
    ds = _get_ds(session, ds_id)
    ensure_can(session, user, ds.project_id, Capability.EDIT)
    views = session.exec(select(SavedView.name).where(SavedView.datasource_id == ds.id).order_by(SavedView.name).limit(6)).all()
    if views:
        names = ", ".join(f"'{n}'" for n in views[:5]) + (", …" if len(views) > 5 else "")
        raise HTTPException(status_code=409, detail=f"The datasource is used by saved views ({names}): delete them first")

    ds_name, ds_kind = ds.name, ds.kind  # snapshot per l'audit
    bucket, key = ds.bucket, ds.key
    # the historical runs that published (or updated) it stay, without the reference
    for run in session.exec(select(Run).where(Run.datasource_id == ds.id)).all():
        run.datasource_id = None
        session.add(run)
    session.delete(ds)
    contract_service.forget(session, ds_id)  # contract, versions and reports go with it
    if key:  # a database datasource never ingested: no blob to delete
        schedule_blob_deletion(session, bucket, key, reason=f"datasource {ds_id} deleted")
    session.commit()
    audit.record_audit(
        session, actor=user, action=audit.DS_DELETE, target_type="datasource",
        target_id=ds_id, target_label=ds_name, detail={"kind": ds_kind}, request=request,
    )
