"""Settings from the panel: the administrator changes some of the engine's parameters
from Administration → Settings, without the environment and without restarts.

The rules these tests hold: only the listed items, whole and within their range; the
constraints between items (soft limit < hard limit, read timeout < soft limit, hard limit
< the gateway's threshold); an invalid value is discarded when read too; Valkey down =
the last values stay; every process sees them within REREAD_EVERY; they really reach
where they are needed (the import's row group, the export's cap, the tasks' limits, the
ClickHouse and BigQuery engines made once).
"""
import io
import json
import os
import sqlite3

import pyarrow.parquet as pq
import pytest

from app.core import panel_settings
from app.core.config import BigQuerySettings, ClickHouseExternalSettings, get_settings, settings_from_env
from tests.fakes import FakeStorage


def _errors(values, **kw):
    return panel_settings.validate(values, settings_from_env(), **kw)[1]


# ── what is accepted ──────────────────────────────────────────────────────

def test_only_the_listed_items_whole_and_in_their_range():
    assert _errors({"storage.endpoint": "http://altrove"}) == [{"key": "storage.endpoint", "code": "unknown"}]
    assert _errors({"export.max_rows": "100"})[0]["code"] == "integer"
    assert _errors({"export.max_rows": True})[0]["code"] == "integer"  # a bool is an int to Python
    assert _errors({"ingest.parquet_row_group_rows": 10})[0] == {
        "key": "ingest.parquet_row_group_rows", "code": "min", "limit": 10_000}
    assert _errors({"ingest.parquet_row_group_rows": 50_000_000})[0]["code"] == "max"
    assert _errors({"ingest.parquet_row_group_rows": 250_000, "export.max_rows": 0}) == []


def test_credentials_and_addresses_are_not_in_the_list():
    key_names = {v.key for v in panel_settings.ITEMS}
    for forbidden in ("storage.secret_key", "storage.endpoint", "redis.host", "security.fernet_key",
                    "clickhouse_external.password", "clickhouse_external.host", "bigquery.credentials_b64",
                    "celery.worker_concurrency", "celery.timezone", "storage.direct_read",
                    "clickhouse_external.transport", "redis.url"):
        assert forbidden not in key_names


def test_the_soft_limit_stays_under_the_hard_one():
    e = _errors({"celery.task_soft_time_limit": 3600})  # the environment's hard limit is 3600
    assert e == [{"key": "celery.task_soft_time_limit", "code": "below", "other": "celery.task_time_limit", "limit": 3600}]
    # lowering the hard limit under the environment's soft one: the hard limit is at fault
    e = _errors({"celery.task_time_limit": 1800})
    assert e[0]["key"] == "celery.task_time_limit" and e[0]["code"] == "above"
    assert _errors({"celery.task_time_limit": 1800, "celery.task_soft_time_limit": 1700,
                    "ingest.db_read_timeout_seconds": 1500}) == []


def test_whoever_waits_on_an_import_reads_the_read_timeout_not_the_killed_task():
    e = _errors({"ingest.db_read_timeout_seconds": 3300})  # the environment's soft limit is 3300
    assert e[0]["key"] == "ingest.db_read_timeout_seconds" and e[0]["code"] == "below"


def test_the_hard_limit_stays_under_the_gateways_threshold():
    """The gateway calls a run lost after its threshold: if the hard limit went beyond it,
    it would close as failed a run still alive."""
    e = _errors({"celery.task_time_limit": 3600}, hard_limit_max=3000)
    assert e[0] == {"key": "celery.task_time_limit", "code": "max", "limit": 3000}


# ── how they reach the processes ────────────────────────────────────────────────

def test_without_panel_values_it_is_the_environment_the_same_object():
    assert get_settings() is settings_from_env()


def test_written_they_hold_and_removed_the_environment_returns():
    panel_settings.write({"export.max_rows": 1234, "cache.max_step_rows": 0})
    s = get_settings()
    assert (s.export.max_rows, s.cache.max_step_rows) == (1234, 0)
    assert s.storage is settings_from_env().storage  # the sections not touched stay the same
    panel_settings.write({})
    assert get_settings().export.max_rows == settings_from_env().export.max_rows


