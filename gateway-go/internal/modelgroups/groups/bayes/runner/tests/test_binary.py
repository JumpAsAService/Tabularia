"""The compiled model of a sampling Job (lot R0): fetched in chunks, hashed as it lands, compared before any chmod, never run when it
differs, and fetched before one row of the input is read."""
import hashlib
import json
import os
import stat
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE.parents[3] / "common" / "py"), str(HERE.parent / "py")]
GO_TESTDATA = HERE.parents[4] / "bayescontract" / "testdata"

import pyarrow as pa  # noqa: E402
import pyarrow.parquet as pq  # noqa: E402

import bayes_binary  # noqa: E402
import bayes_data  # noqa: E402
import bayes_main  # noqa: E402
import bayes_task  # noqa: E402
from tab_protocol import Refusal  # noqa: E402

BINARY = b"\x7fELF" + bytes(range(256)) * 40  # 10,244 bytes: several chunks of 4 KiB


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class BinaryCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.source = self.dir / "bucket" / "model"
        self.source.parent.mkdir()
        self.source.write_bytes(BINARY)
        self.target = self.dir / "run" / "model"

    def tearDown(self):
        self.tmp.cleanup()


class TestFetch(BinaryCase):
    def test_the_right_binary_is_copied_whole_and_becomes_0700(self):
        total = bayes_binary.fetch_hashing(str(self.source), self.target, sha(BINARY), chunk=4096)
        self.assertEqual(total, len(BINARY))
        self.assertEqual(self.target.read_bytes(), BINARY)
        self.assertEqual(stat.S_IMODE(self.target.stat().st_mode), 0o700)

    def test_the_file_is_private_while_it_is_written(self):
        modes = []

        def spy(path, mode):
            modes.append(stat.S_IMODE(Path(path).stat().st_mode))
            raise AssertionError("chmod must come after the comparison, with the file complete")

        with mock.patch.object(bayes_binary.os, "chmod", spy):
            with self.assertRaises(AssertionError):
                bayes_binary.fetch_hashing(str(self.source), self.target, sha(BINARY))
        self.assertEqual(modes, [0o600])

    def test_a_binary_with_one_changed_byte_is_refused_deleted_never_chmoded_never_run(self):
        bad = bytearray(BINARY)
        bad[5000] ^= 1
        self.source.write_bytes(bytes(bad))
        with mock.patch.object(bayes_binary.os, "chmod") as chmod, mock.patch("subprocess.Popen") as popen, \
                mock.patch("subprocess.run") as run, mock.patch("os.execv") as execv:
            with self.assertRaises(Refusal) as e:
                bayes_binary.fetch_hashing(str(self.source), self.target, sha(BINARY), chunk=4096)
        self.assertEqual(e.exception.code, "binary_hash")
        self.assertIn("is not run", e.exception.message)
        self.assertFalse(self.target.exists())
        for call in (chunk_call for chunk_call in (chmod, popen, run, execv)):
            call.assert_not_called()

    def test_a_truncated_binary_is_refused(self):
        self.source.write_bytes(BINARY[:-1])
        with self.assertRaises(Refusal) as e:
            bayes_binary.fetch_hashing(str(self.source), self.target, sha(BINARY), chunk=4096)
        self.assertEqual(e.exception.code, "binary_hash")
        self.assertFalse(self.target.exists())
        self.source.write_bytes(b"")
        with self.assertRaises(Refusal) as e:
            bayes_binary.fetch_hashing(str(self.source), self.target, sha(BINARY))
        self.assertEqual(e.exception.code, "binary_hash")

    def test_a_missing_object_is_refused_and_leaves_nothing(self):
        with mock.patch.object(bayes_binary.os, "chmod") as chmod:
            with self.assertRaises(Refusal) as e:
                bayes_binary.fetch_hashing(str(self.dir / "bucket" / "nowhere"), self.target, sha(BINARY))
        self.assertEqual(e.exception.code, "binary_hash")
        self.assertFalse(self.target.exists())
        chmod.assert_not_called()

    def test_the_chunk_is_8_mib_by_default_and_the_read_is_in_pieces(self):
        self.assertEqual(bayes_binary.CHUNK, 8 * 1024 * 1024)
        sizes = []
        real = bayes_binary._open_input

        class Spy:
            def __init__(self, f):
                self.f = f

            def read(self, n):
                piece = self.f.read(n)
                sizes.append(len(piece))
                return piece

            def __enter__(self):
                return self

            def __exit__(self, *a):
                self.f.close()

        with mock.patch.object(bayes_binary, "_open_input", lambda s: Spy(real(s))):
            bayes_binary.fetch_hashing(str(self.source), self.target, sha(BINARY), chunk=4096)
        self.assertEqual(sizes, [4096, 4096, len(BINARY) - 8192, 0])


