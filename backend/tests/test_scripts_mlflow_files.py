"""A model step's files read back for the gateway, which logs them to MLflow
(docs/specs/statistical-models.md, «What is logged»): POST /scripts/objects/read streams one
file of the scripts bucket, POST /scripts/results/rows gives some sections of the results.
"""
from __future__ import annotations

import asyncio
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


class FakeS3:
    def __init__(self, objects: dict[str, bytes]):
        self.objects = objects

    def _get(self, key: str, op: str) -> bytes:
        if key not in self.objects:
            raise ClientError({"Error": {"Code": "NoSuchKey", "Message": "Not Found"}}, op)
        return self.objects[key]

    def get_object(self, Bucket, Key):
        body = self._get(Key, "GetObject")

        class Body(io.BytesIO):
            def iter_chunks(self, size):
                while chunk := self.read(size):
                    yield chunk

        return {"Body": Body(body), "ContentLength": len(body)}

    def download_file(self, Bucket, Key, Filename):
        Path(Filename).write_bytes(self._get(Key, "HeadObject"))


@pytest.fixture
def bucket(monkeypatch):
    def install(objects: dict[str, bytes]):
        fake = FakeS3(objects)
        monkeypatch.setattr(S3ConnectionSpec, "client", lambda self: fake)
    return install


def _conn() -> S3ConnectionSpec:
    return S3ConnectionSpec(endpoint_url="http://minio:9000", access_key="scripts", bucket="scripts",
                            secret_key_encrypted=encrypt_secret("s3cret"))


def _body(response) -> bytes:
    async def read():
        return b"".join([c async for c in response.body_iterator])
    return asyncio.run(read())


def test_a_file_is_streamed_with_its_length(bucket):
    bucket({"step/artifacts/native.rds": b"\x00RDS" * 1000})
    res = routes.objects_read(routes.ObjectReadRequest(connection=_conn(), key="step/artifacts/native.rds"))
    assert res.headers["content-length"] == "4000"
    assert _body(res) == b"\x00RDS" * 1000


def test_a_missing_file_is_404(bucket):
    bucket({})
    with pytest.raises(HTTPException) as e:
        routes.objects_read(routes.ObjectReadRequest(connection=_conn(), key="step/artifacts/native.pkl"))
    assert e.value.status_code == 404


def test_the_sections_asked_in_their_order(bucket):
    _, _, table = read_fixture(FIXTURES / "linear-unpenalized.json")
    buf = io.BytesIO()
    pq.write_table(table, buf)
    bucket({"step/results.parquet": buf.getvalue()})
    got = routes.results_rows(routes.ResultsRowsRequest(connection=_conn(), key="step/results.parquet",
                                                        sections=["model", "coef"]))["rows"]
    every = sorted(table.to_pylist(), key=lambda r: r["ord"])
    want = [r for r in every if r["section"] in ("model", "coef")]
    assert [(r["section"], r["stat"]) for r in got] == [(r["section"], r["stat"]) for r in want]
    assert set(got[0]) == {"section", "level", "level_2", "stat", "value", "text"}
    assert {r["section"] for r in got} == {"model", "coef"}


def test_results_gone_are_404(bucket):
    bucket({})
    with pytest.raises(HTTPException) as e:
        routes.results_rows(routes.ResultsRowsRequest(connection=_conn(), key="step/results.parquet", sections=["model"]))
    assert e.value.status_code == 404