def test_an_invalid_value_in_valkey_is_discarded_and_the_others_stay(regolazioni_isolate):
    regolazioni_isolate.set(panel_settings.KEY, json.dumps(
        {"export.max_rows": 99, "ingest.parquet_row_group_rows": 3, "storage.endpoint": "http://x"}))
    panel_settings.forget()
    s = get_settings()
    assert s.export.max_rows == 99
    assert s.ingest.parquet_row_group_rows == settings_from_env().ingest.parquet_row_group_rows
    assert s.storage.endpoint == settings_from_env().storage.endpoint


def test_the_other_processes_see_them_within_the_reread_time(regolazioni_isolate, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(panel_settings.time, "monotonic", lambda: now[0])
    panel_settings.forget()
    assert get_settings().export.max_rows == settings_from_env().export.max_rows
    # another process (the gateway, through the API) writes
    regolazioni_isolate.set(panel_settings.KEY, json.dumps({"export.max_rows": 42}))
    now[0] += panel_settings.REREAD_EVERY / 2
    assert get_settings().export.max_rows != 42  # not read again at every call
    now[0] += panel_settings.REREAD_EVERY
    assert get_settings().export.max_rows == 42


def test_valkey_down_the_last_values_stay(regolazioni_isolate, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(panel_settings.time, "monotonic", lambda: now[0])
    panel_settings.write({"export.max_rows": 77})
    assert get_settings().export.max_rows == 77

    class Giu:
        def get(self, *_):
            raise ConnectionError("Valkey non risponde")
    monkeypatch.setattr(panel_settings, "_client", lambda: Giu())
    now[0] += panel_settings.REREAD_EVERY * 3
    assert get_settings().export.max_rows == 77  # no going back to the environment for a passing fault


# ── where they are needed ─────────────────────────────────────────────────────────────

def test_the_imports_row_group_follows_the_panel(monkeypatch):
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
    panel_settings.write({"ingest.parquet_row_group_rows": 10_000})
    storage = FakeStorage()
    ingest_db_to_parquet(DbConnectionSpec(db_type="postgresql", host="h", database="d"),
                         DbSourceSpec(mode="table", ref="t"), bucket="b", key="k.parquet", storage=storage)
    md = pq.ParquetFile(io.BytesIO(storage.blobs[("b", "k.parquet")])).metadata
    assert (md.num_rows, md.num_row_groups) == (25_000, 3)


def test_the_exports_cap_follows_the_panel(monkeypatch):
    from app.tasks import jobs

    seen = {}

    class Fake:
        def export(self, **kw):
            seen.update(kw)
            with open(kw["out_path"], "w") as f:
                f.write("a\n")
    monkeypatch.setattr(jobs, "get_engine", lambda name: Fake())
    monkeypatch.setattr(jobs, "get_storage_service", lambda: FakeStorage())
    panel_settings.write({"export.max_rows": 1234})
    jobs.export_task.apply(kwargs={"bucket": "b", "input_key": "k", "operations": []}).get()
    assert seen["max_rows"] == 1234


def test_the_time_limits_travel_in_the_tasks_message():
    """Celery uses the message's: changed from the panel, they hold from the next task sent,
    without restarting the workers."""
    from app.tasks.celery_app import _time_limits_in_force

    headers = {"timelimit": [None, None]}
    _time_limits_in_force(headers=headers)
    env = settings_from_env().celery
    assert tuple(headers["timelimit"]) == (env.task_time_limit, env.task_soft_time_limit)
    panel_settings.write({"celery.task_time_limit": 1800, "celery.task_soft_time_limit": 1700,
                        "ingest.db_read_timeout_seconds": 1500})
    headers = {"timelimit": [None, None]}
    _time_limits_in_force(headers=headers)
    assert tuple(headers["timelimit"]) == (1800, 1700)
    # a task declaring its own keeps them
    own_ones = {"timelimit": [60, 50]}
    _time_limits_in_force(headers=own_ones)
    assert own_ones["timelimit"] == [60, 50]


def test_the_clickhouse_engine_made_once_follows_the_panel():
    from app.engine.clickhouse_engine import ClickHouseEngine
    from app.engine.matview import MatViewStore

    eng = ClickHouseEngine.__new__(ClickHouseEngine)
    eng.cfg = ClickHouseExternalSettings(host="ch.inner", password="segreta", transport="s3", parquet_scan_max_threads=8)
    store = MatViewStore(None, eng._cfg)
    assert eng.cfg.parquet_scan_max_threads == 8 and not store.cfg.materialize_enabled
    panel_settings.write({"clickhouse_external.parquet_scan_max_threads": 24,
                        "clickhouse_external.materialize_min_rows": 5_000_000,
                        "clickhouse_external.max_execution_time": 120})
    assert eng.cfg.parquet_scan_max_threads == 24 and eng.cfg.max_execution_time == 120
    assert store.cfg.materialize_enabled and store.cfg.materialize_min_rows == 5_000_000
    # the rest is what it was made with, credentials included
    assert eng.cfg.host == "ch.inner" and eng.cfg.password.get_secret_value() == "segreta"
    assert eng.cfg is eng.cfg  # one copy per version, not one at every access


def test_the_bigquery_engine_made_once_follows_the_panel():
    from app.engine.bigquery_engine import BigQueryEngine

    eng = BigQueryEngine.__new__(BigQueryEngine)
    eng.cfg = BigQuerySettings(project="p", maximum_bytes_billed=20 * 1024**3)
    panel_settings.write({"bigquery.maximum_bytes_billed": 5 * 1024**3})
    assert eng.cfg.maximum_bytes_billed == 5 * 1024**3 and eng.cfg.project == "p"


# ── the API the gateway calls ────────────────────────────────────────────────
# (the engine's other routes are tested by calling the functions: no TestClient here)

def _put(values, **kw):
    from app.api.routes.panel_settings import Write, write

    r = write(Write(values=values, **kw))
    if hasattr(r, "status_code"):  # a JSONResponse: the refusal
        return r.status_code, json.loads(r.body)
    return 200, r


def test_the_catalog_says_environment_value_and_range():
    from app.api.routes.panel_settings import read

    panel_settings.write({"export.max_rows": 1234})
    c = read(max_task_time_limit=3000)
    items = {v["key"]: v for v in c["items"]}
    assert items["export.max_rows"]["value"] == 1234
    assert items["export.max_rows"]["env_value"] == settings_from_env().export.max_rows
    assert items["export.max_rows"]["env_var"] == "EXPORT__MAX_ROWS"
    assert items["ingest.parquet_row_group_rows"]["value"] is None
    assert items["celery.task_time_limit"]["max"] == 3000
    assert {v["category"] for v in c["items"]} == {
        "import", "export", "execution", "cache", "clickhouse", "bigquery", "workers", "sftp", "s3", "api",
        "placeholders", "upload", "engine_api", "embedded"}
    assert items["api.min_interval_seconds"]["kind"] == "float" and items["export.max_rows"]["kind"] == "int"
    assert items["celery.max_tasks_per_child"]["rollout"] == ["worker", "preview-worker"]
    assert items["chdb.max_memory_usage"]["env_var"] == "CHDB_MAX_MEMORY_USAGE"


def test_the_writing_is_all_or_nothing():
    panel_settings.write({"export.max_rows": 1234})
    code, body = _put({"export.max_rows": 99, "celery.task_soft_time_limit": 9999})
    assert code == 422 and body["detail"]["errors"][0]["key"] == "celery.task_soft_time_limit"
    assert get_settings().export.max_rows == 1234  # nothing written
    code, _ = _put({"export.max_rows": 99})
    assert code == 200 and get_settings().export.max_rows == 99
    # replaces EVERYTHING: the missing ones go back to the environment
    _put({})
    assert get_settings() is settings_from_env()


def test_the_writing_respects_the_gateways_threshold():
    code, body = _put({"celery.task_time_limit": 3500}, max_task_time_limit=3000)
    assert code == 422 and body["detail"]["errors"][0]["code"] == "max"


# ── from the black-box test (2026-10-12) ───────────────────────────────

@pytest.mark.parametrize("grezzo", ["[1, 2]", "42", '"abc"', "null"])
def test_json_that_is_not_an_object_does_not_bring_down_get_settings(regolazioni_isolate, grezzo, monkeypatch):
    """`validate` used to call `.items()` on a list: an exception at every read, inside any
    request or task sent."""
    now = [1000.0]
    monkeypatch.setattr(panel_settings.time, "monotonic", lambda: now[0])
    panel_settings.write({"export.max_rows": 77})
    assert get_settings().export.max_rows == 77  # read once: they are the last valid ones
    regolazioni_isolate.set(panel_settings.KEY, grezzo)
    now[0] += panel_settings.REREAD_EVERY * 2
    assert get_settings().export.max_rows == 77  # they stay, no exception


@pytest.mark.parametrize("written", [
    {"celery.task_time_limit": 3200, "celery.task_soft_time_limit": 3250},
    {"celery.task_time_limit": 3200, "celery.task_soft_time_limit": 2900},  # the soft limit under the environment's read timeout
    {"celery.task_time_limit": 3200},  # under the environment's soft limit
])
def test_from_valkey_an_inconsistent_pair_never_applies(regolazioni_isolate, written):
    """Removing an item may break a constraint checked already: it is checked again until
    everything holds (a hard 3200 used to apply with a soft 3300)."""
    regolazioni_isolate.set(panel_settings.KEY, json.dumps(written))
    panel_settings.forget()
    c, i = get_settings().celery, get_settings().ingest
    assert c.task_soft_time_limit < c.task_time_limit
    assert i.db_read_timeout_seconds < c.task_soft_time_limit


def test_the_error_names_the_item_just_changed():
    """The soft limit set to 1700 already, the hard one changed to 1600: the hard one is at fault."""
    good, errors = panel_settings.validate(
        {"celery.task_soft_time_limit": 1700, "celery.task_time_limit": 1600, "ingest.db_read_timeout_seconds": 1500},
        settings_from_env(), changed={"celery.task_time_limit"})
    assert errors[0]["key"] == "celery.task_time_limit" and errors[0]["code"] == "above"
    assert good.get("celery.task_soft_time_limit") == 1700


# ── decimals, and what a process reads when it starts (2026-10-23) ───────────

def test_decimals_take_integers_too_and_refuse_the_rest():
    good, errors = panel_settings.validate({"api.min_interval_seconds": 1, "sftp.timeout_seconds": 2.5},
                                       settings_from_env())
    assert errors == [] and good == {"api.min_interval_seconds": 1.0, "sftp.timeout_seconds": 2.5}
    assert isinstance(good["api.min_interval_seconds"], float)
    for bad_value in ("1", True, float("nan"), float("inf")):
        assert _errors({"api.min_interval_seconds": bad_value})[0]["code"] == "number"
    assert _errors({"celery.max_memory_headroom_frac": 0.99})[0] == {
        "key": "celery.max_memory_headroom_frac", "code": "max", "limit": 0.95}


def test_the_new_live_ones_reach_their_readers():
    panel_settings.write({"sftp.max_files": 3, "api.max_pages": 7, "placeholders.max_values": 9,
                        "s3_source.max_listed": 1000, "upload.max_bytes": 2 * 1024 ** 2})
    s = get_settings()
    assert (s.sftp.max_files, s.api.max_pages, s.placeholders.max_values, s.s3_source.max_listed,
            s.upload.max_bytes) == (3, 7, 9, 1000, 2 * 1024 ** 2)
    from app.api.upload_limit import _from_settings

    assert _from_settings() == 2 * 1024 ** 2
    # no limit at all (0) only from the environment: /tmp has one
    assert _errors({"upload.max_bytes": 0})[0]["code"] == "min"


def test_a_rollout_one_waits_for_the_process_to_start_again():
    env = settings_from_env()
    assert get_settings().celery.max_tasks_per_child == env.celery.max_tasks_per_child  # first reading
    panel_settings.write({"celery.max_tasks_per_child": 7, "export.max_rows": 5})
    s = get_settings()
    assert s.export.max_rows == 5  # the live one at once
    assert s.celery.max_tasks_per_child == env.celery.max_tasks_per_child  # this process: as it started
    assert panel_settings.start_values() == {}
    panel_settings.forget()  # a process that starts now
    assert get_settings().celery.max_tasks_per_child == 7
    assert panel_settings.at_start(env).celery.max_tasks_per_child == 7
    assert panel_settings.start_values() == {"celery.max_tasks_per_child": 7}


def test_a_variable_read_at_import_takes_the_panel_value_of_the_start(regolazioni_isolate, monkeypatch):
    monkeypatch.delenv("CHDB_MAX_MEMORY_USAGE", raising=False)
    assert panel_settings.value_at_start("CHDB_MAX_MEMORY_USAGE") == 1_500_000_000  # the default
    panel_settings.forget()
    monkeypatch.setenv("CHDB_MAX_MEMORY_USAGE", "2000000000")
    assert panel_settings.value_at_start("CHDB_MAX_MEMORY_USAGE") == 2_000_000_000  # the environment's
    regolazioni_isolate.set(panel_settings.KEY, json.dumps({"chdb.max_memory_usage": 3_000_000_000}))
    panel_settings.forget()
    assert panel_settings.value_at_start("CHDB_MAX_MEMORY_USAGE") == 3_000_000_000  # the panel's
    s = get_settings()  # not a settings field: nothing to lay on the settings
    assert not hasattr(s, "chdb")
    from app.api.routes.panel_settings import read

    item = next(v for v in read()["items"] if v["key"] == "chdb.max_memory_usage")
    assert (item["env_value"], item["value"]) == (2_000_000_000, 3_000_000_000)


def test_valkey_down_at_start_the_environment_applies(regolazioni_isolate, monkeypatch):
    class Giu:
        def get(self, *_):
            raise ConnectionError("Valkey non risponde")
    monkeypatch.setattr(panel_settings, "_client", lambda: Giu())
    panel_settings.forget()
    env = settings_from_env()
    assert panel_settings.at_start(env).celery.max_tasks_per_child == env.celery.max_tasks_per_child
    assert panel_settings.start_values() == {}


# ── from the bug hunt of 2026-10-23 ───────────────────────────────────────────

def test_an_integer_too_large_for_a_decimal_is_refused_not_raised():
    """10**400 made float() raise inside `validate`: a 500, and in Valkey a get_settings that failed everywhere."""
    assert _errors({"api.min_interval_seconds": 10 ** 400})[0]["code"] == "max"


def test_a_malformed_variable_read_at_import_stops_the_process(regolazioni_isolate, monkeypatch):
    """As before these settings were in the panel: «2GB» does not quietly become the default."""
    monkeypatch.setenv("CHDB_MAX_MEMORY_USAGE", "2GB")
    panel_settings.forget()
    with pytest.raises(ValueError, match="CHDB_MAX_MEMORY_USAGE"):
        panel_settings.value_at_start("CHDB_MAX_MEMORY_USAGE")


def test_the_cross_join_cap_is_not_greyed_out_without_chdb():
    """It limits every engine: with chDB missing, its category was shown as unavailable."""
    from app.api.routes.panel_settings import read

    item = next(v for v in read()["items"] if v["key"] == "engine.max_cross_join_rows")
    assert item["category"] == "execution"


def test_the_hard_limit_back_to_the_environment_respects_the_gateway_threshold():
    """Back to the environment's 3600 with the gateway declaring runs lost after 3300 + 300."""
    good, errors = panel_settings.validate({}, settings_from_env(), hard_limit_max=3000,
                                       changed={"celery.task_time_limit"})
    assert errors == [{"key": "celery.task_time_limit", "code": "env_too_high", "limit": 3000}]
    # a periodic send (nothing changed): the environment is the installer's, not refused
    assert panel_settings.validate({}, settings_from_env(), hard_limit_max=3000)[1] == []


def test_no_values_is_written_and_a_starting_worker_waits_for_them(regolazioni_isolate, monkeypatch):
    """A key deleted when there were no values could not tell «none» from «not sent yet»:
    a worker started on an empty Valkey froze the environment's values for its whole life."""
    panel_settings.write({})
    assert regolazioni_isolate.get(panel_settings.KEY) == "{}"
    assert panel_settings.wait_at_first_start(maximum=0) is True
    regolazioni_isolate.delete(panel_settings.KEY)
    monkeypatch.setattr(panel_settings.time, "sleep", lambda s: None)
    assert panel_settings.wait_at_first_start(maximum=0) is False


def test_a_resized_pool_keeps_the_memory_the_panel_set(regolazioni_isolate):
    """The pool resized from Administration → Workers took the environment's memory ceiling
    again, over the panel's the worker started with."""
    from app.tasks import worker_pool

    regolazioni_isolate.set(panel_settings.KEY, json.dumps({"celery.max_memory_per_child_kb": 3_000_000}))
    panel_settings.forget()
    assert worker_pool.per_process_kb(4) == 3_000_000


def test_an_exception_celery_could_not_rebuild_reads_as_a_sentence():
    """Seen on kind with the storage down: «<class 'botocore.exceptions.EndpointConnectionError'>(['…'])»
    was the error people read."""
    from app.api.routes.tasks import readable

    raw = "<class 'botocore.exceptions.EndpointConnectionError'>(['Could not connect to the endpoint URL: \"http://minio:9000/b/k\"'])"
    assert readable(raw) == 'The object storage is not answering: Could not connect to the endpoint URL: "http://minio:9000/b/k"'
    assert readable("<class 'foo.Bar'>(['went wrong'])") == "Bar: went wrong"
    assert readable("a plain message") == "a plain message"
