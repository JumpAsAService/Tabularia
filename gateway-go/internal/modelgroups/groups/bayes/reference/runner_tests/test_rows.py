"""The streaming writer of the ten columns and the sections of lot K1 (model, data, quantity), checked against the declaration Go writes
(bayescontract/testdata/contract.json) and the models' section shapes."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE.parents[3] / "common" / "py"), str(HERE.parents[1] / "runner" / "py")]
GO_TESTDATA = HERE.parents[4] / "bayescontract" / "testdata"
REPO = HERE.parents[6]

import numpy as np  # noqa: E402
import pyarrow.parquet as pq  # noqa: E402

import bayes_data  # noqa: E402
import bayes_rows  # noqa: E402
import bayes_task  # noqa: E402
import tab_results  # noqa: E402


def declaration(variant):
    contract = json.loads((GO_TESTDATA / "contract.json").read_text(encoding="utf-8"))
    shapes = json.loads((REPO / "backend" / "app" / "engine" / "models" / "catalog.json").read_text(encoding="utf-8"))["section_shapes"]
    return contract["variants"][variant], {**shapes, **contract["shapes"]}, contract


class RowsCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def writer(self, variant=None, **kw):
        dec, shapes = (None, None) if variant is None else declaration(variant)[:2]
        return bayes_rows.ResultsWriter(str(self.dir / "results.parquet"), declaration=dec, shapes=shapes, **kw)

    def start(self, w):
        w.text("model", "model", "bayes_model")
        w.text("model", "variant", "sample")
        w.number("model", "n", 3)


class TestResultsWriter(RowsCase):
    def test_row_groups_of_a_few_rows_with_ord_continuing_and_the_exact_types(self):
        w = self.writer(flush_rows=2)
        self.start(w)
        for i in range(5):
            w.number("summary", "mean", float(i) if i != 3 else float("nan"), level="q", level_2=f"u{i}")
        w.points("curve", "trace:tau", "value", [1.0, 2.0, 3.0], [0.5, 0.25, np.inf], level_2="1")
        total = w.close()
        f = pq.ParquetFile(self.dir / "results.parquet")
        self.assertEqual(total, 11)
        self.assertGreater(f.num_row_groups, 3)
        self.assertEqual(f.schema_arrow, tab_results.schema())
        t = f.read().to_pydict()
        self.assertEqual(t["ord"], list(range(11)))
        self.assertEqual(len(set(t["key"])), 11)
        self.assertIn("summary|q|u0|mean", t["key"])
        self.assertIsNone(t["value"][t["key"].index("summary|q|u3|mean")])          # a non-finite number is NULL
        self.assertIn("curve|trace:tau|1|value|2", t["key"])

    def test_a_unit_with_a_bar_or_a_backslash_is_found_by_its_own_label(self):
        w = self.writer()
        self.start(w)
        for unit in ("a|b", "a\\b", "plain"):
            w.number("summary", "mean", 1.0, level="alpha", level_2=unit)
        w.close()
        keys = pq.read_table(self.dir / "results.parquet").column("key").to_pylist()
        self.assertIn("summary|alpha|a|b|mean", keys)
        self.assertIn("summary|alpha|a\\b|mean", keys)

    def test_a_key_twice_the_cap_and_the_rows_every_model_writes(self):
        w = self.writer()
        self.start(w)
        w.number("summary", "mean", 1.0, level="q")
        w.number("summary", "mean", 2.0, level="q")
        with self.assertRaises(RuntimeError):
            w.flush()
        w.abort()
        self.assertFalse((self.dir / "results.parquet").exists())
        w = self.writer(max_rows=4)
        self.start(w)
        w.number("summary", "mean", 1.0, level="q")
        with self.assertRaises(RuntimeError):
            w.number("summary", "sd", 1.0, level="q")
        w.abort()
        w = self.writer()
        w.text("model", "model", "bayes_model")
        with self.assertRaises(RuntimeError):
            w.close()
        self.assertFalse((self.dir / "results.parquet").exists())

    def test_the_declaration_is_checked_row_by_row(self):
        w = self.writer("sample")
        self.start(w)
        w.number("summary", "bogus", 1.0, level="q")
        with self.assertRaises(RuntimeError):
            w.flush()
        w.abort()
        w = self.writer("sample")
        self.start(w)
        w.number("summary", "mean", 1.0)                       # a summary row needs its level
        with self.assertRaises(RuntimeError):
            w.flush()
        w.abort()


class TestSections(RowsCase):
    def prepared(self, **limits):
        raw = json.loads((GO_TESTDATA / "tasks" / "sample.json").read_text(encoding="utf-8"))
        raw["limits"].update(limits)
        task = bayes_task.parse_task(raw)
        cols = {"col_g": ["s2", "s1", "s3", "s1", "s2", "s3"], "col_x": [1.0, 2.0, 3.0, 4.0, 5.0, 6.5], "col_y": [0.1, 0.2, 0.3, None, 0.5, 0.6]}
        path = str(self.dir / "in.parquet")
        pq.write_table(__import__("pyarrow").table(cols), path)
        facts = bayes_data.prepare(task, path, self.dir / "run")
        layout = bayes_task.build_layout(task, facts.sizes, facts.units)
        return task, facts, layout

    def test_the_model_data_and_quantity_sections_pass_the_declared_check_of_both_variants(self):
        task, facts, layout = self.prepared()
        w = self.writer("sample")
        bayes_rows.model_section(w, task, facts, layout, threads=2, seconds={"warmup": 1.5, "sampling": 2.5, "pass_2": 0.1})
        shown = bayes_rows.quantity_section(w, task, layout)
        bayes_rows.data_section(w, task, facts)
        w.close()
        t = pq.read_table(self.dir / "results.parquet").to_pydict()
        by = dict(zip(t["key"], zip(t["value"], t["text"])))
        self.assertEqual(by["model|||n"][0], 5)
        self.assertEqual(by["model|||n_missing"][0], 1)
        self.assertEqual(by["model|||sampler"][1], "nuts")
        self.assertEqual(by["model|||precision"][1], "8 significant digits")
        self.assertEqual(by["model|||bayes_version"][1], "4")
        self.assertEqual(by["model|||n_quantities"][0], layout.n_quantities)
        self.assertEqual(by["model|||seconds_sampling"][0], 2.5)
        self.assertIn("CmdStan 2.40.0", by["model|||software"][1])
        self.assertEqual(by["quantity|mu_a||label"][1], "Typical level of the groups")
        self.assertEqual(by["quantity|a_g||plate_label"][1], "Group")
        self.assertEqual(by["quantity|a_g||n_quantities"][0], 3)
        self.assertEqual(by["quantity|rho_g||kind"][1], "correlation")
        self.assertEqual([e.stan for e in shown][:5], ["mu_a", "mu_b", "sigma", "tau_a", "tau_b"])
        self.assertEqual(by["data|x||standardized"][1], "yes")
        self.assertAlmostEqual(by["data|x||mean"][0], float(np.mean([1.0, 2.0, 3.0, 5.0, 6.5])))
        self.assertEqual(by["data|g||n_units"][0], 3)
        self.assertEqual(by["data|y||n_missing"][0], 1)
        # the same rows are fine for the prior predictive variant, which declares the same three sections
        w = self.writer("prior_predictive")
        bayes_rows.model_section(w, task, facts, layout, threads=1)
        bayes_rows.quantity_section(w, task, layout)
        bayes_rows.data_section(w, task, facts)
        w.close()

    def test_the_entries_are_capped_and_the_rest_is_a_note(self):
        task, facts, layout = self.prepared(bayes_max_quantity_entries=3)
        w = self.writer("sample")
        bayes_rows.model_section(w, task, facts, layout, threads=1)
        shown = bayes_rows.quantity_section(w, task, layout)
        w.close()
        self.assertEqual(len(shown), 3)
        keys = pq.read_table(self.dir / "results.parquet").column("key").to_pylist()
        self.assertIn("model|||note_quantities_capped", keys)
        said = pq.read_table(self.dir / "results.parquet").to_pydict()
        self.assertRegex(said["text"][said["key"].index("model|||note_quantities_capped")], r"^@bayes_quantities_capped listed=3 total=\d+$")
        self.assertNotIn("quantity|tau_a||label", keys)


if __name__ == "__main__":
    unittest.main()
