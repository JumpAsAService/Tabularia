"""Regolazioni dal pannello: l'amministratore cambia alcuni parametri del motore da
Amministrazione → Impostazioni, senza ambiente e senza riavvii.

Le regole che questi test tengono ferme: solo le voci dell'elenco, intere e dentro
il loro intervallo; i vincoli fra voci (limite morbido < limite duro, timeout di
lettura < limite morbido, limite duro < soglia del gateway); un valore non valido
si scarta anche quando si legge; Valkey giù = restano gli ultimi valori; ogni
processo le vede entro RILEGGI_OGNI; arrivano davvero dove servono (row group
dell'import, tetto dell'export, limiti dei task, motori ClickHouse e BigQuery
creati una volta).
"""
import io
import json
import os
import sqlite3

import pyarrow.parquet as pq
import pytest

from app.core import regolazioni
from app.core.config import BigQuerySettings, ClickHouseExternalSettings, get_settings, settings_dall_ambiente
from tests.fakes import FakeStorage


def _errori(valori, **kw):
    return regolazioni.valida(valori, settings_dall_ambiente(), **kw)[1]


# ── che cosa si accetta ──────────────────────────────────────────────────────

def test_solo_le_voci_dell_elenco_intere_e_nel_loro_intervallo():
    assert _errori({"storage.endpoint": "http://altrove"}) == [{"key": "storage.endpoint", "code": "unknown"}]
    assert _errori({"export.max_rows": "100"})[0]["code"] == "integer"
    assert _errori({"export.max_rows": True})[0]["code"] == "integer"  # un bool è un int per Python
    assert _errori({"ingest.parquet_row_group_rows": 10})[0] == {
        "key": "ingest.parquet_row_group_rows", "code": "min", "limit": 10_000}
    assert _errori({"ingest.parquet_row_group_rows": 50_000_000})[0]["code"] == "max"
    assert _errori({"ingest.parquet_row_group_rows": 250_000, "export.max_rows": 0}) == []


def test_le_credenziali_e_gli_indirizzi_non_sono_nell_elenco():
    chiavi = {v.chiave for v in regolazioni.VOCI}
    for vietata in ("storage.secret_key", "storage.endpoint", "redis.host", "security.fernet_key",
                    "clickhouse_external.password", "clickhouse_external.host", "bigquery.credentials_b64",
                    "celery.worker_concurrency", "celery.max_memory_per_child_kb"):
        assert vietata not in chiavi


def test_il_limite_morbido_resta_sotto_il_duro():
    e = _errori({"celery.task_soft_time_limit": 3600})  # il duro dell'ambiente è 3600
    assert e == [{"key": "celery.task_soft_time_limit", "code": "below", "other": "celery.task_time_limit", "limit": 3600}]
    # abbassare il duro sotto il morbido dell'ambiente: la colpa è del duro
    e = _errori({"celery.task_time_limit": 1800})
    assert e[0]["key"] == "celery.task_time_limit" and e[0]["code"] == "above"
    assert _errori({"celery.task_time_limit": 1800, "celery.task_soft_time_limit": 1700,
                    "ingest.db_read_timeout_seconds": 1500}) == []


def test_chi_aspetta_un_import_legge_il_timeout_di_lettura_non_il_task_ucciso():
    e = _errori({"ingest.db_read_timeout_seconds": 3300})  # il morbido dell'ambiente è 3300
    assert e[0]["key"] == "ingest.db_read_timeout_seconds" and e[0]["code"] == "below"


def test_il_limite_duro_resta_sotto_la_soglia_del_gateway():
    """Il gateway dichiara perso un run dopo la sua soglia: se il limite duro la
    superasse, chiuderebbe come fallito un run ancora vivo."""
    e = _errori({"celery.task_time_limit": 3600}, limite_duro_max=3000)
    assert e[0] == {"key": "celery.task_time_limit", "code": "max", "limit": 3000}


# ── come arrivano ai processi ────────────────────────────────────────────────

def test_senza_regolazioni_e_l_ambiente_lo_stesso_oggetto():
    assert get_settings() is settings_dall_ambiente()