class TestFetchBinary(BinaryCase):
    def test_a_missing_address_or_hash_is_a_fault_of_the_gateway(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TABULARIA_BINARY_IN", None)
            with self.assertRaises(Refusal) as e:
                bayes_binary.fetch_binary(self.dir / "run", sha(BINARY))
        self.assertEqual(e.exception.code, "task_value")
        with mock.patch.dict(os.environ, {"TABULARIA_BINARY_IN": str(self.source)}):
            with self.assertRaises(Refusal) as e:
                bayes_binary.fetch_binary(self.dir / "run", "")
        self.assertEqual(e.exception.code, "task_value")

    def test_the_binary_lands_under_the_workdir(self):
        with mock.patch.dict(os.environ, {"TABULARIA_BINARY_IN": str(self.source)}):
            path = bayes_binary.fetch_binary(self.dir / "run", sha(BINARY))
        self.assertEqual(path, self.dir / "run" / "model")
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700)


class TestDispatcherOrder(BinaryCase):
    def setUp(self):
        super().setUp()
        self.input = str(self.dir / "in.parquet")
        pq.write_table(pa.table({"col_g": ["s1", "s2"], "col_x": [1.0, 2.0], "col_y": [0.1, 0.2]}), self.input)

    def task(self, mode, digest):
        raw = json.loads((GO_TESTDATA / "tasks" / f"{mode}.json").read_text(encoding="utf-8"))
        raw["bayes"]["model"]["binary_sha256"] = digest
        return bayes_task.parse_task(raw)

    def test_a_tampered_binary_is_refused_before_one_row_is_read_in_both_modes(self):
        bad = bytearray(BINARY)
        bad[0] ^= 1
        self.source.write_bytes(bytes(bad))
        for mode in ("sample", "prior_predictive"):
            fake = types.ModuleType("bayes_run")
            fake.begin = lambda task: (None, "sample")
            fake.run = mock.Mock(return_value=0)
            env = {"TABULARIA_BINARY_IN": str(self.source), "TABULARIA_INPUT": self.input}
            with mock.patch.dict(sys.modules, {"bayes_run": fake}), mock.patch.dict(os.environ, env), \
                    mock.patch.object(bayes_data, "pass_a") as pass_a, mock.patch.object(bayes_data, "pass_b") as pass_b, \
                    mock.patch.object(pq, "ParquetFile") as parquet_file, mock.patch.object(pq, "read_table") as read_table, \
                    mock.patch("subprocess.Popen") as popen, mock.patch.object(bayes_binary.os, "chmod") as chmod:
                with self.assertRaises(Refusal) as e:
                    bayes_main.run_task(self.task(mode, sha(BINARY)), self.dir, self.dir / "run")
            self.assertEqual(e.exception.code, "binary_hash", mode)
            for call in (pass_a, pass_b, parquet_file, read_table, popen, chmod, fake.run):
                call.assert_not_called()
            self.assertFalse((self.dir / "run").exists(), mode)

    def test_the_right_binary_is_in_the_hands_of_the_follower_after_the_data(self):
        order = []
        fake = types.ModuleType("bayes_run")
        fake.begin = lambda task: (None, "sample")

        def follow(task, prepared):
            order.append(("follow", prepared.binary_path, stat.S_IMODE(prepared.binary_path.stat().st_mode), prepared.data_path.exists()))
            return 0

        fake.run = follow

        def prepare(task, workdir, checks=None, run="sample"):  # here only that the binary is already there
            order.append(("data", (workdir / "model").exists()))
            return bayes_main.Prepared(task, workdir, None, None, workdir / "data.json")

        env = {"TABULARIA_BINARY_IN": str(self.source), "TABULARIA_INPUT": self.input}
        for mode in ("sample", "prior_predictive"):
            order.clear()
            with mock.patch.dict(sys.modules, {"bayes_run": fake}), mock.patch.dict(os.environ, env), \
                    mock.patch.object(bayes_main, "prepare_run", prepare), \
                    mock.patch.object(Path, "exists", lambda self: True if self.name == "data.json" else os.path.exists(self)):
                self.assertEqual(bayes_main.run_task(self.task(mode, sha(BINARY)), self.dir, self.dir / "run"), 0)
            self.assertEqual(order, [("data", True), ("follow", self.dir / "run" / "model", 0o700, True)], mode)


if __name__ == "__main__":
    unittest.main()
