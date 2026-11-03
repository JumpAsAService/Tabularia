"""The sizes of the sample data (SAMPLES_SCALE): no container needed.

    python3 -m unittest infrastructure/sampledb/test_scales.py

What they pin: the four presets and their order of size, robust's earlier name still
accepted, a name that does not exist refused, and the seed waiting longer where there
is more to import and to run.
"""
import importlib
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import scales  # noqa: E402


class Scales(unittest.TestCase):
    @classmethod
    def tearDownClass(cls):
        # the seed as the other tests import it: the default scale
        with mock.patch.dict(os.environ, {"SAMPLES_SCALE": ""}):
            importlib.reload(importlib.import_module("seed_tabularia"))

    def test_the_four_presets_grow_in_order(self):
        self.assertEqual(list(scales.PRESETS), ["small", "medium", "robust", "massive"])
        sizes = [p["activities"] for p in scales.PRESETS.values()]
        self.assertEqual(sizes, sorted(sizes))
        self.assertEqual(scales.PRESETS["massive"]["activities"], 1_000_000_000)
        # the billion is in ClickHouse: the order system stays at robust's size
        self.assertEqual(scales.PRESETS["massive"]["orders"], scales.PRESETS["robust"]["orders"])

    def test_the_name_is_read_as_written_or_by_its_earlier_name(self):
        self.assertEqual(scales.resolve(None), "small")
        self.assertEqual(scales.resolve(""), "small")
        self.assertEqual(scales.resolve(" Massive "), "massive")
        self.assertEqual(scales.resolve("robusta"), "robust")  # an .env written for 1.3.0
        with self.assertRaisesRegex(ValueError, "small|medium|robust|massive"):
            scales.resolve("huge")

    def test_the_seed_waits_longer_where_there_is_more(self):
        waits = {}
        for name in ("small", "robusta", "massive"):
            with mock.patch.dict(os.environ, {"SAMPLES_SCALE": name}):
                seed = importlib.reload(importlib.import_module("seed_tabularia"))
                waits[seed.SCALE] = (seed.WAIT_IMPORT, seed.WAIT_RUN)
        self.assertEqual(waits["small"], (60, 40))
        self.assertLess(waits["small"], waits["robust"])
        self.assertLess(waits["robust"], waits["massive"])

    def test_the_flows_start_on_an_engine_that_spills_then_the_others(self):
        self.assertEqual(scales.ENGINES[0], "duckdb")
        self.assertEqual(set(scales.ENGINES), {"duckdb", "polars", "chdb", "clickhouse", "bigquery"})

    def test_the_seed_takes_the_first_engine_the_policy_allows(self):
        seed = importlib.import_module("seed_tabularia")
        policy = lambda *allowed: type("G", (), {"must": staticmethod(  # noqa: E731
            lambda m, p, *a, **k: [{"engine_id": e, "allowed": e in allowed} for e in scales.ENGINES])})()
        with mock.patch.object(seed, "WANTED_ENGINE", ""):
            for allowed, chosen in ((("duckdb", "polars"), "duckdb"), (("polars",), "polars"), (("clickhouse",), "clickhouse")):
                with mock.patch.object(seed, "gw", policy(*allowed)):
                    self.assertEqual(seed.choose_engine(), chosen)
        with mock.patch.object(seed, "WANTED_ENGINE", "polars"):
            self.assertEqual(seed.choose_engine(), "polars")  # SAMPLES_ENGINE wins

    def test_the_seed_refuses_a_scale_that_does_not_exist(self):
        with mock.patch.dict(os.environ, {"SAMPLES_SCALE": "huge"}), self.assertRaises(SystemExit):
            importlib.reload(importlib.import_module("seed_tabularia"))


if __name__ == "__main__":
    unittest.main()
