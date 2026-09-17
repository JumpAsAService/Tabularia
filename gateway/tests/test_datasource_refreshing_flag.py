"""Lo spinner del refresh lo decide il SERVER.

Lo stato "sta importando" viveva solo nella memoria della pagina: con un refresh
da cinque minuti bastava cambiare scheda e tornare — o che il refresh fosse
partito dallo scheduler — per non vedere più nulla.
"""
from app.models import Datasource, Run
from app.routes.datasources import _refreshing_ids
from tests.conftest import make_project, make_user


def _ds(session, name, project_id, owner_id):
    d = Datasource(name=name, bucket="b", key=f"datasets/{name}.parquet", project_id=project_id, owner_id=owner_id, kind="database")
    session.add(d); session.commit(); session.refresh(d)
    return d


def _run(session, ds_id, status, kind="ingest"):
    r = Run(status=status, kind=kind, task_id=f"t-{ds_id}-{status}", input_key="k", output_bucket="b", output_key="o", datasource_id=ds_id)
    session.add(r); session.commit()


def test_only_datasources_with_a_live_ingest_are_flagged(session):
    u = make_user(session, email="a@x.it", is_superuser=True)
    p = make_project(session, name="p")
    fermo, in_corso, finito, fallito = (_ds(session, n, p.id, u.id) for n in ("fermo", "in_corso", "finito", "fallito"))
    _run(session, in_corso.id, "STARTED")
    _run(session, finito.id, "SUCCESS")
    _run(session, fallito.id, "FAILURE")
    assert _refreshing_ids(session, [fermo, in_corso, finito, fallito]) == {in_corso.id}


def test_a_pending_ingest_counts_and_an_old_success_does_not_hide_it(session):
    u = make_user(session, email="a@x.it", is_superuser=True)
    p = make_project(session, name="p")
    d = _ds(session, "d", p.id, u.id)
    _run(session, d.id, "SUCCESS")
    _run(session, d.id, "PENDING")
    assert _refreshing_ids(session, [d]) == {d.id}


def test_a_flow_run_publishing_to_the_datasource_is_not_an_ingest(session):
    u = make_user(session, email="a@x.it", is_superuser=True)
    p = make_project(session, name="p")
    d = _ds(session, "d", p.id, u.id)
    _run(session, d.id, "STARTED", kind="flow")
    assert _refreshing_ids(session, [d]) == set()


def test_an_empty_list_costs_no_query(session):
    assert _refreshing_ids(session, []) == set()
