"""The log's expiry: off by default, precise when on."""
from datetime import datetime, timedelta, timezone

from sqlmodel import select

from app.models import AuditLog
from app.services.audit_retention import purge


def _item(session, minutes_ago: int, action: str = "flow.run") -> AuditLog:
    at = datetime.now(timezone.utc).replace(tzinfo=None)
    v = AuditLog(action=action, actor_label="tizio@x.it", outcome="success",
                 ts=at - timedelta(minutes=minutes_ago))
    session.add(v)
    session.commit()
    return v


def test_without_a_window_nothing_is_deleted(session):
    """The default is «forever»: deleting a security log cannot happen to someone without
    their asking."""
    _item(session, 60 * 24 * 365)
    assert purge(session, 0) == 0
    assert len(session.exec(select(AuditLog)).all()) == 1


def test_only_what_is_past_the_window_goes(session):
    _item(session, 120, "vecchia")
    _item(session, 61, "appena oltre")
    _item(session, 59, "appena dentro")
    _item(session, 1, "recente")

    assert purge(session, 60) == 2
    left = sorted(a.action for a in session.exec(select(AuditLog)).all())
    assert left == ["appena dentro", "recente"]


def test_the_edge_is_kept_not_dropped(session):
    """At the exact threshold the item STAYS: what is older than the window is deleted, not
    what touches it."""
    at = datetime.now(timezone.utc).replace(tzinfo=None)
    session.add(AuditLog(action="al limite", actor_label="x", outcome="success",
                         ts=at - timedelta(minutes=30)))
    session.commit()
    assert purge(session, 30, at=at) == 0
    assert len(session.exec(select(AuditLog)).all()) == 1


def test_it_empties_a_backlog_bigger_than_one_batch(session):
    """A first pass on an old log must not stop at the first batch."""
    at = datetime.now(timezone.utc).replace(tzinfo=None)
    session.add_all([
        AuditLog(action="vecchia", actor_label="x", outcome="success",
                 ts=at - timedelta(minutes=120))
        for _ in range(120)
    ])
    session.commit()
    assert purge(session, 60) == 120
    assert session.exec(select(AuditLog)).all() == []
