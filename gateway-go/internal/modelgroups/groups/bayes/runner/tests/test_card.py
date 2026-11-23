"""The card of a run (lot K1): the common keys, the object `bayes` of `bayescontract.Card`, no draw, the cuts above max_card_bytes."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE.parents[3] / "common" / "py"), str(HERE.parent / "py")]
GO_TESTDATA = HERE.parents[4] / "bayescontract" / "testdata"

import pyarrow as pa  # noqa: E402
import pyarrow.parquet as pq  # noqa: E402

import bayes_card  # noqa: E402
import bayes_data  # noqa: E402
import bayes_rows  # noqa: E402
import bayes_task  # noqa: E402

# the keys of bayescontract.Card (gateway-go/internal/bayescontract/card.go)
CARD_KEYS = {"model", "program", "binary", "mapping", "data", "units", "sampler", "facts", "summary"}


class TestCard(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        raw = json.loads((GO_TESTDATA / "tasks" / "sample.json").read_text(encoding="utf-8"))
        self.task = bayes_task.parse_task(raw)
        cols = {"col_g": ["s2", "s1", "s3", "s1", "s2", "s3"], "col_x": [1.0, 2.0, 3.0, 4.0, 5.0, 6.5], "col_y": [0.1, 0.2, 0.3, 0.4, 0.5, 0.6]}
        pq.write_table(pa.table(cols), str(self.dir / "in.parquet"))
        self.facts = bayes_data.prepare(self.task, str(self.dir / "in.parquet"), self.dir / "run")
        self.layout = bayes_task.build_layout(self.task, self.facts.sizes, self.facts.units)
        self.summary = [{"quantity": "mu_a", "mean": 3.1, "sd": 0.4, "q5": 2.4, "q50": 3.1, "q95": 3.8, "rhat": 1.001, "ess_bulk": 3510.0,
                         "ess_tail": 2980.0}]

    def tearDown(self):
        self.tmp.cleanup()

    def card(self, **kw):
        return bayes_card.build(self.task, self.facts, self.layout, {"divergences": 2, "treedepth_hits": 0, "e_bfmi_min": 0.93}, self.summary,
                                program_bytes=2314, image="sha256:abc", **kw)

    def test_the_common_keys_and_the_object_bayes(self):
        c = self.card()
        for key in ("format", "format_version", "model", "variant", "group", "group_version", "recipe_hash", "runner_hash", "software",
                    "created_at", "params", "inputs"):
            self.assertIn(key, c)
        self.assertEqual((c["format"], c["model"], c["group"], c["variant"]), ("tabularia-model", "bayes_model", "bayes", "sample"))
        self.assertEqual(set(c["bayes"]), CARD_KEYS)
        b = c["bayes"]
        self.assertEqual(b["model"], {"id": 17, "version": 4, "semantic_hash": self.task.model["semantic_hash"], "codegen_version": 1})
        self.assertEqual(b["program"], {"sha256": self.task.model["program_sha256"], "bytes": 2314})
        self.assertEqual(b["binary"], {"sha256": self.task.model["binary_sha256"], "cmdstan": "2.40.0", "image": "sha256:abc"})
        self.assertEqual(b["facts"], {"divergences": 2, "treedepth_hits": 0, "e_bfmi_min": 0.93})
        self.assertEqual(b["sampler"]["algorithm"], "nuts")
        self.assertEqual((b["sampler"]["chains"], b["sampler"]["seed"]), (4, 20261123))
        self.assertEqual(b["units"]["grp"], {"count": 3, "labels": ["s1", "s2", "s3"]})
        self.assertNotIn("rows", b["units"])
        by = {m["stan"]: m for m in b["mapping"]}
        self.assertEqual(by["mu_a"], {"stan": "mu_a", "node_id": "mua", "name": "mu_a", "label": "Typical level of the groups", "kind": "parameter"})
        self.assertEqual(by["a_g"]["kind"], "effect")
        self.assertEqual(by["rho_g"]["kind"], "correlation")
        self.assertNotIn("Z_eff", by)                                  # an internal variable is no quantity
        kinds = json.loads((GO_TESTDATA / "contract.json").read_text(encoding="utf-8"))["summary_kinds"]
        self.assertTrue(all(m["kind"] in kinds for m in b["mapping"]))
        self.assertEqual([d["stan"] for d in b["data"]], ["x", "y"])
        self.assertEqual(b["data"][0]["standardized"], True)
        self.assertAlmostEqual(b["data"][0]["mean"], 21.5 / 6)
        self.assertEqual(b["data"][1], {"stan": "y", "column": "col_y", "standardized": False})
        names = {i["name"]: i for i in c["inputs"]}
        self.assertEqual(names["col_g"]["levels"], ["s1", "s2", "s3"])
        self.assertEqual(names["col_x"]["type"], "number")

    def test_no_draw_is_ever_in_the_card(self):
        text = bayes_card.fit(self.task, self.card())
        self.assertIsNotNone(text)
        c = json.loads(text)

        def keys(x):
            if isinstance(x, dict):
                for k, v in x.items():
                    yield k
                    yield from keys(v)
            elif isinstance(x, list):
                for v in x:
                    yield from keys(v)

        self.assertFalse({"draw", "chain", "value", "values"} & set(keys(c)))

    def test_above_the_limit_the_summary_goes_first_then_the_labels_then_the_card(self):
        c = self.card()
        full = len(bayes_card.tab_card.card_text(c).encode("utf-8"))
        text = bayes_card.fit(self.task, c)
        self.assertEqual(len(json.loads(text)["bayes"]["summary"]), 1)
        # a limit just under the full card: the summary is cut, the labels stay
        task = bayes_task.parse_task({**self.task.raw, "limits": {**self.task.raw["limits"], "max_card_bytes": full - 50}})
        cut = json.loads(bayes_card.fit(task, self.card()))
        self.assertEqual(cut["bayes"]["summary"], [])
        self.assertEqual(cut["bayes"]["units"]["grp"]["labels"], ["s1", "s2", "s3"])
        # lower: the labels go, their counts stay
        task = bayes_task.parse_task({**self.task.raw, "limits": {**self.task.raw["limits"], "max_card_bytes": len(bayes_card.tab_card.card_text(cut).encode("utf-8")) - 10}})
        cut2 = json.loads(bayes_card.fit(task, self.card()))
        self.assertEqual(cut2["bayes"]["units"]["grp"], {"count": 3, "labels": []})
        # lower still: no card, and the note of the models spec
        task = bayes_task.parse_task({**self.task.raw, "limits": {**self.task.raw["limits"], "max_card_bytes": 500}})
        writer = bayes_rows.ResultsWriter(str(self.dir / "r.parquet"))
        self.assertIsNone(bayes_card.fit(task, self.card(), writer))
        self.assertEqual(writer.rows.key, ["model|||note_card_too_large"])
        writer.abort()

    def test_model_json_and_mlmodel_are_written_under_the_artifacts_prefix(self):
        c = self.card()
        text = bayes_card.fit(self.task, c)
        with mock.patch.dict(os.environ, {"TABULARIA_ARTIFACTS": str(self.dir / "artifacts")}):
            bayes_card.write(self.task, c, text, run_id="r1")
        self.assertEqual(json.loads((self.dir / "artifacts" / "model.json").read_text(encoding="utf-8")), json.loads(text))
        ml = (self.dir / "artifacts" / "MLmodel").read_text(encoding="utf-8")
        self.assertIn("model: bayes_model", ml)
        self.assertIn("group: bayes", ml)
        self.assertIn("run_id: r1", ml)
        # a node that does not log to MLflow writes the card alone
        task = bayes_task.parse_task({**self.task.raw, "mlflow": False})
        with mock.patch.dict(os.environ, {"TABULARIA_ARTIFACTS": str(self.dir / "other")}):
            bayes_card.write(task, c, text)
        self.assertTrue((self.dir / "other" / "model.json").exists())
        self.assertFalse((self.dir / "other" / "MLmodel").exists())


if __name__ == "__main__":
    unittest.main()
