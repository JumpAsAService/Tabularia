"""Saved views: Viewer configurations in the catalog, inside a folder.

The contracts that count:

1. the view FOLLOWS the data — it stores a reference to the datasource and the
   configuration, never rows;
2. it is SHARED: whoever has VIEW on the folder reads it, whoever has EDIT writes it
   (as for flows, because it is an object of the same nature);
3. the name is unique in the folder, and the configuration must be valid JSON — saving
   a broken one would turn an error of whoever saves into an error of whoever opens;
4. every write leaves a trace in the audit.
"""
import json

import pytest
from fastapi import HTTPException
from sqlmodel import select

from app.models import AuditLog, SavedView
from app.models.permission import Capability
from app.routes.saved_views import (
    create_saved_view,
    delete_saved_view,
    get_saved_view,
    list_project_saved_views,
    update_saved_view,
)
from app.schemas.models import SavedViewCreate, SavedViewUpdate
from app.services import audit
from tests.conftest import make_datasource, make_permission, make_project, make_user

SPEC = json.dumps({
    "engine": "polars",
    "filters": [{"column": "stato", "operator": "eq", "value": "evaso", "value2": ""}],
    "computedFields": [],
    "pivotOn": False,
    "pivot": {"index": [""], "on": [""], "values": "", "func": "sum"},
    "outline": False,
})


def _admin(session):
    return make_user(session, email="admin@x.local", is_superuser=True)


def _actions(session) -> list[str]:
    return [r.action for r in session.exec(select(AuditLog)).all()]


def _folder_with_datasource(session):
    p = make_project(session, name="vendite")
    ds = make_datasource(session, name="ordini", project_id=p.id)
    return p, ds


def _create(session, user, project_id, ds_id, name="vista", spec=SPEC):
    return create_saved_view(
        project_id,
        SavedViewCreate(name=name, datasource_id=ds_id, spec=spec),
        request=None, user=user, session=session,
    )


# ── 1. the view is a reference, not a copy ──────────────────────────────────
def test_it_stores_a_reference_and_a_configuration(session):
    admin = _admin(session)
    p, ds = _folder_with_datasource(session)
    view = _create(session, admin, p.id, ds.id)

    assert view.datasource_id == ds.id
    assert json.loads(view.spec)["filters"][0]["column"] == "stato"
    # no rows, no bucket, no key: there is nothing to keep
    assert not hasattr(view, "bucket") and not hasattr(view, "key")


def test_the_list_resolves_the_datasource_name(session):
    """A folder's listing must say WHAT the view is about without the client resolving N ids."""
    admin = _admin(session)
    p, ds = _folder_with_datasource(session)
    _create(session, admin, p.id, ds.id)

    listing = list_project_saved_views(p.id, user=admin, session=session)
    assert [v.datasource_name for v in listing] == ["ordini"]


def test_a_missing_datasource_is_refused(session):
    admin = _admin(session)
    p = make_project(session, name="vuota")
    with pytest.raises(HTTPException) as e:
        _create(session, admin, p.id, 999)
    assert e.value.status_code == 404


# ── 2. shared: VIEW to read, EDIT to write ──────────────────────────────────
def test_view_lets_you_read_but_not_write(session):
    admin = _admin(session)
    p, ds = _folder_with_datasource(session)
    view = _create(session, admin, p.id, ds.id)

    reader = make_user(session, email="lettore@x.local")
    make_permission(session, user_id=reader.id, project_id=p.id, capability=Capability.VIEW.value)

    # reads: the list and the single view
    assert len(list_project_saved_views(p.id, user=reader, session=session)) == 1
    assert get_saved_view(view.id, user=reader, session=session).name == "vista"

    # but does not write
    for action in (
        lambda: _create(session, reader, p.id, ds.id, name="mia"),
        lambda: update_saved_view(view.id, SavedViewUpdate(name="altra"), request=None, user=reader, session=session),
        lambda: delete_saved_view(view.id, request=None, user=reader, session=session),
    ):
        with pytest.raises(HTTPException) as e:
            action()
        assert e.value.status_code == 403


