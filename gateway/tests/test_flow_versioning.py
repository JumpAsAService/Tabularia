"""Versioning dei flussi (auto-versione + promozione) e statistiche d'esecuzione."""
from datetime import datetime, timedelta, timezone

import pytest
from sqlmodel import select

from app.models import FlowVersion
from app.routes.flows import (
    create_flow,
    flow_stats,
    list_flow_versions,
    promote_flow_version,
    update_flow,
)
from app.schemas.models import FlowCreate, FlowUpdate
from tests.conftest import make_project, make_run, make_user

pytestmark = pytest.mark.anyio


def _admin_and_project(session):
    return make_user(session, email="a@x.local", is_superuser=True), make_project(session, name="p")


def _versions(session, flow_id):
    return session.exec(
        select(FlowVersion).where(FlowVersion.flow_id == flow_id).order_by(FlowVersion.version)
    ).all()


async def test_create_flow_snapshots_v1(session):
    user, p = _admin_and_project(session)
    flow = create_flow(p.id, FlowCreate(name="f", definition='{"nodes":[]}'), user, session)
    vers = _versions(session, flow.id)
    assert len(vers) == 1 and vers[0].version == 1 and vers[0].note == "creazione"


async def test_update_definition_creates_version_and_dedupes(session):
    user, p = _admin_and_project(session)
    flow = create_flow(p.id, FlowCreate(name="f", definition='{"nodes":[]}'), user, session)
    update_flow(flow.id, FlowUpdate(definition='{"nodes":[1]}'), user, session)  # cambia → v2
    update_flow(flow.id, FlowUpdate(definition='{"nodes":[1]}'), user, session)  # uguale → niente
    assert [v.version for v in _versions(session, flow.id)] == [1, 2]


async def test_update_name_only_makes_no_version(session):
    user, p = _admin_and_project(session)
    flow = create_flow(p.id, FlowCreate(name="f", definition='{"nodes":[]}'), user, session)
    update_flow(flow.id, FlowUpdate(name="rinominato"), user, session)
    assert len(_versions(session, flow.id)) == 1


async def test_promote_old_version_becomes_current(session):
    user, p = _admin_and_project(session)
    flow = create_flow(p.id, FlowCreate(name="f", definition='{"v":1}'), user, session)
    update_flow(flow.id, FlowUpdate(definition='{"v":2}'), user, session)  # v2 = corrente
    promoted = promote_flow_version(flow.id, 1, user, session)  # torna alla v1
    assert promoted.definition == '{"v":1}'
    vers = _versions(session, flow.id)
    assert [v.version for v in vers] == [1, 2, 3]
    assert vers[-1].definition == '{"v":1}' and "promossa dalla v1" in vers[-1].note


async def test_list_versions_marks_current(session):
    user, p = _admin_and_project(session)
    flow = create_flow(p.id, FlowCreate(name="f", definition='{"v":1}'), user, session)
    update_flow(flow.id, FlowUpdate(definition='{"v":2}'), user, session)
    out = list_flow_versions(flow.id, user, session)
    assert out[0].version == 2 and out[0].is_current is True
    assert out[1].version == 1 and out[1].is_current is False


async def test_flow_stats_counts_and_average(session):
    user, p = _admin_and_project(session)
    flow = create_flow(p.id, FlowCreate(name="f", definition="{}"), user, session)
    now = datetime(2026, 7, 15, 10, 0, 0, tzinfo=timezone.utc)
    for tid, st, secs in [("a", "SUCCESS", 10), ("b", "SUCCESS", 20), ("c", "FAILURE", 6)]:
        make_run(session, kind="flow", flow_id=flow.id, status=st, task_id=tid,
                 started_at=now, finished_at=now + timedelta(seconds=secs))
    s = flow_stats(flow.id, user, session)
    assert s.run_count == 3 and s.success_count == 2 and s.failure_count == 1
    assert s.avg_duration_seconds == 12.0  # (10+20+6)/3
    assert s.last_run_at is not None


async def test_flow_stats_empty(session):
    user, p = _admin_and_project(session)
    flow = create_flow(p.id, FlowCreate(name="f", definition="{}"), user, session)
    s = flow_stats(flow.id, user, session)
    assert s.run_count == 0 and s.avg_duration_seconds is None and s.last_run_at is None