def test_scritte_valgono_e_tolte_torna_l_ambiente():
    regolazioni.scrivi({"export.max_rows": 1234, "cache.max_step_rows": 0})
    s = get_settings()
    assert (s.export.max_rows, s.cache.max_step_rows) == (1234, 0)
    assert s.storage is settings_dall_ambiente().storage  # le sezioni non toccate restano quelle
    regolazioni.scrivi({})
    assert get_settings().export.max_rows == settings_dall_ambiente().export.max_rows


def test_un_valore_non_valido_in_valkey_si_scarta_e_gli_altri_restano(regolazioni_isolate):
    regolazioni_isolate.set(regolazioni.CHIAVE, json.dumps(
        {"export.max_rows": 99, "ingest.parquet_row_group_rows": 3, "storage.endpoint": "http://x"}))
    regolazioni.dimentica()
    s = get_settings()
    assert s.export.max_rows == 99
    assert s.ingest.parquet_row_group_rows == settings_dall_ambiente().ingest.parquet_row_group_rows
    assert s.storage.endpoint == settings_dall_ambiente().storage.endpoint


def test_gli_altri_processi_le_vedono_entro_il_tempo_di_rilettura(regolazioni_isolate, monkeypatch):
    adesso = [1000.0]
    monkeypatch.setattr(regolazioni.time, "monotonic", lambda: adesso[0])
    regolazioni.dimentica()
    assert get_settings().export.max_rows == settings_dall_ambiente().export.max_rows
    # un altro processo (il gateway, tramite l'API) scrive
    regolazioni_isolate.set(regolazioni.CHIAVE, json.dumps({"export.max_rows": 42}))
    adesso[0] += regolazioni.RILEGGI_OGNI / 2
    assert get_settings().export.max_rows != 42  # non si rilegge a ogni chiamata
    adesso[0] += regolazioni.RILEGGI_OGNI
    assert get_settings().export.max_rows == 42


def test_valkey_giu_restano_gli_ultimi_valori(regolazioni_isolate, monkeypatch):
    adesso = [1000.0]
    monkeypatch.setattr(regolazioni.time, "monotonic", lambda: adesso[0])
    regolazioni.scrivi({"export.max_rows": 77})
    assert get_settings().export.max_rows == 77

    class Giu:
        def get(self, *_):
            raise ConnectionError("Valkey non risponde")
    monkeypatch.setattr(regolazioni, "_client", lambda: Giu())
    adesso[0] += regolazioni.RILEGGI_OGNI * 3
    assert get_settings().export.max_rows == 77  # non si torna all'ambiente per un guasto passeggero


# ── dove servono ─────────────────────────────────────────────────────────────

def test_il_row_group_dell_import_segue_il_pannello(monkeypatch):
    from app.ingest import db_source
    from app.ingest.db_source import DbConnectionSpec, DbSourceSpec, _dbapi_batches, ingest_db_to_parquet

    def driver(conn, query):
        db = sqlite3.connect(":memory:")
        cur = db.cursor()
        cur.execute("CREATE TABLE t (id INTEGER)")
        cur.executemany("INSERT INTO t VALUES (?)", [(i,) for i in range(25_000)])
        cur.execute("SELECT id FROM t")
        yield from _dbapi_batches(cur, batch_rows=1_000)
    monkeypatch.setitem(db_source._DRIVERS, "postgresql", driver)
    regolazioni.scrivi({"ingest.parquet_row_group_rows": 10_000})
    storage = FakeStorage()
    ingest_db_to_parquet(DbConnectionSpec(db_type="postgresql", host="h", database="d"),
                         DbSourceSpec(mode="table", ref="t"), bucket="b", key="k.parquet", storage=storage)
    md = pq.ParquetFile(io.BytesIO(storage.blobs[("b", "k.parquet")])).metadata
    assert (md.num_rows, md.num_row_groups) == (25_000, 3)


