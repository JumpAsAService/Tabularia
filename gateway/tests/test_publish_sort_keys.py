"""Chiavi di ordinamento nel PUBLISH di un flusso.

Il flusso ordina il risultato prima di scriverlo (parquet ordinato → pruning a
valle) e la datasource pubblicata eredita le chiavi. Senza chiavi, tutto come
prima. Le tre parti: il body del resolver (scheduler), l'iniezione dell'op sort
al lancio, l'ereditarietà sulla datasource creata.
"""
import json

import pytest
from sqlmodel import select

from app.models import Datasource, Run
from app.routes.runs import _launch_flow_run, _publish_datasource
from app.schemas.models import PublishSpec, RunCreate
from app.services.flow_resolver import _output_body
from tests.conftest import make_flow, make_project, make_user

pytestmark = pytest.mark.anyio


# ── 1. resolver (percorso scheduler): il nodo porta le chiavi nel body ──────────
def test_output_body_carries_sort_keys_from_the_node():
    node = {"type": "output", "data": {"destType": "datasource", "name": "x", "projectId": 1, "sortKeys": ["id", "data"]}}
    body = _output_body(node, ("bucket", "key"), [], "bucket")
    assert body["publish"]["sort_keys"] == ["id", "data"]


def test_output_body_without_keys_is_empty():
    node = {"type": "output", "data": {"destType": "datasource", "name": "x", "projectId": 1}}
    assert _output_body(node, ("bucket", "key"), [], "bucket")["publish"]["sort_keys"] == []


# ── 2. lancio: op sort in coda + chiavi salvate sul run ─────────────────────────
def _body(project_id, *, sort_keys):
    return RunCreate(
        bucket="data-prep", input_key="datasets/x.parquet", operations=[{"type": "select", "params": {"columns": ["id"]}}],
        publish=PublishSpec(name="vendite", project_id=project_id, sort_keys=sort_keys),
    )


async def test_publish_with_keys_appends_a_sort_op(session, fake_engine):
    admin = make_user(session, email="a@x.local", is_superuser=True)
    p = make_project(session, name="p")
    flow = make_flow(session, name="f", project_id=p.id)

    run = await _launch_flow_run(session, admin, flow, _body(p.id, sort_keys=["id", "data"]))

    ops = fake_engine.transforms[0]["operations"]
    assert ops[-1] == {
        "type": "sort",
        "params": {"by": ["id", "data"], "ignore_missing": True},
    }, ops
    assert ops[0]["type"] == "select", "le operazioni originali restano davanti"
    assert json.loads(session.get(Run, run.id).publish_sort_keys) == ["id", "data"]


async def test_the_injected_sort_tolerates_a_vanished_key(session, fake_engine):
    """`ignore_missing` è la differenza fra «la chiave non c'è più, ordino per le
    altre» e «ogni run di questo flusso fallisce». La checklist mostra solo le
    colonne esistenti, quindi una chiave stantia non sarebbe nemmeno togliibile."""
    admin = make_user(session, email="a@x.local", is_superuser=True)
    p = make_project(session, name="p")
    flow = make_flow(session, name="f", project_id=p.id)

    await _launch_flow_run(session, admin, flow, _body(p.id, sort_keys=["colonna_sparita"]))

    sort_op = fake_engine.transforms[0]["operations"][-1]
    assert sort_op["params"]["ignore_missing"] is True


async def test_publish_without_keys_adds_no_sort_op(session, fake_engine):
    admin = make_user(session, email="a@x.local", is_superuser=True)
    p = make_project(session, name="p")
    flow = make_flow(session, name="f", project_id=p.id)

    await _launch_flow_run(session, admin, flow, _body(p.id, sort_keys=[]))

    ops = fake_engine.transforms[0]["operations"]
    assert all(o["type"] != "sort" for o in ops), "niente sort se non ci sono chiavi"


async def test_blank_keys_are_dropped_at_launch(session, fake_engine):
    admin = make_user(session, email="a@x.local", is_superuser=True)
    p = make_project(session, name="p")
    flow = make_flow(session, name="f", project_id=p.id)

    await _launch_flow_run(session, admin, flow, _body(p.id, sort_keys=["  ", "id", ""]))
    assert fake_engine.transforms[0]["operations"][-1]["params"]["by"] == ["id"]


# ── 3. la datasource pubblicata eredita le chiavi ───────────────────────────────
def test_the_published_datasource_inherits_the_keys(session):
    admin = make_user(session, email="a@x.local", is_superuser=True)
    p = make_project(session, name="p")
    run = Run(
        flow_id=None, task_id="abcd1234efgh", launched_by=admin.id,
        publish_name="vendite", publish_project_id=p.id, publish_sort_keys=json.dumps(["id", "data"]),
        input_key="in", output_bucket="b", output_key="datasets/new/out.parquet",
    )
    session.add(run)
    session.commit()
    session.refresh(run)

    _publish_datasource(session, run, {"columns": [{"name": "id", "dtype": "Int64"}], "rows_written": 10})
    session.commit()

    ds = session.exec(select(Datasource).where(Datasource.name == "vendite")).one()
    assert json.loads(ds.sort_keys) == ["id", "data"]
