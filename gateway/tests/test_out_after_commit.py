"""Regressione: serializzare un oggetto ORM DOPO un commit (es. record_audit).

SQLAlchemy marca l'istanza "expired" al commit: `model_dump()` su un'istanza
expired torna {} e la costruzione dello schema di uscita esplodeva con
"N validation errors … Field required" (500 su POST /projects/{id}/connections).
I `_to_out` devono leggere gli attributi, che ricaricano dal DB.
"""
from app.models import Connection
from app.routes.connections import _to_out as conn_out
from app.routes.datasources import _to_out as ds_out
from tests.conftest import make_datasource, make_project


def test_connection_out_after_expire(session):
    p = make_project(session, name="p")
    conn = Connection(name="c", project_id=p.id, db_type="postgresql", host="h", port=5432,
                      username="u", password_encrypted="enc", database="d", db_schema="")
    session.add(conn); session.commit()
    session.expire(conn)  # come dopo il commit di record_audit
    out = conn_out(conn)
    assert out.name == "c" and out.host == "h" and out.has_password is True


def test_datasource_out_after_expire(session):
    p = make_project(session, name="p")
    ds = make_datasource(session, project_id=p.id, name="d")
    session.expire(ds)
    out = ds_out(ds)
    assert out.name == "d" and out.project_id == p.id
