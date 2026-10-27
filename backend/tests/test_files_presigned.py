"""A notebook's pod downloads a datasource's parquet from the storage itself, through a
short-lived link the engine makes (docs/specs/notebooks.md, «Data»): only for the managed
data prefixes of the storage's own bucket, only for a file that exists, valid as long as
asked within bounds. The gateway asks only after checking the person may read it."""
import os

import pytest
from fastapi import HTTPException

from app.api.routes import files
from app.core.config import get_settings


class Storage:
    def __init__(self, objects):
        self.objects = set(objects)
        self.links: list[tuple[str, str, int]] = []

    def object_exists(self, bucket, key):
        return (bucket, key) in self.objects

    def get_presigned_url(self, bucket, key, expiration=3600, operation="get_object"):
        self.links.append((bucket, key, expiration))
        return f"https://s3.example.com/{bucket}/{key}?X-Amz-Expires={expiration}"


@pytest.fixture
def storage(monkeypatch):
    bucket = get_settings().storage.bucket
    s = Storage({(bucket, "datasets/7/a.parquet")})
    import app.utils

    monkeypatch.setattr(app.utils, "get_storage_service", lambda: s)
    return s


def test_a_link_to_a_datasource_file(storage):
    bucket = get_settings().storage.bucket
    out = files.presigned_link(bucket, "datasets/7/a.parquet", expires=600)
    assert out == {"url": f"https://s3.example.com/{bucket}/datasets/7/a.parquet?X-Amz-Expires=600"}
    assert storage.links == [(bucket, "datasets/7/a.parquet", 600)]


@pytest.mark.parametrize("key", ["cache/x.parquet", "raw/up.csv", "backups/postgres/x.dump", "datasets/../raw/x",
                                 "keys/fernet.key"])
def test_only_the_managed_data_is_linked(storage, key):
    with pytest.raises(HTTPException) as e:
        files.presigned_link(get_settings().storage.bucket, key, expires=600)
    assert e.value.status_code == 422 and storage.links == []


def test_only_the_storages_own_bucket(storage):
    with pytest.raises(HTTPException) as e:
        files.presigned_link("someone-elses", "datasets/7/a.parquet", expires=600)
    assert e.value.status_code == 422


def test_a_file_that_is_not_there_is_a_404(storage):
    with pytest.raises(HTTPException) as e:
        files.presigned_link(get_settings().storage.bucket, "datasets/7/gone.parquet", expires=600)
    assert e.value.status_code == 404 and storage.links == []


def test_the_link_lasts_minutes_not_days():
    """10 minutes by default, at most an hour (the route's bounds; the engine image has no
    test client)."""
    import inspect

    expires = inspect.signature(files.presigned_link).parameters["expires"].default
    assert expires.default == 600
    assert {type(m).__name__: m for m in expires.metadata}["Le"].le == 3600


@pytest.mark.skipif(not os.getenv("S3_TEST_BUCKET"), reason="needs a throwaway S3 (S3_TEST_BUCKET, STORAGE__*)")
def test_on_a_real_storage_the_link_downloads_the_file(tmp_path, monkeypatch):
    import urllib.request

    from app.utils import StorageService

    bucket = os.environ["S3_TEST_BUCKET"]
    monkeypatch.setattr(get_settings().storage, "bucket", bucket)
    s3 = StorageService()
    s3.create_bucket(bucket)
    src = tmp_path / "a.parquet"
    src.write_bytes(os.urandom(4096))
    s3.upload_file(str(src), bucket, "datasets/zz-presign/a.parquet")
    try:
        url = files.presigned_link(bucket, "datasets/zz-presign/a.parquet", expires=60)["url"]
        with urllib.request.urlopen(url) as r:
            assert r.read() == src.read_bytes()
    finally:
        s3.delete_object(bucket, "datasets/zz-presign/a.parquet")
