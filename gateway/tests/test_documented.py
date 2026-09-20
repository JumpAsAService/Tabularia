"""Quando una datasource si può dire «pronta per l'AI».

La regola vive sul server perché tre pagine la mostrano: se ognuna se la
calcolasse, divergerebbero alla prima modifica.
"""
import pytest

from app.services.documented import conta_descritte, is_ai_ready

COLONNE = [{"name": "paese", "dtype": "String"}, {"name": "totale", "dtype": "Float64"}]


def test_a_description_counts_from_either_place():
    """Curata a mano o già dentro la colonna: vale comunque."""
    assert conta_descritte(COLONNE, {"paese": "il paese", "totale": "in euro"}) == (2, 2)
    dentro = [{"name": "paese", "description": "il paese"}, {"name": "totale"}]
    assert conta_descritte(dentro, {}) == (1, 2)


def test_whitespace_is_not_a_description():
    assert conta_descritte(COLONNE, {"paese": "   ", "totale": "\n"}) == (0, 2)


def test_ready_needs_the_table_and_every_field():
    assert is_ai_ready("ordini per paese", 2, 2) is True
    assert is_ai_ready("", 2, 2) is False           # la tabella non dice cosa è
    assert is_ai_ready("ordini", 1, 2) is False     # un campo resta anonimo


def test_a_datasource_without_columns_is_not_ready():
    """Zero colonne non è «tutto descritto»: è una tabella di cui non sappiamo
    ancora niente."""
    assert is_ai_ready("descritta benissimo", 0, 0) is False


def test_the_flag_travels_with_the_datasource(session):
    from app.routes import datasources as ds_routes
    from tests.conftest import make_datasource, make_project
    import json

    p = make_project(session, name="P")
    ds = make_datasource(
        session, name="ordini", project_id=p.id, description="ordini per paese",
        columns=json.dumps(COLONNE), column_descriptions=json.dumps({"paese": "il paese", "totale": "in euro"}),
    )
    out = ds_routes._to_out(ds)
    assert out.ai_ready is True and (out.described_columns, out.total_columns) == (2, 2)

    ds.description = ""
    out = ds_routes._to_out(ds)
    assert out.ai_ready is False and out.described_columns == 2  # il conteggio resta vero
