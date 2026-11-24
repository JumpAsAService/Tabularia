"""The task reader and the expansion of the quantity manifest (lot K1): the tasks Go writes (bayescontract/testdata/tasks) are read, the
values out of range are refused, and the CSV header the manifest expands to is the one Go and CmdStan 2.40.0 write.

Run: python -I -m unittest discover -s <this folder> (numpy and pyarrow as the recipe pins them; see docs/specs/bayes-contract.md, «Commands»).
"""
import copy
import dataclasses
import json
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE.parents[3] / "common" / "py"), str(HERE.parents[1] / "runner" / "py")]
GO_TESTDATA = HERE.parents[4] / "bayescontract" / "testdata"

import bayes_task  # noqa: E402
from tab_protocol import Refusal  # noqa: E402


def go_task(mode: str) -> dict:
    return json.loads((GO_TESTDATA / "tasks" / f"{mode}.json").read_text(encoding="utf-8"))


def go_header(mode: str) -> dict:
    return json.loads((GO_TESTDATA / "tasks" / f"{mode}.header.json").read_text(encoding="utf-8"))


class TestReadTask(unittest.TestCase):
    def test_the_four_modes_go_writes_are_read(self):
        for mode in bayes_task.MODES:
            task = bayes_task.parse_task(go_task(mode))
            self.assertEqual(task.mode, mode)
        sample = bayes_task.parse_task(go_task("sample"))
        self.assertEqual((sample.output, sample.algorithm), ("summary", "nuts"))
        self.assertEqual(sample.sampler["chains"], 4)
        self.assertEqual(sample.sampler_columns, bayes_task.SAMPLER_COLUMNS_NUTS)
        self.assertEqual(sample.limit("block_values"), 1_048_576)
        self.assertEqual(sample.block_draws(100), 10_485)
        self.assertEqual(sample.block_draws(5_000_000), 1)
        prior = bayes_task.parse_task(go_task("prior_predictive"))
        self.assertEqual((prior.algorithm, prior.sampler["warmup"]), ("fixed_param", 0))
        self.assertEqual(prior.sampler_columns, bayes_task.SAMPLER_COLUMNS_FIXED_PARAM)
        compile_ = bayes_task.parse_task(go_task("compile"))
        self.assertIsNone(compile_.sampler)
        self.assertEqual(compile_.data, [])

    def test_the_contract_the_go_side_declares_is_the_one_read(self):
        contract = json.loads((GO_TESTDATA / "contract.json").read_text(encoding="utf-8"))
        self.assertEqual(list(bayes_task.MODES), contract["modes"])
        self.assertEqual(list(bayes_task.OUTPUTS), contract["outputs"])
        self.assertEqual(list(bayes_task.SAMPLER_COLUMNS_NUTS), contract["sampler_columns"]["sample"])
        self.assertEqual(list(bayes_task.SAMPLER_COLUMNS_FIXED_PARAM), contract["sampler_columns"]["prior_predictive"])
        for limit in contract["limits"]:
            if limit["key"] in bayes_task.DEFAULT_LIMITS:
                self.assertEqual(bayes_task.DEFAULT_LIMITS[limit["key"]], limit["default"], limit["key"])
        s = contract["sampler"]
        self.assertEqual((s["chains"], s["warmup"], s["draws"]), ([1, 8, 4], [0, 10000, 1000], [1, 10000, 1000]))

    def test_a_missing_file_a_protocol_and_a_mode(self):
        with self.assertRaises(Refusal) as e:
            bayes_task.read_task(HERE / "nothing.json")
        self.assertEqual(e.exception.code, "no_task")
        bad = go_task("sample")
        bad["protocol"] = 2
        with self.assertRaises(Refusal) as e:
            bayes_task.parse_task(bad)
        self.assertEqual(e.exception.code, "protocol")
        bad = go_task("sample")
        bad["mode"] = "fit"
        with self.assertRaises(Refusal) as e:
            bayes_task.parse_task(bad)
        self.assertEqual(e.exception.code, "mode")

    def test_every_value_out_of_range_is_task_value(self):
        def broken(mutate):
            t = go_task("sample")
            mutate(t)
            with self.assertRaises(Refusal) as e:
                bayes_task.parse_task(t)
            self.assertEqual(e.exception.code, "task_value")

        broken(lambda t: t["bayes"]["sampler"].update(chains=9))
        broken(lambda t: t["bayes"]["sampler"].update(chains=0))
        broken(lambda t: t["bayes"]["sampler"].update(warmup=10_001))
        broken(lambda t: t["bayes"]["sampler"].update(draws=0))
        broken(lambda t: t["bayes"]["sampler"].update(adapt_delta=1.0))
        broken(lambda t: t["bayes"]["sampler"].update(adapt_delta=0.0))
        broken(lambda t: t["bayes"]["sampler"].update(max_treedepth=16))
        broken(lambda t: t["bayes"]["sampler"].update(seed=2 ** 32))
        broken(lambda t: t["bayes"]["sampler"].update(seed=-1))
        broken(lambda t: t["bayes"]["sampler"].update(refresh=0))
        broken(lambda t: t["bayes"]["sampler"].update(init=-1))
        broken(lambda t: t["bayes"]["sampler"].update(init={"mu": "x"}))
        broken(lambda t: t["bayes"].update(sampler=None))
        broken(lambda t: t["bayes"]["model"].update(binary_sha256="abc"))
        broken(lambda t: t["bayes"]["model"].update(program_sha256=""))
        broken(lambda t: t["bayes"].update(replicated={"draws_per_chain": 5}))      # set, and the output is not replicated
        broken(lambda t: t.update(output="replicated"))                              # replicated, and none set
        broken(lambda t: t.update(output="nope"))
        broken(lambda t: t["bayes"].update(data="x"))
        broken(lambda t: t.update(group="regression"))
        broken(lambda t: t["bayes"].update(series={"by": 3}))
        replicated = go_task("sample")
        replicated.update(output="replicated")
        replicated["bayes"]["replicated"] = {"draws_per_chain": 50}
        self.assertEqual(bayes_task.parse_task(replicated).replicated["draws_per_chain"], 50)
        replicated["bayes"]["replicated"] = {"draws_per_chain": 1001}
        with self.assertRaises(Refusal):
            bayes_task.parse_task(replicated)
        prior = go_task("prior_predictive")
        prior["bayes"]["sampler"]["warmup"] = 10
        with self.assertRaises(Refusal):
            bayes_task.parse_task(prior)
        prior = go_task("prior_predictive")
        prior["output"] = "summary"
        with self.assertRaises(Refusal):
            bayes_task.parse_task(prior)

    def test_init_as_a_radius_or_values_is_accepted(self):
        for init in (0, 0.5, {"mu_a": 1.0, "a_g": [0.1, 0.2]}):
            t = go_task("sample")
            t["bayes"]["sampler"]["init"] = init
            self.assertEqual(bayes_task.parse_task(t).sampler["init"], init)

    def test_the_block_never_passes_a_row_group(self):
        t = go_task("sample")
        t["limits"]["block_values"] = 10 ** 9
        self.assertEqual(bayes_task.parse_task(t).limit("block_values"), bayes_task.MAX_ROW_GROUP)


