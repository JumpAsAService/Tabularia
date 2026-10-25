"""The "this flow is also open by …" warning.

Two people may open the same flow and whoever saves last overwrites the other silently.
By choice nothing is blocked: it warns. These tests pin the register (who is there, who
expires, who leaves) and the fact that only whoever can see the flow sees it.
"""
import pytest
from fastapi import HTTPException

from app.models.permission import Capability
from app.routes.flows import PresenceBeat, flow_presence_beat, flow_presence_leave
from app.services import flow_presence as fp
from tests.conftest import make_flow, make_project, make_user


# ── the register ───────────────────────────────────────────────────────────────
def test_alone_there_is_nobody_else(session):
    assert fp.beat(session, 1, "tab-aaaa", 10, "a@x.it", "A", now=100) == []


def test_the_second_one_sees_the_first_and_vice_versa(session):
    fp.beat(session, 1, "tab-aaaa", 10, "a@x.it", "A", now=100)
    assert [o["email"] for o in fp.beat(session, 1, "tab-bbbb", 20, "b@x.it", "B", now=105)] == ["a@x.it"]
    assert [o["email"] for o in fp.beat(session, 1, "tab-aaaa", 10, "a@x.it", "A", now=110)] == ["b@x.it"]


def test_a_second_tab_of_the_same_user_counts_too(session):
    """A second tab overwrites as much as a second person."""
    fp.beat(session, 1, "tab-aaaa", 10, "a@x.it", "A", now=100)
    assert [o["email"] for o in fp.beat(session, 1, "tab-a2a2", 10, "a@x.it", "A", now=101)] == ["a@x.it"]


def test_other_flows_are_not_mixed_in(session):
    fp.beat(session, 1, "tab-aaaa", 10, "a@x.it", "A", now=100)
    assert fp.beat(session, 2, "tab-bbbb", 20, "b@x.it", "B", now=100) == []


def test_whoever_stops_beating_expires(session):
    """Tab closed abruptly, network down: no DELETE ever arrives."""
    fp.beat(session, 1, "tab-aaaa", 10, "a@x.it", "A", now=100)
    assert fp.beat(session, 1, "tab-bbbb", 20, "b@x.it", "B", now=100 + fp.TTL_SECONDS + 1) == []


def test_a_beat_keeps_you_alive_and_keeps_since(session):
    fp.beat(session, 1, "tab-aaaa", 10, "a@x.it", "A", now=100)
    fp.beat(session, 1, "tab-aaaa", 10, "a@x.it", "A", now=100 + fp.TTL_SECONDS - 1)
    others = fp.beat(session, 1, "tab-bbbb", 20, "b@x.it", "B", now=100 + fp.TTL_SECONDS + 5)
    assert [o["since"] for o in others] == [100]


def test_leaving_is_immediate(session):
    fp.beat(session, 1, "tab-aaaa", 10, "a@x.it", "A", now=100)
    fp.leave(session, 1, "tab-aaaa")
    assert fp.beat(session, 1, "tab-bbbb", 20, "b@x.it", "B", now=101) == []


def test_the_registry_cannot_be_flooded(session):
    for i in range(fp._MAX_INSTANCES_PER_FLOW + 20):
        fp.beat(session, 1, f"tab-{i:04d}", i, f"{i}@x.it", "", now=100)
    from sqlmodel import select

    from app.models import FlowPresence

    assert len(session.exec(select(FlowPresence).where(FlowPresence.flow_id == 1)).all()) == fp._MAX_INSTANCES_PER_FLOW


def test_two_replicas_see_the_same_people(db_engine):
    """The reason it sits in a table: whoever beats through one replica must be seen by
    whoever goes through another. Two sessions = two processes."""
    from sqlmodel import Session

    with Session(db_engine) as replica_a, Session(db_engine) as replica_b:
        fp.beat(replica_a, 1, "tab-aaaa", 10, "a@x.it", "A", now=100)
        assert [o["email"] for o in fp.beat(replica_b, 1, "tab-bbbb", 20, "b@x.it", "B", now=101)] == ["a@x.it"]
        assert [o["email"] for o in fp.beat(replica_a, 1, "tab-aaaa", 10, "a@x.it", "A", now=102)] == ["b@x.it"]