def test_il_tetto_dell_export_segue_il_pannello(monkeypatch):
    from app.tasks import jobs

    visto = {}

    class Finto:
        def export(self, **kw):
            visto.update(kw)
            with open(kw["out_path"], "w") as f:
                f.write("a\n")
    monkeypatch.setattr(jobs, "get_engine", lambda nome: Finto())
    monkeypatch.setattr(jobs, "get_storage_service", lambda: FakeStorage())
    regolazioni.scrivi({"export.max_rows": 1234})
    jobs.export_task.apply(kwargs={"bucket": "b", "input_key": "k", "operations": []}).get()
    assert visto["max_rows"] == 1234


def test_i_limiti_di_tempo_viaggiano_nel_messaggio_del_task():
    """Celery usa quelli del messaggio: cambiati dal pannello, valgono dal task
    spedito dopo, senza riavviare i worker."""
    from app.tasks.celery_app import _limiti_di_tempo_in_vigore

    intestazioni = {"timelimit": [None, None]}
    _limiti_di_tempo_in_vigore(headers=intestazioni)
    env = settings_dall_ambiente().celery
    assert tuple(intestazioni["timelimit"]) == (env.task_time_limit, env.task_soft_time_limit)
    regolazioni.scrivi({"celery.task_time_limit": 1800, "celery.task_soft_time_limit": 1700,
                        "ingest.db_read_timeout_seconds": 1500})
    intestazioni = {"timelimit": [None, None]}
    _limiti_di_tempo_in_vigore(headers=intestazioni)
    assert tuple(intestazioni["timelimit"]) == (1800, 1700)
    # un task che ne dichiara di suoi li tiene
    proprie = {"timelimit": [60, 50]}
    _limiti_di_tempo_in_vigore(headers=proprie)
    assert proprie["timelimit"] == [60, 50]


def test_il_motore_clickhouse_creato_una_volta_segue_il_pannello():
    from app.engine.clickhouse_engine import ClickHouseEngine
    from app.engine.matview import MatViewStore

    eng = ClickHouseEngine.__new__(ClickHouseEngine)
    eng.cfg = ClickHouseExternalSettings(host="ch.interno", password="segreta", transport="s3", parquet_scan_max_threads=8)
    store = MatViewStore(None, eng._cfg)
    assert eng.cfg.parquet_scan_max_threads == 8 and not store.cfg.materialize_enabled
    regolazioni.scrivi({"clickhouse_external.parquet_scan_max_threads": 24,
                        "clickhouse_external.materialize_min_rows": 5_000_000,
                        "clickhouse_external.max_execution_time": 120})
    assert eng.cfg.parquet_scan_max_threads == 24 and eng.cfg.max_execution_time == 120
    assert store.cfg.materialize_enabled and store.cfg.materialize_min_rows == 5_000_000
    # il resto è quello con cui è stato creato, credenziali comprese
    assert eng.cfg.host == "ch.interno" and eng.cfg.password.get_secret_value() == "segreta"
    assert eng.cfg is eng.cfg  # una copia per versione, non una a ogni accesso


def test_il_motore_bigquery_creato_una_volta_segue_il_pannello():
    from app.engine.bigquery_engine import BigQueryEngine

    eng = BigQueryEngine.__new__(BigQueryEngine)
    eng.cfg = BigQuerySettings(project="p", maximum_bytes_billed=20 * 1024**3)
    regolazioni.scrivi({"bigquery.maximum_bytes_billed": 5 * 1024**3})
    assert eng.cfg.maximum_bytes_billed == 5 * 1024**3 and eng.cfg.project == "p"


# ── l'API che chiama il gateway ──────────────────────────────────────────────
# (le altre rotte dell'engine si provano chiamando le funzioni: qui niente TestClient)

def _put(valori, **kw):
    from app.api.routes.regolazioni import Scrittura, scrivi

    r = scrivi(Scrittura(values=valori, **kw))
    if hasattr(r, "status_code"):  # una JSONResponse: il rifiuto
        return r.status_code, json.loads(r.body)
    return 200, r


