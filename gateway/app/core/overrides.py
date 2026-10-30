"""The gateway's settings changed from Administration → Settings (docs/specs/panel-settings.md).

The environment (`.env`, the Helm values) stays the starting value of every setting; the
administrators override some of them from the panel, without touching the deployment.
Only the ones listed in `ITEMS` can be: what the code reads each time it uses it (applied
within `REREAD_EVERY` seconds by every gateway and orchestrator process), and what a
process reads once when it starts (`rollout`: applied when those components are rolled
out). Addresses, credentials, keys, identities, the audit log's retention and the
switches tied to resources of the chart are not here, on purpose: they belong to the
values of the installation.

Where they are: the table `gateway_settings`, the source (with the audit of every change).
Each process reads it again at most every `REREAD_EVERY` seconds (`overrides_loop`) and
`get_settings()` returns the environment with them on top. A value that does not pass the
setting's own validation, the panel's range or a constraint between two settings is
dropped (the environment's applies) and written in the log.

The engine's settings take another road (services/engine_panel_settings.py): the engine owns
their catalogue and holds them in Valkey for its processes.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import threading
import time
from dataclasses import dataclass
from typing import Any, Optional

logger = logging.getLogger(__name__)

REREAD_EVERY = 10.0  # seconds: after a change, every process sees it within this time
MIN_MEMORY = 64 * 1024 ** 2  # the least memory a build or a Container node may be given from the panel

DAY = 86_400


@dataclass(frozen=True)
class Item:
    key: str  # "<section>.<field>" of the gateway's settings
    group: str  # the box of the panel (the labels are the frontend's)
    kind: str  # int | float | bool | str | timezone | cpu | memory
    unit: str = ""  # seconds | minutes | days | rows | bytes | usd | tokens | messages | requests | chats | runs | threads | connections | people
    minimum: Optional[float] = None
    maximum: Optional[float] = None
    max_length: int = 200
    # a text that may not be empty (the claim with the groups: empty, every sign-in would
    # take every group away)
    required: bool = False
    # the components that read it only when they start: empty = applied live
    rollout: tuple[str, ...] = ()
    # shown only on installations that have it: sso | kubernetes | scripts | containers
    when: str = ""

    @property
    def section(self) -> str:
        return self.key.split(".", 1)[0]

    @property
    def field(self) -> str:
        return self.key.split(".", 1)[1]

    @property
    def env_var(self) -> str:
        return f"{self.section.upper()}__{self.field.upper()}"


GW = ("gateway", "orchestrator")
ITEMS: tuple[Item, ...] = (
    # ── sign-in and sessions ──
    Item("jwt.access_ttl_minutes", "signin", "int", "minutes", 15, 7 * 24 * 60),
    Item("auth.session_idle_minutes", "signin", "int", "minutes", 0, 24 * 60),
    Item("auth.personal_tokens", "signin", "bool"),
    Item("auth.api_token_max_days", "signin", "int", "days", 1, 3650),
    # ── single sign-on ──
    Item("oidc.groups_claim", "sso", "str", max_length=500, required=True, when="sso"),
    Item("oidc.group_allowlist", "sso", "str", max_length=4000, when="sso"),
    Item("oidc.auto_create_groups", "sso", "bool", when="sso"),
    Item("oidc.authoritative", "sso", "bool", when="sso"),
    Item("oidc.allowed_email_domains", "sso", "str", max_length=4000, when="sso"),
    Item("oidc.require_allowlisted_group", "sso", "bool", when="sso"),
    Item("oidc.button_label", "sso", "str", max_length=60, when="sso"),
    Item("oidc.discovery_ttl_seconds", "sso", "int", "seconds", 60, DAY, when="sso"),
    Item("oidc.timeout_seconds", "sso", "float", "seconds", 1, 60, when="sso"),
    Item("oidc.login_tx_ttl_seconds", "sso", "int", "seconds", 120, 3600, when="sso"),
    # ── orchestration and schedules ──
    Item("app.timezone", "orchestration", "timezone"),
    Item("engine.run_stale_timeout_seconds", "orchestration", "int", "seconds", 900, 7 * DAY),
    Item("orchestrator.refresh_wait_seconds", "orchestration", "int", "seconds", 60, DAY),
    Item("orchestrator.poll_interval_seconds", "orchestration", "float", "seconds", 0.5, 60),
    Item("orchestrator.queue_poll_seconds", "orchestration", "float", "seconds", 0.2, 30),
    Item("orchestrator.max_concurrent", "orchestration", "int", "runs", 1, 200),
    Item("orchestrator.queue_timeout_seconds", "orchestration", "float", "seconds", 60, DAY),
    Item("orchestrator.heartbeat_seconds", "orchestration", "float", "seconds", 2, 120),
    Item("orchestrator.dead_after_seconds", "orchestration", "float", "seconds", 15, 3600),
    Item("scheduling.worker_capacity", "orchestration", "int", "runs", 1, 256),
    # ── history kept ──
    Item("orchestrator.log_retention_days", "history", "int", "days", 0, 3650),
    Item("orchestrator.contract_results_kept", "history", "int", "reports", 0, 100_000),
    Item("orchestrator.versions_kept", "history", "int", "versions", 0, 10_000),
    Item("orchestrator.ai_chat_retention_days", "history", "int", "days", 0, 3650),
    Item("webhooks.retention_days", "history", "int", "days", 1, 3650),
    # ── webhooks and SIEM ──
    Item("webhooks.timeout_seconds", "webhooks", "float", "seconds", 1, 60),
    # ── AI assistant ──
    Item("ai.metadata_only", "ai", "bool"),
    Item("ai.default_model", "ai", "str", max_length=200),
    Item("ai.max_result_rows", "ai", "int", "rows", 1, 2000),
    Item("ai.max_requests", "ai", "int", "requests", 1, 50),
    Item("ai.max_flow_requests", "ai", "int", "requests", 1, 80),
    Item("ai.max_history_messages", "ai", "int", "messages", 0, 400),
    Item("ai.max_cost_per_turn_usd", "ai", "float", "usd", 0, 100),
    Item("ai.max_cost_per_day_usd", "ai", "float", "usd", 0, 10_000),
    Item("ai.max_cost_per_day_total_usd", "ai", "float", "usd", 0, 100_000),
    Item("ai.max_input_tokens_per_turn", "ai", "int", "tokens", 1000, 2_000_000),
    Item("ai.max_chats_listed", "ai", "int", "chats", 1, 500),
    Item("ai.dbt_export_timeout_seconds", "ai", "float", "seconds", 10, 3600),
    # ── agents over MCP (docs/specs/mcp-agent.md) ──
    Item("mcp.enabled", "mcp", "bool"),
    Item("mcp.confirm_in_tabularia_only", "mcp", "bool"),
    Item("mcp.metadata_only", "mcp", "bool"),
    # ── dbt export, previews, uploads, backups, monitoring, lineage ──
    Item("engine.dbt_export_timeout_seconds", "dbt", "float", "seconds", 10, 3600),
    Item("preview.default_sample_rows", "previews", "int", "rows", 0, 10_000_000),
    # never 0 from here (no limit at all): the engine converts on its /tmp, which has one
    Item("upload.max_bytes", "previews", "int", "bytes", 1024 ** 2, 1024 ** 4),
    Item("backup.timeout_seconds", "backups", "int", "seconds", 60, DAY),
    Item("monitoring.timeout_seconds", "monitoring", "float", "seconds", 0.5, 30),
    Item("monitoring.cache_seconds", "monitoring", "float", "seconds", 0, 300),
    Item("openlineage.timeout_seconds", "openlineage", "float", "seconds", 1, 120),
    # ── read when the gateway and the orchestrators start ──
    Item("app.gateway_threads", "processes", "int", "threads", 4, 200, rollout=GW),
    Item("db.pool_size", "processes", "int", "connections", 1, 200, rollout=GW),
    Item("db.max_overflow", "processes", "int", "connections", 0, 200, rollout=GW),
    # ── Kubernetes ──
    Item("kubernetes.timeout_seconds", "kubernetes", "float", "seconds", 1, 120, when="kubernetes"),
    Item("scripts.poll_seconds", "scripts", "float", "seconds", 1, 60, when="scripts"),
    Item("scripts.job_ttl_seconds", "scripts", "int", "seconds", 60, 7 * DAY, when="scripts"),
    Item("scripts.log_max_bytes", "scripts", "int", "bytes", 4096, 16 * 1024 ** 2, when="scripts"),
    Item("scripts.lint", "scripts", "bool", when="scripts"),
    Item("scripts.build_timeout_seconds", "builds", "int", "seconds", 60, 6 * 3600, when="scripts"),
    Item("scripts.build_cpu", "builds", "cpu", when="scripts"),
    Item("scripts.build_memory", "builds", "memory", when="scripts"),
    Item("scripts.check_timeout_seconds", "builds", "int", "seconds", 30, 3600, when="scripts"),
    Item("notebooks.max_visitors_per_app", "notebooks", "int", "people", 1, 1000, when="scripts"),
    Item("notebooks.max_visitors_ceiling", "notebooks", "int", "people", 1, 1000, when="scripts"),
    Item("containers.pull_check_seconds", "containers", "int", "seconds", 30, 3600, when="containers"),
    Item("containers.poll_seconds", "containers", "float", "seconds", 1, 60, when="containers"),
    Item("containers.job_ttl_seconds", "containers", "int", "seconds", 60, 7 * DAY, when="containers"),
    Item("containers.max_cpu", "containers", "cpu", when="containers"),
    Item("containers.max_memory", "containers", "memory", when="containers"),
    Item("containers.max_timeout_seconds", "containers", "int", "seconds", 60, 30 * DAY, when="containers"),
    Item("containers.log_max_bytes", "containers", "int", "bytes", 4096, 16 * 1024 ** 2, when="containers"),
)
PER_KEY = {v.key: v for v in ITEMS}


# ── checking one value, and the set ─────────────────────────────────────────

def _section(base: Any, item: Item) -> Any:
    return getattr(base, item.section)


def check_one(item: Item, value: Any, base: Any) -> tuple[Any, Optional[dict]]:
    """(the value as the setting holds it, None) or (None, an error with a code the
    panel translates). The setting's own validation (bounds, validators of the settings
    class) runs on top of the panel's range."""
    k = item.kind
    if k == "bool":
        if not isinstance(value, bool):
            return None, {"key": item.key, "code": "boolean"}
    elif k == "int":
        if isinstance(value, bool) or not isinstance(value, int):
            return None, {"key": item.key, "code": "integer"}
    elif k == "float":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None, {"key": item.key, "code": "number"}
        try:
            value = float(value)
        except OverflowError:  # an integer too large for a decimal
            return None, {"key": item.key, "code": "max", "limit": item.maximum}
        if not math.isfinite(value):  # NaN passes every comparison, Infinity every minimum
            return None, {"key": item.key, "code": "number"}
    else:  # str, timezone, cpu, memory
        if not isinstance(value, str):
            return None, {"key": item.key, "code": "text"}
        value = value.strip()
        if len(value) > item.max_length:
            return None, {"key": item.key, "code": "too_long", "limit": item.max_length}
        if (k in ("timezone", "cpu", "memory") or item.required) and not value:
            return None, {"key": item.key, "code": "invalid"}
        if k in ("cpu", "memory"):
            # the settings classes check the form (2, 500m, 4Gi); here the size: a build or a
            # Container node with no CPU or a few bytes of memory would never run
            from app.services.cluster_view import cpu_millis, memory_bytes

            try:
                size = cpu_millis(value) if k == "cpu" else memory_bytes(value)
            except (TypeError, ValueError):
                size = None
            if size is not None and (size <= 0 if k == "cpu" else size < MIN_MEMORY):
                return None, {"key": item.key, "code": "too_small", "message": "at least 64Mi" if k == "memory" else "more than 0"}
    if item.minimum is not None and value < item.minimum:
        return None, {"key": item.key, "code": "min", "limit": item.minimum}
    if item.maximum is not None and value > item.maximum:
        return None, {"key": item.key, "code": "max", "limit": item.maximum}
    section = _section(base, item)
    try:
        checked = type(section).model_validate({**section.model_dump(), item.field: value})
    except Exception as e:  # noqa: BLE001 — pydantic's ValidationError: the setting's own rule
        errs = e.errors() if hasattr(e, "errors") else []
        msg = errs[0].get("msg", str(e)) if errs else str(e)
        return None, {"key": item.key, "code": "invalid", "message": str(msg)[:300]}
    return getattr(checked, item.field), None


