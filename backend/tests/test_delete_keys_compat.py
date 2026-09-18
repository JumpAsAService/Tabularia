"""Cancellazione di piu' chiavi su storage con e senza batch (GCS non ce l'ha).

Semantica attesa ovunque: il batch e' usato quando c'e'; quando l'endpoint lo
rifiuta si passa alle DELETE singole (e non si riprova il batch); cancellare una
chiave assente non e' un errore (GCS risponde 404, S3 no); un errore vero sulla
prima chiave interrompe subito invece di insistere su centinaia di chiavi.
"""
import pytest
from botocore.exceptions import ClientError

from app import utils
from app.utils import delete_keys


def _err(code: str, status: int = 400) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": code}, "ResponseMetadata": {"HTTPStatusCode": status}}, "DeleteObjects")


class _Meta:
    def __init__(self, endpoint):
        self.endpoint_url = endpoint


class _Client:
    def __init__(self, endpoint="https://s3.example", batch=True, missing=(), deny=()):
        self.meta = _Meta(endpoint)
        self.batch = batch
        self.missing = set(missing)
        self.deny = set(deny)
        self.batch_calls: list[list[str]] = []
        self.single_calls: list[str] = []

    def delete_objects(self, Bucket, Delete):
        if not self.batch:
            raise _err("MethodNotAllowed", 405)
        keys = [o["Key"] for o in Delete["Objects"]]
        self.batch_calls.append(keys)
        return {"Deleted": [{"Key": k} for k in keys if k not in self.deny], "Errors": [{"Key": k, "Code": "AccessDenied"} for k in keys if k in self.deny]}

    def delete_object(self, Bucket, Key):
        self.single_calls.append(Key)
        if Key in self.deny:
            raise _err("AccessDenied", 403)
        if Key in self.missing:
            raise _err("NoSuchKey", 404)
        return {}


@pytest.fixture(autouse=True)
def _reset():
    utils._batch_delete_unsupported.clear()
    yield
    utils._batch_delete_unsupported.clear()


def test_batch_when_available_chunks_of_1000():
    c = _Client()
    keys = [f"k{i}" for i in range(2300)]
    deleted, errors = delete_keys(c, "b", keys)
    assert deleted == 2300 and errors == []
    assert [len(x) for x in c.batch_calls] == [1000, 1000, 300]
    assert c.single_calls == []


def test_falls_back_to_single_deletes_and_remembers():
    c = _Client(endpoint="https://storage.googleapis.com", batch=False)
    deleted, errors = delete_keys(c, "b", ["a", "b", "c"])
    assert (deleted, errors) == (3, [])
    assert c.single_calls == ["a", "b", "c"]
    assert "https://storage.googleapis.com" in utils._batch_delete_unsupported
    # seconda volta: niente tentativo batch (che fallirebbe di nuovo)
    c2 = _Client(endpoint="https://storage.googleapis.com", batch=True)
    delete_keys(c2, "b", ["x"])
    assert c2.batch_calls == [] and c2.single_calls == ["x"]


def test_missing_key_is_not_an_error():
    c = _Client(endpoint="https://storage.googleapis.com", batch=False, missing={"gone"})
    deleted, errors = delete_keys(c, "b", ["gone", "there"])
    assert (deleted, errors) == (2, [])


def test_real_error_on_first_key_stops_immediately():
    c = _Client(endpoint="https://storage.googleapis.com", batch=False, deny={"a", "b", "c"})
    with pytest.raises(ClientError):
        delete_keys(c, "b", ["a", "b", "c"])
    assert c.single_calls == ["a"]


def test_real_error_later_is_collected():
    c = _Client(endpoint="https://storage.googleapis.com", batch=False, deny={"b"})
    deleted, errors = delete_keys(c, "b", ["a", "b", "c"])
    assert deleted == 2 and [e["Key"] for e in errors] == ["b"] and errors[0]["Code"] == "AccessDenied"


def test_batch_per_key_errors_are_returned():
    c = _Client(deny={"b"})
    deleted, errors = delete_keys(c, "b", ["a", "b"])
    assert deleted == 1 and errors == [{"Key": "b", "Code": "AccessDenied"}]


def test_empty_and_blank_keys():
    c = _Client()
    assert delete_keys(c, "b", ["", None]) == (0, [])
    assert c.batch_calls == []


def test_storage_service_single_delete_is_idempotent(monkeypatch):
    svc = utils.StorageService.__new__(utils.StorageService)
    svc.client = _Client(endpoint="https://storage.googleapis.com", missing={"gone"})
    assert svc.delete_object("b", "gone")["status"] == "deleted"
    svc.client = _Client(deny={"x"})
    with pytest.raises(ClientError):
        svc.delete_object("b", "x")


def test_storage_client_does_not_send_chunked_checksums(monkeypatch):
    """GCS rifiuta le PUT con i checksum a blocchi che boto3 >= 1.36 aggiunge di
    default (SignatureDoesNotMatch): il client deve calcolarli solo dove richiesti."""
    monkeypatch.setenv("STORAGE__ENDPOINT", "https://storage.googleapis.com")
    from app.core.config import get_settings

    get_settings.cache_clear()
    try:
        svc = utils.StorageService()
    finally:
        get_settings.cache_clear()
    cfg = svc.client.meta.config
    assert cfg.request_checksum_calculation == "when_required"
    assert cfg.response_checksum_validation == "when_required"
