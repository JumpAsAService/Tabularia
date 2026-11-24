"""The memory of the predictive checks' second pass (docs/specs/bayes-predictive-checks.md, «The memory»; lot P3b): the peak resident memory of
`plan.rows` does not grow when the draws are multiplied by ten (within 5 MB, a declared tolerance of the test): the pass reads the checks file
one row group at a time and keeps the series' counts and twenty draws' bin counts, never the draws.

A heavy job for a small machine: skipped unless BAYES_MEMORY_TEST is set; run alone, by hand:

    BAYES_MEMORY_TEST=1 python -I -B gateway-go/internal/modelgroups/groups/bayes/reference/runner_tests/test_ppc_memory.py -v

Each measure is a child process (this file with --child), with the C library's thresholds fixed as in test_pass2_memory.py: it writes the
checks file first, resets its peak («5» to /proc/self/clear_refs), runs the pass, and reports VmHWM minus the resident memory at the reset.
Sizes: 4 chains × 500 draws and 4 × 5,000 (×10), each measured twice, the peak taken as the higher.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE.parents[3] / "common" / "py"), str(HERE.parents[1] / "runner" / "py"), str(HERE)]

MB = 1 << 20
CHAINS = 4
SIZES = {"base": 500, "draws": 5000}
BLOCK_VALUES = 8192      # a block of 143 draws: the row group is at its cap at both sizes
TOLERANCE_MB = 5.0


def _status(field: str) -> float:
    with open("/proc/self/status") as f:
        for line in f:
            if line.startswith(field + ":"):
                return int(line.split()[1]) / 1024.0
    raise RuntimeError(field)


def child(case: dict) -> dict:
    import time
    import tracemalloc

    import pyarrow.parquet  # noqa: F401

    import bayes_follow
    import bayes_rows
    import fake_checks

    bayes_follow.use_system_allocator()
    root = Path(case["dir"])
    task, plan, facts, layout = fake_checks.prepared(root, n=60, run="checks")
    block_draws = max(1, BLOCK_VALUES // len(layout.checks))
    path = root / "checks.parquet"
    fake_checks.write(path, plan, layout, "continuous", CHAINS, case["draws"], block_draws, nan_draws=((1, 2), (3, 7)))
    if case["tracemalloc"]:
        tracemalloc.start()
    with open("/proc/self/clear_refs", "w") as f:
        f.write("5")
    start = _status("VmRSS")
    t0 = time.monotonic()
    w = bayes_rows.ResultsWriter(str(root / "results.parquet"))
    w.text("model", "model", "m")
    w.text("model", "variant", "sample")
    w.number("model", "n", 1)
    report = plan.rows(str(path), w, "ppc")
    w.close()
    return {"draws": case["draws"], "peak_rss_mb": round(_status("VmHWM") - start, 2),
            "peak_traced_mb": round(tracemalloc.get_traced_memory()[1] / MB, 2) if case["tracemalloc"] else None,
            "seconds": round(time.monotonic() - t0, 2), "valid": report.valid, "dropped": report.dropped, "rows": w.total,
            "file_mb": round(path.stat().st_size / MB, 1), "start_rss_mb": round(start, 1)}


def measure(case: dict) -> dict:
    case = {"tracemalloc": False, **case}
    with tempfile.TemporaryDirectory(prefix="bayes-ppc-memory-") as d:
        done = subprocess.run([sys.executable, "-I", "-B", str(Path(__file__).resolve()), "--child", json.dumps({**case, "dir": d})],
                              capture_output=True, text=True, timeout=3600,
                              env={**os.environ, "MALLOC_MMAP_THRESHOLD_": "262144", "MALLOC_TRIM_THRESHOLD_": "262144"})
        if done.returncode != 0:
            raise AssertionError(f"the measure failed:\n{done.stderr[-4000:]}")
        return json.loads(done.stdout.strip().splitlines()[-1])


@unittest.skipUnless(os.environ.get("BAYES_MEMORY_TEST"), "a heavy test: BAYES_MEMORY_TEST=1 runs it, alone")
class TestPpcMemory(unittest.TestCase):
    def test_the_peak_stays_flat_with_ten_times_the_draws(self):
        peaks, traces = {}, {}
        for name, draws in SIZES.items():
            runs = [measure({"draws": draws}) for _ in range(2)]
            traced = measure({"draws": draws, "tracemalloc": True})
            for r in runs + [traced]:
                print(json.dumps(r), file=sys.stderr, flush=True)
            peaks[name] = max(r["peak_rss_mb"] for r in runs)
            traces[name] = traced["peak_traced_mb"]
        print(f"peak above the start: {peaks}; tracemalloc: {traces}", file=sys.stderr, flush=True)
        self.assertLessEqual(peaks["draws"] - peaks["base"], TOLERANCE_MB)
        self.assertLessEqual(traces["draws"] - traces["base"], TOLERANCE_MB)


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "--child":
        print(json.dumps(child(json.loads(sys.argv[2]))), flush=True)
        sys.exit(0)
    unittest.main()
