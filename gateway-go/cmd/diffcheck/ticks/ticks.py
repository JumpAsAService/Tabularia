"""The Python side of the comparison «a un giro» (scripts/diffcheck.sh): ONE round of a
background loop of the Python gateway, at a fixed moment, on stack A's database and engine.
The Go side is cmd/jobsonce, which knows the same rounds by the same names.

    POST /tick {"tick": "<name>", "now": "2026-03-29T00:59:00Z"}
         → 200 {"result": …}   or   500 {"error": "<exception>"}

The clock: every module of the gateway (`app.*`) sees `datetime.now`, `datetime.utcnow`,
`date.today` and `time.time` at `now` while a round runs (monotonic time and sleeps stay
real). The panel's settings are read again before every round (overrides.reload), as the Go
side does.

The rounds are declared by the files next to this one, `ticks_<loop>.py`, each with a dict
`TICKS = {"<name>": async function}` — the same names as the Go Job's Ticks.

Run inside the gateway image, with the gateway's sources at /app/app and this folder at
/ticks:  python /ticks/ticks.py
"""


import asyncio
import datetime as _dt
import glob
import importlib
import importlib.util
import os
import pkgutil
import sys
import time as _time
import traceback
import types

sys.path.insert(0, "/app")

FROZEN = None  # an aware datetime in UTC, while a round runs


class _Like(type):
    """isinstance(x, datetime) stays true for the real datetimes the code gets elsewhere."""

    def __instancecheck__(cls, obj):
        return isinstance(obj, cls.__real__)

    def __subclasscheck__(cls, sub):
        return issubclass(sub, cls.__real__)


class FrozenDatetime(_dt.datetime, metaclass=_Like):
    __real__ = _dt.datetime

    @classmethod
    def now(cls, tz=None):
        if FROZEN is None:
            return _dt.datetime.now(tz)
        if tz is None:
            return FROZEN.astimezone().replace(tzinfo=None)
        return FROZEN.astimezone(tz)

    @classmethod
    def utcnow(cls):
        if FROZEN is None:
            return _dt.datetime.utcnow()
        return FROZEN.replace(tzinfo=None)

    @classmethod
    def today(cls):
        return cls.now()


class FrozenDate(_dt.date, metaclass=_Like):
    __real__ = _dt.date

    @classmethod
    def today(cls):
        if FROZEN is None:
            return _dt.date.today()
        return FROZEN.astimezone().date()


class _TimeShim(types.ModuleType):
    def __getattr__(self, name):
        return getattr(_time, name)

    def time(self):  # noqa: D102
        return FROZEN.timestamp() if FROZEN is not None else _time.time()


class _DatetimeModuleShim(types.ModuleType):
    datetime = FrozenDatetime
    date = FrozenDate

    def __getattr__(self, name):
        return getattr(_dt, name)


_time_shim = _TimeShim("time")
_dt_shim = _DatetimeModuleShim("datetime")


def _import_everything() -> None:
    import app

    for pkg in ("app.core", "app.db", "app.models", "app.services", "app.routes", "app.deps", "app.schemas"):
        try:
            mod = importlib.import_module(pkg)
        except Exception:  # noqa: BLE001
            continue
        for info in pkgutil.walk_packages(mod.__path__, pkg + "."):
            try:
                importlib.import_module(info.name)
            except Exception:  # noqa: BLE001 — a module this image cannot import: not ours to run
                pass
    importlib.import_module("app.main")
    del app


def _patch_clock() -> None:
    for name, mod in list(sys.modules.items()):
        if not (name == "app" or name.startswith("app.")) or mod is None:
            continue
        d = mod.__dict__
        if d.get("datetime") is _dt.datetime:
            d["datetime"] = FrozenDatetime
        elif d.get("datetime") is _dt:
            d["datetime"] = _dt_shim
        if d.get("date") is _dt.date:
            d["date"] = FrozenDate
        if d.get("time") is _time:
            d["time"] = _time_shim


def _load_ticks() -> dict:
    ticks: dict = {}
    here = os.path.dirname(os.path.abspath(__file__))
    for path in sorted(glob.glob(os.path.join(here, "ticks_*.py"))):
        name = os.path.splitext(os.path.basename(path))[0]
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        for k, fn in getattr(module, "TICKS", {}).items():
            if k in ticks:
                raise SystemExit(f"round {k!r} declared twice")
            ticks[k] = fn
    return ticks


def _jsonable(v):
    from fastapi.encoders import jsonable_encoder

    return jsonable_encoder(v)


def main() -> None:
    import logging

    logging.basicConfig(level=logging.INFO)
    _import_everything()
    _patch_clock()
    if os.environ.get("GATEWAY_GO__INSTANCE"):
        # the orchestrator's signature, the same on both sides (cmd/jobsonce reads the variable)
        from app.services import orchestrator

        orchestrator.INSTANCE = os.environ["GATEWAY_GO__INSTANCE"]
    ticks = _load_ticks()

    import uvicorn
    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse

    from app.core import overrides
    from app.db.session import engine

    api = FastAPI()
    lock = asyncio.Lock()
    original = dict(os.environ)
    in_force: dict = {}

    def environment(overlay: dict) -> None:
        """A round's own variables on top of the container's (an installation where a feature
        is on), kept until a round asks for others: as the Go side rebuilds its jobs."""
        nonlocal in_force
        if overlay == in_force:
            return
        os.environ.clear()
        os.environ.update(original)
        os.environ.update(overlay)
        in_force = dict(overlay)
        from app.core.config import settings_from_env

        settings_from_env.cache_clear()

    @api.post("/tick")
    async def tick(request: Request):
        global FROZEN
        body = await request.json()
        fn = ticks.get(body.get("tick"))
        if fn is None:
            return JSONResponse(status_code=404, content={"error": f"no round {body.get('tick')!r}"})
        async with lock:
            now = body.get("now")
            FROZEN = _dt.datetime.fromisoformat(now.replace("Z", "+00:00")).astimezone(_dt.timezone.utc) if now else None
            try:
                environment(body.get("env") or {})
                await asyncio.to_thread(overrides.reload, engine)
                result = await fn()
            except Exception as e:  # noqa: BLE001 — the round's own failure, compared as one
                traceback.print_exc()
                return JSONResponse(status_code=500, content={"error": f"{type(e).__name__}: {e}"})
            finally:
                FROZEN = None
        return {"result": _jsonable(result)}

    @api.get("/health")
    async def health():
        return {"status": "ok"}

    uvicorn.run(api, host="0.0.0.0", port=8000, log_level="warning")


if __name__ == "__main__":
    main()
