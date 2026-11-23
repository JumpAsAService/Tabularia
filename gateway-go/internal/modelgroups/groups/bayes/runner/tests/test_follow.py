"""The follower of CmdStan's CSV files (bayes_follow, lot K2 of docs/specs/bayes-contract.md): criterion 2 — on fake CSVs written at
different speeds the draws parquet is the same file byte for byte, its row groups block-major and chain-minor, every draw present
once, the CSV bytes read exactly once — and the follower's refusals, which leave no file.

Small data only (a few quantities, a few hundred draws). From the repository root, with the image's pins:

    uv run --no-project --with pyarrow==26.0.1 --with numpy==2.5.5 \\
      python -I -m unittest discover -s gateway-go/internal/modelgroups/groups/bayes/runner/tests -v

Two kinds of test: with `fake_cmdstan.py` as a real child process (chains at different speeds, files cut anywhere: the concurrency
of a real run), and scripted ones, where a fake process and the follower's `sleep` play a fixed sequence of writes (a truncated
file, a line cut in two, a chain that never starts), so that each edge is reached every time.
"""
from __future__ import annotations

import builtins
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
RUNNER = HERE.parent
sys.path[:0] = [str(RUNNER.parent.parent.parent / "common" / "py"), str(RUNNER / "py"), str(HERE)]

import numpy as np  # noqa: E402
import pyarrow.parquet as pq  # noqa: E402

import bayes_follow as bf  # noqa: E402
import fake_cmdstan  # noqa: E402
from tab_protocol import Refusal  # noqa: E402

try:  # the draws writer of lot K1
    import bayes_draws  # noqa: E402
    from bayes_task import Element  # noqa: E402
except ImportError:  # pragma: no cover — before lot K1
    bayes_draws = None

DATA = HERE / "data"
FAKE = HERE / "fake_cmdstan.py"


class Recorder:
    """A sink that keeps a hash of every block it was given, in order, and the blocks' (chain, first draw, draws)."""

    def __init__(self, block_draws: int | None = None):
        if block_draws is not None:
            self.block_draws = block_draws
        self.sha = hashlib.sha256()
        self.calls: list[tuple[int, int, int]] = []
        self.aborted = False
        self.draws: dict[int, list[np.ndarray]] = {}

    def write_block(self, chain: int, first_draw: int, block: np.ndarray) -> None:
        self.calls.append((chain, first_draw, block.shape[0]))
        self.sha.update(f"{chain},{first_draw},{block.shape}".encode())
        self.sha.update(np.ascontiguousarray(block).tobytes())

    def abort(self) -> None:
        self.aborted = True


def elements(header: list[str], sampler: int) -> tuple[list, np.ndarray]:
    """The draws writer's quantities for a header of the fake's names (q.1 … q.P)."""
    meta = [Element(name, None, None, None, None) for name in header[:sampler]]
    for name in header[sampler:]:
        stan, index = name.split(".")
        meta.append(Element(f"{stan}[{index}]", stan, None, int(index), None))
    return meta, np.arange(len(header))


class FakeRun:
    """One run of fake_cmdstan.py as a child process, its streams in files, as the runner starts CmdStan."""

    def __init__(self, root: Path, **spec):
        self.root = root
        self.spec = {"out": str(root / "out.csv"), **spec}
        self.spec_path = root / "spec.json"
        self.spec_path.write_text(json.dumps(self.spec))
        self.stdout = root / "stdout.txt"
        self.stderr = root / "stderr.txt"
        self.paths = bf.csv_paths(self.spec["out"], int(spec["chains"]))
        self.header = fake_cmdstan.header(self.spec)

    def start(self) -> subprocess.Popen:
        with open(self.stdout, "wb") as out, open(self.stderr, "wb") as err:
            return subprocess.Popen([sys.executable, "-I", str(FAKE), str(self.spec_path)], stdout=out, stderr=err,
                                    stdin=subprocess.DEVNULL)

    def follower(self, process, block_values: int, **kw) -> bf.Follower:
        return bf.Follower(paths=self.paths, header=self.header, draws=int(self.spec["draws"]), block_values=block_values,
                           process=process, stdout=self.stdout, stderr=self.stderr, poll_seconds=0.002, **kw)