class TestLayout(unittest.TestCase):
    def layout(self, mode, units=None, sizes=None):
        task = bayes_task.parse_task(go_task(mode))
        h = go_header(mode)
        return task, bayes_task.build_layout(task, sizes or h["sizes"], units)

    def test_the_header_is_the_one_go_and_cmdstan_write(self):
        for mode in ("sample", "prior_predictive"):
            task, layout = self.layout(mode)
            self.assertEqual(layout.header, go_header(mode)["header"], mode)
            self.assertEqual(layout.n_columns, len(layout.header))
            layout.check_header(go_header(mode)["header"])

    def test_a_header_that_differs_is_a_bug_of_tabularia(self):
        _, layout = self.layout("sample")
        wrong = list(layout.header)
        wrong[10], wrong[11] = wrong[11], wrong[10]
        with self.assertRaises(bayes_task.ManifestMismatch) as e:
            layout.check_header(wrong)
        self.assertIn("column 11", str(e.exception))
        with self.assertRaises(bayes_task.ManifestMismatch):
            layout.check_header(wrong[:-1])

    def test_the_draws_keep_the_quantities_and_drop_the_internal_columns(self):
        task, layout = self.layout("sample")
        names = [e.quantity for e in layout.draws]
        self.assertEqual(names[:7], list(bayes_task.SAMPLER_COLUMNS_NUTS))
        self.assertEqual(layout.n_sampler, 7)
        # 5 scalars + 20 + 20 (a_g, b_g) + one element of the 2x2 correlation below the diagonal
        self.assertEqual(layout.n_quantities, 46)
        self.assertEqual(len(layout.draws), 53)
        self.assertFalse(any(n.startswith(("Z_eff", "L_eff")) for n in names))
        self.assertEqual(len(set(names)), len(names))
        self.assertEqual(list(layout.draw_columns[:7]), list(range(7)))
        for col, element in zip(layout.draw_columns, layout.draws):
            stem = element.quantity.split("[")[0]
            self.assertTrue(layout.header[col].startswith(stem), (layout.header[col], element.quantity))
        self.assertEqual(layout.check_columns.size, 0)

    def test_the_names_units_and_indices_of_a_group_a_correlation_and_a_row(self):
        units = {"grp": [f"School {i:02d}" for i in range(1, 21)]}
        task, layout = self.layout("sample", units)
        by = {e.quantity: e for e in layout.draws}
        a3 = by["a_g[School 03]"]
        self.assertEqual((a3.node, a3.unit, a3.index, a3.index_2), ("a_g", "School 03", 3, None))
        self.assertEqual((by["mu_a"].node, by["mu_a"].unit, by["mu_a"].index), ("mu_a", None, None))
        rho = by["rho_g[b_g,a_g]"]
        self.assertEqual((rho.node, rho.unit, rho.index, rho.index_2), ("rho_g", "b_g,a_g", 2, 1))
        self.assertEqual(by["lp__"].node, None)

    def test_the_levels_after_the_reference_and_a_row_plate(self):
        task = bayes_task.parse_task(go_task("prior_predictive"))
        sizes = go_header("prior_predictive")["sizes"]
        layout = bayes_task.build_layout(task, sizes, {"levels:c": ["email", "radio", "tv"], "grp": [f"g{i}" for i in range(20)]})
        by = {e.quantity: e for e in layout.draws}
        self.assertEqual(by["b_c[radio]"].index, 2)
        self.assertEqual(by["b_c[tv]"].index, 3)
        self.assertNotIn("b_c[email]", by)
        self.assertEqual(by["b_c[tv]"].unit, "tv")

    def test_the_prior_draw_replaces_its_quantity_and_the_check_goes_to_the_checks(self):
        task, layout = self.layout("prior_predictive", {"grp": [f"g{i}" for i in range(20)]})
        header = layout.header
        names = [e.quantity for e in layout.draws]
        self.assertEqual(names[:2], ["lp__", "accept_stat__"])
        self.assertEqual(len(layout.draws) - 2, 2 + 4 + 20)
        a1 = layout.draw_columns[names.index("a[g0]")]
        self.assertEqual(header[a1], "Prior_a.1")  # the prior draw's column, not the parameter's own
        self.assertEqual([e.quantity for e in layout.checks], ["Ppc_first_rep"])
        self.assertEqual(header[layout.check_columns[0]], "Ppc_first_rep")
        self.assertEqual(layout.checks[0].node, "first")
        # the order is the manifest's (the quantity's place), whatever the mode
        order = [e.stan for e in layout.entries]
        self.assertEqual(order, ["b_c", "b_x", "mu", "s", "tau", "a"])
        self.assertEqual([e.count for e in layout.entries], [2, 1, 1, 1, 1, 20])

    def test_a_prior_draw_in_the_sample_mode_has_no_column_with_a_dimension_and_is_dropped_without(self):
        task = bayes_task.parse_task(go_task("prior_predictive"))
        sample = dataclasses.replace(task, mode="sample", variant="sample")
        t = copy.deepcopy(sample)
        t.quantities.append({"stan": "Prior_mu", "role": "prior_draw", "dims": [], "of": "mu"})
        layout = bayes_task.build_layout(t, go_header("prior_predictive")["sizes"], None)
        self.assertNotIn("Prior_a.1", layout.header)           # a size by the mode: none in sample
        self.assertIn("Prior_mu", layout.header)               # a single value: its column, read and dropped
        self.assertNotIn("Prior_mu", [e.quantity for e in layout.draws])
        self.assertEqual(layout.header[:7], list(bayes_task.SAMPLER_COLUMNS_NUTS))

    def test_a_row_plate_has_the_kept_rows_as_its_indices(self):
        task = bayes_task.parse_task(go_task("sample"))
        t = copy.deepcopy(task)
        t.quantities.append({"stan": "level", "role": "quantity", "node": "level", "dims": [{"plate": "rows"}]})
        layout = bayes_task.build_layout(t, {"rows": 3, "grp": 20}, None)
        last = layout.draws[-3:]
        self.assertEqual([(e.quantity, e.unit, e.index) for e in last], [("level[1]", None, 1), ("level[2]", None, 2), ("level[3]", None, 3)])

    def test_the_report_order_is_scalars_correlations_then_the_plates(self):
        task, layout = self.layout("sample")
        order = [e.stan for e in bayes_task.report_order(layout.entries)]
        self.assertEqual(order, ["mu_a", "mu_b", "sigma", "tau_a", "tau_b", "rho_g", "a_g", "b_g"])


if __name__ == "__main__":
    unittest.main()
