"""Pinning the data plane's reads (`ensure_reads_pinned`).

Regression for the fix: `_launch_flow_run` must anchor every source to the engine's
bucket and the managed prefixes, as the proxy does already. Here we test the pure
function both paths share.
"""

import pytest
from fastapi import HTTPException

from app.services.objects import ensure_reads_pinned
from tests.conftest import make_user

ENGINE_BUCKET = "data-prep"


@pytest.fixture
def _normal(session):
    return make_user(session, email="normale@x.it")


@pytest.fixture
def _super(session):
    return make_user(session, email="admin@x.it", is_superuser=True)


def test_valid_managed_source_passes(session, _normal):
    # the engine's bucket + a key under a managed prefix → no error
    ensure_reads_pinned(
        session, _normal, {"bucket": ENGINE_BUCKET, "input_key": "datasets/abc.parquet"}, ENGINE_BUCKET
    )


def test_foreign_bucket_rejected(session, _normal):
    with pytest.raises(HTTPException) as e:
        ensure_reads_pinned(
            session, _normal, {"bucket": "altro-bucket", "input_key": "datasets/abc.parquet"}, ENGINE_BUCKET
        )
    assert e.value.status_code == 403


def test_unmanaged_key_rejected(session, _normal):
    with pytest.raises(HTTPException) as e:
        ensure_reads_pinned(
            session, _normal, {"bucket": ENGINE_BUCKET, "input_key": "secrets/creds.parquet"}, ENGINE_BUCKET
        )
    assert e.value.status_code == 403


def test_nested_join_right_source_bucket_rejected(session, _normal):
    # a nested source (a join's right branch) with a foreign bucket must not escape the
    # pinning
    payload = {
        "bucket": ENGINE_BUCKET,
        "input_key": "datasets/a.parquet",
        "operations": [
            {
                "type": "join",
                "params": {"right": {"source": {"bucket": "evil", "key": "datasets/b.parquet"}}},
            }
        ],
    }
    with pytest.raises(HTTPException) as e:
        ensure_reads_pinned(session, _normal, payload, ENGINE_BUCKET)
    assert e.value.status_code == 403


def test_superuser_bypasses_pinning(session, _super):
    # the superuser is not bound (no exception even with a foreign bucket)
    ensure_reads_pinned(
        session, _super, {"bucket": "qualsiasi", "input_key": "ovunque/x.parquet"}, ENGINE_BUCKET
    )
