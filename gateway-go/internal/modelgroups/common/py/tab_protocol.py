"""The runner protocol (docs/specs/statistical-models.md, «The runner protocol» and «The runner
in Python»), shared by every Python runner; the twin of common/R/protocol.R, the same files, codes
and sentences. A model Job runs `python -I -B /tabularia/code/runner/run.py` in /tabularia/code,
which holds:

  runner/run.py, runner/py/*.py   the group's runner (the dispatcher and its modules)
  common/py/tab_*.py              this library (flat modules: //go:embed leaves out a file whose
                                  name starts with «_», so there is no __init__.py)
  catalog.json                    the catalogue (the gateway's copy)
  card.schema.json                the model card's schema
  task.json                       what to do (below)
  reference/                      the group's reference cases (self-test only)

task.json: {"protocol": 1, "mode": "fit" | "predict" | "selftest", "group", "group_version",
  "model", "variant", "params" (checked by the gateway, every key that applies), "output"
  ("results" | "rows"), "write_output" (the node has nodes after it), "rows_from" ("trial" |
  "run"), "mlflow" (write MLmodel and the model file), "limits" (the catalogue's,
  MODELS__MAX_CHART_POINTS applied, time_budget_seconds the step's timeout), "input_rows",
  "ids" {flow_id, node_id, run_id, step_run_id}, "recipe_hash", "runner_hash"}.

Environment: TABULARIA_INPUT (the input, Parquet), TABULARIA_OUTPUT (rows, Parquet),
TABULARIA_RESULTS (the ten columns, Parquet), TABULARIA_ARTIFACTS (a prefix: model.json, the
model file, MLmodel, run.json, error.json), TABULARIA_MODEL_IN (Predict: the card),
TABULARIA_MODEL_FILE_IN (Predict: the model file), all s3:// addresses on a cluster (pyarrow
reads the AWS_* variables), paths on a laptop.

Exit codes: 0 done; 3 refused (artifacts/error.json says why, and the log's line
«TABULARIA_ERROR {json}» says the same when the bucket cannot be written); 4 a self-test that
failed; anything else is a crash of the runner (a bug), the log's tail says what.

No user text ever becomes code here: a column's name is data, renamed to v1, v2… before a
library sees it (tab_columns), and mapped back when results are written.
"""
from __future__ import annotations

import json
import math
import os
import re
import sys
from functools import lru_cache
from pathlib import Path
from typing import Any

PROTOCOL = 1
EXIT_OK = 0
EXIT_REFUSED = 3
EXIT_SELFTEST_FAILED = 4
MODES = ("fit", "predict", "selftest")

ENV_INPUT = "TABULARIA_INPUT"
ENV_OUTPUT = "TABULARIA_OUTPUT"
ENV_RESULTS = "TABULARIA_RESULTS"
ENV_ARTIFACTS = "TABULARIA_ARTIFACTS"
ENV_MODEL_IN = "TABULARIA_MODEL_IN"
ENV_MODEL_FILE_IN = "TABULARIA_MODEL_FILE_IN"

# the threads of a self-test, whatever the machine: the reference values are computed with them
SELFTEST_THREADS = 2

_URI = re.compile(r"^[a-z][a-z0-9+.-]*://")