def _effective(base: Any, values: dict[str, Any], key: str) -> Any:
    return values[key] if key in values else effective(base, key)


def _pool(base: Any, values: dict[str, Any]) -> int:
    pool = _effective(base, values, "db.pool_size")
    return pool if pool else _effective(base, values, "app.gateway_threads")


def check(values: Any, base: Any, changed: Optional[set[str]] = None) -> tuple[dict[str, Any], list[dict]]:
    """(good values, errors). `base` is the environment; the constraints between two
    settings are checked on the effective values; when one breaks, the setting just
    changed is the one refused (`changed`), else the panel's. The constraints:
    - a process is declared dead after missing at least two beats (heartbeat × 3 ≤ dead after);
    - each orchestration holds a database connection while it asks the engine, so the
      executions an orchestrator carries stay within its pool (pool + overflow);
    - single sign-on that admits only the members of the groups taken needs a list of
      them (or the administrators' group): an empty one would let nobody in;
    - the notebooks' default of visitors at once stays within the most a notebook may set."""
    good: dict[str, Any] = {}
    errors: list[dict] = []
    if not isinstance(values, dict):
        return {}, [{"key": "", "code": "unknown"}]
    for key, value in values.items():
        item = PER_KEY.get(key)
        if item is None:
            errors.append({"key": key, "code": "unknown"})
            continue
        v, err = check_one(item, value, base)
        if err:
            errors.append(err)
        else:
            good[key] = v
    changed = changed or set()

    def blame(keys: tuple[str, ...]) -> Optional[str]:
        mine = [k for k in keys if k in good]
        if not mine:
            return None
        return next((k for k in mine if k in changed), mine[0])

    moved = True
    while moved:
        moved = False

        def e(k: str) -> Any:
            return _effective(base, good, k)

        if not (e("orchestrator.heartbeat_seconds") * 3 <= e("orchestrator.dead_after_seconds")):
            k = blame(("orchestrator.heartbeat_seconds", "orchestrator.dead_after_seconds"))
            if k:
                good.pop(k)
                errors.append({"key": k, "code": "heartbeat_dead_after"})
                moved = True
                continue
        if e("oidc.require_allowlisted_group") and not str(e("oidc.group_allowlist") or "").strip() \
                and not str(getattr(base.oidc, "superuser_group", "") or "").strip():
            k = blame(("oidc.require_allowlisted_group", "oidc.group_allowlist"))
            if k:
                good.pop(k)
                errors.append({"key": k, "code": "allowlist_needed"})
                moved = True
                continue
        limit = _pool(base, good) + e("db.max_overflow")
        if e("orchestrator.max_concurrent") > limit:
            k = blame(("orchestrator.max_concurrent", "db.pool_size", "app.gateway_threads", "db.max_overflow"))
            if k:
                good.pop(k)
                errors.append({"key": k, "code": "concurrent_pool", "limit": limit})
                moved = True
                continue
        per_app, ceiling = e("notebooks.max_visitors_per_app"), e("notebooks.max_visitors_ceiling")
        if per_app > ceiling:
            k = blame(("notebooks.max_visitors_per_app", "notebooks.max_visitors_ceiling"))
            if k:
                good.pop(k)
                errors.append({"key": k, "other": next(o for o in ("notebooks.max_visitors_per_app",
                                                                   "notebooks.max_visitors_ceiling") if o != k),
                               "code": "visitors_above_ceiling" if k.endswith("per_app") else "ceiling_below_visitors",
                               "limit": ceiling if k.endswith("per_app") else per_app})
                moved = True
                continue
    return good, errors


