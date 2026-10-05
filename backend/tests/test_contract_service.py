"""I data contracts dentro i task che scrivono i dati: il referto viaggia col
risultato, e con una regola bloccante violata niente esce dallo storage."""
import polars as pl
import pytest

from app.contracts import service


@pytest.fixture
def parquet(tmp_path, monkeypatch):
    """Uno «storage» che è una cartella: `download_file` copia il parquet richiesto."""
    import shutil

    origine = tmp_path / "dati.parquet"
    pl.DataFrame({"id": [1, 2, 3], "stato": ["a", None, "c"]}).write_parquet(origine)

    class Storage:
        def download_file(self, bucket, key, path):
            if key != "datasets/ok.parquet":
                raise FileNotFoundError(f"{bucket}/{key}")
            shutil.copy(origine, path)

    monkeypatch.setattr(service, "get_storage_service", lambda: Storage())
    return "datasets/ok.parquet"


SOLO_AVVISI = {"rules": [{"id": "a", "kind": "not_null", "column": "stato", "severity": "warning"}]}
BLOCCANTE = {"rules": [{"id": "a", "kind": "not_null", "column": "stato", "severity": "error"}]}


def test_il_referto_sullo_snapshot_nello_storage(parquet):
    r = service.evaluate_key("b", parquet, BLOCCANTE)
    assert (r["outcome"], r["rows"], r["rules"][0]["violations"]) == ("failed", 3, 1)
    assert service.evaluate_key("b", parquet, SOLO_AVVISI)["outcome"] == "warning"


def test_profilo_e_proposta_dallo_snapshot(parquet):
    r = service.profile_key("b", parquet)
    assert r["profile"]["rows"] == 3 and {c["name"] for c in r["profile"]["columns"]} == {"id", "stato"}
    assert any(x["kind"] == "column" and x["column"] == "id" for x in r["proposal"]["rules"])


def test_se_non_si_riesce_a_valutare_un_contratto_bloccante_non_passa(parquet):
    r = service.check_before_publish("b", "datasets/sparito.parquet", BLOCCANTE)
    assert r["outcome"] == "failed" and r["errors"] == 1 and "sparito" in r["error"]


def test_se_non_si_riesce_a_valutare_un_contratto_di_soli_avvisi_non_blocca(parquet):
    r = service.check_before_publish("b", "datasets/sparito.parquet", SOLO_AVVISI)
    assert r["outcome"] == "warning" and r["errors"] == 0 and r["error"]


def test_il_task_di_ingest_porta_il_referto_solo_se_c_e_un_contratto(parquet, monkeypatch):
    import app.ingest.db_source as db_source
    from app.tasks.jobs import ingest_database_task

    monkeypatch.setattr(db_source, "ingest_db_to_parquet", lambda **kw: {"rows_written": 3, "columns": [{"name": "id", "dtype": "Int64"}]})
    conn = {"db_type": "postgresql", "host": "h", "port": 5432, "username": "u", "password_encrypted": "", "database": "d"}
    senza = ingest_database_task.run(connection=conn, source={"mode": "table", "ref": "t"}, bucket="b", output_key=parquet)
    assert "contract" not in senza and senza["rows_written"] == 3
    con = ingest_database_task.run(connection=conn, source={"mode": "table", "ref": "t"}, bucket="b", output_key=parquet, contract=BLOCCANTE)
    assert con["contract"]["outcome"] == "failed" and con["rows_written"] == 3


def test_con_una_regola_bloccante_violata_la_copia_e_l_email_non_partono(parquet, monkeypatch):
    """Il task di trasformazione consegna l'output anche altrove (copia su S3,
    email): se il contratto lo rifiuta, quei dati non devono uscire."""
    from types import SimpleNamespace

    import app.tasks.jobs as jobs

    class Motore:
        def run(self, source, operations, destination):
            return SimpleNamespace(rows_written=3, columns=[])

    monkeypatch.setattr(jobs, "get_engine", lambda nome=None: Motore())
    consegne = []
    import app.ingest.s3_destination as s3d

    monkeypatch.setattr(s3d, "write_output_to_s3", lambda *a, **k: consegne.append("s3") or {"rows": 3})
    copia = {"connection": {"endpoint": "http://x", "access_key": "a", "secret_key_encrypted": "", "region": "r"}, "target": {"bucket": "fuori", "key": "k.parquet"}}

    out = jobs.transform_data_task.run(bucket="b", input_key="in", operations=[], output_key=parquet, mirror=copia, contract=BLOCCANTE)
    assert out["contract"]["outcome"] == "failed" and "mirror" not in out and consegne == []

    out = jobs.transform_data_task.run(bucket="b", input_key="in", operations=[], output_key=parquet, contract=SOLO_AVVISI)
    assert out["contract"]["outcome"] == "warning"
    assert "contract" not in jobs.transform_data_task.run(bucket="b", input_key="in", operations=[], output_key=parquet)
