"""The memory test of the whole sampling Job (docs/specs/bayes-runner.md, «Testing strategy» and criterion 5; lot R6): the peak resident memory
of `bayes_main.run_task` — the data passes, the sampling followed into the draws parquet, the second pass, the checks' and the replicated runs
of `generate_quantities`, the original scale, the results, the card, the copies — does not grow when the draws are multiplied by ten: within
5 MB, the tolerance the contract declares, not a verdict of any report. The prior predictive mode is a case of its own.

A heavy job for a small machine: it is skipped unless BAYES_RUN_MEMORY_TEST is set, and is run alone, by hand, under the heavy-job lock:

    BAYES_RUN_MEMORY_TEST=1     the small profile: 800 groups (a vector quantity of 800 elements and four scalars), 4 chains, 600 then 6,000
                                draws a chain, blocks of 400,000 values (492 draws)
    BAYES_RUN_MEMORY_TEST=spec  the spec's sizes: 2,000 groups (2,000 quantities and more once the prior's draws are counted), 4 chains,
                                1,000 then 10,000 draws a chain, blocks of 1,048,576 values

    BAYES_RUN_MEMORY_TEST=1 uv run --no-project --with pyarrow==26.0.1 --with numpy==2.5.5 \\
      python -I -B gateway-go/internal/modelgroups/groups/bayes/reference/runner_tests/test_run_memory.py -v

Each measure is a child process (this file with --child) on the fake CmdStan (a separate process, whose memory is not counted: «Memory» of
the spec gives CmdStan's own apart): the Job's environment is the Job's (`MALLOC_MMAP_THRESHOLD_` and `MALLOC_TRIM_THRESHOLD_` at 256 KiB,
the system allocator for Arrow, set by the runner itself); the child prepares the task, the fake binary and its input, resets its peak («5»
to /proc/self/clear_refs), runs the whole Job and reports VmHWM minus the resident memory at the reset. Each size is measured twice; the
peak at one time is the LOWER of its two and the peak at ten times the draws must be within 5 MB of it: the C library's allocator adds to a
peak and never takes from it, and two identical runs of the whole Job differ by up to 24 MB at the spec's sizes (84.6 and 108.5 MB at ten
times the draws, measured: the same run twice), so the higher of two would measure the noise. A third run with tracemalloc gives the Python and
numpy allocations, which do not vary between two identical runs (46.18 and 46.25 MB at the spec's sizes) and must be within the 5 MB too.

The Job's `memory_bytes` is lowered (8 MiB in the small profile, 192 MiB at the spec's) so that the second pass's budgets (`pass_bytes` a
quarter, `compute_bytes` an eighth) are reached at BOTH sizes: with the 2 GiB of the real Job the pass would hold the whole of the smaller
run and 512 MB of the larger, and the test would measure the budget, not the growth (the first try, at 64 MiB with 213 columns × 2,000 draws,
measured exactly that: +12 MB, all of it `np.empty((nq, chains, draws))` at its cap). The budget is a fixed number of the Job, never a
function of the draws; that the pass stays inside it is the second pass's own test (test_pass2_memory.py).

The smaller size must itself fill what is filled at the larger: a follower's block (the blocks hold fewer draws than a chain writes), the
second pass's block (above), and the copy's piece (8 MiB: the output is copied in pieces, and a file under 8 MiB is one smaller piece, so the
smaller size's output must be over 8 MiB). A size under any of these measures the size, not the growth (each of the three was seen).

The cases, each at the two sizes:

    sample       the sample mode with the checks on (a thinned `generate_quantities` run), the original scale of a regression's coefficients
                 and of the vector intercept, output `draws` (the whole table copied to the output address)
    replicated   the sample mode with the checks and the replicated output (a second `generate_quantities` run, 5 draws a chain)
    prior        the prior predictive mode (`fixed_param`, the checks' statistics of the prior's data sets, the prior's draws)
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
RUNNER = HERE.parents[1] / "runner"
sys.path[:0] = [str(RUNNER.parent.parent.parent / "common" / "py"), str(RUNNER / "py"), str(HERE)]

TOLERANCE_MB = 5.0
TMP_BYTES = 64 * 1024 ** 3   # the test measures memory, not the Job's disk: the projection must not refuse the larger size
PROFILES = {
    "small": {"groups": 800, "rows": 1600, "chains": 4, "draws": 600, "block_values": 400_000,
              "memory_bytes": 8 * 1024 ** 2},
    "spec": {"groups": 2000, "rows": 6000, "chains": 4, "draws": 1000, "block_values": 1_048_576,
             "memory_bytes": 192 * 1024 ** 2},
}
MALLOC = {"MALLOC_MMAP_THRESHOLD_": "262144", "MALLOC_TRIM_THRESHOLD_": "262144"}   # as bayesJobManifest sets them
CASES = ("sample", "replicated", "prior")


def _status(field: str) -> float:
    """A field of /proc/self/status in MB (VmRSS, VmHWM)."""
    with open("/proc/self/status") as f:
        for line in f:
            if line.startswith(field + ":"):
                return int(line.split()[1]) / 1024.0
    raise RuntimeError(f"no {field} in /proc/self/status")


def columns_of(groups: int, rows: int) -> dict:
    """`rows` rows on `groups` groups, a predictor and an outcome: a random-intercepts regression whose group vector has `groups` elements."""
    import numpy as np

    rng = np.random.default_rng(11)
    g = np.arange(rows) % groups
    x = rng.normal(50.0, 10.0, rows)
    return {"g": [f"s{i + 1}" for i in g], "x": x.tolist(), "y": (1.0 + 0.1 * x + rng.normal(size=rows)).tolist()}


def task_of(case: dict) -> dict:
    """The task of a case: hier_intercepts' golden program with the extension, the sampler's sizes, the checks, the original scale."""
    import test_prior
    import test_run

    if case["kind"] == "prior":
        raw = test_prior.prior_task("hier_intercepts")
        raw["bayes"]["sampler"].update({"chains": case["chains"], "warmup": 0, "draws": case["draws"]})
    else:
        raw = test_run.golden_task("hier_intercepts")
        raw["bayes"]["sampler"].update({"chains": case["chains"], "warmup": 20, "draws": case["draws"], "refresh": 100})
        raw["bayes"]["checks"]["on"] = True
        raw["bayes"]["scale_pairs"] = [{"coef": "b_x", "data": "x"},
                                       {"intercept": "a_g", "lp": ["b_x"], "data": ["x"]}]
        if case["kind"] == "replicated":
            raw["output"] = raw["params"]["output"] = "replicated"
            raw["bayes"]["replicated"] = {"draws_per_chain": 5}
        else:
            raw["output"] = raw["params"]["output"] = "draws"
    raw["limits"].update({"memory_bytes": case["memory_bytes"], "tmp_bytes": TMP_BYTES, "block_values": case["block_values"]})
    return raw


def child(case: dict) -> dict:
    """One measure: the whole Job on the fake CmdStan; returns the peak above the start, in MB."""
    import tracemalloc
    from unittest import mock

    import numpy as np  # noqa: F401 — imported before the reset, as the runner has it
    import pyarrow  # noqa: F401
    import pyarrow.parquet as pq

    import bayes_main
    import bayes_task
    import test_gq
    import test_prior

    root = Path(case["dir"])
    columns = columns_of(case["groups"], case["rows"])
    raw = task_of(case)
    if case["kind"] == "prior":
        h = test_prior.PriorCase()
        h.setUp()
        header = h.program_header(raw, columns)
        spec = {"header": header, "fixed_param": True, "warmup": 0, "means": [0.0] * len(header)}
    else:
        h = test_gq.GqCase()
        h.setUp()
        gq, _runs = h.gq_spec(raw, columns)
        header = h.program_header(raw, columns)
        spec = {"header": header, "means": [0.0] * len(header), **gq}
    spec.update({"tick": 0, "max_piece": 1 << 20, "pool": 97})
    binary, digest = h.binary(spec)
    raw["bayes"]["model"]["binary_sha256"] = digest
    task = bayes_task.parse_task(raw)
    out = h.out
    addresses = {"TABULARIA_INPUT": h.write_input(columns), "TABULARIA_BINARY_IN": str(binary),
                 "TABULARIA_RESULTS": str(out / "results" / "results.parquet"),
                 "TABULARIA_OUTPUT": str(out / "output" / "output.parquet"), "TABULARIA_ARTIFACTS": str(out / "artifacts"),
                 "OMP_NUM_THREADS": "2"}
    quantities = len(header)
    if case["tracemalloc"]:
        tracemalloc.start()
    with open("/proc/self/clear_refs", "w") as f:
        f.write("5")   # resets VmHWM to the current resident memory
    start = _status("VmRSS")
    t0 = time.monotonic()
    with mock.patch.dict(os.environ, addresses):
        code = bayes_main.run_task(task, HERE, h.work)
    seconds = time.monotonic() - t0
    peak = _status("VmHWM") - start
    traced = tracemalloc.get_traced_memory()[1] / 2 ** 20 if case["tracemalloc"] else None
    if code != 0:
        raise AssertionError(f"the Job exited {code}")
    sizes = {p.name: p.stat().st_size for p in out.rglob("*.parquet")}
    results = pq.read_table(out / "results" / "results.parquet").num_rows
    h.tearDown()
    return {"kind": case["kind"], "draws": case["draws"], "peak_rss_mb": round(peak, 2),
            "peak_traced_mb": round(traced, 2) if traced is not None else None, "seconds": round(seconds, 2),
            "columns": quantities, "results_rows": results, "output_bytes": sizes.get("output.parquet"), "start_rss_mb": round(start, 2)}


def measure(case: dict) -> dict:
    """Runs one measure in a child process, in a fresh temporary folder, with the Job's allocator variables."""
    with tempfile.TemporaryDirectory(prefix="bayes-run-memory-") as d:
        payload = json.dumps({**case, "dir": d})
        env = {**{k: v for k, v in os.environ.items() if k not in MALLOC}, **MALLOC, "TMPDIR": d}
        done = subprocess.run([sys.executable, "-I", "-B", str(Path(__file__).resolve()), "--child", payload], capture_output=True,
                              text=True, timeout=7200, env=env)
        if done.returncode != 0:
            raise AssertionError(f"the measure failed:\n{done.stderr[-4000:]}")
        return json.loads(done.stdout.strip().splitlines()[-1])