def apply(base: Any, values: dict[str, Any]) -> Any:
    """`base` with the values on top, section by section (the others stay the same objects)."""
    if not values:
        return base
    by_section: dict[str, dict[str, Any]] = {}
    for key, value in values.items():
        section, field = key.split(".", 1)
        by_section.setdefault(section, {})[field] = value
    return base.model_copy(update={s: getattr(base, s).model_copy(update=f) for s, f in by_section.items()})


# ── the values in force in this process ─────────────────────────────────────

_lock = threading.Lock()
_values: dict[str, Any] = {}
_version = 0
# (version, values) replaced as one object: a reader takes both from the same moment
_state: tuple[int, dict[str, Any]] = (0, {})
_result: tuple[Any, int, Any] | None = None  # (base, version, settings)
_loaded_at: float = float("-inf")
# what this process started with, for the settings read only then: the panel tells
# which wait for a rollout
_at_start: dict[str, Any] = {}


def current() -> dict[str, Any]:
    return dict(_values)


def effective(settings: Any, key: str) -> Any:
    section, field = key.split(".", 1)
    return getattr(getattr(settings, section), field)


def remember_start() -> None:
    from app.core.config import get_settings

    s = get_settings()
    _at_start.update({v.key: effective(s, v.key) for v in ITEMS if v.rollout})


