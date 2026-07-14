"""Pinning delle letture del data plane (`ensure_reads_pinned`).

Regressione per il fix: `_launch_flow_run` deve ancorare ogni sorgente al bucket
dell'engine e ai prefissi gestiti, come già fa il proxy. Qui testiamo la
funzione pura che entrambi i path condividono.
"""
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.services.objects import ensure_reads_pinned

ENGINE_BUCKET = "data-prep"
_normal = SimpleNamespace(is_superuser=False)
_super = SimpleNamespace(is_superuser=True)


def test_valid_managed_source_passes():
    # bucket dell'engine + chiave sotto un prefisso gestito → nessun errore
    ensure_reads_pinned(
        _normal, {"bucket": ENGINE_BUCKET, "input_key": "datasets/abc.parquet"}, ENGINE_BUCKET
    )


def test_foreign_bucket_rejected():
    with pytest.raises(HTTPException) as e:
        ensure_reads_pinned(
            _normal, {"bucket": "altro-bucket", "input_key": "datasets/abc.parquet"}, ENGINE_BUCKET
        )
    assert e.value.status_code == 403


def test_unmanaged_key_rejected():
    with pytest.raises(HTTPException) as e:
        ensure_reads_pinned(
            _normal, {"bucket": ENGINE_BUCKET, "input_key": "secrets/creds.parquet"}, ENGINE_BUCKET
        )
    assert e.value.status_code == 403


def test_nested_join_right_source_bucket_rejected():
    # una sorgente annidata (ramo destro di un join) con bucket estraneo non
    # deve sfuggire al pinning
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
        ensure_reads_pinned(_normal, payload, ENGINE_BUCKET)
    assert e.value.status_code == 403


def test_superuser_bypasses_pinning():
    # il superuser non è vincolato (nessuna eccezione anche con bucket estraneo)
    ensure_reads_pinned(
        _super, {"bucket": "qualsiasi", "input_key": "ovunque/x.parquet"}, ENGINE_BUCKET
    )
