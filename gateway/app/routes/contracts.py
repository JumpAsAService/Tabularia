"""A datasource's data contracts: read, write, check, propose.

Who may: read the contract and its state whoever has VIEW on the datasource's folder
(they see the data already); write it, check it and get one proposed whoever has
EDIT. The routes that wait for the engine are asynchronous and do not hold a
database connection while waiting (see routes/proxy.py).
"""
import json
import logging
import re
from datetime import timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel
from sqlmodel import Session, select

from app.core.engine_client import get_engine_client
from app.core.routing import ReleasingRoute
from app.db.session import get_session
from app.deps.auth import get_current_user
from app.deps.permissions import ensure_can
from app.models import Connection, DataContract, DataContractResult, Datasource, User
from app.models.permission import Capability
from app.routes.proxy import _release_db, _user
from app.schemas.models import UtcDateTime
from app.services import audit, contracts, notify_targets, odcs
from app.services.permissions import has_capability

logger = logging.getLogger(__name__)
router = APIRouter(route_class=ReleasingRoute, tags=["contracts"])

# how long the engine is waited for a check asked by hand (it gives up at 120 s)
ENGINE_WAIT_SECONDS = 150


class ContractIn(BaseModel):
    document: dict[str, Any]
    enabled: bool = True
    # Whom to notify when the state changes. None = as before; "" removes the addresses; 0
    # removes the connection (as for the flows' notice).
    notify_emails: Optional[str] = None
    notify_connection_id: Optional[int] = None


class ContractOut(BaseModel):
    datasource_id: int
    version: int
    enabled: bool
    document: dict[str, Any]
    # what the icon says: pending | passed | warning | failed
    status: str
    checked_at: Optional[UtcDateTime] = None
    errors: int = 0
    warnings: int = 0
    # the last update was refused: the data of before is being served
    blocked: bool = False
    blocked_at: Optional[UtcDateTime] = None
    blocked_run_id: Optional[int] = None
    report: Optional[dict[str, Any]] = None
    updated_at: Optional[UtcDateTime] = None
    # the check asked with this request failed (engine down, dataset too large): the
    # contract is saved, the state stays the one of before
    check_error: Optional[str] = None
    # whoever reads may also change it (EDIT on the folder): the interface shows the
    # commands only to whoever can use them. Whoever writes got here with EDIT.
    editable: bool = True
    # who is notified at a change of state: only whoever can change it sees it
    notify_emails: str = ""
    notify_connection_id: Optional[int] = None


class ContractResultOut(BaseModel):
    id: int
    contract_version: int
    trigger: str
    run_id: Optional[int] = None
    outcome: str
    blocked: bool
    errors: int
    warnings: int
    rows: Optional[int] = None
    evaluated_at: UtcDateTime
    report: Optional[dict[str, Any]] = None


def _loads(text: str | None) -> dict | None:
    try:
        return json.loads(text) if text else None
    except json.JSONDecodeError:
        return None


def _out(c: DataContract, check_error: str | None = None) -> ContractOut:
    return ContractOut(
        datasource_id=c.datasource_id, version=c.version, enabled=c.enabled, document=_loads(c.document) or {"rules": []},
        status=contracts.effective_status(c), checked_at=c.checked_at, errors=c.errors, warnings=c.warnings,
        blocked=c.blocked_at is not None, blocked_at=c.blocked_at, blocked_run_id=c.blocked_run_id,
        report=_loads(c.last_report), updated_at=c.updated_at, check_error=check_error,
        notify_emails=c.notify_emails or "", notify_connection_id=c.notify_connection_id,
    )


def _datasource(session: Session, user: User, ds_id: int, capability: Capability) -> Datasource:
    ds = session.get(Datasource, ds_id)
    if ds is None:
        raise HTTPException(status_code=404, detail="Datasource not found")
    ensure_can(session, user, ds.project_id, capability)
    return ds


def _contract(session: Session, ds_id: int) -> DataContract:
    c = contracts.get(session, ds_id)
    if c is None:
        raise HTTPException(status_code=404, detail="This datasource has no data contract")
    return c


def _snapshot(ds: Datasource) -> dict:
    """What the engine needs to evaluate the data the datasource serves now."""
    produced = ds.refreshed_at or ds.updated_at or ds.created_at
    if produced is not None and produced.tzinfo is None:
        produced = produced.replace(tzinfo=timezone.utc)
    return {"bucket": ds.bucket, "key": ds.key, "snapshot_at": produced.isoformat() if produced else None}