# ── refusals: a sentence for the person, never a crash ────────────────────────
class Refusal(Exception):
    """A refusal (tab_refuse): code (a short id), message (an English sentence that names the
    way out), params (what the sentence names, as data)."""

    def __init__(self, code: str, message: str, params: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.params = params or {}


def not_built(what: str) -> Refusal:
    """The refusal of a piece that is not built yet (raise not_built(…))."""
    return Refusal("not_built", f"{what} is not built yet in this version of Tabularia", {"what": what})


# ── JSON: numbers that read back as the same double ───────────────────────────
def plain(x: Any) -> Any:
    """`x` as JSON holds it: a non-finite number becomes None, numpy's scalars and arrays
    Python's, tuples lists. Python's repr of a float is the shortest text that reads back as the
    same double, so a card's numbers keep their last bits (R's twin needs 17 digits for it)."""
    if isinstance(x, dict):
        return {str(k): plain(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [plain(v) for v in x]
    if isinstance(x, bool) or x is None or isinstance(x, str):
        return x
    if isinstance(x, int):
        return int(x)
    if isinstance(x, float):
        return float(x) if math.isfinite(x) else None
    tolist = getattr(x, "tolist", None)  # numpy scalars and arrays
    if tolist is not None:
        return plain(tolist())
    return x


def to_json(x: Any, pretty: bool = False) -> str:
    """tab_json: compact UTF-8 JSON, never NaN or Infinity."""
    if pretty:
        return json.dumps(plain(x), ensure_ascii=False, allow_nan=False, indent=2)
    return json.dumps(plain(x), ensure_ascii=False, allow_nan=False, separators=(",", ":"))


# ── files: a path on a laptop, an s3:// address on a cluster ───────────────────
def is_uri(path: str) -> bool:
    return bool(_URI.match(path))


def _fs(path: str):
    from pyarrow import fs as pafs

    return pafs.FileSystem.from_uri(path)


def write_bytes(path: str, data: bytes) -> bool:
    """Writes bytes to a path or an address (pyarrow's file systems); nothing for an empty path."""
    if not path:
        return False
    if is_uri(path):
        filesystem, where = _fs(path)
        with filesystem.open_output_stream(where) as stream:
            stream.write(data)
    else:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_bytes(data)
    return True


def write_text(path: str, text: str) -> bool:
    return write_bytes(path, text.encode("utf-8"))


def read_bytes(path: str) -> bytes:
    if is_uri(path):
        filesystem, where = _fs(path)
        with filesystem.open_input_stream(where) as stream:
            return stream.read()
    return Path(path).read_bytes()


def read_text(path: str) -> str:
    return read_bytes(path).decode("utf-8")


def artifact(name: str) -> str:
    """A file under the artifacts' prefix ("" when there is none)."""
    prefix = os.environ.get(ENV_ARTIFACTS, "")
    if not prefix:
        return ""
    return prefix.rstrip("/") + "/" + name


def write_error(e: Refusal) -> dict[str, Any]:
    """error.json and the log's line: what the step fails with."""
    body = {"code": e.code or "refused", "message": e.message, "params": e.params or {}}
    text = to_json(body)
    print("TABULARIA_ERROR " + text, flush=True)
    try:
        write_text(artifact("error.json"), text)
    except Exception as w:  # noqa: BLE001 — the log's line says it anyway
        print(f"error.json could not be written: {w}", file=sys.stderr, flush=True)
    return body


# ── task.json and the catalogue ───────────────────────────────────────────────
def code_dir() -> Path:
    """/tabularia/code: this file is common/py/tab_protocol.py under it."""
    return Path(__file__).resolve().parents[2]


def read_task(path: str | Path) -> dict[str, Any]:
    path = Path(path)
    if not path.exists():
        raise Refusal("no_task", "task.json is missing: the Job was not made by Tabularia")
    task = json.loads(path.read_text(encoding="utf-8"))
    if task.get("protocol") != PROTOCOL:
        raise Refusal("protocol", f"task.json speaks protocol {task.get('protocol', 'none')}, this runner {PROTOCOL}: "
                                  "the image and Tabularia's version differ")
    if task.get("mode") not in MODES:
        raise Refusal("mode", f"unknown mode «{task.get('mode') or ''}»")
    task["params"] = task.get("params") or {}
    task["limits"] = task.get("limits") or dict(catalog()["limits"])
    return task


@lru_cache(maxsize=1)
def catalog() -> dict[str, Any]:
    """The catalogue (catalog.json beside the runner), read once."""
    return json.loads((code_dir() / "catalog.json").read_text(encoding="utf-8"))


def model_spec(model: str) -> dict[str, Any] | None:
    return next((m for m in catalog()["models"] if m["id"] == model), None)


def variant_spec(model: str, variant: str) -> dict[str, Any] | None:
    spec = model_spec(model)
    return next((v for v in (spec or {}).get("variants", []) if v["id"] == variant), None)


def variant_of(model: str, params: dict[str, Any]) -> str | None:
    """The variant a model's parameters make, as the catalogue says (the gateway writes it in
    task.json; this is for a task written by hand)."""
    spec = model_spec(model)
    if spec is None:
        return None
    for v in spec["variants"]:
        if all(params.get(k, spec["defaults"].get(k)) in values for k, values in v["when"].items()):
            return v["id"]
    return None


def limit(task: dict[str, Any], name: str) -> float:
    value = (task.get("limits") or {}).get(name)
    return float(value if value is not None else catalog()["limits"][name])


def rng(task: dict[str, Any]):
    """The run's generator: numpy's PCG64 from the node's seed, one per run, consumed in a
    fixed order (the split, then the SHAP sample, then the charts' samples)."""
    import numpy as np

    return np.random.Generator(np.random.PCG64(int((task.get("params") or {}).get("seed", 1))))


def threads(task: dict[str, Any]) -> int:
    """The threads of a fit: the Job's CPU limit (OMP_NUM_THREADS, as the gateway sets it), one
    without it; a self-test's are SELFTEST_THREADS, so that its numbers do not follow the machine."""
    if task.get("group_version") == "selftest" or task.get("mode") == "selftest":
        return SELFTEST_THREADS
    try:
        return max(1, int(os.environ.get("OMP_NUM_THREADS", "1")))
    except ValueError:
        return 1
