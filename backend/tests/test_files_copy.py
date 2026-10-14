"""A duplicated datasource gets its own copy of the data file, made on the storage
(nothing downloaded): two datasources sharing one file would lose it when either is
refreshed or deleted."""
import os

import pytest
from botocore.exceptions import ClientError
from fastapi import HTTPException

from app.api.routes import files


class Storage:
    def __init__(self, objects):
        self.objects = dict(objects)

    def copy_object(self, bucket, source_key, target_key):
        if (bucket, source_key) not in self.objects:
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "CopyObject")
        self.objects[(bucket, target_key)] = self.objects[(bucket, source_key)]


@pytest.fixture
def storage(monkeypatch):
    s = Storage({("b", "datasets/7/a.parquet"): b"data"})
    import app.utils

    monkeypatch.setattr(app.utils, "get_storage_service", lambda: s)
    return s


def test_the_copy_lands_under_its_own_key(storage):
    files.copy_object("b", "datasets/7/a.parquet", "datasets/8/b.parquet")
    assert storage.objects[("b", "datasets/8/b.parquet")] == b"data"


@pytest.mark.parametrize("source, target", [("cache/x.parquet", "datasets/8/b.parquet"), ("datasets/7/a.parquet", "out/x.parquet"),
                                            ("../etc", "datasets/x")])
def test_only_managed_data_is_copied_and_only_into_datasets(storage, source, target):
    with pytest.raises(HTTPException) as e:
        files.copy_object("b", source, target)
    assert e.value.status_code == 422


def test_a_missing_source_is_a_404(storage):
    with pytest.raises(HTTPException) as e:
        files.copy_object("b", "datasets/7/gone.parquet", "datasets/8/b.parquet")
    assert e.value.status_code == 404


@pytest.mark.skipif(not os.getenv("S3_TEST_BUCKET"), reason="needs a throwaway S3 (S3_TEST_BUCKET, STORAGE__*)")
def test_on_a_real_storage_the_copy_is_byte_for_byte(tmp_path):
    from app.utils import StorageService

    s3, bucket = StorageService(), os.environ["S3_TEST_BUCKET"]
    s3.create_bucket(bucket)
    src = tmp_path / "a.parquet"
    src.write_bytes(os.urandom(9 * 1024 * 1024))  # above boto3's 8 MB multipart threshold
    s3.upload_file(str(src), bucket, "datasets/zz-copy/a.parquet")
    try:
        s3.copy_object(bucket, "datasets/zz-copy/a.parquet", "datasets/zz-copy/b.parquet")
        out = tmp_path / "b.parquet"
        s3.download_file(bucket, "datasets/zz-copy/b.parquet", str(out))
        assert out.read_bytes() == src.read_bytes()
    finally:
        s3.delete_objects(bucket, ["datasets/zz-copy/a.parquet", "datasets/zz-copy/b.parquet"])
