"""Il refresh schedulato riverifica i permessi ogni volta che scatta.

Audit 2026-09-19, A7: `_fire_ds` controllava solo che l'autore dello schedule
esistesse e fosse attivo. Togliere CONNECT a qualcuno non fermava il suo cron, e
chi aveva EDIT poteva spostarsi in casa una datasource schedulata da altri e
farsi rifornire di dati con l'autorità della vittima. L'orchestratore faceva già
i due controlli: lo scheduler no.
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
    """Una datasource database schedulata da Alice, che ha tutto ciò che serve."""
    cartella_dati = make_project(session, name="Dati")
    cartella_conn = make_project(session, name="Connessioni")
    alice = make_user(session, email="alice@x.it")
    conn = Connection(
        name="prod", db_type="postgresql", project_id=cartella_conn.id, owner_id=alice.id,
        host="db.example", port=5432, username="u", database="d",
    )
    session.add(conn)
    session.commit()
    session.refresh(conn)
    ds = make_datasource(
        session, name="clienti", project_id=cartella_dati.id, kind="database",
        connection_id=conn.id, refresh_schedule="0 3 * * *", refresh_scheduled_by=alice.id,
        next_refresh_at=datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=1),
    )
    for progetto, cap in ((cartella_dati, Capability.RUN), (cartella_conn, Capability.CONNECT)):
        session.add(Permission(project_id=progetto.id, user_id=alice.id, capability=cap))
    session.commit()

    lanciati = []

    async def _finto_launch(session, user, ds, conn, trigger_type="manual"):
        lanciati.append((user.email, ds.id))

    monkeypatch.setattr(scheduler, "launch_ingest_run", _finto_launch)
    return ds, conn, alice, lanciati


def _scatta(session, ds):
    asyncio.run(scheduler._fire_ds(session, ds, datetime.now(timezone.utc)))


def test_with_the_permissions_in_place_it_fires(session, scenario):
    ds, _, alice, lanciati = scenario
    _scatta(session, ds)
    assert lanciati == [(alice.email, ds.id)]
    assert ds.refresh_schedule == "0 3 * * *"  # resta attivo


@pytest.mark.parametrize("revocata", [Capability.RUN, Capability.CONNECT])
def test_revoking_a_capability_stops_the_cron(session, scenario, revocata):
    ds, _, _, lanciati = scenario
    perm = next(
        p for p in session.query(Permission).all() if p.capability == revocata
    )
    session.delete(perm)
    session.commit()

    _scatta(session, ds)

    assert lanciati == []  # nessun ingest lanciato
    assert ds.refresh_schedule is None and ds.refresh_scheduled_by is None  # e lo schedule si spegne


def test_moving_a_datasource_drops_someone_elses_schedule(session, scenario):
    """La seconda metà di A7: chi ha EDIT non si porta in casa l'autorità altrui."""
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
