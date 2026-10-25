"""The engines allowed in the installation: read and changed from the admin panel.

It serves to standardize the fleet — «here one designs only on ClickHouse» — without
having to repeat it at every flow.

The contract, chosen deliberately:

- disabling an engine prevents CHOOSING it (creation, engine change, production
  engine), but does NOT stop the flows that use it already: a switch pressed here must
  not be able to stop a scheduled DAG while it runs;
- so that standardization does not stay cosmetic, every engine reports how many flows
  still use it — it is the migration's work list, visible to the administrator instead
  of buried in the database;
- the LAST allowed engine cannot be removed: an installation without engines could no
  longer create a flow, and it would be a dead end reachable with one click.

Write for superusers only, and every change ends up in the audit: it changes what the
whole installation can do.
"""
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import func, or_
from sqlmodel import Session, select

from app.db.session import get_session
from app.deps.auth import require_observer, require_superuser
from app.models import DisabledEngine, Flow, User
from app.schemas.models import EnginePolicyOut, EnginePolicyUpdate
from app.services import audit
from app.services.engine_policy import KNOWN_ENGINES, disabled_engines
from app.core.routing import ReleasingRoute

router = APIRouter(route_class=ReleasingRoute, prefix="/engine-policy", tags=["engine-policy"])


def _flows_using(session: Session, engine_id: str) -> int:
    """The flows that would still use this engine: the DEVELOPMENT one or the PRODUCTION
    one: they are two distinct fields and a flow may be off standard for one or the
    other."""
    return session.exec(
        select(func.count())
        .select_from(Flow)
        .where(or_(Flow.engine == engine_id, Flow.production_engine == engine_id))
    ).one()


def _state(session: Session) -> list[EnginePolicyOut]:
    forbidden = disabled_engines(session)
    return [
        EnginePolicyOut(
            engine_id=e,
            allowed=e not in forbidden,
            flows_using=_flows_using(session, e),
        )
        for e in KNOWN_ENGINES
    ]


@router.get("", response_model=list[EnginePolicyOut])
def list_engine_policy(
    user: User = Depends(require_observer),
    session: Session = Depends(get_session),
):
    """All the known engines, with their state and how many flows still use them."""
    return _state(session)


@router.put("/{engine_id}", response_model=EnginePolicyOut)
def set_engine_policy(
    engine_id: str,
    body: EnginePolicyUpdate,
    request: Request = None,  # type: ignore[assignment]
    user: User = Depends(require_superuser),
    session: Session = Depends(get_session),
):
    eid = engine_id.strip().lower()
    if eid not in KNOWN_ENGINES:
        raise HTTPException(status_code=404, detail=f"Unknown engine: '{engine_id}'")

    forbidden = disabled_engines(session)
    row = session.get(DisabledEngine, eid)

    if body.allowed:
        if row is not None:
            session.delete(row)
            session.commit()
    else:
        # no dead ends: if this were the last allowed one, nobody could create a flow any more
        # nor fix it from the interface
        if len(forbidden | {eid}) >= len(KNOWN_ENGINES):
            raise HTTPException(
                status_code=422,
                detail="At least one engine must stay allowed: without one, no flow could be created.",
            )
        if row is None:
            session.add(DisabledEngine(engine_id=eid, disabled_by=user.id))
            session.commit()

    used = _flows_using(session, eid)
    audit.record_audit(
        session, actor=user,
        action=audit.ENGINE_ALLOW if body.allowed else audit.ENGINE_DISALLOW,
        target_type="engine", target_label=eid,
        # the count goes in the log: it says how many flows stay off standard AT THE MOMENT of
        # the decision, which is the useful information for whoever reads the audit months
        # later
        detail={"flows_using": used}, request=request,
    )
    return EnginePolicyOut(engine_id=eid, allowed=body.allowed, flows_using=used)
