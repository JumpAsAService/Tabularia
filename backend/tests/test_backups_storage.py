"""The backups of Tabularia's database in the object storage (app/api/routes/backups.py).

Fixed here: a backup is stored under the prefix and read back whole; the list holds
only backups, newest first, and says when they share the data's bucket; nothing but a
backup's name is accepted, so nothing else of the bucket can be read or removed; the
place falls back to STORAGE__* and takes BACKUP__* when given.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest
from fastapi import HTTPException

from app.api.routes import backups as routes
from app.core.config import get_settings

FIRST = "tabularia-2026-10-21T03-00-00Z.dump"
SECOND = "tabularia-2026-10-22T03-00-00Z.dump"


class _Body:
    def __init__(self, data: bytes):
        self._data = data

    def iter_chunks(self, size):
        for i in range(0, len(self._data), size):
            yield self._data[i:i + size]


class FakeS3:
    def __init__(self):
        self.objects: dict[str, bytes] = {}

    def upload_fileobj(self, f, bucket, key, Config=None):
        self.objects[key] = f.read()

    def get_object(self, Bucket, Key):
        from botocore.exceptions import ClientError

        if Key not in self.objects:
            raise ClientError({"Error": {"Code": "NoSuchKey", "Message": "gone"}}, "GetObject")
        return {"Body": _Body(self.objects[Key]), "ContentLength": len(self.objects[Key])}

    def delete_object(self, Bucket, Key):
        self.objects.pop(Key, None)

    def get_paginator(self, _name):
        fake = self

        class _P:
            def paginate(self, Bucket, Prefix):
                yield {"Contents": [{"Key": k, "Size": len(v), "LastModified": datetime(2026, 10, 22, tzinfo=timezone.utc)}
                                    for k, v in fake.objects.items() if k.startswith(Prefix)]}
        return _P()


class _Request:
    def __init__(self, data: bytes):
        self._data = data

    async def stream(self):
        for i in range(0, len(self._data), 65536):
            yield self._data[i:i + 65536]


def _put(name: str, data: bytes):
    return asyncio.run(routes.store_backup(name, _Request(data)))


def _read(name: str) -> bytes:
    resp = routes.read_backup(name)

    async def collect():
        return b"".join([chunk async for chunk in resp.body_iterator])
    return asyncio.run(collect())


def _status(fn, *args) -> int:
    try:
        fn(*args)
    except HTTPException as e:
        return e.status_code
    return 200


@pytest.fixture
def s3(monkeypatch):
    fake = FakeS3()
    monkeypatch.setattr(routes, "_place", lambda: (fake, "tabularia", "backups/postgres/", True))
    return fake


def test_a_backup_is_stored_under_its_prefix_and_read_back_whole(s3):
    data = b"PGDMP" + bytes(range(256)) * 5000
    assert _put(FIRST, data)["size"] == len(data)
    assert s3.objects[f"backups/postgres/{FIRST}"] == data
    assert _read(FIRST) == data


def test_the_list_holds_only_backups_newest_first(s3):
    s3.objects["backups/postgres/" + FIRST] = b"a"
    s3.objects["backups/postgres/" + SECOND] = b"bb"
    s3.objects["backups/postgres/notes.txt"] = b"not a backup"
    out = routes.list_backups()
    assert [i["name"] for i in out["items"]] == [SECOND, FIRST]
    assert out["same_as_data"] is True and out["prefix"] == "backups/postgres/" and out["error"] is None


@pytest.mark.parametrize("name", ["notes.txt", "../datasets/x.parquet", "tabularia-latest.dump"])
def test_nothing_but_a_backup_name_is_accepted(s3, name):
    s3.objects["backups/postgres/notes.txt"] = b"x"
    assert _status(routes.read_backup, name) == 422
    assert _status(routes.delete_backup, name) == 422
    assert _status(_put, name, b"x") == 422
    assert s3.objects == {"backups/postgres/notes.txt": b"x"}


def test_an_empty_backup_is_refused_and_a_missing_one_is_404(s3):
    assert _status(_put, FIRST, b"") == 422
    assert _status(routes.read_backup, SECOND) == 404
    assert _status(routes.delete_backup, SECOND) == 200


def test_the_place_falls_back_to_the_data_storage(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s.backup, "bucket", "")
    monkeypatch.setattr(s.backup, "endpoint", "")
    monkeypatch.setattr(s.backup, "access_key", "")
    _, bucket, prefix, same = routes._place()
    assert bucket == s.storage.bucket and prefix == "backups/postgres/" and same is True
    monkeypatch.setattr(s.backup, "bucket", "tabularia-backups")
    _, bucket, _, same = routes._place()
    assert bucket == "tabularia-backups" and same is False
