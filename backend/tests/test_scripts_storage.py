"""The check of the scripts bucket (POST /scripts/storage/test): a probe object
written, read back and deleted with the administrators' key, and whether that key
can also list the bucket — which it should not.
"""
from __future__ import annotations

import io

import pytest
from botocore.exceptions import ClientError

from app.api.routes import scripts as routes
from app.core.crypto import encrypt_secret
from app.ingest.s3_destination import S3ConnectionSpec

SECRET = "scripts-bucket-s3cret"


class FakeS3:
    def __init__(self, *, can_list: bool = True, denied: str = ""):
        self.objects: dict[str, bytes] = {}
        self.can_list = can_list
        self.denied = denied  # "PutObject" | "GetObject" | "DeleteObject"
        self.calls: list[str] = []

    def _check(self, op: str):
        self.calls.append(op)
        if op == self.denied:
            raise ClientError({"Error": {"Code": "AccessDenied", "Message": "Access Denied."}}, op)

    def put_object(self, Bucket, Key, Body):
        self._check("PutObject")
        self.objects[Key] = Body

    def get_object(self, Bucket, Key):
        self._check("GetObject")
        return {"Body": io.BytesIO(self.objects[Key])}

    def delete_object(self, Bucket, Key):
        self._check("DeleteObject")
        self.objects.pop(Key, None)

    def list_objects_v2(self, Bucket, MaxKeys):
        self._check("ListObjectsV2")
        if not self.can_list:
            raise ClientError({"Error": {"Code": "AccessDenied", "Message": "Access Denied."}}, "ListObjectsV2")
        return {"Contents": []}


@pytest.fixture
def s3(monkeypatch):
    holder: dict = {}

    def install(fake: FakeS3, *, expect_secret: str = SECRET):
        def client(self: S3ConnectionSpec):
            assert self.resolve_secret() == expect_secret  # decrypted only here
            return fake
        monkeypatch.setattr(S3ConnectionSpec, "client", client)
        holder["fake"] = fake
        return fake
    return install


def _call(**conn) -> dict:
    spec = {"endpoint_url": "http://minio:9000", "access_key": "scripts", "bucket": "scripts",
            "secret_key_encrypted": encrypt_secret(SECRET), **conn}
    return routes.storage_test(routes.StorageTestRequest(connection=S3ConnectionSpec(**spec)))


def test_a_key_without_listing_passes_and_leaves_nothing_behind(s3):
    fake = s3(FakeS3(can_list=False))
    out = _call()
    assert out["ok"] is True and out["can_list"] is False
    assert fake.objects == {}  # the probe is deleted
    assert fake.calls == ["PutObject", "GetObject", "DeleteObject", "ListObjectsV2"]
    assert SECRET not in str(out)


def test_a_key_that_can_list_passes_with_the_warning(s3):
    s3(FakeS3(can_list=True))
    out = _call()
    assert out["ok"] is True and out["can_list"] is True


@pytest.mark.parametrize("denied, step", [("PutObject", "write"), ("GetObject", "read"), ("DeleteObject", "delete")])
def test_a_missing_right_says_which_step_failed(s3, denied, step):
    s3(FakeS3(denied=denied))
    out = _call()
    assert out["ok"] is False and out["can_list"] is None
    assert out["message"].startswith(f"Could not {step} _tabularia-check/") and "AccessDenied" in out["message"]
    assert SECRET not in out["message"]


def test_a_secret_that_cannot_be_decrypted_is_a_clear_failure():
    out = _call(secret_key_encrypted="not-a-fernet-token")
    assert out["ok"] is False and out["message"].startswith("The client could not be created")