def test_expired_rows_are_swept(session):
    from sqlmodel import select

    from app.models import FlowPresence

    fp.beat(session, 1, "tab-aaaa", 10, "a@x.it", "A", now=100)
    fp.beat(session, 2, "tab-bbbb", 20, "b@x.it", "B", now=100 + fp.TTL_SECONDS)
    assert fp.clean(session, now=100 + fp.TTL_SECONDS + 1) == 1
    assert [r.instance for r in session.exec(select(FlowPresence)).all()] == ["tab-bbbb"]


def test_coming_back_after_expiring_starts_over(session):
    fp.beat(session, 1, "tab-aaaa", 10, "a@x.it", "A", now=100)
    later = 100 + fp.TTL_SECONDS + 50
    fp.beat(session, 1, "tab-aaaa", 10, "a@x.it", "A", now=later)
    assert [o["since"] for o in fp.beat(session, 1, "tab-bbbb", 20, "b@x.it", "B", now=later + 1)] == [later]


# ── the route: who may see the flow ────────────────────────────────────────────
def test_the_route_answers_who_else_is_there(session):
    admin = make_user(session, email="admin@x.it", is_superuser=True)
    other = make_user(session, email="altro@x.it", is_superuser=True)
    p = make_project(session, name="p")
    flow = make_flow(session, name="f", project_id=p.id)
    assert flow_presence_beat(flow.id, PresenceBeat(instance="tab-aaaa"), admin, session).others == []
    out = flow_presence_beat(flow.id, PresenceBeat(instance="tab-bbbb"), other, session)
    assert [o.email for o in out.others] == ["admin@x.it"]
    assert out.others[0].since.tzinfo is not None  # goes out with the offset, as every datetime
    flow_presence_leave(flow.id, "tab-aaaa", admin, session)
    assert flow_presence_beat(flow.id, PresenceBeat(instance="tab-bbbb"), other, session).others == []


def test_someone_who_cannot_see_the_flow_learns_nothing(session):
    admin = make_user(session, email="admin@x.it", is_superuser=True)
    stranger = make_user(session, email="fuori@x.it")
    p = make_project(session, name="p")
    flow = make_flow(session, name="f", project_id=p.id)
    flow_presence_beat(flow.id, PresenceBeat(instance="tab-aaaa"), admin, session)
    with pytest.raises(HTTPException) as e:
        flow_presence_beat(flow.id, PresenceBeat(instance="tab-xxxx"), stranger, session)
    assert e.value.status_code in (403, 404)
    from app.models import FlowPresence

    assert session.get(FlowPresence, (flow.id, "tab-xxxx")) is None  # and leaves no trace


@pytest.mark.parametrize("bad", ["a", "x" * 65, "con spazi", "a/b", ""])
def test_an_odd_instance_id_is_rejected(bad):
    with pytest.raises(Exception):
        PresenceBeat(instance=bad)


def test_an_observer_sees_masked_colleagues(session):
    """An observer (the shared guest of a demo) reads emails masked everywhere else:
    the editor's «also open by» must not hand them out in clear."""
    from app.models import Group, UserGroupLink
    from app.routes.flows import flow_presence_beat, PresenceBeat
    from tests.conftest import make_flow, make_permission, make_project, make_user

    p = make_project(session, name="p")
    flow = make_flow(session, name="f", project_id=p.id)
    anna = make_user(session, email="anna.rossi@azienda.it", full_name="Anna Rossi", is_superuser=True)
    guest = make_user(session, email="guest@x.it")
    observers = Group(name="observers", is_observer=True)
    session.add(observers)
    session.commit()
    session.add(UserGroupLink(user_id=guest.id, group_id=observers.id))
    make_permission(session, user_id=guest.id, project_id=p.id)
    session.commit()
    flow_presence_beat(flow.id, PresenceBeat(instance="tab-anna"), user=anna, session=session)
    (other,) = flow_presence_beat(flow.id, PresenceBeat(instance="tab-guest"), user=guest, session=session).others
    assert other.email != "anna.rossi@azienda.it" and other.email.endswith("@azienda.it")
    assert other.full_name == ""
    # the administrator still sees who is there
    (seen,) = flow_presence_beat(flow.id, PresenceBeat(instance="tab-anna"), user=anna, session=session).others
    assert seen.email == "guest@x.it"
