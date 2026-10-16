"""Multipart uploads and copies stay under 1,000 parts, whatever the object's size.

Scaleway's object storage takes at most 1,000 parts per multipart upload (AWS 10,000);
with boto3's default 8 MiB parts the parquet of a one-billion-row import (~14 GB) was
refused at part 1,001 (load test of 2026-10-15)."""
import io
import math

import pytest

from app import utils
from app.utils import MAX_UPLOAD_PARTS, MIN_PART_BYTES, STREAM_PART_BYTES, StorageService, part_size

GiB, MiB = 1024**3, 1024**2


@pytest.mark.parametrize("size", [1, 5 * MiB, 8 * GiB, 14 * GiB, 100 * GiB, 4 * 1024 * GiB])
def test_any_size_fits_in_fewer_than_a_thousand_parts(size):
    p = part_size(size)
    assert p >= MIN_PART_BYTES and p % MiB == 0
    assert math.ceil(size / p) <= MAX_UPLOAD_PARTS < 1000


def test_small_objects_keep_the_default_parts_and_a_stream_gets_large_ones():
    assert part_size(100 * MiB) == MIN_PART_BYTES
    assert part_size(None) == STREAM_PART_BYTES and 900 * STREAM_PART_BYTES > 50 * GiB


class Client:
    def __init__(self):
        self.calls = []

    def upload_file(self, path, bucket, key, Config=None):
        self.calls.append(("upload_file", Config))

    def upload_fileobj(self, fileobj, bucket, key, Config=None):
        self.calls.append(("upload_fileobj", Config))

    def head_object(self, Bucket, Key):
        return {"ContentLength": 20 * GiB}

    def copy(self, source, bucket, key, Config=None):
        self.calls.append(("copy", Config))


@pytest.fixture
def storage():
    s = object.__new__(StorageService)
    s.client = Client()
    return s


def test_a_file_upload_sizes_its_parts_to_the_file(storage, tmp_path, monkeypatch):
    f = tmp_path / "big.parquet"
    f.write_bytes(b"x")
    monkeypatch.setattr(utils.os.path, "getsize", lambda p: 14 * GiB)
    storage.upload_file(str(f), "b", "k")
    (name, config), = storage.client.calls
    assert config.multipart_chunksize == part_size(14 * GiB) and math.ceil(14 * GiB / config.multipart_chunksize) <= 900


def test_a_buffer_says_its_size_and_a_stream_gets_large_parts(storage):
    storage.upload_fileobj(io.BytesIO(b"x" * 1000), "b", "k")

    class Stream:
        def read(self, n=-1):
            return b""

    storage.upload_fileobj(Stream(), "b", "k")
    assert [c.multipart_chunksize for _, c in storage.client.calls] == [MIN_PART_BYTES, STREAM_PART_BYTES]


def test_a_copy_above_five_gigabytes_sizes_its_parts_to_the_source(storage):
    storage.copy_object("b", "datasets/1/a.parquet", "datasets/2/b.parquet")
    (name, config), = storage.client.calls
    assert config.multipart_threshold == 5 * GiB and math.ceil(20 * GiB / config.multipart_chunksize) <= 900


def test_clickhouse_writes_grow_their_parts_every_hundred():
    from app.engine.clickhouse_engine import _PARQUET_OUT

    assert _PARQUET_OUT["s3_upload_part_size_multiply_parts_count_threshold"] == 100
