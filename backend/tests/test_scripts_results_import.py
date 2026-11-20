"""The results of a model step (POST /scripts/results/import, docs/specs/statistical-models.md,
«A run»): the table the runner wrote is checked against the catalogue before it is copied into
Tabularia's storage, with the model card; a table of another shape is refused with the
sentence, the runner's fault and never the data's.
"""
from __future__ import annotations

import io
from pathlib import Path

import pyarrow.parquet as pq
import pytest
from botocore.exceptions import ClientError
from fastapi import HTTPException

from app.api.routes import scripts as routes
from app.core.crypto import encrypt_secret
from app.engine.models.results import read_fixture
from app.ingest.s3_destination import S3ConnectionSpec

FIXTURES = Path(__file__).parent / "fixtures" / "models"


def _parquet(table) -> bytes:
    buf = io.BytesIO()
    pq.write_table(table, buf)
    return buf.getvalue()


class FakeS3:
    def __init__(self, objects: dict[str, bytes]):
        self.objects = objects

    def head_object(self, Bucket, Key):
        if Key not in self.objects:
            raise ClientError({"Error": {"Code": "404", "Message": "Not Found"}}, "HeadObject")
        return {"ContentLength": len(self.objects[Key])}

    def download_file(self, Bucket, Key, Filename):
        Path(Filename).write_bytes(self.objects[Key])

    def get_object(self, Bucket, Key, Range=None):
        if Key not in self.objects:
            raise ClientError({"Error": {"Code": "NoSuchKey", "Message": "Not Found"}}, "GetObject")
        body = self.objects[Key]
        if Range:
            end = int(Range.split("-")[1])
            body = body[: end + 1]
        return {"Body": io.BytesIO(body)}


class FakeStorage:
    def __init__(self):
        self.uploaded: dict[str, bytes] = {}

    def upload_file(self, path, bucket, key):
        self.uploaded[f"{bucket}/{key}"] = Path(path).read_bytes()


@pytest.fixture
def world(monkeypatch):
    storage = FakeStorage()
    monkeypatch.setattr("app.utils.get_storage_service", lambda: storage)

    def install(objects: dict[str, bytes]):
        fake = FakeS3(objects)
        monkeypatch.setattr(S3ConnectionSpec, "client", lambda self: fake)
        return storage
    return install


def _call(model: str, params: dict, card_key: str | None = "step/artifacts/model.json"):
    conn = S3ConnectionSpec(endpoint_url="http://minio:9000", access_key="scripts", bucket="scripts",
                            secret_key_encrypted=encrypt_secret("s3cret"))
    return routes.results_import(routes.ResultsImportRequest(connection=conn, key="step/results.parquet", bucket="data-prep",
                                                             output_key="out/r.parquet", model=model, params=params,
                                                             card_key=card_key))


def test_the_results_of_the_variant_are_copied_with_the_card(world):
    model, params, table = read_fixture(FIXTURES / "linear-unpenalized.json")
    storage = world({"step/results.parquet": _parquet(table), "step/artifacts/model.json": b'{"format_version": 1}'})
    out = _call(model, params)
    assert out == {"found": True, "rows": table.num_rows, "card": '{"format_version": 1}'}
    assert list(storage.uploaded) == ["data-prep/out/r.parquet"]


def test_no_results_is_said_and_nothing_is_copied(world):
    storage = world({})
    model, params, _ = read_fixture(FIXTURES / "kaplan_meier-default.json")
    assert _call(model, params) == {"found": False}
    assert storage.uploaded == {}


def test_a_table_of_another_variant_is_refused_and_not_copied(world):
    # the penalized table, read as the results of an unpenalized fit: the runner's fault
    _, _, table = read_fixture(FIXTURES / "linear-penalized.json")
    model, params, _ = read_fixture(FIXTURES / "linear-unpenalized.json")
    storage = world({"step/results.parquet": _parquet(table)})
    with pytest.raises(HTTPException) as e:
        _call(model, params)
    assert e.value.status_code == 422 and e.value.detail.startswith("results.parquet:")
    assert storage.uploaded == {}


def test_a_file_that_is_not_parquet_is_refused(world):
    model, params, _ = read_fixture(FIXTURES / "cox-default.json")
    world({"step/results.parquet": b"not a parquet file"})
    with pytest.raises(HTTPException) as e:
        _call(model, params)
    assert e.value.status_code == 422 and "not a Parquet file" in e.value.detail


def test_a_card_over_the_limit_or_missing_is_not_kept(world):
    model, params, table = read_fixture(FIXTURES / "arima-manual.json")
    big = b"x" * (routes.CARD_READ_LIMIT + 10)
    world({"step/results.parquet": _parquet(table), "step/artifacts/model.json": big})
    assert _call(model, params)["card"] is None
    world({"step/results.parquet": _parquet(table)})
    assert _call(model, params)["card"] is None
    assert _call(model, params, card_key=None)["card"] is None


def test_an_unknown_model_is_refused(world):
    _, params, table = read_fixture(FIXTURES / "linear-unpenalized.json")
    world({"step/results.parquet": _parquet(table)})
    with pytest.raises(HTTPException) as e:
        _call("nope", params)
    assert e.value.status_code == 422


def test_the_parameters_are_checked_again_and_what_the_gateway_checked_passes(world):
    model, params, table = read_fixture(FIXTURES / "arima-manual.json")
    world({"step/results.parquet": _parquet(table)})
    assert _call(model, params)["found"] is True  # an ARIMA's own defaults, given back
    with pytest.raises(HTTPException) as e:
        _call(model, {**params, "nope": 1})
    assert e.value.status_code == 422 and "there is no parameter 'nope'" in e.value.detail
