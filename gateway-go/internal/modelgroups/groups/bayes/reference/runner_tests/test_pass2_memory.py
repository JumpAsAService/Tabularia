"""The memory of the second pass (docs/specs/bayes-diagnostics.md, criterion 3; lot X3): at FIXED budgets (`pass_bytes`, `compute_bytes`)
the peak resident memory of the pass does not grow when the quantities, or the draws, are multiplied by ten (within 5 MB, a declared
tolerance of the test) and stays within the budgets plus a row group, the charts' thinned draws and the interpreter's own slack.

A heavy job for a small machine: skipped unless BAYES_MEMORY_TEST is set; run alone, by hand:

    BAYES_MEMORY_TEST=1 python -I -B gateway-go/internal/modelgroups/groups/bayes/reference/runner_tests/test_pass2_memory.py -v

The child runs with MALLOC_MMAP_THRESHOLD_ and MALLOC_TRIM_THRESHOLD_ fixed at 256 KiB: the C library's dynamic threshold made the
SAME run's peak vary by 5 to 8 MB (15 to 23 MB, measured), more than the tolerance; with fixed thresholds the large arrays are mapped and
returned at once. The results writer's set of keys grows with the rows (at most `max_result_rows`: about 5 MB at 42,000 rows, seen as
the tracemalloc difference of the second size), which is why the tolerance is 5 MB and not less.

Each measure is a child process (this file with --child): it writes the fake draws file first (fake_draws.py), then resets its peak
(«5» to /proc/self/clear_refs), runs the pass with a results writer and a summary file, and reports VmHWM minus the resident memory
at the reset. Three sizes, each measured twice, the peak taken as the higher of the two: 200 quantities × 4 × 500 draws (S = 2,000),
2,000 quantities × 4 × 500 (×10 quantities), 200 quantities × 4 × 5,000 draws (S = 20,000: ×10 draws).
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
BUDGET = {"pass_bytes": 8 * MB, "compute_bytes": 4 * MB, "block_values": 262_144}
CHAINS = 4
SIZES = {"base": (600, 500), "quantities": (6000, 500), "draws": (600, 5000)}
TOLERANCE_MB = 5.0
SLACK_MB = 40.0   # the interpreter's own: the charts' arrays, the results writer's rows, the allocator


def _status(field: str) -> float:
    with open("/proc/self/status") as f:
        for line in f:
            if line.startswith(field + ":"):
                return int(line.split()[1]) / 1024.0
    raise RuntimeError(field)


def child(case: dict) -> dict:
    import time
    import tracemalloc

    import numpy as np  # noqa: F401
    import pyarrow.parquet  # noqa: F401

    import bayes_diag  # noqa: F401
    import bayes_follow
    import bayes_pass2
    import bayes_rows
    import fake_draws

    bayes_follow.use_system_allocator()
    root = Path(case["dir"])
    layout = fake_draws.make_layout(case["quantities"])
    draws_path = root / "draws.parquet"
    block_draws = max(1, BUDGET["block_values"] // len(layout.draws))
    fake_draws.write(draws_path, layout, CHAINS, case["draws"], block_draws, divergent_every=11)
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
    report = bayes_pass2.second_pass(str(draws_path), layout, {}, w, summary_path=str(root / "summary.parquet"),
                                     budget=bayes_pass2.Budget(**BUDGET), prior=lambda position: None)
    w.close()
    return {"quantities": case["quantities"], "draws": case["draws"], "peak_rss_mb": round(_status("VmHWM") - start, 2),
            "peak_traced_mb": round(tracemalloc.get_traced_memory()[1] / MB, 2) if case["tracemalloc"] else None,
            "seconds": round(time.monotonic() - t0, 2), "passes": report.passes, "sub_blocks": report.sub_blocks,
            "rows": w.total, "file_mb": round(draws_path.stat().st_size / MB, 1), "start_rss_mb": round(start, 1)}


def measure(case: dict) -> dict:
    case = {"tracemalloc": False, **case}
    with tempfile.TemporaryDirectory(prefix="bayes-pass2-memory-") as d:
        done = subprocess.run([sys.executable, "-I", "-B", str(Path(__file__).resolve()), "--child", json.dumps({**case, "dir": d})],
                              capture_output=True, text=True, timeout=3600,
                              env={**os.environ, "MALLOC_MMAP_THRESHOLD_": "262144", "MALLOC_TRIM_THRESHOLD_": "262144"})
        if done.returncode != 0:
            raise AssertionError(f"the measure failed:\n{done.stderr[-4000:]}")
        return json.loads(done.stdout.strip().splitlines()[-1])


@unittest.skipUnless(os.environ.get("BAYES_MEMORY_TEST"), "a heavy test: BAYES_MEMORY_TEST=1 runs it, alone")
class TestSecondPassMemory(unittest.TestCase):
    def test_the_peak_stays_flat_with_ten_times_the_quantities_or_the_draws(self):
        peaks, traces = {}, {}
        for name, (quantities, draws) in SIZES.items():
            runs = [measure({"quantities": quantities, "draws": draws}) for _ in range(2)]
            traced = measure({"quantities": quantities, "draws": draws, "tracemalloc": True})
            for r in runs + [traced]:
                print(json.dumps(r), file=sys.stderr, flush=True)
            peaks[name] = max(r["peak_rss_mb"] for r in runs)
            traces[name] = traced["peak_traced_mb"]
        print(f"peak above the start: {peaks}; tracemalloc: {traces}", file=sys.stderr, flush=True)
        for name in ("quantities", "draws"):
            self.assertLessEqual(peaks[name] - peaks["base"], TOLERANCE_MB, name)
            self.assertLessEqual(traces[name] - traces["base"], TOLERANCE_MB, name)
        limit = (BUDGET["pass_bytes"] + BUDGET["compute_bytes"] + 8 * MB) / MB + SLACK_MB
        for name, peak in peaks.items():
            self.assertLessEqual(peak, limit, name)


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "--child":
        print(json.dumps(child(json.loads(sys.argv[2]))), flush=True)
        sys.exit(0)
    unittest.main()