async def _ask_engine(path: str, body: dict) -> tuple[dict | None, str | None]:
    """(answer, None) or (None, why there is none)."""
    try:
        resp = await get_engine_client().post(path, json=body, timeout=ENGINE_WAIT_SECONDS)
    except Exception as e:  # engine irraggiungibile
        logger.warning("engine %s not reachable: %s", path, e)
        return None, "The engine cannot be reached"
    if resp.status_code >= 400:
        try:
            return None, str(resp.json().get("detail") or resp.text)[:300]
        except Exception:
            return None, resp.text[:300]
    return resp.json(), None


# ── reading ──────────────────────────────────────────────────────────────────
@router.get("/datasources/{ds_id}/contract", response_model=ContractOut)
def get_contract(ds_id: int, user: User = Depends(get_current_user), session: Session = Depends(get_session)):
    ds = _datasource(session, user, ds_id, Capability.VIEW)
    out = _out(_contract(session, ds_id))
    out.editable = has_capability(session, user, ds.project_id, Capability.EDIT)
    if not out.editable:  # the addresses are personal data: whoever only reads does not need them
        out.notify_emails, out.notify_connection_id = "", None
    return out


@router.get("/datasources/{ds_id}/contract/history", response_model=list[ContractResultOut])
def contract_history(
    ds_id: int, limit: int = 30, with_report: bool = False,
    user: User = Depends(get_current_user), session: Session = Depends(get_session),
):
    _datasource(session, user, ds_id, Capability.VIEW)
    rows = session.exec(
        select(DataContractResult)
        .where(DataContractResult.datasource_id == ds_id)
        .order_by(DataContractResult.evaluated_at.desc(), DataContractResult.id.desc())
        .limit(max(1, min(limit, 200)))
    ).all()
    return [
        ContractResultOut(
            id=r.id, contract_version=r.contract_version, trigger=r.trigger, run_id=r.run_id, outcome=r.outcome,
            blocked=r.blocked, errors=r.errors, warnings=r.warnings, rows=r.rows, evaluated_at=r.evaluated_at,
            report=_loads(r.report) if with_report else None,
        )
        for r in rows
    ]


@router.get("/datasources/{ds_id}/contract/odcs")
def export_odcs(ds_id: int, user: User = Depends(get_current_user), session: Session = Depends(get_session)) -> Response:
    """The contract in the open ODCS format (YAML): what is handed to a catalog or another
    tool. Export only: the source stays the contract here."""
    ds = _datasource(session, user, ds_id, Capability.VIEW)
    c = _contract(session, ds_id)
    try:
        columns = json.loads(ds.columns or "[]")
        descriptions = json.loads(ds.column_descriptions or "{}")
    except json.JSONDecodeError:
        columns, descriptions = [], {}
    document = odcs.to_odcs(
        datasource_id=ds.id, name=ds.name, description=ds.description or "",
        columns=columns if isinstance(columns, list) else [],
        column_descriptions=descriptions if isinstance(descriptions, dict) else {},
        document=_loads(c.document) or {"rules": []}, version=c.version, enabled=c.enabled, created_at=c.created_at,
    )
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", ds.name).strip("._") or f"datasource_{ds.id}"
    return Response(
        content=odcs.dump_yaml(document), media_type="application/yaml",
        headers={"Content-Disposition": f'attachment; filename="{name}.odcs.yaml"'},
    )


# ── writing ──────────────────────────────────────────────────────────────────
def _notify_targets(session: Session, user: User, ds_id: int, body: ContractIn) -> tuple[str | None, int | None] | None:
    """(addresses, SMTP connection) to save, checked; None = do not touch them.

    Deciding TO WHOM a mail server sends asks for CONNECT on its connection: both when it
    is chosen, and when the addresses change keeping the one there. Whoever only changes
    the rules sends everything back as it is and does not need it; and turning the
    notices off is always allowed."""
    if body.notify_emails is None and body.notify_connection_id is None:
        return None
    current_one = contracts.get(session, ds_id)
    before = (current_one.notify_emails, current_one.notify_connection_id) if current_one else (None, None)
    emails, conn_id = before
    if body.notify_emails is not None:
        emails = ", ".join(notify_targets.parse_recipients(body.notify_emails)) or None
    if body.notify_connection_id is not None:
        conn_id = None if body.notify_connection_id in (0, -1) else body.notify_connection_id
    if emails and conn_id:
        if (emails, conn_id) != before:
            notify_targets.smtp_connection(session, user, conn_id)
        notify_targets.ensure_deliverable(session.get(Connection, conn_id), emails.split(", "))
    elif conn_id and conn_id != before[1]:
        notify_targets.smtp_connection(session, user, conn_id)   # chosen without addresses: it must still be SMTP and theirs
    return emails, conn_id