def started_with(key: str) -> Any:
    return _at_start.get(key)


def overlay(base: Any) -> Any:
    """The environment with the panel's values on top: the road of every `get_settings()`.
    A comparison while nothing changes."""
    global _result
    version, values = _state  # one read: a change in between cannot pair old values with a new version
    if not values:
        return base
    r = _result
    if r is not None and r[0] is base and r[1] == version:
        return r[2]
    s = apply(base, values)
    _result = (base, version, s)
    return s


def set_values(values: dict[str, Any]) -> None:
    global _values, _version, _result, _loaded_at, _state
    with _lock:
        _loaded_at = time.monotonic()
        if values != _values:
            _values = dict(values)
            _version += 1
            _state = (_version, _values)
            _result = None
            logger.info("settings from the panel: %s", values or "none (the environment's apply)")


def forget() -> None:
    """Back to the environment's (tests, and `get_settings.cache_clear()`)."""
    global _values, _version, _result, _loaded_at, _state
    with _lock:
        _values, _result, _loaded_at = {}, None, float("-inf")
        _version += 1
        _state = (_version, _values)


def read_rows(bind: Any) -> dict[str, Any]:
    """The saved values, raw. Raises when the table cannot be read (before migration
    0022, or the database down)."""
    from sqlalchemy import text

    try:
        with bind.connect() as conn:
            rows = conn.execute(text("SELECT key, value FROM gateway_settings")).all()
    except Exception as e:  # noqa: BLE001 — the table is not there yet, or the database is down
        logger.debug("settings from the panel not read: %s", e)
        raise
    out = {}
    for key, raw in rows:
        try:
            out[key] = json.loads(raw)
        except ValueError:
            logger.warning("settings from the panel: %s is not JSON, ignored", key)
    return out


