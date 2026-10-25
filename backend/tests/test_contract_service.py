"""The data contracts inside the tasks that write the data: the report travels with the
result, and with a blocking rule violated nothing leaves the storage."""
import polars as pl
import pytest

from app.contracts import service


@pytest.fixture
def parquet(tmp_path, monkeypatch):
    """A «storage» that is a folder: `download_file` copies the parquet asked for."""
    import shutil

    origin = tmp_path / "data.parquet"
    pl.DataFrame({"id": [1, 2, 3], "stato": ["a", None, "c"]}).write_parquet(origin)

    class Storage:
        def download_file(self, bucket, key, path):
            if key != "datasets/ok.parquet":
                raise FileNotFoundError(f"{bucket}/{key}")
            shutil.copy(origin, path)

    monkeypatch.setattr(service, "get_storage_service", lambda: Storage())
    return "datasets/ok.parquet"


WARNINGS_ONLY = {"rules": [{"id": "a", "kind": "not_null", "column": "stato", "severity": "warning"}]}
BLOCKING = {"rules": [{"id": "a", "kind": "not_null", "column": "stato", "severity": "error"}]}


def test_the_report_on_the_snapshot_in_the_storage(parquet):
    r = service.evaluate_key("b", parquet, BLOCKING)
    assert (r["outcome"], r["rows"], r["rules"][0]["violations"]) == ("failed", 3, 1)
    assert service.evaluate_key("b", parquet, WARNINGS_ONLY)["outcome"] == "warning"


def test_profile_and_proposal_from_the_snapshot(parquet):
    r = service.profile_key("b", parquet)
    assert r["profile"]["rows"] == 3 and {c["name"] for c in r["profile"]["columns"]} == {"id", "stato"}
    assert any(x["kind"] == "column" and x["column"] == "id" for x in r["proposal"]["rules"])


def test_a_blocking_contract_that_cannot_be_evaluated_does_not_pass(parquet):
    r = service.check_before_publish("b", "datasets/sparito.parquet", BLOCKING)
    assert r["outcome"] == "failed" and r["errors"] == 1 and "sparito" in r["error"]


def test_a_warnings_only_contract_that_cannot_be_evaluated_does_not_block(parquet):
    r = service.check_before_publish("b", "datasets/sparito.parquet", WARNINGS_ONLY)
    assert r["outcome"] == "warning" and r["errors"] == 0 and r["error"]


def test_the_ingest_task_carries_the_report_only_with_a_contract(parquet, monkeypatch):
    import app.ingest.db_source as db_source
    from app.tasks.jobs import ingest_database_task

    monkeypatch.setattr(db_source, "ingest_db_to_parquet", lambda **kw: {"rows_written": 3, "columns": [{"name": "id", "dtype": "Int64"}]})
    conn = {"db_type": "postgresql", "host": "h", "port": 5432, "username": "u", "password_encrypted": "", "database": "d"}
    without = ingest_database_task.run(connection=conn, source={"mode": "table", "ref": "t"}, bucket="b", output_key=parquet)
    assert "contract" not in without and without["rows_written"] == 3
    with_contract = ingest_database_task.run(connection=conn, source={"mode": "table", "ref": "t"}, bucket="b", output_key=parquet, contract=BLOCKING)
    assert with_contract["contract"]["outcome"] == "failed" and with_contract["rows_written"] == 3


def test_with_a_blocking_rule_violated_the_copy_and_the_email_do_not_go(parquet, monkeypatch):
    """The transformation task delivers the output elsewhere too (S3 copy, email): when the
    contract refuses it, that data must not leave."""
    from types import SimpleNamespace

    import app.tasks.jobs as jobs

    class StubEngine:
        def run(self, source, operations, destination):
            return SimpleNamespace(rows_written=3, columns=[])

    monkeypatch.setattr(jobs, "get_engine", lambda name=None: StubEngine())
    deliveries = []
    import app.ingest.s3_destination as s3d

    monkeypatch.setattr(s3d, "write_output_to_s3", lambda *a, **k: deliveries.append("s3") or {"rows": 3})
    copy = {"connection": {"endpoint": "http://x", "access_key": "a", "secret_key_encrypted": "", "region": "r"}, "target": {"bucket": "fuori", "key": "k.parquet"}}

    out = jobs.transform_data_task.run(bucket="b", input_key="in", operations=[], output_key=parquet, mirror=copy, contract=BLOCKING)
    assert out["contract"]["outcome"] == "failed" and "mirror" not in out and deliveries == []

    out = jobs.transform_data_task.run(bucket="b", input_key="in", operations=[], output_key=parquet, contract=WARNINGS_ONLY)
    assert out["contract"]["outcome"] == "warning"
    assert "contract" not in jobs.transform_data_task.run(bucket="b", input_key="in", operations=[], output_key=parquet)