class ScriptedProcess:
    """A stand-in for CmdStan's Popen whose exit code a script sets."""

    def __init__(self):
        self.code: int | None = None
        self.terminated = False

    def poll(self):
        return self.code

    def terminate(self):
        self.terminated = True
        self.code = -15

    def kill(self):
        self.code = -9

    def wait(self, timeout=None):
        return self.code


class Script:
    """The follower's `sleep`: each call plays the next step (a function); a follower that sleeps with no step left would wait
    forever, which is a failure."""

    def __init__(self, steps):
        self.steps = list(steps)
        self.sleeps = 0

    def __call__(self, seconds):
        self.sleeps += 1
        if not self.steps:
            raise AssertionError("the follower waits for something the script never writes")
        self.steps.pop(0)()


def append(path: Path, data: bytes):
    def step():
        with open(path, "ab") as f:
            f.write(data)
    return step


class TestFormat(unittest.TestCase):
    """The format of CmdStan 2.40.0, read from files the image wrote (tests/data)."""

    def test_csv_names(self):
        self.assertEqual(bf.csv_paths("/tmp/run/out.csv", 1), [Path("/tmp/run/out.csv")])
        self.assertEqual(bf.csv_paths("/tmp/run/out.csv", 3),
                         [Path("/tmp/run/out_1.csv"), Path("/tmp/run/out_2.csv"), Path("/tmp/run/out_3.csv")])

    def test_block_rows(self):
        self.assertEqual(bf.block_rows(1_048_576, 10), 104_857)
        self.assertEqual(bf.block_rows(1_048_576, 20_007), 52)
        self.assertEqual(bf.block_rows(5, 10), 1)

    def _follow_file(self, name: str, header: list[str], draws: int):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "out.csv"
            path.write_bytes((DATA / name).read_bytes())
            process = ScriptedProcess()
            process.code = 0
            rec = Recorder()
            facts = bf.Follower(paths=[path], header=header, draws=draws, block_values=4 * len(header), process=process,
                                sleep=Script([])).follow(rec)
            return facts[0], rec, path.stat().st_size

    def test_a_real_nuts_file(self):
        header = list(bf.SAMPLER_NUTS) + ["alpha", "b_x", "sigma"]
        facts, rec, size = self._follow_file("cmdstan-2.40.0-nuts-chain1.csv", header, 5)
        self.assertEqual(rec.calls, [(1, 1, 4), (1, 5, 1)])
        self.assertEqual(facts.draws, 5)
        self.assertEqual(facts.step_size, 0.0256932)
        self.assertEqual((facts.seconds_warmup, facts.seconds_sampling), (0.0, 0.0))
        self.assertEqual(facts.bytes_read, size)

    def test_a_real_fixed_param_file(self):
        header = list(bf.SAMPLER_FIXED_PARAM) + ["alpha", "b_x", "sigma"]
        facts, rec, size = self._follow_file("cmdstan-2.40.0-fixed-param-chain1.csv", header, 3)
        self.assertEqual(rec.calls, [(1, 1, 3)])
        self.assertIsNone(facts.step_size)
        self.assertEqual(facts.bytes_read, size)

    def test_nan_and_inf_are_read_as_numbers(self):
        # CmdStan writes nan, inf and -inf as text; the follower reads them, the draws writer refuses them in a quantity
        header = list(bf.SAMPLER_NUTS) + ["m", "g", "h", "k"]
        got = []

        class Keep(Recorder):
            def write_block(self, chain, first_draw, block):
                got.append(block.copy())

        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "out.csv"
            path.write_bytes((DATA / "cmdstan-2.40.0-nan-inf-chain1.csv").read_bytes())
            process = ScriptedProcess()
            process.code = 0
            bf.Follower(paths=[path], header=header, draws=3, block_values=100, process=process, sleep=Script([])).follow(Keep())
        block = got[0]
        self.assertTrue(np.isnan(block[:, 8]).all())
        self.assertTrue((block[:, 9] == np.inf).all() and (block[:, 10] == -np.inf).all())

    def test_progress_lines(self):
        for name, chains in (("cmdstan-2.40.0-stdout-4-chains.txt", 4), ("cmdstan-2.40.0-stdout-1-chain.txt", 1)):
            s = bf._Stdout(DATA / name, 64)   # small reads: lines cut in two
            seen = s.pump()
            s.close()
            self.assertEqual(sorted({p.chain for p in seen}), list(range(1, chains + 1)))
            for p in s.latest.values():
                self.assertEqual((p.iteration, p.total, p.phase), (25, 25, "sampling"))
            self.assertIn(bf.Progress(1, 5, 25, "warmup"), seen)

    def test_the_fake_writes_what_cmdstan_writes(self):
        """The fake's comments and header have the real file's shape: the same configuration keys, the adaptation and timing
        comments, the same numbers' format."""
        real = (DATA / "cmdstan-2.40.0-nuts-chain1.csv").read_text().splitlines()
        spec = {"out": "/tmp/run/out.csv", "chains": 4, "draws": 5, "warmup": 20, "refresh": 5, "seed": 7,
                "header": ["alpha", "b_x", "sigma"]}
        fake = "".join(text for text, _ in fake_cmdstan.chain_steps(spec, 1)).splitlines()

        def keys(lines):
            return [line.split("=")[0].rstrip() for line in lines if line.startswith("#") and "=" in line]
        self.assertEqual(keys(real)[:-1], keys(fake)[:-1])  # the last, «Step size», differs in its value only
        self.assertEqual([x for x in real if not x.startswith("#")][0], [x for x in fake if not x.startswith("#")][0])
        def tail(lines):
            return [re.sub(r"[0-9.]+ seconds", "N seconds", line) for line in lines[-5:]]
        self.assertEqual(tail(real), tail(fake))
        draw = next(x for x in fake if x and not x.startswith("#") and not x.startswith("lp__"))
        for field in draw.split(","):
            digits = field.lstrip("-").replace(".", "").split("e")[0].lstrip("0")
            self.assertLessEqual(len(digits), 8)