# ── a script's output, and the step's files ─────────────────────────────────
class FakeScriptsBucket(FakeS3):
    def head_object(self, Bucket, Key):
        self.calls.append("HeadObject")
        if Key not in self.objects:
            # without the listing right S3 answers 403 for a key that does not exist
            raise ClientError({"Error": {"Code": "403", "Message": "Forbidden"}}, "HeadObject")
        return {"ContentLength": len(self.objects[Key])}

    def download_file(self, Bucket, Key, Filename):
        with open(Filename, "wb") as f:
            f.write(self.objects[Key])

    def upload_file(self, Filename, Bucket, Key):
        self.calls.append("PutObject")
        self.objects[Key] = open(Filename, "rb").read()


class FakeStorage:
    def __init__(self, objects: dict | None = None):
        self.uploads: list[tuple[str, str, bytes]] = []
        self.objects = objects or {}

    def download_file(self, bucket, key, path):
        if (bucket, key) not in self.objects:
            raise ClientError({"Error": {"Code": "404", "Message": "Not Found"}}, "HeadObject")
        with open(path, "wb") as f:
            f.write(self.objects[(bucket, key)])

    def upload_file(self, path, bucket, key):
        self.uploads.append((bucket, key, open(path, "rb").read()))
        self.objects[(bucket, key)] = self.uploads[-1][2]


def _parquet() -> bytes:
    import polars as pl

    buf = io.BytesIO()
    pl.DataFrame({"x": [1, 2, 3]}).write_parquet(buf)
    return buf.getvalue()


def _import(key: str, contract: dict | None = None):
    spec = S3ConnectionSpec(endpoint_url="http://minio:9000", access_key="scripts", bucket="scripts",
                            secret_key_encrypted=encrypt_secret(SECRET))
    return routes.output_import(routes.OutputImportRequest(connection=spec, key=key, bucket="data-prep",
                                                           output_key="out/abc.parquet", contract=contract))


def test_a_scripts_output_is_copied_into_tabularias_storage(s3, monkeypatch):
    fake = s3(FakeScriptsBucket())
    fake.objects["env-1/step/output.parquet"] = _parquet()
    storage = FakeStorage()
    monkeypatch.setattr("app.utils.get_storage_service", lambda: storage)
    out = _import("env-1/step/output.parquet")
    assert out == {"found": True, "rows": 3, "bytes": len(fake.objects["env-1/step/output.parquet"]),
                   "columns": [{"name": "x", "dtype": "Int64"}]}
    assert storage.uploads[0][:2] == ("data-prep", "out/abc.parquet")


# ── the data contract of the datasource a saved script's output replaces ──────
BLOCKING = {"version": 4, "rules": [{"id": "r1", "kind": "range", "column": "x", "max": 2, "severity": "error"}]}


class Worker:
    """celery_app.send_task: the task run here, or a worker that never answers."""

    def __init__(self, answers: bool = True):
        self.sent: list[tuple[str, dict]] = []
        self.answers = answers

    def send_task(self, name, kwargs):
        from celery.exceptions import TimeoutError as CeleryTimeoutError

        from app.tasks import contract_jobs

        self.sent.append((name, kwargs))
        answers = self.answers

        class Result:
            def get(self, timeout):
                if not answers:
                    raise CeleryTimeoutError("The operation timed out.")
                return getattr(contract_jobs, name.rsplit(".", 1)[1])(**kwargs)

            def forget(self):
                pass

        return Result()


@pytest.fixture
def copied(s3, monkeypatch):
    """A script's output in its bucket, and Tabularia's storage it is copied to."""
    from app.contracts import service

    fake = s3(FakeScriptsBucket())
    fake.objects["env-1/step/output.parquet"] = _parquet()
    storage = FakeStorage()
    monkeypatch.setattr("app.utils.get_storage_service", lambda: storage)
    monkeypatch.setattr(service, "get_storage_service", lambda: storage)
    return "env-1/step/output.parquet"


