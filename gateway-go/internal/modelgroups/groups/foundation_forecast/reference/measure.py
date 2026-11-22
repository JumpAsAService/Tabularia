"""The runner's time and memory (docs/specs/foundation-forecast.md, «Performance and limits», «As built / F3»): one run of
Forecast (Timestral) on made-up daily series, inside the group's image, with the runner's own code mounted as a model Job
has it. It prints one line «MEASURE {json}»: the seconds (loading the weights, the forecasts, the whole fit), the batches,
the output's rows and row groups, the process's peak resident memory (VmHWM) and the container's (memory.peak), and fails
(exit 1) when the container's peak passes `peak_mb`. From the repository's root, one container at a time:

  docker run --rm --memory 2500m --memory-swap 2500m --cpus 2 --read-only --tmpfs /tmp --user 10001 -e OMP_NUM_THREADS=2 \\
    -v <a folder made by `go run ./cmd/modelgroup code foundation_forecast <dir>`>:/tabularia/code:ro \\
    -v gateway-go/internal/modelgroups/groups/foundation_forecast/reference:/ref:ro \\
    tabularia-models-foundation_forecast:dev python -I -B /ref/measure.py \\
    size=28m series=1000 points=100 [horizon=28] [holdout=28] [budget=<seconds>] [peak_mb=<MB>]

The series are generated one block at a time into a Parquet file under /tmp (never the whole table in memory beside the
run), then read by the runner as the step's input. With `budget`, the run has that time budget from its start: the
refusal `too_slow`, its sentence and the seconds it took are printed instead.
"""
from __future__ import annotations

import json
import os
import sys
import time

T0 = time.monotonic()
CODE = "/tabularia/code"
sys.path[:0] = [f"{CODE}/common/py", f"{CODE}/runner/py"]


def _status(key: str) -> float:
    for line in open("/proc/self/status"):
        if line.startswith(key + ":"):
            return int(line.split()[1]) / 1024
    return float("nan")


def _cgroup_peak() -> float:
    for f in ("/sys/fs/cgroup/memory.peak", "/sys/fs/cgroup/memory/memory.max_usage_in_bytes"):
        try:
            return int(open(f).read()) / 1048576
        except (OSError, ValueError):
            pass
    return float("nan")


def write_input(path: str, series: int, points: int) -> None:
    import numpy as np
    import pyarrow as pa
    import pyarrow.parquet as pq

    writer = None
    g = np.random.Generator(np.random.PCG64(17))
    step = max(1, 200_000 // points)
    for first in range(0, series, step):
        k = min(step, series - first)
        i = np.tile(np.arange(points), k)
        s = np.repeat(np.arange(first, first + k), points)
        v = 50 + 10 * np.sin(2 * np.pi * i / 7 + s) + 0.01 * i + (s % 17) + g.normal(0, 2, i.size)
        t = pa.table({"serie": pa.array([f"s{x:06d}" for x in s]), "giorno": pa.array((i + 19000).astype(np.int32)).cast(pa.date32()),
                      "valore": pa.array(v)})
        if writer is None:
            writer = pq.ParquetWriter(path, t.schema)
        writer.write_table(t)
    writer.close()


def main(argv: list[str]) -> int:
    a = dict(x.split("=", 1) for x in argv)
    size, series, points = a.get("size", "28m"), int(a.get("series", 1)), int(a.get("points", 1000))
    horizon, holdout = int(a.get("horizon", 28)), int(a.get("holdout", a.get("horizon", 28)))
    write_input("/tmp/input.parquet", series, points)
    import gc

    gc.collect()
    os.environ["TABULARIA_INPUT"] = "/tmp/input.parquet"
    os.environ["TABULARIA_OUTPUT"] = "/tmp/output.parquet"
    import ff_timestral as ft
    import pyarrow.parquet as pq
    from tab_protocol import PROTOCOL, Refusal, catalog, threads

    limits = dict(catalog()["limits"])
    if "budget" in a:
        limits["time_budget_seconds"] = float(a["budget"])
    params = {"time": "giorno", "value": "valore", "frequency": "day", "size": size, "horizon": horizon, "holdout": holdout}
    if series > 1:
        params["series"] = "serie"
    task = {"protocol": PROTOCOL, "mode": "fit", "group": "foundation_forecast", "group_version": "measure", "model": "timestral",
            "variant": "default", "params": params, "output": "quantile_forecast", "write_output": True, "rows_from": "run",
            "limits": limits, "_started": time.monotonic()}
    out = {"size": size, "series": series, "points": points, "horizon": horizon, "holdout": holdout, "threads": threads(task)}
    t1 = time.monotonic()
    try:
        got = ft.fit_timestral(task)
        rows = got["rows"]
        at = {k: i for i, k in enumerate(rows.key)}
        out.update({k: rows.value[at[f"model|||{k}"]] for k in ("seconds_load", "seconds_forecast", "batches", "n_points")})
        f = pq.ParquetFile("/tmp/output.parquet")
        out.update({"output_rows": f.metadata.num_rows, "row_groups": f.num_row_groups, "result_rows": len(rows)})
    except Refusal as e:
        out.update({"refused": e.code, "message": e.message, "output_left": os.path.exists("/tmp/output.parquet")})
    out.update({"seconds_fit": round(time.monotonic() - t1, 3), "seconds_all": round(time.monotonic() - T0, 3),
                "hwm_mb": round(_status("VmHWM"), 1), "cgroup_peak_mb": round(_cgroup_peak(), 1)})
    print("MEASURE " + json.dumps(out), flush=True)
    if "peak_mb" in a and out["cgroup_peak_mb"] > float(a["peak_mb"]):
        print(f"the container's peak {out['cgroup_peak_mb']} MB passes {a['peak_mb']} MB", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