@unittest.skipIf(bayes_draws is None, "the draws writer of lot K1 is not there")
class TestOrder(unittest.TestCase):
    """Criterion 2: the same file byte for byte whatever the chains' speeds; every draw once; every byte read once."""

    SPEEDS = ([1, 1, 1, 1], [7, 1, 3, 0.5], [0.3, 5, 1, 9], [2, 2, 0.25, 4], [11, 0.7, 0.7, 11])

    def setUp(self):
        self.sleeps = 0

    def _sleep(self, seconds):
        self.sleeps += 1
        time.sleep(seconds)

    def _run(self, root: Path, speeds, piece_seed: int, start, block_values: int = 7 * 13) -> tuple[bytes, Recorder, list]:
        run = FakeRun(root, chains=4, draws=50, warmup=7, quantities=6, seed=11, speeds=speeds, start=start,
                      piece_seed=piece_seed, max_piece=97, tick=0.0005)
        follower = run.follower(run.start(), block_values, sleep=self._sleep)
        meta, columns = elements(run.header, 7)
        writer = bayes_draws.DrawsWriter(root / "draws.parquet", meta, columns, 4, 50, follower.rows, n_unchecked=7)
        rec = Recorder(follower.rows)
        facts = follower.follow(writer, rec)
        writer.close()
        for f, p in zip(facts, run.paths):
            self.assertEqual(f.bytes_read, p.stat().st_size, "every byte of a CSV is read once")
            self.assertEqual(f.draws, 50)
        return (root / "draws.parquet").read_bytes(), rec, facts

    def test_the_same_file_whatever_the_speeds(self):
        files, hashes = set(), set()
        for k, speeds in enumerate(self.SPEEDS):
            with tempfile.TemporaryDirectory() as d:
                data, rec, _ = self._run(Path(d), speeds, piece_seed=k, start=[0, 3 * k, k, 0])
                files.add(hashlib.sha256(data).hexdigest())
                hashes.add(rec.sha.hexdigest())
                rows = 7 * 13 // 13
                self.assertEqual(rec.calls, [(c, b * rows + 1, min(rows, 50 - b * rows))
                                             for b in range(-(-50 // rows)) for c in (1, 2, 3, 4)])
                if k == 0:
                    pf = pq.ParquetFile(str(Path(d) / "draws.parquet"))
                    self.assertEqual(pf.metadata.num_row_groups, 4 * -(-50 // rows))
                    order = [(pf.metadata.row_group(i).column(0).statistics.min,
                              pf.metadata.row_group(i).column(1).statistics.min) for i in range(pf.metadata.num_row_groups)]
                    self.assertEqual(order, [(c, b * rows + 1) for b in range(-(-50 // rows)) for c in (1, 2, 3, 4)])
                    table = pf.read(columns=["chain", "draw", "quantity"])  # small: the test's own check
                    keys = set(zip(table["chain"].to_pylist(), table["draw"].to_pylist(),
                                   table["quantity"].cast("string").to_pylist()))
                    self.assertEqual(len(keys), table.num_rows, "every draw of every quantity once")
                    self.assertEqual(table.num_rows, 4 * 50 * 13)
        self.assertGreater(self.sleeps, 0, "the follower waited for lines not yet written: it followed growing files")
        self.assertEqual(len(hashes), 1, "the blocks handed on depend on the files, never on the speeds")
        self.assertEqual(len(files), 1, "the draws parquet is the same byte for byte")

    def test_the_values_are_the_csvs(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self._run(root, [3, 1, 2, 1], piece_seed=5, start=[0, 0, 0, 0])
            table = pq.ParquetFile(str(root / "draws.parquet")).read()
            spec = {"chains": 4, "draws": 50, "warmup": 7, "quantities": 6, "seed": 11}
            want = fake_cmdstan.Draws(spec, 3)
            line = [float(x) for x in want.line(1).split(",")]   # draw 1 of chain 3
            got = {q: v for c, dr, q, v in zip(table["chain"].to_pylist(), table["draw"].to_pylist(),
                                                table["quantity"].cast("string").to_pylist(), table["value"].to_pylist())
                   if c == 3 and dr == 1}
            self.assertEqual(got["q[1]"], line[7])
            self.assertEqual(got["lp__"], line[0])


class TestNoWholeRead(unittest.TestCase):
    """«No whole read»: a whole fake sampling with every way to read a whole file or table made to fail on the CSVs and the draws,
    and every read of a CSV no longer than READ_BYTES."""

    @unittest.skipIf(bayes_draws is None, "the draws writer of lot K1 is not there")
    def test_a_whole_sampling_reads_by_pieces(self):
        import pyarrow.csv as pacsv
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            run = FakeRun(root, chains=3, draws=40, warmup=5, quantities=4, seed=3, speeds=[2, 1, 3], piece_seed=9,
                          max_piece=61, tick=0.0005)
            process = run.start()
            guarded = (".csv", "draws.parquet")
            real_open, real_pread = builtins.open, os.pread
            sizes: list[int] = []

            def no_open(file, *a, **kw):
                if str(file).endswith(guarded):
                    raise AssertionError(f"open() of {file}")
                return real_open(file, *a, **kw)

            def fail(*a, **kw):
                raise AssertionError("a whole read or write")

            def pread(fd, n, offset):
                sizes.append(n)
                return real_pread(fd, n, offset)

            follower = run.follower(process, 3 * 11, read_bytes=256)
            meta, columns = elements(run.header, 7)
            writer = bayes_draws.DrawsWriter(root / "draws.parquet", meta, columns, 3, 40, follower.rows, n_unchecked=7)
            with mock.patch.object(builtins, "open", no_open), mock.patch.object(pacsv, "read_csv", fail), \
                    mock.patch.object(pq, "read_table", fail), mock.patch.object(pq, "write_table", fail), \
                    mock.patch.object(np, "loadtxt", fail), mock.patch.object(np, "genfromtxt", fail), \
                    mock.patch.object(os, "pread", pread):
                facts = follower.follow(writer)
            writer.close()
            self.assertTrue(sizes and max(sizes) <= 256)
            self.assertEqual(sum(f.bytes_read for f in facts), sum(p.stat().st_size for p in run.paths))


class TestScripted(unittest.TestCase):
    """Edges reached every time: a line cut in two, a chain that waits on disk, a file that shrinks, a chain that ends early."""

    HEADER = list(bf.SAMPLER_FIXED_PARAM) + ["a", "b"]

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.root = Path(self._dir.name)
        self.paths = bf.csv_paths(self.root / "out.csv", 2)
        self.process = ScriptedProcess()

    def tearDown(self):
        self._dir.cleanup()

    def follower(self, steps, draws=2, **kw):
        return bf.Follower(paths=self.paths, header=self.HEADER, draws=draws, block_values=4, process=self.process,
                           stderr=self.root / "stderr.txt", sleep=Script(steps), **kw)

    def ended(self, code=0):
        def step():
            self.process.code = code
        return step

    def test_a_line_cut_in_two_is_read_once(self):
        head = b"# c\nlp__,accept_stat__,a,b\n"
        one, two = self.paths
        steps = [append(one, head + b"0,0,1.5,2"), append(one, b".25\n0,0,3,4\n"), append(two, head + b"0,0,5,6\n0,0,7,"),
                 append(two, b"8\n# \n#  Elapsed Time: 0.5 seconds (Warm-up)\n"), self.ended()]
        got = []

        class Keep(Recorder):
            def write_block(self, chain, first_draw, block):
                super().write_block(chain, first_draw, block)
                got.append(block.tolist())
        rec = Keep()
        lines = []
        facts = self.follower(steps, on_line=lambda c, d, line: lines.append((c, d, line))).follow(rec)
        self.assertEqual(rec.calls, [(1, 1, 1), (2, 1, 1), (1, 2, 1), (2, 2, 1)])
        self.assertEqual(lines, [(1, 1, b"0,0,1.5,2.25"), (2, 1, b"0,0,5,6"), (1, 2, b"0,0,3,4"), (2, 2, b"0,0,7,8")])
        self.assertEqual(got, [[[0, 0, 1.5, 2.25]], [[0, 0, 5, 6]], [[0, 0, 3, 4]], [[0, 0, 7, 8]]])
        self.assertEqual(facts[1].seconds_warmup, 0.5)
        self.assertEqual([f.bytes_read for f in facts], [p.stat().st_size for p in self.paths])

    def test_a_fast_chain_waits_on_disk(self):
        # chain 2 has written everything before chain 1 starts: its lines stay in its file until its turn
        head = b"lp__,accept_stat__,a,b\n"
        one, two = self.paths
        two.write_bytes(head + b"".join(b"0,0,%d,%d\n" % (i, i) for i in range(4)))
        follower = self.follower([append(one, head + b"0,0,1,1\n0,0,2,2\n"), append(one, b"0,0,3,3\n0,0,4,4\n"), self.ended()],
                                 draws=4)
        self.assertEqual(follower.rows, 1)
        reads_of_two = []

        class Watch(Recorder):
            def write_block(self, chain, first_draw, block):
                super().write_block(chain, first_draw, block)
                reads_of_two.append(follower.chains[1].offset)
        follower.follow(Watch())
        # chain 2 was read only on its turns: after chain 1's first block nothing of it had been read
        self.assertEqual(reads_of_two[0], 0)

    def test_a_truncated_file_is_refused(self):
        head = b"lp__,accept_stat__,a,b\n"
        one, two = self.paths
        one.write_bytes(head + b"0,0,1,1\n")
        two.write_bytes(head + b"0,0,1,1\n")

        def shrink():
            with open(one, "r+b") as f:
                f.truncate(5)
        rec = Recorder()
        with self.assertRaises(Refusal) as e:
            self.follower([shrink]).follow(rec)
        self.assertEqual((e.exception.code, e.exception.params["reason"]), ("sampler_failed", "truncated"))
        self.assertTrue(rec.aborted)
        self.assertTrue(self.process.terminated, "CmdStan is stopped")

    def test_a_chain_that_ends_early(self):
        head = b"lp__,accept_stat__,a,b\n"
        one, two = self.paths
        one.write_bytes(head + b"0,0,1,1\n0,0,2,2\n")
        two.write_bytes(head + b"0,0,1,1\n")
        rec = Recorder()
        with self.assertRaises(Refusal) as e:
            self.follower([self.ended(0)]).follow(rec)
        self.assertEqual((e.exception.code, e.exception.params["reason"], e.exception.params["chain"]),
                         ("sampler_failed", "chain_ended", 2))
        self.assertTrue(rec.aborted)

    def test_a_chain_never_written(self):
        self.paths[0].write_bytes(b"lp__,accept_stat__,a,b\n0,0,1,1\n0,0,2,2\n")
        with self.assertRaises(Refusal) as e:
            self.follower([lambda: None, self.ended(0)]).follow(Recorder())
        self.assertEqual(e.exception.params["reason"], "chain_ended")
        self.assertIn("never written", e.exception.message)

    def test_malformed_lines(self):
        for bad, why in ((b"0,0,1\n", "fields"), (b"0,0,1,x\n", "number"), (b"0,0,1,1\n\n", "fields")):
            with self.subTest(why):
                head = b"lp__,accept_stat__,a,b\n"
                one, two = self.paths
                one.write_bytes(head + b"0,0,1,1\n" + bad)
                two.write_bytes(head + b"0,0,1,1\n0,0,1,1\n")
                self.process.code = None
                with self.assertRaises(Refusal) as e:
                    self.follower([self.ended(0)]).follow(Recorder())
                self.assertEqual((e.exception.code, e.exception.params["reason"]), ("sampler_failed", "malformed"))

    def test_more_draws_than_asked(self):
        head = b"lp__,accept_stat__,a,b\n"
        for p in self.paths:
            p.write_bytes(head + b"0,0,1,1\n" * 3)
        self.process.code = 0
        with self.assertRaises(Refusal) as e:
            self.follower([]).follow(Recorder())
        self.assertEqual(e.exception.params["reason"], "malformed")

    def test_a_header_that_is_not_the_manifests(self):
        for p in self.paths:
            p.write_bytes(b"lp__,accept_stat__,a,c\n0,0,1,1\n0,0,1,1\n")
        self.process.code = 0
        rec = Recorder()
        with self.assertRaises(bf.ManifestMismatch):
            self.follower([]).follow(rec)
        self.assertTrue(rec.aborted)

    def test_a_runner_check_stops_cmdstan(self):
        # the time projection (on_idle / on_progress) refuses: CmdStan is stopped, the sinks aborted
        self.paths[0].write_bytes(b"lp__,accept_stat__,a,b\n")

        def too_slow():
            raise Refusal("too_slow", "about 41 minutes", {})
        rec = Recorder()
        with self.assertRaises(Refusal) as e:
            self.follower([lambda: None], on_idle=too_slow).follow(rec)
        self.assertEqual(e.exception.code, "too_slow")
        self.assertTrue(self.process.terminated and rec.aborted)

    def test_a_non_zero_exit_after_the_draws(self):
        head = b"lp__,accept_stat__,a,b\n"
        for p in self.paths:
            p.write_bytes(head + b"0,0,1,1\n0,0,1,1\n")
        self.process.code = 134
        (self.root / "stderr.txt").write_text("terminate called after throwing an instance of 'std::bad_alloc'\n")
        with self.assertRaises(Refusal) as e:
            self.follower([]).follow(Recorder())
        self.assertEqual((e.exception.code, e.exception.params["reason"]), ("sampler_failed", "exit"))
        self.assertIn("bad_alloc", e.exception.params["log"])

    def test_a_block_size_the_sink_does_not_share(self):
        with self.assertRaises(ValueError):
            self.follower([]).follow(Recorder(block_draws=3))


class TestCmdStanArguments(unittest.TestCase):
    """The fixed argument list of «The CSV files»: numbers formatted here, no text of a person, init as a radius or a file."""

    SAMPLER = {"chains": 4, "warmup": 1000, "draws": 1000, "seed": 20261123, "adapt_delta": 0.8, "max_treedepth": 10, "init": None,
               "refresh": 100}

    def test_nuts(self):
        args = bf.cmdstan_args("/tmp/run/model", self.SAMPLER, "nuts", "/tmp/run/data.json", "/tmp/run/out.csv", 2)
        self.assertEqual(args, ["/tmp/run/model", "method=sample", "num_samples=1000", "num_warmup=1000", "save_warmup=0", "adapt",
                                "delta=0.8", "algorithm=hmc", "engine=nuts", "max_depth=10", "num_chains=4", "data",
                                "file=/tmp/run/data.json", "random", "seed=20261123", "output", "file=/tmp/run/out.csv",
                                "refresh=100", "num_threads=2"])

    def test_fixed_param_and_a_radius(self):
        args = bf.cmdstan_args("m", {**self.SAMPLER, "warmup": 0, "init": 0.5}, "fixed_param", "d.json", "o.csv", 1)
        self.assertIn("algorithm=fixed_param", args)
        self.assertNotIn("engine=nuts", args)
        self.assertIn("init=0.5", args)
        self.assertIn("num_warmup=0", args)

    def test_init_values_go_to_a_file(self):
        with tempfile.TemporaryDirectory() as d:
            path = bf.write_init({**self.SAMPLER, "init": {"mu": 0.5, "a": [1, 2]}}, Path(d))
            self.assertEqual(json.loads(path.read_text()), {"a": [1, 2], "mu": 0.5})
            args = bf.cmdstan_args("m", {**self.SAMPLER, "init": {"mu": 0.5}}, "nuts", "d", "o", 1, path)
            self.assertIn(f"init={path}", args)
            self.assertIsNone(bf.write_init(self.SAMPLER, Path(d)))

    def test_a_value_that_is_not_a_number_is_refused(self):
        with self.assertRaises(TypeError):
            bf.cmdstan_args("m", {**self.SAMPLER, "init": "2; rm"}, "nuts", "d", "o", 1)


class TestFailingRuns(unittest.TestCase):
    """The fake sampler failing as CmdStan does: the refusal's code, CmdStan stopped, no file left."""

    def _fail(self, **spec):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            run = FakeRun(root, **{"chains": 2, "draws": 30, "warmup": 3, "quantities": 3, "seed": 2, "tick": 0.0005, **spec})
            process = run.start()
            rec = Recorder()
            draws = root / "draws.parquet"
            sinks = [rec]
            if bayes_draws is not None:
                follower = run.follower(process, 10 * 5)
                meta, columns = elements(run.header, len(run.header) - 3)
                sinks.insert(0, bayes_draws.DrawsWriter(draws, meta, columns, 2, 30, follower.rows,
                                                        n_unchecked=len(run.header) - 3))
            else:
                follower = run.follower(process, 10 * 5)
            with self.assertRaises(Refusal) as e:
                follower.follow(*sinks)
            self.assertIsNotNone(process.poll(), "CmdStan is not left running")
            self.assertTrue(rec.aborted)
            self.assertFalse(draws.exists(), "no partial result")
            return e.exception

    def test_initialization_failed(self):
        e = self._fail(init_fail=True)
        self.assertEqual(e.code, "init_failed")

    def test_a_chain_dies(self):
        e = self._fail(die={"chain": 2, "after": 7, "exit": 70})
        self.assertEqual((e.code, e.params["reason"]), ("sampler_failed", "exit"))
        self.assertIn("stops chain 2", e.params["log"])

    def test_a_malformed_line(self):
        e = self._fail(malformed={"chain": 1, "draw": 12})
        self.assertEqual((e.code, e.params["reason"], e.params["chain"]), ("sampler_failed", "malformed", 1))

    def test_an_extra_draw(self):
        e = self._fail(extra={"chain": 2})
        self.assertEqual((e.code, e.params["reason"]), ("sampler_failed", "malformed"))

    @unittest.skipIf(bayes_draws is None, "the draws writer of lot K1 is not there")
    def test_not_finite(self):
        e = self._fail(special=[[2, 17, 2, "nan"]])
        self.assertEqual((e.code, e.params["quantity"], e.params["chain"], e.params["draw"]), ("not_finite", "q[2]", 2, 17))

    def test_a_sampler_still_running_is_stopped(self):
        with tempfile.TemporaryDirectory() as d:
            run = FakeRun(Path(d), chains=2, draws=100000, warmup=0, quantities=2, tick=0.01, speeds=[1, 1], refresh=10)
            process = run.start()
            calls = []

            def too_slow(p):
                calls.append(p)
                if len(calls) > 2:
                    raise Refusal("too_slow", "too slow", {})
            with self.assertRaises(Refusal):
                run.follower(process, 50, on_progress=too_slow, stop_grace=2).follow(Recorder())
            self.assertIsNotNone(process.poll())


if __name__ == "__main__":
    unittest.main()
