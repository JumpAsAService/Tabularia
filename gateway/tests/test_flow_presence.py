"""Avviso "questo flusso è aperto anche da …".

Due persone possono aprire lo stesso flusso e l'ultimo che salva sovrascrive
l'altro in silenzio. Per scelta non si blocca: si avvisa. Questi test fissano il
registro (chi c'è, chi scade, chi se ne va) e il fatto che a vederlo sia solo chi
può vedere il flusso.
"""
import pytest
from fastapi import HTTPException

from app.models.permission import Capability
from app.routes.flows import PresenceBeat, flow_presence_beat, flow_presence_leave
from app.services import flow_presence as fp
from tests.conftest import make_flow, make_project, make_user


@pytest.fixture(autouse=True)
def _clean():
    fp.reset()
    yield
    fp.reset()


# ── il registro ────────────────────────────────────────────────────────────────
def test_alone_there_is_nobody_else():
    assert fp.beat(1, "tab-aaaa", 10, "a@x.it", "A", now=100) == []


def test_the_second_one_sees_the_first_and_vice_versa():
    fp.beat(1, "tab-aaaa", 10, "a@x.it", "A", now=100)
    assert [o["email"] for o in fp.beat(1, "tab-bbbb", 20, "b@x.it", "B", now=105)] == ["a@x.it"]
    assert [o["email"] for o in fp.beat(1, "tab-aaaa", 10, "a@x.it", "A", now=110)] == ["b@x.it"]


def test_a_second_tab_of_the_same_user_counts_too():
    """Una seconda scheda sovrascrive quanto una seconda persona."""
    fp.beat(1, "tab-aaaa", 10, "a@x.it", "A", now=100)
    assert [o["email"] for o in fp.beat(1, "tab-a2a2", 10, "a@x.it", "A", now=101)] == ["a@x.it"]


def test_other_flows_are_not_mixed_in():
    fp.beat(1, "tab-aaaa", 10, "a@x.it", "A", now=100)
    assert fp.beat(2, "tab-bbbb", 20, "b@x.it", "B", now=100) == []


def test_whoever_stops_beating_expires():
    """Scheda chiusa di colpo, rete caduta: nessun DELETE arriva mai."""
    fp.beat(1, "tab-aaaa", 10, "a@x.it", "A", now=100)
    assert fp.beat(1, "tab-bbbb", 20, "b@x.it", "B", now=100 + fp.TTL_SECONDS + 1) == []


def test_a_beat_keeps_you_alive_and_keeps_since():
    fp.beat(1, "tab-aaaa", 10, "a@x.it", "A", now=100)
    fp.beat(1, "tab-aaaa", 10, "a@x.it", "A", now=100 + fp.TTL_SECONDS - 1)
    others = fp.beat(1, "tab-bbbb", 20, "b@x.it", "B", now=100 + fp.TTL_SECONDS + 5)
    assert [o["since"] for o in others] == [100]


def test_leaving_is_immediate():
    fp.beat(1, "tab-aaaa", 10, "a@x.it", "A", now=100)
    fp.leave(1, "tab-aaaa")
    assert fp.beat(1, "tab-bbbb", 20, "b@x.it", "B", now=101) == []


def test_the_registry_cannot_be_flooded():
    for i in range(fp._MAX_INSTANCES_PER_FLOW + 20):
        fp.beat(1, f"tab-{i:04d}", i, f"{i}@x.it", "", now=100)
    assert len(fp._open[1]) == fp._MAX_INSTANCES_PER_FLOW


# ── la rotta: chi può vedere il flusso ─────────────────────────────────────────
def test_the_route_answers_who_else_is_there(session):
    admin = make_user(session, email="admin@x.it", is_superuser=True)
    altro = make_user(session, email="altro@x.it", is_superuser=True)
    p = make_project(session, name="p")
    flow = make_flow(session, name="f", project_id=p.id)
    assert flow_presence_beat(flow.id, PresenceBeat(instance="tab-aaaa"), admin, session).others == []
    out = flow_presence_beat(flow.id, PresenceBeat(instance="tab-bbbb"), altro, session)
    assert [o.email for o in out.others] == ["admin@x.it"]
    assert out.others[0].since.tzinfo is not None  # esce con l'offset, come ogni datetime
    flow_presence_leave(flow.id, "tab-aaaa", admin, session)
    assert flow_presence_beat(flow.id, PresenceBeat(instance="tab-bbbb"), altro, session).others == []


def test_someone_who_cannot_see_the_flow_learns_nothing(session):
    admin = make_user(session, email="admin@x.it", is_superuser=True)
    estraneo = make_user(session, email="fuori@x.it")
    p = make_project(session, name="p")
    flow = make_flow(session, name="f", project_id=p.id)
    flow_presence_beat(flow.id, PresenceBeat(instance="tab-aaaa"), admin, session)
    with pytest.raises(HTTPException) as e:
        flow_presence_beat(flow.id, PresenceBeat(instance="tab-xxxx"), estraneo, session)
    assert e.value.status_code in (403, 404)
    assert "tab-xxxx" not in fp._open.get(flow.id, {})  # e non lascia traccia


@pytest.mark.parametrize("bad", ["a", "x" * 65, "con spazi", "a/b", ""])
def test_an_odd_instance_id_is_rejected(bad):
    with pytest.raises(Exception):
        PresenceBeat(instance=bad)
