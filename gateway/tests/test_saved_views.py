"""Viste salvate: configurazioni del Viewer nel catalogo, dentro una cartella.

I contratti che contano:

1. la vista SEGUE il dato — memorizza un riferimento alla datasource e la
   configurazione, mai delle righe;
2. è CONDIVISA: la legge chi ha VIEW sulla cartella, la scrive chi ha EDIT
   (come i flussi, perché è un oggetto della stessa natura);
3. il nome è unico nella cartella, e la configurazione dev'essere JSON valido —
   salvarne una rotta trasformerebbe un errore di chi salva in un errore di chi
   apre;
4. ogni scrittura lascia traccia nell'audit.
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


def _azioni(session) -> list[str]:
    return [r.action for r in session.exec(select(AuditLog)).all()]


def _cartella_con_datasource(session):
    p = make_project(session, name="vendite")
    ds = make_datasource(session, name="ordini", project_id=p.id)
    return p, ds


def _crea(session, user, project_id, ds_id, nome="vista", spec=SPEC):
    return create_saved_view(
        project_id,
        SavedViewCreate(name=nome, datasource_id=ds_id, spec=spec),
        request=None, user=user, session=session,
    )


# ── 1. la vista è un riferimento, non una copia ─────────────────────────────
def test_it_stores_a_reference_and_a_configuration(session):
    admin = _admin(session)
    p, ds = _cartella_con_datasource(session)
    vista = _crea(session, admin, p.id, ds.id)

    assert vista.datasource_id == ds.id
    assert json.loads(vista.spec)["filters"][0]["column"] == "stato"
    # nessuna riga, nessun bucket, nessuna chiave: non c'è nulla da conservare
    assert not hasattr(vista, "bucket") and not hasattr(vista, "key")


def test_the_list_resolves_the_datasource_name(session):
    """L'elenco di una cartella deve dire SU COSA è la vista senza che il client
    risolva N id."""
    admin = _admin(session)
    p, ds = _cartella_con_datasource(session)
    _crea(session, admin, p.id, ds.id)

    elenco = list_project_saved_views(p.id, user=admin, session=session)
    assert [v.datasource_name for v in elenco] == ["ordini"]


def test_a_missing_datasource_is_refused(session):
    admin = _admin(session)
    p = make_project(session, name="vuota")
    with pytest.raises(HTTPException) as e:
        _crea(session, admin, p.id, 999)
    assert e.value.status_code == 404


# ── 2. condivisa: VIEW per leggere, EDIT per scrivere ───────────────────────
def test_view_lets_you_read_but_not_write(session):
    admin = _admin(session)
    p, ds = _cartella_con_datasource(session)
    vista = _crea(session, admin, p.id, ds.id)

    lettore = make_user(session, email="lettore@x.local")
    make_permission(session, user_id=lettore.id, project_id=p.id, capability=Capability.VIEW.value)

    # legge: l'elenco e la singola vista
    assert len(list_project_saved_views(p.id, user=lettore, session=session)) == 1
    assert get_saved_view(vista.id, user=lettore, session=session).name == "vista"

    # ma non scrive
    for azione in (
        lambda: _crea(session, lettore, p.id, ds.id, nome="mia"),
        lambda: update_saved_view(vista.id, SavedViewUpdate(name="altra"), request=None, user=lettore, session=session),
        lambda: delete_saved_view(vista.id, request=None, user=lettore, session=session),
    ):
        with pytest.raises(HTTPException) as e:
            azione()
        assert e.value.status_code == 403


def test_without_any_permission_you_do_not_even_see_it(session):
    admin = _admin(session)
    p, ds = _cartella_con_datasource(session)
    _crea(session, admin, p.id, ds.id)

    estraneo = make_user(session, email="estraneo@x.local")
    with pytest.raises(HTTPException) as e:
        list_project_saved_views(p.id, user=estraneo, session=session)
    assert e.value.status_code == 403


# ── 3. nome unico e configurazione valida ───────────────────────────────────
def test_the_name_is_unique_within_the_folder(session):
    admin = _admin(session)
    p, ds = _cartella_con_datasource(session)
    _crea(session, admin, p.id, ds.id, nome="margine")
    with pytest.raises(HTTPException) as e:
        _crea(session, admin, p.id, ds.id, nome="margine")
    assert e.value.status_code == 409

    # ma lo stesso nome in un'ALTRA cartella va bene
    altra = make_project(session, name="acquisti")
    assert _crea(session, admin, altra.id, ds.id, nome="margine").name == "margine"


def test_a_blank_name_is_refused(session):
    admin = _admin(session)
    p, ds = _cartella_con_datasource(session)
    with pytest.raises(HTTPException) as e:
        _crea(session, admin, p.id, ds.id, nome="   ")
    assert e.value.status_code == 422


def test_a_malformed_spec_is_refused_on_the_way_in(session):
    admin = _admin(session)
    p, ds = _cartella_con_datasource(session)
    with pytest.raises(HTTPException) as e:
        _crea(session, admin, p.id, ds.id, spec="{non json")
    assert e.value.status_code == 422
    assert session.exec(select(SavedView)).all() == []  # niente salvato a metà


def test_an_update_validates_the_spec_too(session):
    admin = _admin(session)
    p, ds = _cartella_con_datasource(session)
    vista = _crea(session, admin, p.id, ds.id)
    with pytest.raises(HTTPException) as e:
        update_saved_view(vista.id, SavedViewUpdate(spec="{rotto"), request=None, user=admin, session=session)
    assert e.value.status_code == 422


# ── spostamento fra cartelle ────────────────────────────────────────────────
def test_moving_needs_edit_on_the_destination(session):
    admin = _admin(session)
    p, ds = _cartella_con_datasource(session)
    dest = make_project(session, name="destinazione")
    vista = _crea(session, admin, p.id, ds.id)

    spostata = update_saved_view(
        vista.id, SavedViewUpdate(project_id=dest.id), request=None, user=admin, session=session
    )
    assert spostata.project_id == dest.id

    editore = make_user(session, email="editore@x.local")
    make_permission(session, user_id=editore.id, project_id=dest.id, capability=Capability.EDIT.value)
    with pytest.raises(HTTPException) as e:  # EDIT sulla destinazione, non sull'origine
        update_saved_view(
            spostata.id, SavedViewUpdate(project_id=p.id), request=None, user=editore, session=session
        )
    assert e.value.status_code == 403


# ── eliminazione ────────────────────────────────────────────────────────────
def test_delete_removes_it(session):
    admin = _admin(session)
    p, ds = _cartella_con_datasource(session)
    vista = _crea(session, admin, p.id, ds.id)
    delete_saved_view(vista.id, request=None, user=admin, session=session)
    assert session.exec(select(SavedView)).all() == []


def test_delete_missing_is_404(session):
    with pytest.raises(HTTPException) as e:
        delete_saved_view(999, request=None, user=_admin(session), session=session)
    assert e.value.status_code == 404


# ── 4. audit ────────────────────────────────────────────────────────────────
def test_every_write_is_audited(session):
    admin = _admin(session)
    p, ds = _cartella_con_datasource(session)
    vista = _crea(session, admin, p.id, ds.id)
    assert audit.SAVED_VIEW_CREATE in _azioni(session)

    update_saved_view(vista.id, SavedViewUpdate(name="rinominata"), request=None, user=admin, session=session)
    assert audit.SAVED_VIEW_UPDATE in _azioni(session)

    delete_saved_view(vista.id, request=None, user=admin, session=session)
    assert audit.SAVED_VIEW_DELETE in _azioni(session)
