"""A compiled Bayesian model's binary between the scripts bucket and Tabularia's storage
(docs/specs/bayes-stan-compile.md, «The binary's way»): POST /scripts/binary/keep, place and delete.
The sha256 the gateway names is the only one trusted; bytes that are not it are never written.
"""
from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path

import pytest
from botocore.exceptions import ClientError
from fastapi import HTTPException

from app.api.routes import scripts as routes
from app.core.crypto import encrypt_secret
from app.ingest.s3_destination import S3ConnectionSpec

BINARY = b"\x7fELF" + bytes(range(256)) * 40
SHA = hashlib.sha256(BINARY).hexdigest()
KEY = "bayes-compile/17-0/model"


class FakeBucket:
    """The part of a boto3 client the routes call, over a dict of keys."""

    def __init__(self, objects: dict[str, bytes] | None = None, refuse: str = "NoSuchKey"):
        self.objects = dict(objects or {})
        self.refuse = refuse  # the code a missing key answers (AccessDenied without the listing right)
        self.reads: list[str] = []

    def get_object(self, Bucket, Key):
        self.reads.append(Key)
        if Key not in self.objects:
            raise ClientError({"Error": {"Code": self.refuse, "Message": "no"}}, "GetObject")
        data = self.objects[Key]

        class Body(io.BytesIO):
            def iter_chunks(self, size):
                while chunk := self.read(size):
                    yield chunk

        return {"Body": Body(data), "ContentLength": len(data)}

    def upload_file(self, Filename, Bucket, Key, **_):
        self.objects[Key] = Path(Filename).read_bytes()


class FakeStorage:
    """get_storage_service(): Tabularia's storage."""

    def __init__(self, objects: dict[str, bytes] | None = None):
        self.client = FakeBucket(objects)
        self.deleted: list[str] = []

    @property
    def objects(self) -> dict[str, bytes]:
        return self.client.objects

    def upload_file(self, file_path, bucket, object_key):
        self.client.objects[object_key] = Path(file_path).read_bytes()

    def delete_objects(self, bucket, object_keys):
        for k in object_keys:
            self.deleted.append(k)
            self.client.objects.pop(k, None)
        return {"bucket": bucket, "deleted": len(object_keys), "errors": []}


@pytest.fixture
def world(monkeypatch):
    scripts, storage = FakeBucket(), FakeStorage()
    monkeypatch.setattr(S3ConnectionSpec, "client", lambda self: scripts)
    monkeypatch.setattr("app.utils.get_storage_service", lambda: storage)
    return scripts, storage


def _conn() -> S3ConnectionSpec:
    return S3ConnectionSpec(endpoint_url="http://minio:9000", access_key="scripts", bucket="scripts",
                            secret_key_encrypted=encrypt_secret("s3cret"))


def _keep(key=KEY, sha=SHA, most=routes.BINARY_MAX_BYTES):
    return routes.binary_keep(routes.BinaryKeepRequest(connection=_conn(), key=key, sha256=sha, max_bytes=most,
                                                       bucket="data-prep"))


def _place(sha=SHA, target="env-4/0f2c/model"):
    return routes.binary_place(routes.BinaryPlaceRequest(connection=_conn(), sha256=sha, bucket="data-prep",
                                                         target_key=target))


def _refusal(res) -> tuple[int, dict]:
    return res.status_code, json.loads(res.body)


def test_keep_copies_the_binary_under_its_sha256(world):
    scripts, storage = world
    scripts.objects[KEY] = BINARY
    assert _keep() == {"bytes": len(BINARY), "existed": False}
    assert storage.objects == {"bayes-binaries/" + SHA: BINARY}


def test_keep_refuses_another_sha256_and_writes_nothing(world):
    scripts, storage = world
    scripts.objects[KEY] = BINARY[:-1] + b"\x00"  # one byte changed in the shared bucket after the compile
    status, body = _refusal(_keep())
    assert status == 409 and body["altered"] is True
    assert storage.objects == {}