@router.put("/datasources/{ds_id}/contract", response_model=ContractOut)
async def put_contract(
    ds_id: int, body: ContractIn, request: Request,
    user: User = Depends(_user), session: Session = Depends(get_session),
):
    """Creates or replaces the contract, then checks it on the current data. The check here
    cannot block anything (there is no new snapshot to refuse): it only says whether the
    data there already respects it."""
    try:
        document = contracts.validate_document(body.document)
    except contracts.ContractInvalid as e:
        raise HTTPException(status_code=422, detail={"message": "The contract is not valid", "errors": e.errors})

    def save() -> tuple[dict, bool]:
        ds = _datasource(session, user, ds_id, Capability.EDIT)
        c = contracts.save(session, ds, document, body.enabled, user.id, notify=_notify_targets(session, user, ds_id, body))
        to_check = c.enabled and c.status == "pending" and bool(document["rules"])
        snap = _snapshot(ds)
        session.commit()
        audit.record_audit(
            session, actor=user, action=audit.CONTRACT_SAVE, target_type="datasource", target_id=ds_id,
            target_label=ds.name, detail={"version": c.version, "rules": len(document["rules"]), "enabled": c.enabled,
                                          "notify": bool(c.notify_emails and c.notify_connection_id)},
            request=request,
        )
        _release_db(session, user)
        return snap, to_check

    snap, to_check = await run_in_threadpool(save)
    report, error = (None, None)
    if to_check:
        report, error = await _ask_engine("/contracts/evaluate", {**snap, "document": document})

    def close() -> ContractOut:
        c = _contract(session, ds_id)
        # the report holds only if the contract is still the one just saved
        if report is not None and json.loads(c.document) == document:
            contracts.record_result(session, c, report, trigger="save", snapshot_key=snap["key"])
            session.commit()
            session.refresh(c)
        out = _out(c, error)
        _release_db(session, user)
        return out

    return await run_in_threadpool(close)


@router.delete("/datasources/{ds_id}/contract", status_code=status.HTTP_204_NO_CONTENT)
def delete_contract(
    ds_id: int, request: Request, user: User = Depends(get_current_user), session: Session = Depends(get_session),
):
    ds = _datasource(session, user, ds_id, Capability.EDIT)
    _contract(session, ds_id)
    contracts.forget(session, ds_id, keep_history=True)
    session.commit()
    audit.record_audit(
        session, actor=user, action=audit.CONTRACT_DELETE, target_type="datasource", target_id=ds_id,
        target_label=ds.name, request=request,
    )


@router.post("/datasources/{ds_id}/contract/check", response_model=ContractOut)
async def check_contract(ds_id: int, user: User = Depends(_user), session: Session = Depends(get_session)):
    """Checks the contract now on the data the datasource is serving."""

    def before() -> tuple[dict, dict]:
        ds = _datasource(session, user, ds_id, Capability.EDIT)
        c = _contract(session, ds_id)
        data = (_snapshot(ds), _loads(c.document) or {"rules": []})
        _release_db(session, user)
        return data

    snap, document = await run_in_threadpool(before)
    report, error = await _ask_engine("/contracts/evaluate", {**snap, "document": document})

    def after() -> ContractOut:
        c = _contract(session, ds_id)
        if report is not None and (_loads(c.document) or {}) == document:
            contracts.record_result(session, c, report, trigger="manual", snapshot_key=snap["key"])
            session.commit()
            session.refresh(c)
        out = _out(c, error)
        _release_db(session, user)
        return out

    return await run_in_threadpool(after)


@router.post("/datasources/{ds_id}/contract/proposal")
async def propose_contract(ds_id: int, user: User = Depends(_user), session: Session = Depends(get_session)) -> dict:
    """A contract proposed from the current data. It does not save it: it is a starting point."""

    def before() -> dict:
        ds = _datasource(session, user, ds_id, Capability.EDIT)
        snap = _snapshot(ds)
        _release_db(session, user)
        return snap

    snap = await run_in_threadpool(before)
    answer, error = await _ask_engine("/contracts/profile", {"bucket": snap["bucket"], "key": snap["key"]})
    if answer is None:
        raise HTTPException(status_code=502, detail=error or "Data profile not available")
    return {"document": answer.get("proposal") or {"rules": []}, "profile": answer.get("profile")}
