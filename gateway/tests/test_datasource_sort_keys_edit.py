"""Sort keys can be changed after the import (review 2026-09-16, R3).

They used to be write-once: a mistyped key made every import and refresh fail
for good, and the only way out was deleting the datasource. Now a PATCH replaces
them, checked against the columns of the last import when there is one.
"""
import json

import pytest
from fastapi import HTTPException

from app.models import Connection
from app.models.permission import Capability
from app.routes.datasources import update_datasource
from app.routes.runs import launch_ingest_run
from app.schemas.models import DatasourceUpdate
from tests.conftest import make_datasource, make_permission, make_project, make_user

COLUMNS = json.dumps([{"name": "id", "dtype": "Int64"}, {"name": "day", "dtype": "Date"}, {"name": "amount", "dtype": "Float64"}])


def _db_datasource(session, project_id, **kw):
    kw.setdefault("columns", COLUMNS)
    return make_datasource(session, project_id=project_id, kind="database", source_type="table", source_ref="t", **kw)


def _keys(ds):
    return json.loads(ds.sort_keys)


def test_keys_are_replaced_cleaned_and_kept_in_order(session):
    admin = make_user(session, email="a@x.local", is_superuser=True)
    p = make_project(session, name="P")
    ds = _db_datasource(session, p.id, sort_keys=json.dumps(["id"]))
    out = update_datasource(ds.id, DatasourceUpdate(sort_keys=[" day ", "id", "", "day"]), user=admin, session=session)
    assert out.sort_keys == ["day", "id"]
    session.refresh(ds)
    assert _keys(ds) == ["day", "id"]


def test_an_empty_list_removes_the_ordering(session):
    admin = make_user(session, email="a@x.local", is_superuser=True)
    p = make_project(session, name="P")
    ds = _db_datasource(session, p.id, sort_keys=json.dumps(["id"]))
    assert update_datasource(ds.id, DatasourceUpdate(sort_keys=[]), user=admin, session=session).sort_keys == []


def test_a_key_that_is_not_a_column_is_refused_and_nothing_changes(session):
    admin = make_user(session, email="a@x.local", is_superuser=True)
    p = make_project(session, name="P")
    ds = _db_datasource(session, p.id, sort_keys=json.dumps(["id"]))
    with pytest.raises(HTTPException) as e:
        update_datasource(ds.id, DatasourceUpdate(sort_keys=["day", "dya"]), user=admin, session=session)
    assert e.value.status_code == 422 and "dya" in e.value.detail
    session.refresh(ds)
    assert _keys(ds) == ["id"]


def test_without_known_columns_the_keys_are_taken_as_typed(session):
    # the stuck case: the first import failed on a mistyped key, so no column is known
    admin = make_user(session, email="a@x.local", is_superuser=True)
    p = make_project(session, name="P")
    ds = _db_datasource(session, p.id, columns="[]", sort_keys=json.dumps(["dya"]))
    assert update_datasource(ds.id, DatasourceUpdate(sort_keys=["day"]), user=admin, session=session).sort_keys == ["day"]


def test_a_stale_key_can_be_removed_while_keeping_the_others(session):
    # a column renamed at the source: the stored key is no longer a column
    admin = make_user(session, email="a@x.local", is_superuser=True)
    p = make_project(session, name="P")
    ds = _db_datasource(session, p.id, sort_keys=json.dumps(["old_day", "id"]))
    assert update_datasource(ds.id, DatasourceUpdate(sort_keys=["id"]), user=admin, session=session).sort_keys == ["id"]


def test_a_flow_datasource_takes_its_keys_from_the_output_node(session):
    admin = make_user(session, email="a@x.local", is_superuser=True)
    p = make_project(session, name="P")
    ds = make_datasource(session, project_id=p.id, kind="flow", columns=COLUMNS)
    with pytest.raises(HTTPException) as e:
        update_datasource(ds.id, DatasourceUpdate(sort_keys=["id"]), user=admin, session=session)
    assert e.value.status_code == 422 and "Output node" in e.value.detail


def test_sharepoint_imports_have_no_sort_keys(session):
    admin = make_user(session, email="a@x.local", is_superuser=True)
    p = make_project(session, name="P")
    ds = make_datasource(session, project_id=p.id, kind="database", source_type="sharepoint", columns=COLUMNS)
    with pytest.raises(HTTPException) as e:
        update_datasource(ds.id, DatasourceUpdate(sort_keys=["id"]), user=admin, session=session)
    assert e.value.status_code == 422


def test_changing_them_needs_edit(session):
    reader = make_user(session, email="r@x.local")
    p = make_project(session, name="P")
    make_permission(session, user_id=reader.id, project_id=p.id, capability=Capability.VIEW)
    ds = _db_datasource(session, p.id, sort_keys=json.dumps(["id"]))
    with pytest.raises(HTTPException) as e:
        update_datasource(ds.id, DatasourceUpdate(sort_keys=["day"]), user=reader, session=session)
    assert e.value.status_code == 403


def test_other_fields_leave_the_keys_alone(session):
    admin = make_user(session, email="a@x.local", is_superuser=True)
    p = make_project(session, name="P")
    ds = _db_datasource(session, p.id, sort_keys=json.dumps(["id"]))
    update_datasource(ds.id, DatasourceUpdate(description="orders"), user=admin, session=session)
    session.refresh(ds)
    assert _keys(ds) == ["id"]


@pytest.mark.anyio
async def test_the_next_refresh_orders_on_the_new_keys(session, fake_engine):
    admin = make_user(session, email="a@x.local", is_superuser=True)
    p = make_project(session, name="P")
    conn = Connection(name="c", db_type="postgresql", project_id=p.id, owner_id=admin.id,
                      host="h", port=5432, username="u", database="d")
    session.add(conn)
    session.commit()
    session.refresh(conn)
    ds = _db_datasource(session, p.id, connection_id=conn.id, sort_keys=json.dumps(["id"]))
    update_datasource(ds.id, DatasourceUpdate(sort_keys=["day", "id"]), user=admin, session=session)
    session.refresh(ds)
    await launch_ingest_run(session, admin, ds, conn)
    assert fake_engine.ingests[-1][1]["source"]["sort_keys"] == ["day", "id"]