def test_keep_refuses_an_oversized_binary_and_writes_nothing(world):
    scripts, storage = world
    scripts.objects[KEY] = BINARY
    status, body = _refusal(_keep(most=len(BINARY) - 1))
    assert status == 409 and body["too_long"] is True
    assert storage.objects == {}
    # the engine's own ceiling, whatever the gateway asks
    assert routes.BINARY_MAX_BYTES == 64 * 1024 * 1024


def test_keep_says_a_missing_binary(world):
    scripts, storage = world
    scripts.refuse = "AccessDenied"  # the advised key has no listing right: a missing key is a 403
    status, body = _refusal(_keep())
    assert status == 404 and body["missing"] is True
    assert storage.objects == {}


def test_keep_is_idempotent_and_repairs_an_altered_copy(world):
    scripts, storage = world
    scripts.objects[KEY] = BINARY
    storage.objects["bayes-binaries/" + SHA] = BINARY
    assert _keep() == {"bytes": len(BINARY), "existed": True}
    storage.objects["bayes-binaries/" + SHA] = b"something else"
    assert _keep() == {"bytes": len(BINARY), "existed": False}
    assert storage.objects["bayes-binaries/" + SHA] == BINARY


@pytest.mark.parametrize("key", ["datasets/x/model", "bayes-compile/17-0/other", "bayes-compile/x-0/model",
                                 "env-4/step/model", "../bayes-compile/1-0/model", "bayes-compile/1-0/model/"])
def test_keep_refuses_a_key_outside_the_compiles(world, key):
    with pytest.raises(HTTPException) as e:
        _keep(key=key)
    assert e.value.status_code == 422


def test_keep_refuses_a_sha256_that_is_not_one(world):
    with pytest.raises(HTTPException) as e:
        _keep(sha=SHA.upper())
    assert e.value.status_code == 422


def test_place_copies_the_stored_binary_into_the_step(world):
    scripts, storage = world
    storage.objects["bayes-binaries/" + SHA] = BINARY
    assert _place() == {"bytes": len(BINARY)}
    assert scripts.objects == {"env-4/0f2c/model": BINARY}


def test_place_refuses_an_altered_stored_object(world):
    scripts, storage = world
    storage.objects["bayes-binaries/" + SHA] = BINARY + b"\x00"
    status, body = _refusal(_place())
    assert status == 409 and body["altered"] is True
    assert scripts.objects == {}


def test_place_says_a_missing_binary(world):
    scripts, _ = world
    status, body = _refusal(_place())
    assert status == 404 and body["missing"] is True
    assert scripts.objects == {}


@pytest.mark.parametrize("target", ["bayes-binaries/" + SHA, "datasets/x/model", "env-4/../x/model", "env-4/step/other",
                                    "bayes-compile/1-0/model"])
def test_place_refuses_a_target_outside_a_step(world, target):
    with pytest.raises(HTTPException) as e:
        _place(target=target)
    assert e.value.status_code == 422


def test_delete_removes_only_binaries(world):
    _, storage = world
    storage.objects.update({"bayes-binaries/" + SHA: BINARY, "datasets/x.parquet": b"PAR1"})
    out = routes.binary_delete(routes.BinaryDeleteRequest(bucket="data-prep", sha256s=[SHA, SHA, "b" * 64]))
    assert out == {"deleted": 2, "failed": []}
    assert storage.deleted == ["bayes-binaries/" + SHA, "bayes-binaries/" + "b" * 64]
    assert storage.objects == {"datasets/x.parquet": b"PAR1"}


@pytest.mark.parametrize("sha", ["../datasets/x.parquet", "", "g" * 64, SHA + "/x"])
def test_delete_refuses_anything_but_a_sha256(world, sha):
    _, storage = world
    with pytest.raises(HTTPException) as e:
        routes.binary_delete(routes.BinaryDeleteRequest(bucket="data-prep", sha256s=[SHA, sha]))
    assert e.value.status_code == 422
    assert storage.deleted == []
