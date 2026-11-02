"""The engine's settings from the panel (see core/panel_settings.py). Only the gateway
calls them, as every route of this API: the gateway decides who may, keeps the history
and the audit, and here they are checked and written for all the engine's processes."""
from typing import Any, Optional

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.core import panel_settings
from app.core.config import settings_from_env

router = APIRouter(prefix="/settings", tags=["settings"])


class Write(BaseModel):
    values: dict[str, Any] = {}
    # the threshold beyond which the gateway calls a run lost, minus the margin: the
    # tasks' hard limit must stay below it
    max_task_time_limit: Optional[int] = None
    # the items the writer just changed: when a constraint breaks, the error names them
    changed: list[str] = []


def _chdb() -> bool:
    import importlib.util

    return importlib.util.find_spec("chdb") is not None


def _catalog(max_task_time_limit: Optional[int] = None) -> dict:
    env = settings_from_env()
    try:
        set_values, discarded = panel_settings.validate(panel_settings.read_raw(), env)
    except Exception:  # noqa: BLE001 — Valkey down: the environment's values are shown
        set_values, discarded = {}, []
    items = []
    for v in panel_settings.ITEMS:
        maximum = v.maximum
        if v.key == "celery.task_time_limit" and max_task_time_limit is not None:
            maximum = min(maximum, max_task_time_limit)
        items.append({
            "key": v.key, "category": v.category, "unit": v.unit, "min": v.minimum, "max": maximum,
            "kind": v.kind, "rollout": list(v.rollout), "choices": list(v.choices), "optional": v.optional,
            "format": v.format,
            "env_var": v.variable,
            # a secret is only «set» or not, the environment's as the panel's
            "env_value": panel_settings.shown(v.key, panel_settings._value(env, v.key)),
            "value": panel_settings.shown(v.key, set_values.get(v.key)) if v.key in set_values else None,
        })
    # ClickHouse is there when a host is set, in the environment or from the panel
    clickhouse = panel_settings.apply_values(env, {k: x for k, x in set_values.items()
                                                    if k in panel_settings.CONNECTION_KEYS})
    return {
        "items": items,
        # the categories of the engines this installation lacks: the panel shows them off
        # (setting them would change nothing)
        "unavailable": [c for c, active in (("clickhouse", clickhouse.clickhouse_external.enabled),
                                            ("bigquery", env.bigquery.enabled),
                                            ("embedded", _chdb()),) if not active],
        "refresh_seconds": panel_settings.REREAD_EVERY,
        "discarded": discarded,
    }


@router.get("")
def read(max_task_time_limit: Optional[int] = None):
    return _catalog(max_task_time_limit)


class Check(BaseModel):
    # the connection's items as the panel holds them (a password as its encrypted token);
    # the missing ones are the ones in force: the saved password stays when none is typed
    values: dict[str, Any] = {}


@router.post("/clickhouse-check")
def clickhouse_check(body: Check):
    """«Test connection»: the external ClickHouse with these values, step by step
    (engine/clickhouse_check.py). Nothing is saved; 422 when a value is malformed."""
    from app.engine.clickhouse_check import check

    asked = body.values
    errors = [{"key": k, "code": "unknown"} for k in asked
              if k not in panel_settings.CONNECTION_KEYS and k != "clickhouse_external.connect_timeout"]
    env = settings_from_env()
    good, wrong = panel_settings.validate(asked, env)
    if errors or wrong:
        return JSONResponse(status_code=422, content={"detail": {"errors": errors + wrong}})
    try:
        saved, _ = panel_settings.validate(panel_settings.read_raw(), env)
    except Exception:  # noqa: BLE001 — Valkey down: the environment's under the ones typed
        saved = {}
    cfg = panel_settings.apply_values(env, {**saved, **good}).clickhouse_external
    return check(cfg)


@router.put("")
def write(body: Write):
    """Replaces ALL the panel values (the missing ones go back to the environment's value).
    Nothing written when even one is wrong: 422 with the list."""
    good, errors = panel_settings.validate(body.values, settings_from_env(), body.max_task_time_limit,
                                      changed=set(body.changed))
    if errors:
        return JSONResponse(status_code=422, content={"detail": {"errors": errors}})
    panel_settings.write(good)
    return _catalog(body.max_task_time_limit)