def reload(bind: Any) -> dict[str, Any]:
    """Reads the saved values, checks them against the environment and puts the good ones
    in force in this process. Returns them."""
    from app.core.config import settings_from_env

    if not settings_from_env().engine.settings_panel:  # ENGINE__SETTINGS_PANEL=false: the environment only
        set_values({})
        return {}
    try:
        raw = read_rows(bind)
    except Exception:  # noqa: BLE001 — database down: the last values stay
        return current()
    return apply_rows(raw)


def apply_rows(raw: dict[str, Any]) -> dict[str, Any]:
    """Puts in force in this process the saved values just read (raw), the good ones."""
    from app.core.config import settings_from_env

    base = settings_from_env()
    if not base.engine.settings_panel:
        set_values({})
        return {}
    good, errors = check(raw, base)
    if errors:
        logger.warning("settings from the panel dropped, the environment's apply: %s", errors)
    set_values(good)
    return good


def load_at_start(dsn: str) -> None:
    """Before the database pool is made (db/session.py): the values that size it are
    among the panel's. A connection of its own, closed at once; nothing if it fails."""
    try:
        from sqlalchemy import create_engine
        from sqlalchemy.pool import NullPool

        eng = create_engine(dsn, poolclass=NullPool, connect_args={"connect_timeout": 5} if dsn.startswith("postgresql") else {})
        try:
            reload(eng)
        finally:
            eng.dispose()
    except Exception as e:  # noqa: BLE001 — the environment's apply, as before the panel
        logger.debug("settings from the panel not read at start: %s", e)
    remember_start()


def _reload_now() -> None:
    from app.db.session import engine

    reload(engine)


async def overrides_loop(stop: asyncio.Event) -> None:
    """Every process, whatever its role: the routes read the settings as the background
    work does."""
    while not stop.is_set():
        try:
            await asyncio.to_thread(_reload_now)
        except Exception:  # noqa: BLE001 — never stop the gateway for this
            logger.exception("settings from the panel: a reading failed")
        try:
            await asyncio.wait_for(stop.wait(), timeout=REREAD_EVERY)
        except asyncio.TimeoutError:
            pass
