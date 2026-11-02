"""«Test connection» of the external ClickHouse, from Administration → Settings: before a
connection is saved and the engine's processes are rolled out onto it, the administrator
sees whether it works, step by step, with the server's own reason when it does not.

The steps, each only when the one before held:
- server: signing in (the main user) and the version;
- database: the working database exists;
- ai_user: the AI assistant's user signs in too (when both its name and password are set);
- storage (transport s3): the server reads from the object storage a small parquet written
  for the test, as it will read the datasets (endpoint as it sees it, named collection or
  keys); the file is removed afterwards;
- staging (transport push): the user may create and drop a table in the working database,
  as the staging tables need.

Nothing stays on the server or in the bucket. The messages go through the engine's error
cleaning and the redaction of the secrets — the candidate passwords too, which are not in
the settings yet."""
from __future__ import annotations

import io
import logging
import uuid
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from app.core.config import ClickHouseExternalSettings, get_settings
from app.core.redaction import MASK, redact_secrets
from app.engine.base import DataSource
from app.engine.chdb_ops import _qi
from app.engine.clickhouse_engine import _clean_error, s3_table_function

logger = logging.getLogger(__name__)

# a test must not keep the administrator waiting as long as a query may run
TIMEOUT_SECONDS = 30
CHECK_PREFIX = "tmp/clickhouse-check/"


def _client(cfg: ClickHouseExternalSettings, user: str, password: str):
    """Without the working database: a missing one would fail the sign-in itself, and the
    test would blame the server instead of naming the database (the next step does)."""
    import clickhouse_connect

    return clickhouse_connect.get_client(
        host=cfg.host, port=cfg.port, username=user or "default", password=password, secure=cfg.secure,
        connect_timeout=min(cfg.connect_timeout, TIMEOUT_SECONDS), send_receive_timeout=TIMEOUT_SECONDS,
        autogenerate_session_id=False,
    )


def _reason(e: Exception, cfg: ClickHouseExternalSettings) -> str:
    text = redact_secrets(_clean_error(e, cfg.host, cfg.port)) or ""
    for secret in (cfg.password.get_secret_value(), cfg.ai_password.get_secret_value()):
        if secret and len(secret) >= 4:
            text = text.replace(secret, MASK)
    return text[:600]


def check(cfg: ClickHouseExternalSettings, storage=None) -> dict[str, Any]:
    """{"ok": bool, "steps": [{"step", "ok", "detail"}]}: `detail` is the server's version,
    or why the step failed."""
    steps: list[dict[str, Any]] = []

    def step(name: str, ok: bool, detail: str = "") -> bool:
        steps.append({"step": name, "ok": ok, "detail": detail})
        return ok

    if not cfg.enabled:
        step("server", False, "No host: set the ClickHouse server's address.")
        return {"ok": False, "steps": steps}
    client = None
    try:
        try:
            client = _client(cfg, cfg.username, cfg.password.get_secret_value())
            version = str(client.command("SELECT version()"))
        except Exception as e:  # noqa: BLE001 — every failure is the test's answer
            step("server", False, _reason(e, cfg))
            return {"ok": False, "steps": steps}
        step("server", True, version)

        database = cfg.database or "default"
        try:
            found = int(client.command("SELECT count() FROM system.databases WHERE name = {db:String}",
                                       parameters={"db": database}))
        except Exception as e:  # noqa: BLE001
            found, reason = -1, _reason(e, cfg)
        if found == 0:
            reason = f"The database «{database}» does not exist on the server: create it, or set another one."
        if found != 1:
            step("database", False, reason)
            return {"ok": False, "steps": steps}
        step("database", True, database)

        ok = True
        if cfg.ai_username and cfg.ai_password.get_secret_value():
            try:
                ai = _client(cfg, cfg.ai_username, cfg.ai_password.get_secret_value())
                try:
                    ai.command("SELECT 1")
                finally:
                    ai.close()
                step("ai_user", True, cfg.ai_username)
            except Exception as e:  # noqa: BLE001
                ok = step("ai_user", False, _reason(e, cfg))

        if cfg.transport == "s3":
            ok = _storage(cfg, client, storage, step) and ok
        else:
            table = f"{_qi(database)}.{_qi('_tabularia_check_' + uuid.uuid4().hex[:12])}"
            try:
                client.command(f"CREATE TABLE {table} (x UInt8) ENGINE = Memory")
                client.command(f"DROP TABLE IF EXISTS {table}")
                step("staging", True, database)
            except Exception as e:  # noqa: BLE001
                ok = step("staging", False, _reason(e, cfg))
        return {"ok": ok, "steps": steps}
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:  # noqa: BLE001 — closing a client that failed half way
                pass


def _storage(cfg: ClickHouseExternalSettings, client, storage, step) -> bool:
    """The server reads a small parquet from the bucket, as it reads the datasets."""
    if storage is None:
        from app.utils import get_storage_service

        storage = get_storage_service()
    bucket = get_settings().storage.bucket
    key = f"{CHECK_PREFIX}{uuid.uuid4().hex}.parquet"
    buf = io.BytesIO()
    pq.write_table(pa.table({"x": [1, 2, 3]}), buf)
    buf.seek(0)
    try:
        storage.upload_fileobj(buf, bucket, key)
    except Exception as e:  # noqa: BLE001 — the engine's own storage: not the server's fault
        logger.warning("ClickHouse check: the test file cannot be written to the bucket: %s", e)
        return step("storage", False, "The engine cannot write the test file to its own bucket: see the engine's log.")
    try:
        rows = int(client.command(f"SELECT count() FROM {s3_table_function(cfg, DataSource(bucket=bucket, key=key))}"))
        if rows != 3:
            return step("storage", False, f"The server read {rows} rows of the 3 written.")
        return step("storage", True, (cfg.s3_endpoint or get_settings().storage.endpoint).rstrip("/"))
    except Exception as e:  # noqa: BLE001
        return step("storage", False, _reason(e, cfg))
    finally:
        try:
            storage.delete_object(bucket, key)
        except Exception as e:  # noqa: BLE001 — a few hundred bytes left: said in the log
            logger.warning("ClickHouse check: the test file %s was not removed: %s", key, e)