def test_il_catalogo_dice_ambiente_valore_e_intervallo():
    from app.api.routes.regolazioni import leggi

    regolazioni.scrivi({"export.max_rows": 1234})
    c = leggi(max_task_time_limit=3000)
    voci = {v["key"]: v for v in c["items"]}
    assert voci["export.max_rows"]["value"] == 1234
    assert voci["export.max_rows"]["env_value"] == settings_dall_ambiente().export.max_rows
    assert voci["export.max_rows"]["env_var"] == "EXPORT__MAX_ROWS"
    assert voci["ingest.parquet_row_group_rows"]["value"] is None
    assert voci["celery.task_time_limit"]["max"] == 3000
    assert {v["category"] for v in c["items"]} == {"import", "export", "execution", "cache", "clickhouse", "bigquery", "workers"}


def test_la_scrittura_e_tutto_o_niente():
    regolazioni.scrivi({"export.max_rows": 1234})
    codice, corpo = _put({"export.max_rows": 99, "celery.task_soft_time_limit": 9999})
    assert codice == 422 and corpo["detail"]["errors"][0]["key"] == "celery.task_soft_time_limit"
    assert get_settings().export.max_rows == 1234  # niente di scritto
    codice, _ = _put({"export.max_rows": 99})
    assert codice == 200 and get_settings().export.max_rows == 99
    # sostituisce TUTTO: chi non c'è torna all'ambiente
    _put({})
    assert get_settings() is settings_dall_ambiente()


def test_la_scrittura_rispetta_la_soglia_del_gateway():
    codice, corpo = _put({"celery.task_time_limit": 3500}, max_task_time_limit=3000)
    assert codice == 422 and corpo["detail"]["errors"][0]["code"] == "max"


# ── dalla prova a scatola chiusa (2026-10-12) ───────────────────────────────

@pytest.mark.parametrize("grezzo", ["[1, 2]", "42", '"abc"', "null"])
def test_un_json_che_non_e_un_oggetto_non_fa_cadere_get_settings(regolazioni_isolate, grezzo, monkeypatch):
    """Prima `valida` chiamava `.items()` su una lista: un'eccezione a ogni rilettura,
    dentro qualunque richiesta o invio di task."""
    adesso = [1000.0]
    monkeypatch.setattr(regolazioni.time, "monotonic", lambda: adesso[0])
    regolazioni.scrivi({"export.max_rows": 77})
    assert get_settings().export.max_rows == 77  # lette una volta: sono le ultime valide
    regolazioni_isolate.set(regolazioni.CHIAVE, grezzo)
    adesso[0] += regolazioni.RILEGGI_OGNI * 2
    assert get_settings().export.max_rows == 77  # restano quelle, nessuna eccezione


@pytest.mark.parametrize("scritto", [
    {"celery.task_time_limit": 3200, "celery.task_soft_time_limit": 3250},
    {"celery.task_time_limit": 3200, "celery.task_soft_time_limit": 2900},  # il morbido sotto la lettura dell'ambiente
    {"celery.task_time_limit": 3200},  # sotto il morbido dell'ambiente
])
def test_da_valkey_non_si_applica_mai_una_coppia_incoerente(regolazioni_isolate, scritto):
    """Togliere una voce può rompere un vincolo già controllato: si ricontrolla
    finché tutto torna (prima si applicava un duro 3200 col morbido 3300)."""
    regolazioni_isolate.set(regolazioni.CHIAVE, json.dumps(scritto))
    regolazioni.dimentica()
    c, i = get_settings().celery, get_settings().ingest
    assert c.task_soft_time_limit < c.task_time_limit
    assert i.db_read_timeout_seconds < c.task_soft_time_limit


def test_l_errore_nomina_la_voce_appena_cambiata():
    """Morbido già impostato a 1700, si cambia il duro a 1600: la colpa è del duro."""
    buoni, errori = regolazioni.valida(
        {"celery.task_soft_time_limit": 1700, "celery.task_time_limit": 1600, "ingest.db_read_timeout_seconds": 1500},
        settings_dall_ambiente(), cambiate={"celery.task_time_limit"})
    assert errori[0]["key"] == "celery.task_time_limit" and errori[0]["code"] == "above"
    assert buoni.get("celery.task_soft_time_limit") == 1700
