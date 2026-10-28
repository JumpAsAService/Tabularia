"""Groups. Read for every authenticated user; write for superusers only."""
from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.db.session import get_session
from app.deps.auth import get_current_user, require_superuser
from app.models import ContainerRegistryAccess, Group, Permission, ScriptEnvironmentGroup, User, UserGroupLink
from app.schemas.models import GroupOut, GroupCreate, GroupUpdate
from app.services import audit
from app.services.permissions import ensure_still_admin
from app.core.routing import ReleasingRoute

router = APIRouter(route_class=ReleasingRoute, prefix="/groups", tags=["groups"])


@router.get("", response_model=list[GroupOut], dependencies=[Depends(get_current_user)])
def list_groups(session: Session = Depends(get_session)):
    # how many members per group, in ONE query (the admin list shows it for each)
    counts = dict(
        session.exec(
            select(UserGroupLink.group_id, func.count()).group_by(UserGroupLink.group_id)  # type: ignore[arg-type]
        ).all()
    )
    return [
        GroupOut(
            id=g.id, name=g.name, description=g.description,
            created_at=g.created_at, member_count=counts.get(g.id, 0),
            is_admin=g.is_admin, is_observer=g.is_observer,
        )
        for g in session.exec(select(Group)).all()
    ]


@router.post("", response_model=GroupOut, status_code=status.HTTP_201_CREATED)
def create_group(
    body: GroupCreate,
    session: Session = Depends(get_session),
    current: User = Depends(require_superuser),
    request: Request = None,  # type: ignore[assignment]
):
    if session.exec(select(Group).where(Group.name == body.name)).first():
        raise HTTPException(status_code=409, detail="There is already a group with this name")
    group = Group(name=body.name, description=body.description)
    session.add(group)
    try:
        session.commit()
    except IntegrityError:  # the same name created twice at once: the unique index stops the second
        session.rollback()
        raise HTTPException(status_code=409, detail="There is already a group with this name") from None
    session.refresh(group)
    audit.record_audit(session, actor=current, action=audit.GROUP_CREATE, request=request,
                       target_type="group", target_id=group.id, target_label=group.name)
    session.refresh(group)
    # explicit as in list_groups: so the route tells the truth even to whoever calls it
    # directly, without going through FastAPI's serialization
    return GroupOut(
        id=group.id, name=group.name, description=group.description,
        created_at=group.created_at, member_count=0,  # just created: no members
    )


@router.patch("/{group_id}", response_model=GroupOut)
def update_group(
    group_id: int,
    body: GroupUpdate,
    request: Request = None,  # type: ignore[assignment]
    session: Session = Depends(get_session),
    current: User = Depends(require_superuser),
):
    """Description and, above all, the two flags: ADMIN (from then on every member of the
    group is an administrator, and stops being one by leaving it) and OBSERVER (every
    member reads the panels without being able to write in them). They are independent: an
    admin group does not need the second, because whoever administers reads already."""
    group = session.get(Group, group_id)
    if group is None:
        raise HTTPException(status_code=404, detail="Group not found")
    was_admin = group.is_admin
    was_observer = group.is_observer
    if (body.is_admin and not was_admin) or (body.is_observer and not was_observer):
        # every member would receive the role: a way in that would stay after a leaked token is
        # revoked
        from app.routes.api_tokens import deny_if_from_token
        from app.services import api_tokens

        deny_if_from_token(request, api_tokens.NO_ROLES_FROM_TOKEN)
    described = (
        {"from": group.description, "to": body.description}
        if body.description is not None and body.description != group.description else None
    )
    if body.description is not None:
        group.description = body.description
    if body.is_admin is not None:
        group.is_admin = body.is_admin
    if body.is_observer is not None:
        group.is_observer = body.is_observer
    session.add(group)
    if was_admin and not group.is_admin:
        ensure_still_admin(session, current)
    if group.is_observer and not was_observer:
        from app.services import api_tokens

        session.flush()
        member_ids = session.exec(select(UserGroupLink.user_id).where(UserGroupLink.group_id == group_id)).all()
        api_tokens.withdraw_if_observer(session, member_ids)
    session.commit()
    session.refresh(group)
    members = session.exec(
        select(func.count()).select_from(UserGroupLink).where(UserGroupLink.group_id == group_id)
    ).one()
    if described:
        audit.record_audit(
            session, actor=current, request=request, action=audit.GROUP_UPDATE,
            target_type="group", target_id=group.id, target_label=group.name,
            detail={"description": described},
        )
    if group.is_admin != was_admin:
        audit.record_audit(
            session, actor=current, request=request,
            action=audit.GROUP_PROMOTE if group.is_admin else audit.GROUP_DEMOTE,
            target_type="group", target_id=group.id, target_label=group.name,
            detail={"members": members},
        )
    if group.is_observer != was_observer:
        audit.record_audit(
            session, actor=current, request=request,
            action=audit.GROUP_OBSERVER_GRANT if group.is_observer else audit.GROUP_OBSERVER_REVOKE,
            target_type="group", target_id=group.id, target_label=group.name,
            detail={"members": members},
        )
    return GroupOut(
        id=group.id, name=group.name, description=group.description,
        created_at=group.created_at, member_count=members,
        is_admin=group.is_admin, is_observer=group.is_observer,
    )


@router.delete("/{group_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_group(
    group_id: int,
    request: Request = None,  # type: ignore[assignment]
    session: Session = Depends(get_session),
    current: User = Depends(require_superuser),
):
    group = session.get(Group, group_id)
    if group is None:
        raise HTTPException(status_code=404, detail="Group not found")
    was_admin, was_observer, name = group.is_admin, group.is_observer, group.name
    links = session.exec(select(UserGroupLink).where(UserGroupLink.group_id == group_id)).all()
    for link in links:
        session.delete(link)
    # the PERMISSIONS granted to the group too: the foreign key protects them, and without
    # this the deletion blew up in a 500
    for perm in session.exec(select(Permission).where(Permission.group_id == group_id)).all():
        session.delete(perm)
    # and what the administration panel gives it: scripting environments, container
    # registries (their keys protect them too)
    for row in session.exec(select(ScriptEnvironmentGroup).where(ScriptEnvironmentGroup.group_id == group_id)).all():
        session.delete(row)
    for row in session.exec(select(ContainerRegistryAccess).where(ContainerRegistryAccess.group_id == group_id)).all():
        session.delete(row)
    session.flush()
    session.delete(group)
    if was_admin:
        ensure_still_admin(session, current)
    session.commit()
    # the group, with how many people were in it: their memberships go with it, and this
    # one action stands for all of them (no `group.member_remove` per person)
    detail: dict = {"members": len(links)}
    if was_admin:
        detail["admin"] = True
    if was_observer:
        detail["observer"] = True
    audit.record_audit(
        session, actor=current, action=audit.GROUP_DELETE, request=request,
        target_type="group", target_id=group_id, target_label=name, detail=detail,
    )
    if was_admin:
        # an administrators' group disappears: its members lose the privileges
        audit.record_audit(
            session, actor=current, action=audit.GROUP_DEMOTE, request=request,
            target_type="group", target_id=group_id, target_label=name,
            detail={"deleted": True},
        )
    if was_observer:
        # an observers' group too: its members no longer read the panels
        audit.record_audit(
            session, actor=current, action=audit.GROUP_OBSERVER_REVOKE, request=request,
            target_type="group", target_id=group_id, target_label=name,
            detail={"deleted": True},
        )