def test_the_contract_is_evaluated_on_the_copy_by_a_worker(copied, monkeypatch):
    from app.tasks.celery_app import celery_app

    worker = Worker()
    monkeypatch.setattr(celery_app, "send_task", worker.send_task)
    out = _import(copied, BLOCKING)
    assert worker.sent == [("app.tasks.contract_jobs.check_before_publish_task",
                            {"bucket": "data-prep", "key": "out/abc.parquet", "contract": BLOCKING})]
    report = out["contract"]
    assert (report["outcome"], report["version"], report["rules"][0]["violations"]) == ("failed", 4, 1)
    # without a contract: nothing to evaluate, the answer as before
    assert "contract" not in _import(copied) and len(worker.sent) == 1


def test_a_worker_that_does_not_answer_does_not_let_a_blocking_contract_pass(copied, monkeypatch):
    from app.tasks.celery_app import celery_app

    monkeypatch.setattr(celery_app, "send_task", Worker(answers=False).send_task)
    report = _import(copied, BLOCKING)["contract"]
    assert (report["outcome"], report["errors"], report["error"]) == ("failed", 1, "The operation timed out.")


def test_the_evaluation_of_a_scripts_output_goes_to_the_runs_queue():
    """A run's data, as heavy as the data: not on the previews' worker, which must stay
    free for the people in the editor (the checks asked for by hand stay there)."""
    from app.tasks.celery_app import celery_app

    def queue(name):
        return celery_app.amqp.router.route({}, name)["queue"].name

    assert queue("app.tasks.contract_jobs.check_before_publish_task") == "celery"
    assert queue("app.tasks.contract_jobs.evaluate_contract_task") == "preview"


def test_an_output_that_was_not_written_or_is_not_parquet(s3, monkeypatch):
    fake = s3(FakeScriptsBucket())
    monkeypatch.setattr("app.utils.get_storage_service", lambda: FakeStorage())
    assert _import("env-1/step/output.parquet") == {"found": False}
    fake.objects["env-1/step/output.parquet"] = b"a,b\n1,2\n"
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as e:
        _import("env-1/step/output.parquet")
    assert e.value.status_code == 400 and "not a parquet file" in e.value.detail


def test_the_steps_files_are_deleted_by_key(s3):
    fake = s3(FakeScriptsBucket())
    fake.objects = {"env-1/s/input.parquet": b"x", "env-1/s/output.parquet": b"y", "env-1/other/input.parquet": b"z"}
    spec = S3ConnectionSpec(endpoint_url="http://minio:9000", access_key="scripts", bucket="scripts",
                            secret_key_encrypted=encrypt_secret(SECRET))
    out = routes.objects_delete(routes.ObjectsDeleteRequest(connection=spec, keys=["env-1/s/input.parquet",
                                                                                   "env-1/s/output.parquet"]))
    assert out == {"deleted": 2, "failed": []} and list(fake.objects) == ["env-1/other/input.parquet"]
    assert "ListObjectsV2" not in fake.calls


def _export(key: str):
    spec = S3ConnectionSpec(endpoint_url="http://minio:9000", access_key="scripts", bucket="scripts",
                            secret_key_encrypted=encrypt_secret(SECRET))
    return routes.input_export(routes.InputExportRequest(connection=spec, bucket="data-prep", key=key,
                                                         target_key="script-1/step/in-SALES.parquet"))


def test_a_declared_input_is_copied_to_the_scripts_bucket(s3, monkeypatch):
    fake = s3(FakeScriptsBucket())
    data = _parquet()
    monkeypatch.setattr("app.utils.get_storage_service", lambda: FakeStorage({("data-prep", "datasets/7/v2.parquet"): data}))
    assert _export("datasets/7/v2.parquet") == {"bytes": len(data)}
    assert fake.objects == {"script-1/step/in-SALES.parquet": data}


def test_an_input_whose_file_is_gone_is_a_404(s3, monkeypatch):
    fake = s3(FakeScriptsBucket())
    monkeypatch.setattr("app.utils.get_storage_service", lambda: FakeStorage())
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as e:
        _export("datasets/7/old.parquet")
    assert e.value.status_code == 404 and fake.objects == {}
