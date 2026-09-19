"""Prestazioni dei run: aggregati calcolati AL VOLO dalla tabella `runs`, senza
tabelle nuove. Solo admin."""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

from app.deps.auth import require_superuser
from app.models import Run
from app.routes.performance import _pct, runs_performance
from tests.conftest import make_datasource, make_flow, make_project, make_user

NOW = datetime.now(timezone.utc).replace(tzinfo=None)


def _run(session, *, flow_id=None, ds_id=None, status="SUCCESS", dur=10.0, wait=1.0, ago_h=1.0, rows=100, error=None):
    start = NOW - timedelta(hours=ago_h)
    r = Run(kind="ingest" if ds_id else "flow", flow_id=flow_id, datasource_id=ds_id, status=status, task_id=f"t{ago_h}{dur}{status}",
            input_key="k", output_bucket="b", output_key="o", started_at=start, engine_started_at=start + timedelta(seconds=wait),
            finished_at=start + timedelta(seconds=wait + dur), rows_written=rows, error=error)
    session.add(r); session.commit()


def test_flows_are_ranked_by_the_time_they_cost(session):
    p = make_project(session, name="p")
    lento, veloce = make_flow(session, name="lento", project_id=p.id), make_flow(session, name="veloce", project_id=p.id)
    for d in (100, 120, 300): _run(session, flow_id=lento.id, dur=d, wait=0)
    for d in (2, 3): _run(session, flow_id=veloce.id, dur=d, wait=0)
    out = runs_performance(days=7, session=session)
    assert [i["name"] for i in out["items"]] == ["lento", "veloce"]
    top = out["items"][0]
    assert (top["runs"], top["median_s"], top["max_s"], top["total_s"]) == (3, 120, 300, 520)


def test_failures_count_but_do_not_pollute_the_durations(session):
    p = make_project(session, name="p"); f = make_flow(session, name="f", project_id=p.id)
    _run(session, flow_id=f.id, dur=10)
    _run(session, flow_id=f.id, status="FAILURE", dur=0.5, error="colonna sparita", ago_h=0.5)
    i = runs_performance(days=7, session=session)["items"][0]
    assert (i["runs"], i["failures"], i["median_s"], i["last_error"]) == (2, 1, 10, "colonna sparita")


def test_queue_wait_is_what_tells_if_the_workers_are_enough(session):
    p = make_project(session, name="p"); f = make_flow(session, name="f", project_id=p.id)
    for w in (0.2, 0.4, 45.0): _run(session, flow_id=f.id, wait=w)
    out = runs_performance(days=7, session=session)
    assert out["totals"]["median_wait_s"] == pytest.approx(0.4) and out["totals"]["p95_wait_s"] == pytest.approx(45.0)


def test_datasource_refreshes_are_a_separate_line(session):
    p = make_project(session, name="p"); f = make_flow(session, name="f", project_id=p.id)
    ds = make_datasource(session, name="ordini", project_id=p.id, kind="database")
    _run(session, flow_id=f.id); _run(session, ds_id=ds.id, dur=270)
    kinds = {(i["kind"], i["name"]) for i in runs_performance(days=7, session=session)["items"]}
    assert kinds == {("flow", "f"), ("ingest", "ordini")}


def test_runs_outside_the_window_are_left_out(session):
    p = make_project(session, name="p"); f = make_flow(session, name="f", project_id=p.id)
    _run(session, flow_id=f.id, ago_h=1); _run(session, flow_id=f.id, ago_h=24 * 10)
    assert runs_performance(days=7, session=session)["totals"]["runs"] == 1
    assert runs_performance(days=30, session=session)["totals"]["runs"] == 2


def test_percentile_of_nothing_and_of_one():
    assert _pct([], 0.95) is None and _pct([7.0], 0.95) == 7.0


def test_it_is_for_admins_only(session):
    with pytest.raises(HTTPException) as e:
        require_superuser(make_user(session, email="u@x.it"), session)
    assert e.value.status_code == 403


def test_deleting_a_group_that_holds_permissions_works(session):
    """Esplodeva in un 500 sul vincolo di chiave esterna dei permessi."""
    from app.models import Group, Permission
    from app.models.permission import Capability
    from app.routes.groups import delete_group
    from tests.conftest import make_project

    p = make_project(session, name="p")
    g = Group(name="g"); session.add(g); session.commit(); session.refresh(g)
    session.add(Permission(project_id=p.id, group_id=g.id, capability=Capability.VIEW)); session.commit()
    delete_group(g.id, session=session, current=make_user(session, email="capo@x.it", is_superuser=True))
    assert session.get(Group, g.id) is None and session.query(Permission).count() == 0