def test_without_any_permission_you_do_not_even_see_it(session):
    admin = _admin(session)
    p, ds = _folder_with_datasource(session)
    _create(session, admin, p.id, ds.id)

    stranger = make_user(session, email="estraneo@x.local")
    with pytest.raises(HTTPException) as e:
        list_project_saved_views(p.id, user=stranger, session=session)
    assert e.value.status_code == 403


# ── 3. unique name and valid configuration ──────────────────────────────────
def test_the_name_is_unique_within_the_folder(session):
    admin = _admin(session)
    p, ds = _folder_with_datasource(session)
    _create(session, admin, p.id, ds.id, name="margine")
    with pytest.raises(HTTPException) as e:
        _create(session, admin, p.id, ds.id, name="margine")
    assert e.value.status_code == 409

    # but the same name in ANOTHER folder is fine
    other_one = make_project(session, name="acquisti")
    assert _create(session, admin, other_one.id, ds.id, name="margine").name == "margine"


def test_a_blank_name_is_refused(session):
    admin = _admin(session)
    p, ds = _folder_with_datasource(session)
    with pytest.raises(HTTPException) as e:
        _create(session, admin, p.id, ds.id, name="   ")
    assert e.value.status_code == 422


def test_a_malformed_spec_is_refused_on_the_way_in(session):
    admin = _admin(session)
    p, ds = _folder_with_datasource(session)
    with pytest.raises(HTTPException) as e:
        _create(session, admin, p.id, ds.id, spec="{non json")
    assert e.value.status_code == 422
    assert session.exec(select(SavedView)).all() == []  # nothing saved halfway


def test_an_update_validates_the_spec_too(session):
    admin = _admin(session)
    p, ds = _folder_with_datasource(session)
    view = _create(session, admin, p.id, ds.id)
    with pytest.raises(HTTPException) as e:
        update_saved_view(view.id, SavedViewUpdate(spec="{rotto"), request=None, user=admin, session=session)
    assert e.value.status_code == 422


# ── moving between folders ──────────────────────────────────────────────────
def test_moving_needs_edit_on_the_destination(session):
    admin = _admin(session)
    p, ds = _folder_with_datasource(session)
    dest = make_project(session, name="destinazione")
    view = _create(session, admin, p.id, ds.id)

    moved = update_saved_view(
        view.id, SavedViewUpdate(project_id=dest.id), request=None, user=admin, session=session
    )
    assert moved.project_id == dest.id

    editor = make_user(session, email="editore@x.local")
    make_permission(session, user_id=editor.id, project_id=dest.id, capability=Capability.EDIT.value)
    with pytest.raises(HTTPException) as e:  # EDIT on the destination, not on the origin
        update_saved_view(
            moved.id, SavedViewUpdate(project_id=p.id), request=None, user=editor, session=session
        )
    assert e.value.status_code == 403


# ── deletion ────────────────────────────────────────────────────────────────
def test_delete_removes_it(session):
    admin = _admin(session)
    p, ds = _folder_with_datasource(session)
    view = _create(session, admin, p.id, ds.id)
    delete_saved_view(view.id, request=None, user=admin, session=session)
    assert session.exec(select(SavedView)).all() == []


def test_delete_missing_is_404(session):
    with pytest.raises(HTTPException) as e:
        delete_saved_view(999, request=None, user=_admin(session), session=session)
    assert e.value.status_code == 404


# ── 4. audit ────────────────────────────────────────────────────────────────
def test_every_write_is_audited(session):
    admin = _admin(session)
    p, ds = _folder_with_datasource(session)
    view = _create(session, admin, p.id, ds.id)
    assert audit.SAVED_VIEW_CREATE in _actions(session)

    update_saved_view(view.id, SavedViewUpdate(name="rinominata"), request=None, user=admin, session=session)
    assert audit.SAVED_VIEW_UPDATE in _actions(session)

    delete_saved_view(view.id, request=None, user=admin, session=session)
    assert audit.SAVED_VIEW_DELETE in _actions(session)
