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

    def put_object(self, Bucket, Key, Body, ContentType=None):
        if Key == "refused":
            raise ClientError({"Error": {"Code": "AccessDenied", "Message": "Access Denied"}}, "PutObject")
        self.objects[Key] = Body


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


# ── a Predict step's card written into its prefix ───────────────────────────────


def test_a_card_is_written_at_its_key(bucket):
    objects: dict[str, bytes] = {}
    bucket(objects)
    card = '{"format": "tabularia-model", "model": "linear", "name": "prix à l\u2019unité"}'
    got = routes.objects_write(routes.ObjectWriteRequest(connection=_conn(), key="env-4/step/model.json", text=card))
    assert got == {"bytes": len(card.encode("utf-8"))}
    assert objects["env-4/step/model.json"] == card.encode("utf-8")


def test_writing_refuses_what_is_not_a_card(bucket):
    bucket({})
    with pytest.raises(HTTPException) as big:
        routes.objects_write(routes.ObjectWriteRequest(connection=_conn(), key="k", text="x" * (routes.OBJECT_WRITE_MAX + 1)))
    assert big.value.status_code == 413
    for key in ("", "/abs", "env-4/../other"):
        with pytest.raises(HTTPException) as bad:
            routes.objects_write(routes.ObjectWriteRequest(connection=_conn(), key=key, text="{}"))
        assert bad.value.status_code == 422
    with pytest.raises(HTTPException) as refused:
        routes.objects_write(routes.ObjectWriteRequest(connection=_conn(), key="refused", text="{}"))
    assert refused.value.status_code == 502 and "AccessDenied" in refused.value.detail


# ── a Predict step's model file uploaded as raw bytes ───────────────────────────


def _b64_conn() -> str:
    import base64
    import json

    return base64.b64encode(json.dumps(_conn().model_dump()).encode()).decode()


class FakeRequest:
    """What the route reads of a request: its body in chunks."""

    def __init__(self, body: bytes, chunk: int = 7000):
        self.body, self.chunk, self.served = body, chunk, 0

    async def stream(self):
        for i in range(0, len(self.body), self.chunk):
            self.served += 1
            yield self.body[i:i + self.chunk]


def _upload(body: bytes, *, sha: str | None = None, size: int | None = None, key: str = "env-4/step/model_file",
            connection: str | None = None):
    import hashlib

    return asyncio.run(routes.objects_upload(
        FakeRequest(body), key=key, sha256=sha if sha is not None else hashlib.sha256(body).hexdigest(),
        size=len(body) if size is None else size, connection=_b64_conn() if connection is None else connection))


@pytest.fixture
def uploads(bucket, monkeypatch):
    objects: dict[str, bytes] = {}
    bucket(objects)

    def upload_file(self, path, bucket_name, key):
        objects[key] = Path(path).read_bytes()

    monkeypatch.setattr(FakeS3, "upload_file", upload_file, raising=False)
    return objects


def test_a_model_file_is_written_at_its_key(uploads):
    body = bytes(range(256)) * 200  # binary, not UTF-8 text
    assert _upload(body) == {"bytes": len(body)}
    assert uploads["env-4/step/model_file"] == body


def test_a_wrong_hash_is_409_and_leaves_no_object(uploads):
    body = b"\x00\xffxgboost" * 100
    with pytest.raises(HTTPException) as e:
        _upload(body, sha="0" * 64)
    assert e.value.status_code == 409 and "sha256" in e.value.detail
    assert uploads == {}


def test_a_size_other_than_declared_is_409_and_leaves_no_object(uploads):
    body = b"abc" * 1000
    for declared in (len(body) - 1, len(body) + 1):
        with pytest.raises(HTTPException) as e:
            _upload(body, size=declared)
        assert e.value.status_code == 409
    assert uploads == {}


def test_a_file_over_the_limit_is_413_and_is_not_read_to_its_end(uploads, monkeypatch):
    monkeypatch.setattr(routes, "_upload_limit", lambda: 20_000)
    body = b"x" * 100_000
    req = FakeRequest(body)
    import hashlib

    with pytest.raises(HTTPException) as declared:  # declares more than the limit: refused before reading
        asyncio.run(routes.objects_upload(req, key="k", sha256=hashlib.sha256(body).hexdigest(), size=len(body),
                                          connection=_b64_conn()))
    assert declared.value.status_code == 413 and req.served == 0
    req = FakeRequest(body)
    with pytest.raises(HTTPException) as lied:  # declares 20 000, sends 100 000: stops at the first chunk past it
        asyncio.run(routes.objects_upload(req, key="k", sha256="0" * 64, size=20_000, connection=_b64_conn()))
    assert lied.value.status_code == 413 and req.served <= 4
    req = FakeRequest(body)
    with pytest.raises(HTTPException) as longer:  # declares 10 000 (within the limit), sends more
        asyncio.run(routes.objects_upload(req, key="k", sha256="0" * 64, size=10_000, connection=_b64_conn()))
    assert longer.value.status_code == 409 and req.served <= 2
    assert uploads == {}


def test_a_bad_request_is_refused_before_anything_is_read(uploads):
    body = b"abc"
    for key in ("", "/abs", "env-4/../other"):
        with pytest.raises(HTTPException) as e:
            _upload(body, key=key)
        assert e.value.status_code == 422
    for sha in ("abc", "G" * 64, "A" * 64):
        with pytest.raises(HTTPException) as e:
            _upload(body, sha=sha)
        assert e.value.status_code == 422
    with pytest.raises(HTTPException) as e:
        _upload(body, connection="not base64 json")
    assert e.value.status_code == 422
    assert uploads == {}


def test_a_bucket_that_refuses_is_502_and_no_temporary_file_stays(uploads, monkeypatch):
    import tempfile

    def refuse(self, path, bucket_name, key):
        raise ClientError({"Error": {"Code": "AccessDenied", "Message": "Access Denied"}}, "PutObject")

    monkeypatch.setattr(FakeS3, "upload_file", refuse, raising=False)
    before = set(Path(tempfile.gettempdir()).glob("*.model"))
    with pytest.raises(HTTPException) as e:
        _upload(b"abc" * 10)
    assert e.value.status_code == 502 and "AccessDenied" in e.value.detail
    assert set(Path(tempfile.gettempdir()).glob("*.model")) == before
