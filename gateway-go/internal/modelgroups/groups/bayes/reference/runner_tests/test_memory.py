"""The memory test of the follower and the draws writer (docs/specs/bayes-contract.md, «The memory test», criterion 3; lot K2): the
peak resident memory of the process that follows CmdStan's CSV files and appends the draws to the parquet does not grow when the
draws are multiplied by ten — within 5 MB, a declared tolerance of the test, not a verdict of any report — for the draws (long) and
for the replicated output.

A heavy job for a small machine: it is skipped unless BAYES_MEMORY_TEST is set, and is run alone, by hand.

    BAYES_MEMORY_TEST=1     the small profile: 200 quantities, 4 chains, 1,000 then 10,000 draws a chain, blocks of 65,536 values
                            (316 draws: 9 then 91 MB of CSV); the replicated output with 1,000 rows, 50 then 500 draws a chain
    BAYES_MEMORY_TEST=spec  the spec's sizes: 2,000 quantities, 4 chains, 1,000 then 10,000 draws a chain, blocks of 1,048,576
                            values (82 MB then 0.82 GB of CSV); the replicated output as above

The replicated output's blocks are lowered to 5,010 values (5 draws of 1,000 rows) in both profiles: at the default 1,048,576 a block
would hold a whole chain of 50 or of 500 draws, and the test would measure a block ten times larger instead of ten times more
blocks. The blocks are not made smaller than that: the parquet writer keeps about 1.5 KB of metadata a column chunk until it
closes, so a run of hundreds of row groups grows by that footer (508 row groups at 16,384 values: about 5 MB), a cost of the row
groups, which the spec's sizes keep under a hundred.

    BAYES_MEMORY_TEST=1 uv run --no-project --with pyarrow==26.0.1 --with numpy==2.5.5 \\
      python -I gateway-go/internal/modelgroups/groups/bayes/reference/runner_tests/test_memory.py -v

Each measure is a child process (this file with --child): it imports what the runner imports, uses the system allocator for Arrow
as a sampling Job does (bayes_follow.use_system_allocator: pyarrow's default, mimalloc, made the same run's peak vary by about 20 MB
at the spec's size), sets up the writer, resets its peak
(«5» to /proc/self/clear_refs), starts fake_cmdstan.py — a separate process, whose memory is not counted — follows its files to the
end, closes the parquet, and reports VmHWM minus the resident memory at the reset (the peak above its start), and the same with
tracemalloc's peak of the Python and numpy allocations in a third run. Each size is measured twice; the peak at one time is the
higher of its two, and each run at ten times the draws must be within 5 MB of it (measured at the spec's size: 105.5 and 107.5 MB
at one time, 109.5 and 110.6 MB at ten; tracemalloc 46.06 and 46.09 MB: the C library's allocator varies by a few MB between two
identical runs, the Python and numpy allocations not at all). The temporary files go to TMPDIR and are deleted.
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
PROFILES = {
    "small": {"quantities": 200, "chains": 4, "draws": 1000, "block_values": 65_536,
              "replicated_rows": 1000, "replicated_draws": 50, "replicated_block_values": 5_010},
    "spec": {"quantities": 2000, "chains": 4, "draws": 1000, "block_values": 1_048_576,
             "replicated_rows": 1000, "replicated_draws": 50, "replicated_block_values": 5_010},
}


def _status(field: str) -> float:
    """A field of /proc/self/status in MB (VmRSS, VmHWM)."""
    with open("/proc/self/status") as f:
        for line in f:
            if line.startswith(field + ":"):
                return int(line.split()[1]) / 1024.0
    raise RuntimeError(f"no {field} in /proc/self/status")


def child(case: dict) -> dict:
    """One measure: follow a fake sampling to the end and write its parquet; returns the peaks above the start, in MB."""
    import tracemalloc

    import numpy as np  # noqa: F401 — imported before the reset, as the runner has it
    import pyarrow
    import pyarrow.parquet  # noqa: F401

    import bayes_draws
    import bayes_follow as bf
    import fake_cmdstan
    from bayes_task import Element

    bf.use_system_allocator()   # as a sampling Job does, before its writers are opened
    root = Path(case["dir"])
    replicated = case["kind"] == "replicated"
    spec = {"out": str(root / "out.csv"), "chains": case["chains"], "draws": case["draws"], "warmup": 20, "refresh": 100, "seed": 5,
            "speeds": [37, 23, 53, 31][:case["chains"]], "tick": 0, "max_piece": 1 << 20, "pool": 97}
    if replicated:
        spec.update(fixed_param=True, header=[f"y_rep.{i}" for i in range(1, case["rows"] + 1)])
    else:
        spec["quantities"] = case["quantities"]
    (root / "spec.json").write_text(json.dumps(spec))
    header = fake_cmdstan.header(spec)
    n_sampler = len(bf.SAMPLER_FIXED_PARAM if replicated else bf.SAMPLER_NUTS)
    rows = bf.block_rows(case["block_values"], len(header))
    draws_path = root / "draws.parquet"
    if replicated:
        class Replicated(bayes_draws.ReplicatedWriter):
            """The replicated writer takes the y_rep columns of the CSV's block (the sampler's two fields are not data)."""

            def write_block(self, chain, first_draw, block):
                super().write_block(chain, first_draw, block[:, n_sampler:])
        writer = Replicated(draws_path, case["rows"], case["chains"], case["draws"], rows)
    else:
        meta = [Element(name, None, None, None, None) for name in header[:n_sampler]]
        meta += [Element(f"q[{name.split('.')[1]}]", "q", None, int(name.split(".")[1]), None) for name in header[n_sampler:]]
        writer = bayes_draws.DrawsWriter(draws_path, meta, np.arange(len(header)), case["chains"], case["draws"], rows,
                                         n_unchecked=n_sampler)
    if case["tracemalloc"]:
        tracemalloc.start()
    with open("/proc/self/clear_refs", "w") as f:
        f.write("5")   # resets VmHWM to the current resident memory
    start = _status("VmRSS")
    t0 = time.monotonic()
    with open(root / "stdout.txt", "wb") as out, open(root / "stderr.txt", "wb") as err:
        process = subprocess.Popen([sys.executable, "-I", str(HERE / "fake_cmdstan.py"), str(root / "spec.json")],
                                   stdout=out, stderr=err, stdin=subprocess.DEVNULL)
    paths = bf.csv_paths(spec["out"], case["chains"])
    facts = bf.Follower(paths=paths, header=header, draws=case["draws"], block_values=case["block_values"], process=process,
                        stdout=root / "stdout.txt", stderr=root / "stderr.txt").follow(writer)
    meta_out = writer.close()
    seconds = time.monotonic() - t0
    peak = _status("VmHWM") - start
    traced = tracemalloc.get_traced_memory()[1] / 2 ** 20 if case["tracemalloc"] else None
    return {"kind": case["kind"], "draws": case["draws"], "peak_rss_mb": round(peak, 2),
            "peak_traced_mb": round(traced, 2) if traced is not None else None, "seconds": round(seconds, 2),
            "csv_bytes": sum(f.bytes_read for f in facts), "parquet_bytes": draws_path.stat().st_size,
            "row_groups": len(meta_out["row_groups"]), "block_draws": rows, "start_rss_mb": round(start, 2),
            "arrow_pool": pyarrow.default_memory_pool().backend_name}


def measure(case: dict) -> dict:
    """Runs one measure in a child process, in a fresh temporary folder."""
    with tempfile.TemporaryDirectory(prefix="bayes-memory-") as d:
        payload = json.dumps({**case, "dir": d})
        done = subprocess.run([sys.executable, "-I", str(Path(__file__).resolve()), "--child", payload], capture_output=True,
                              text=True, timeout=3600)
        if done.returncode != 0:
            raise AssertionError(f"the measure failed:\n{done.stderr[-4000:]}")
        return json.loads(done.stdout.strip().splitlines()[-1])


@unittest.skipUnless(os.environ.get("BAYES_MEMORY_TEST"), "a heavy test: BAYES_MEMORY_TEST=1 (or spec) runs it, alone")
class TestMemory(unittest.TestCase):
    profile = PROFILES["spec" if os.environ.get("BAYES_MEMORY_TEST") == "spec" else "small"]

    def _flat(self, kind: str, draws: int, block_values: int, **extra):
        results = {}
        for scale in (1, 10):
            case = {"kind": kind, "chains": self.profile["chains"], "draws": draws * scale, "block_values": block_values,
                    "tracemalloc": False, **extra}
            runs = [measure(case) for _ in range(2)]
            traced = measure({**case, "tracemalloc": True})
            for r in runs + [traced]:
                print(json.dumps(r), file=sys.stderr, flush=True)
            results[scale] = (runs, traced)
        rss_1 = max(r["peak_rss_mb"] for r in results[1][0])   # «the peak at one time»: the highest of its runs
        rss_10 = max(r["peak_rss_mb"] for r in results[10][0])
        traced_1, traced_10 = results[1][1]["peak_traced_mb"], results[10][1]["peak_traced_mb"]
        print(f"{kind}: peak RSS above the start {rss_1:.1f} MB at {draws} draws a chain, {rss_10:.1f} MB at {10 * draws}; "
              f"tracemalloc {traced_1:.1f} MB, {traced_10:.1f} MB", file=sys.stderr, flush=True)
        self.assertLessEqual(rss_10 - rss_1, TOLERANCE_MB)
        self.assertLessEqual(traced_10 - traced_1, TOLERANCE_MB)

    def test_the_draws(self):
        self._flat("draws", self.profile["draws"], self.profile["block_values"], quantities=self.profile["quantities"])

    def test_the_replicated_output(self):
        self._flat("replicated", self.profile["replicated_draws"], self.profile["replicated_block_values"],
                   rows=self.profile["replicated_rows"])


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "--child":
        print(json.dumps(child(json.loads(sys.argv[2]))), flush=True)
        sys.exit(0)
    unittest.main()
