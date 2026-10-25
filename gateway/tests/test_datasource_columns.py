"""Descriptions of a datasource's FIELDS (`Datasource.column_descriptions`).

A {column: text} map curated by hand, kept apart from the `columns` schema (which every
refresh regenerates): it survives the refreshes and travels inside
`columns[*].description` too.
"""
import json

import pytest
from fastapi import HTTPException

from app.routes.datasources import _to_out, clean_column_descriptions, update_datasource
from app.schemas.models import DatasourceUpdate
from tests.conftest import make_datasource, make_project, make_user


def test_clean_drops_empty_and_trims():
    out = clean_column_descriptions({" prezzo ": "  netto EUR ", "vuota": "   ", "": "x"})
    assert out == {"prezzo": "netto EUR"}


def test_clean_rejects_too_long():
    with pytest.raises(HTTPException) as e:
        clean_column_descriptions({"c": "x" * 2001})
    assert e.value.status_code == 422


def test_out_merges_descriptions_into_columns(session):
    p = make_project(session, name="p")
    ds = make_datasource(
        session, project_id=p.id,
        columns=json.dumps([{"name": "id", "dtype": "Int64"}, {"name": "prezzo", "dtype": "Float64"}]),
        column_descriptions=json.dumps({"prezzo": "netto EUR", "sparita": "colonna non più nello schema"}),
    )
    out = _to_out(ds)
    assert out.column_descriptions == {"prezzo": "netto EUR", "sparita": "colonna non più nello schema"}
    by = {c["name"]: c for c in out.columns}
    assert by["prezzo"]["description"] == "netto EUR"
    assert "description" not in by["id"]  # not described: key missing, not an empty string


def test_update_replaces_map_and_survives_schema_change(session):
    admin = make_user(session, email="a@x.local", is_superuser=True)
    p = make_project(session, name="p")
    ds = make_datasource(session, project_id=p.id, columns=json.dumps([{"name": "a", "dtype": "Int64"}]))
    out = update_datasource(ds.id, DatasourceUpdate(column_descriptions={"a": "campo A", "b": ""}), user=admin, session=session)
    assert out.column_descriptions == {"a": "campo A"}
    # updating name/description does NOT touch the fields' descriptions (omitted = unchanged)
    out = update_datasource(ds.id, DatasourceUpdate(description="nuova"), user=admin, session=session)
    assert out.column_descriptions == {"a": "campo A"}
    # a refresh rewrites the schema: the description stays tied to the name
    ds.columns = json.dumps([{"name": "a", "dtype": "String"}, {"name": "c", "dtype": "Int64"}])
    session.add(ds); session.commit(); session.refresh(ds)
    out = _to_out(ds)
    assert {c["name"]: c.get("description") for c in out.columns} == {"a": "campo A", "c": None}
    # empty map = delete everything
    out = update_datasource(ds.id, DatasourceUpdate(column_descriptions={}), user=admin, session=session)
    assert out.column_descriptions == {}


def test_datasource_description_is_trimmed_and_capped(session):
    """The DATASOURCE's description is curated as the fields' are: spaces stripped, length
    limited, and it stays when only the fields are saved."""
    from app.routes.datasources import MAX_DATASOURCE_DESCRIPTION

    admin = make_user(session, email="a@x.local", is_superuser=True)
    make_project(session, name="p")
    ds = make_datasource(session, columns=json.dumps([{"name": "x", "dtype": "Int64"}]))
    out = update_datasource(ds.id, DatasourceUpdate(description="  Ordini 2024, una riga per ordine.  "), user=admin, session=session)
    assert out.description == "Ordini 2024, una riga per ordine."
    out = update_datasource(ds.id, DatasourceUpdate(column_descriptions={"x": "id"}), user=admin, session=session)
    assert out.description == "Ordini 2024, una riga per ordine."  # not touched
    with pytest.raises(HTTPException) as e:
        update_datasource(ds.id, DatasourceUpdate(description="x" * (MAX_DATASOURCE_DESCRIPTION + 1)), user=admin, session=session)
    assert e.value.status_code == 422
    assert update_datasource(ds.id, DatasourceUpdate(description=""), user=admin, session=session).description == ""
