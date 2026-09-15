"""sort_keys sulla datasource: si salvano e tornano al client (per riproporli nel
form e per farli ereditare alla copia materializzata del viewer)."""
import json

from app.models import Datasource
from app.routes.datasources import _to_out
from app.schemas.models import DbDatasourceCreate


def test_sort_keys_round_trip_through_the_serializer():
    ds = Datasource(id=1, name="ordini", project_id=1, bucket="b", key="k", sort_keys=json.dumps(["id", "data"]))
    assert _to_out(ds).sort_keys == ["id", "data"]


def test_missing_or_broken_sort_keys_serialize_as_empty():
    ds = Datasource(id=2, name="x", project_id=1, bucket="b", key="k", sort_keys="{rotto")
    assert _to_out(ds).sort_keys == []
    ds2 = Datasource(id=3, name="y", project_id=1, bucket="b", key="k")  # default
    assert _to_out(ds2).sort_keys == []


def test_the_create_schema_accepts_sort_keys_and_defaults_empty():
    assert DbDatasourceCreate(name="a", connection_id=1, source_type="table", source_ref="t").sort_keys == []
    body = DbDatasourceCreate(name="a", connection_id=1, source_type="table", source_ref="t", sort_keys=["id"])
    assert body.sort_keys == ["id"]