@unittest.skipUnless(os.environ.get("BAYES_RUN_MEMORY_TEST"), "a heavy test: BAYES_RUN_MEMORY_TEST=1 (or spec) runs it, alone")
class TestTheWholeJobsMemory(unittest.TestCase):
    profile = PROFILES["spec" if os.environ.get("BAYES_RUN_MEMORY_TEST") == "spec" else "small"]

    def _flat(self, kind: str):
        p = self.profile
        results = {}
        for scale in (1, 10):
            case = {"kind": kind, "groups": p["groups"], "rows": p["rows"], "chains": p["chains"], "draws": p["draws"] * scale,
                    "block_values": p["block_values"], "memory_bytes": p["memory_bytes"], "tracemalloc": False}
            runs = [measure(case) for _ in range(2)]
            traced = measure({**case, "tracemalloc": True})
            for r in runs + [traced]:
                print(json.dumps(r), file=sys.stderr, flush=True)
            results[scale] = (runs, traced)
        rss_1 = min(r["peak_rss_mb"] for r in results[1][0])   # «the peak at one time»: the lowest of its runs (the noise only adds)
        rss_10 = min(r["peak_rss_mb"] for r in results[10][0])
        highest = [max(r["peak_rss_mb"] for r in results[k][0]) for k in (1, 10)]
        traced_1, traced_10 = results[1][1]["peak_traced_mb"], results[10][1]["peak_traced_mb"]
        print(f"{kind}: peak RSS above the start {rss_1:.1f} MB at {p['draws']} draws a chain, {rss_10:.1f} MB at {10 * p['draws']}; "
              f"(the highest of the two runs: {highest[0]:.1f} and {highest[1]:.1f} MB); tracemalloc {traced_1:.1f} MB, {traced_10:.1f} MB; "
              f"{results[10][0][0]['columns']} columns, "
              f"{results[10][0][0]['seconds']:.0f} s at ten times", file=sys.stderr, flush=True)
        self.assertLessEqual(rss_10 - rss_1, TOLERANCE_MB)
        self.assertLessEqual(traced_10 - traced_1, TOLERANCE_MB)

    def test_the_sample_mode_with_the_checks_and_the_original_scale(self):
        self._flat("sample")

    def test_the_sample_mode_with_the_replicated_output(self):
        self._flat("replicated")

    def test_the_prior_predictive_mode(self):
        self._flat("prior")


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "--child":
        print(json.dumps(child(json.loads(sys.argv[2]))), flush=True)
        sys.exit(0)
    unittest.main()
