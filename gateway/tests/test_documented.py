"""When a datasource can be called «ready for the AI».

The rule lives on the server because three pages show it: if each computed it, they
would diverge at the first change.
"""
import pytest

from app.services.documented import count_described, is_ai_ready

COLUMNS = [{"name": "paese", "dtype": "String"}, {"name": "totale", "dtype": "Float64"}]


def test_a_description_counts_from_either_place():
    """Curated by hand or already inside the column: it holds either way."""
    assert count_described(COLUMNS, {"paese": "il paese", "totale": "in euro"}) == (2, 2)
    inside = [{"name": "paese", "description": "il paese"}, {"name": "totale"}]
    assert count_described(inside, {}) == (1, 2)


def test_whitespace_is_not_a_description():
    assert count_described(COLUMNS, {"paese": "   ", "totale": "\n"}) == (0, 2)


def test_ready_needs_the_table_and_every_field():
    assert is_ai_ready("ordini per paese", 2, 2) is True
    assert is_ai_ready("", 2, 2) is False           # the table does not say what it is
    assert is_ai_ready("ordini", 1, 2) is False     # a field stays anonymous


def test_a_datasource_without_columns_is_not_ready():
    """Zero columns is not «all described»: it is a table we know nothing about yet."""
    assert is_ai_ready("descritta benissimo", 0, 0) is False


def test_the_flag_travels_with_the_datasource(session):
    from app.routes import datasources as ds_routes
    from tests.conftest import make_datasource, make_project
    import json

    p = make_project(session, name="P")
    ds = make_datasource(
        session, name="ordini", project_id=p.id, description="ordini per paese",
        columns=json.dumps(COLUMNS), column_descriptions=json.dumps({"paese": "il paese", "totale": "in euro"}),
    )
    out = ds_routes._to_out(ds)
    assert out.ai_ready is True and (out.described_columns, out.total_columns) == (2, 2)

    ds.description = ""
    out = ds_routes._to_out(ds)
    assert out.ai_ready is False and out.described_columns == 2  # the count stays true
