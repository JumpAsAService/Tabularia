"""The second pass at the map's size (docs/specs/bayes-diagnostics.md, «Verification», lot X5): 20,000 quantities × 4 chains × 1,000
draws = 4,000 draws a quantity, 80 million values, the draws parquet about 1.5 GB on disk, measured with the limits' defaults (the Job's
`memory_bytes` 2 GiB: `pass_bytes` a quarter, `compute_bytes` an eighth). The numbers are in the spec's «As built».

A heavy job for a small machine, run by hand, ALONE, and skipped unless BAYES_DIAG_MEASURE is set:

    BAYES_DIAG_MEASURE=1 [BAYES_DIAG_MEASURE_DIR=<a folder with 2 GB free>] python -I -B \\
        gateway-go/internal/modelgroups/groups/bayes/reference/runner_tests/test_diag_measure.py -v

The measure is a child process (this file with --child), started with MALLOC_MMAP_THRESHOLD_ and MALLOC_TRIM_THRESHOLD_ fixed at 256 KiB
(the second pass's memory test explains why) and the Job's own allocator choice (`bayes_follow.use_system_allocator`). The child writes
the fake draws file first (fake_draws.py, the matrix never kept), resets its peak («5» to /proc/self/clear_refs), runs the pass with a
results writer and a summary file, and reports the time of the pass, the peak resident memory above the reset and the absolute one, the
files' sizes and the results' rows. The files are in a temporary folder removed when the measure ends.
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
QUANTITIES, CHAINS, DRAWS = 20_000, 4, 1_000
# What the pass may hold: the Job's memory (2 GiB by default), its two budgets, and the interpreter's own room.
MEMORY_BYTES = 2 * 1024 ** 3
SLACK_MB = 256.0


def _status(field: str) -> float:
    with open("/proc/self/status") as f:
        for line in f:
            if line.startswith(field + ":"):
                return int(line.split()[1]) / 1024.0
    raise RuntimeError(field)


def child(case: dict) -> dict:
    import time

    import numpy as np  # noqa: F401
    import pyarrow.parquet  # noqa: F401

    import bayes_diag  # noqa: F401
    import bayes_follow
    import bayes_pass2
    import bayes_rows
    import bayes_task
    import fake_draws

    bayes_follow.use_system_allocator()
    root = Path(case["dir"])
    layout = fake_draws.make_layout(case["quantities"])
    draws_path = root / "draws.parquet"
    limits = dict(bayes_task.DEFAULT_LIMITS)
    block_draws = max(1, limits["block_values"] // len(layout.draws))
    t_write = time.monotonic()
    fake_draws.write(draws_path, layout, case["chains"], case["draws"], block_draws, divergent_every=11, keep=False)
    seconds_write = time.monotonic() - t_write
    with open("/proc/self/clear_refs", "w") as f:
        f.write("5")
    start = _status("VmRSS")
    t0 = time.monotonic()
    w = bayes_rows.ResultsWriter(str(root / "results.parquet"))
    w.text("model", "model", "m")
    w.text("model", "variant", "sample")
    w.number("model", "n", 1)
    report = bayes_pass2.second_pass(str(draws_path), layout, limits, w, summary_path=str(root / "summary.parquet"),
                                     budget=bayes_pass2.Budget.of(limits), prior=lambda position: None,
                                     prior_variance=lambda position: 4.0)
    w.close()
    return {"quantities": case["quantities"], "chains": case["chains"], "draws": case["draws"],
            "seconds_pass": round(time.monotonic() - t0, 1), "seconds_write_fixture": round(seconds_write, 1),
            "peak_rss_above_start_mb": round(_status("VmHWM") - start, 1), "start_rss_mb": round(start, 1),
            "peak_rss_absolute_mb": round(_status("VmHWM"), 1), "passes": report.passes, "sub_blocks": report.sub_blocks,
            "results_rows": w.total, "summary_rows": report.summary_rows,
            "draws_file_mb": round(draws_path.stat().st_size / MB, 1), "summary_file_mb": round((root / "summary.parquet").stat().st_size / MB, 1),
            "results_file_mb": round((root / "results.parquet").stat().st_size / MB, 1),
            "seconds_read": round(report.seconds_read, 1), "seconds_compute": round(report.seconds_compute, 1)}


def measure(case: dict) -> dict:
    with tempfile.TemporaryDirectory(prefix="bayes-diag-measure-", dir=os.environ.get("BAYES_DIAG_MEASURE_DIR")) as d:
        done = subprocess.run([sys.executable, "-I", "-B", str(Path(__file__).resolve()), "--child", json.dumps({**case, "dir": d})],
                              capture_output=True, text=True, timeout=6 * 3600,
                              env={**os.environ, "MALLOC_MMAP_THRESHOLD_": "262144", "MALLOC_TRIM_THRESHOLD_": "262144"})
        if done.returncode != 0:
            raise AssertionError(f"the measure failed:\n{done.stderr[-4000:]}")
        return json.loads(done.stdout.strip().splitlines()[-1])


@unittest.skipUnless(os.environ.get("BAYES_DIAG_MEASURE"), "a heavy test: BAYES_DIAG_MEASURE=1 runs it, alone, by hand")
class TestTheMapsSize(unittest.TestCase):
    def test_the_second_pass_at_20000_quantities_and_4000_draws_stays_in_the_jobs_memory(self):
        r = measure({"quantities": QUANTITIES, "chains": CHAINS, "draws": DRAWS})
        print(json.dumps(r), file=sys.stderr, flush=True)
        self.assertEqual(r["summary_rows"], QUANTITIES)
        # the budgets are a quarter and an eighth of the memory; the rest is the row group, the charts and the interpreter
        limit = (MEMORY_BYTES // 4 + MEMORY_BYTES // 8) / MB + SLACK_MB
        self.assertLessEqual(r["peak_rss_above_start_mb"], limit)
        self.assertLessEqual(r["peak_rss_absolute_mb"], MEMORY_BYTES / MB)


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "--child":
        print(json.dumps(child(json.loads(sys.argv[2]))), flush=True)
        sys.exit(0)
    unittest.main()
