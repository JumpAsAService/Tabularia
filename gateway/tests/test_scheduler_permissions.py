"""The scheduled refresh checks the permissions again every time it fires.

Audit 2026-09-19, A7: `_fire_ds` only checked that the schedule's author existed and
was active. Removing CONNECT from someone did not stop their cron, and whoever had EDIT
could move a datasource scheduled by others into their own place and get data
supplied with the victim's authority. The orchestrator made both checks already: the
scheduler did not.
"""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from app.models import Connection, Permission
from app.models.permission import Capability
from app.services import scheduler
from tests.conftest import make_datasource, make_project, make_user


@pytest.fixture
def scenario(session, monkeypatch):
    """A database datasource scheduled by Alice, who has everything needed."""
    data_folder = make_project(session, name="Dati")
    conn_folder = make_project(session, name="Connessioni")
    alice = make_user(session, email="alice@x.it")
    conn = Connection(
        name="prod", db_type="postgresql", project_id=conn_folder.id, owner_id=alice.id,
        host="db.example", port=5432, username="u", database="d",
    )
    session.add(conn)
    session.commit()
    session.refresh(conn)
    ds = make_datasource(
        session, name="clienti", project_id=data_folder.id, kind="database",
        connection_id=conn.id, refresh_schedule="0 3 * * *", refresh_scheduled_by=alice.id,
        next_refresh_at=datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=1),
    )
    for project, cap in ((data_folder, Capability.RUN), (conn_folder, Capability.CONNECT)):
        session.add(Permission(project_id=project.id, user_id=alice.id, capability=cap))
    session.commit()

    launched = []

    async def _fake_launch(session, user, ds, conn, trigger_type="manual"):
        launched.append((user.email, ds.id))

    monkeypatch.setattr(scheduler, "launch_ingest_run", _fake_launch)
    return ds, conn, alice, launched


def _fire(session, ds):
    asyncio.run(scheduler._fire_ds(session, ds, datetime.now(timezone.utc)))


def test_with_the_permissions_in_place_it_fires(session, scenario):
    ds, _, alice, launched = scenario
    _fire(session, ds)
    assert launched == [(alice.email, ds.id)]
    assert ds.refresh_schedule == "0 3 * * *"  # stays active


@pytest.mark.parametrize("revoked", [Capability.RUN, Capability.CONNECT])
def test_revoking_a_capability_stops_the_cron(session, scenario, revoked):
    ds, _, _, launched = scenario
    perm = next(
        p for p in session.query(Permission).all() if p.capability == revoked
    )
    session.delete(perm)
    session.commit()

    _fire(session, ds)

    assert launched == []  # no ingest launched
    assert ds.refresh_schedule is None and ds.refresh_scheduled_by is None  # and the schedule turns off


def test_moving_a_datasource_drops_someone_elses_schedule(session, scenario):
    """The second half of A7: whoever has EDIT does not take someone else's authority home."""
    from app.routes import datasources as ds_routes
    from app.schemas.models import DatasourceUpdate

    ds, _, alice, _ = scenario
    bob_dir = make_project(session, name="Cartella di Bob")
    bob = make_user(session, email="bob@x.it")
    session.add(Permission(project_id=bob_dir.id, user_id=bob.id, capability=Capability.EDIT))
    session.add(Permission(project_id=ds.project_id, user_id=bob.id, capability=Capability.EDIT))
    session.commit()

    ds_routes.update_datasource(ds.id, DatasourceUpdate(project_id=bob_dir.id), user=bob, session=session)

    session.refresh(ds)
    assert ds.project_id == bob_dir.id
    assert ds.refresh_schedule is None and ds.refresh_scheduled_by is None
