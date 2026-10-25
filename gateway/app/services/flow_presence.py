"""Who has a flow open in the editor, now.

Two people may open the same flow and the LAST to save overwrites the other, silently
(the version history keeps the work, but one has to know to look for it). By choice
nothing is blocked and the changes are not merged: it WARNS. The editor says "I am
here" every few seconds and gets back who else is there; whoever stops saying it —
tab closed, network down — expires by itself.

In a TABLE (`flow_presence`) and not in memory: with several gateway replicas two
people on the same flow would talk to different processes, and each would see only
who came by it — the warning would be missing exactly when needed. A heartbeat is an
UPDATE by key and a read of the flow's other rows; the expired ones are not read
(filtered on the last heartbeat) and the scheduler removes them.
"""
import time

from sqlalchemy import delete, func, update
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.models.shared_state import FlowPresence

# an instance not heard from for a long time is considered closed; the editor beats
# every HEARTBEAT_SECONDS, so two lost beats are tolerated
HEARTBEAT_SECONDS = 15
TTL_SECONDS = 40
_MAX_INSTANCES_PER_FLOW = 50  # a safety cap: nobody fills the table


def beat(session: Session, flow_id: int, instance: str, user_id: int, email: str, full_name: str, now: float | None = None) -> list[dict]:
    """Records `instance`'s heartbeat and returns the OTHER instances open on the same flow
    (the same user's too: a second tab overwrites as much as a second person)."""
    now = time.time() if now is None else now
    alive = (FlowPresence.flow_id == flow_id) & (FlowPresence.seen >= now - TTL_SECONDS)
    beaten = session.exec(
        update(FlowPresence).where(alive, FlowPresence.instance == instance).values(seen=now)
    )
    if beaten.rowcount == 0:
        # first time (or back after expiring: `since` starts again)
        session.exec(delete(FlowPresence).where(FlowPresence.flow_id == flow_id, FlowPresence.instance == instance))
        how_many = session.exec(select(func.count()).select_from(FlowPresence).where(alive)).one()
        if how_many < _MAX_INSTANCES_PER_FLOW:
            session.add(FlowPresence(flow_id=flow_id, instance=instance, user_id=user_id, email=email, full_name=full_name, since=now, seen=now))
    try:
        session.commit()
    except IntegrityError:  # the same tab beat twice together: it is there already
        session.rollback()
    others = session.exec(
        select(FlowPresence).where(alive, FlowPresence.instance != instance).order_by(FlowPresence.since)
    ).all()
    out = [{"user_id": e.user_id, "email": e.email, "full_name": e.full_name, "since": e.since} for e in others]
    session.rollback()  # reading done: the connection goes back to the pool
    return out


def leave(session: Session, flow_id: int, instance: str) -> None:
    session.exec(delete(FlowPresence).where(FlowPresence.flow_id == flow_id, FlowPresence.instance == instance))
    session.commit()


def clean(session: Session, now: float | None = None) -> int:
    """Removes the tabs that no longer beat (the scheduler calls it)."""
    now = time.time() if now is None else now
    removed_ones = session.exec(delete(FlowPresence).where(FlowPresence.seen < now - TTL_SECONDS))
    session.commit()
    return removed_ones.rowcount or 0
