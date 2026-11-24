"""The predictive checks' data (lot P3a): pass A's observations of the checked outcomes and the extension's values of the four runs, on tiny
inputs, and against the golden programs of `bayeschecks` (every `checks_*` case: the data the runner writes has the dimensions the program
declares). Small tables only."""
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE.parents[3] / "common" / "py"), str(HERE.parents[1] / "runner" / "py")]
GO_TESTDATA = HERE.parents[4] / "bayescontract" / "testdata"
GOLDEN = HERE.parents[4] / "bayeschecks" / "testdata" / "golden"

import numpy as np  # noqa: E402
import pyarrow as pa  # noqa: E402
import pyarrow.parquet as pq  # noqa: E402

import bayes_checks  # noqa: E402
import bayes_data  # noqa: E402
import bayes_task  # noqa: E402
from tab_protocol import Refusal  # noqa: E402

CHECKS = {"format": 1, "on": True, "outcomes": 3, "value_bins": 40, "stat_bins": 30, "density_draws": 20, "max_units": 8, "ppc_draws": 1000}
STATS = {"continuous": ["mean", "sd", "min", "max", "q10", "q50", "q90"], "counts": ["mean", "sd", "max", "q50", "q90", "zeros"], "binary": ["mean"]}


def extension_entries(first, kind="continuous", units=False, prior=True, y_kind="real", series=False):
    """The data manifest of a model with one checked outcome `y` (the kinds the golden files hold), the extension after `Mode`."""
    data = [{"stan": "N", "kind": "size", "of": "rows"}]
    if units:
        data += [{"stan": "J_grp", "kind": "size", "of": "grp"},
                 {"stan": "g", "kind": "group_index", "plate": "rows", "column": "grp", "node": "g", "target": "grp"}]
    data.append({"stan": "y", "kind": y_kind, "plate": "rows", "column": "y", "node": "y"} | ({"support": "nonnegative"} if kind == "counts" else
                                                                                                {"support": "binary"} if kind == "binary" else {}))
    if series:
        data += [{"stan": "N_lag", "kind": "size", "of": "Rows_lag"}, {"stan": "Rows_lag", "kind": "rows_subset", "plate": "rows"},
                 {"stan": "Lag1", "kind": "lag", "plate": "rows", "lag": 1}]
    data.append({"stan": "Mode", "kind": "mode"})
    ext = [("Ppc_on", "checks_on", None), ("Ppc_o1_n", "post_count", "y"), ("Ppc_o1_rows", "post_rows", "y")]
    if prior:
        ext += [("Prior_o1_n", "prior_count", "y"), ("Prior_o1_rows", "prior_rows", "y")]
    ext += [("Ppc_o1_m", "replicated_count", "y"), ("Ppc_o1_slots", "bin_slots", "y"), ("Ppc_o1_scale", "bin_scale", "y"),
            ("Ppc_o1_shift", "bin_shift", "y"), ("Ppc_o1_binned", "binned_count", "y")]
    if units:
        ext += [("Ppc_o1_units", "unit_count", "y"), ("Ppc_o1_slot", "unit_slot", "g")]
    ext.append(("Ppc_o1_rep", "replicated_rows", "y"))
    data += [{"stan": s, "kind": k} | ({"node": n} if n else {}) for s, k, n in ext]
    quantities = [{"stan": f"Ppc_o1_{s}", "role": "check", "dims": [{"size": "Ppc_on"}], "check": s, "of": "y"} for s in STATS[kind]]
    return data, quantities


def make_task(data, quantities, checks=CHECKS, series=None, mode="sample", plates=None):
    raw = json.loads((GO_TESTDATA / "tasks" / "sample.json").read_text(encoding="utf-8"))
    raw["bayes"]["data"], raw["bayes"]["quantities"], raw["bayes"]["mapping"] = data, quantities, []
    raw["bayes"]["plates"] = plates or [{"id": "rows", "kind": "rows", "label": "Rows"}] + [
        {"id": e["target"], "kind": "group", "label": e["target"]} for e in data if e.get("kind") == "group_index"]
    raw["bayes"]["checks"] = checks
    if series is not None:
        raw["bayes"]["series"] = series
    return bayes_task.parse_task(raw)


class Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def prepare(self, task, columns, run="sample", batch_rows=4):
        path = self.dir / "input.parquet"
        pq.write_table(pa.table(columns), str(path), row_group_size=3)
        plan = bayes_checks.plan(task)
        work = self.dir / f"run_{run}"
        facts = bayes_data.prepare(task, str(path), work, batch_rows=batch_rows, checks=plan, run=run)
        return plan, facts, json.loads((work / "data.json").read_text(encoding="utf-8"))


class TestObservations(Case):
    GRP = ["b", "a", "c", "a", "b", "a", "c", "a", "d", "b", "a", "e"]
    Y = [0.5, 1.5, -1.0, 2.0, 3.5, 1.0, 0.0, 2.5, -2.0, 4.0, 1.25, 0.75]

    def test_the_statistics_the_bins_and_the_units_of_a_continuous_outcome(self):
        data, quantities = extension_entries(0, units=True)
        task = make_task(data, quantities, checks=CHECKS | {"max_units": 3})
        plan, facts, values = self.prepare(task, {"grp": self.GRP, "y": self.Y})
        o = plan.outcomes[0]
        y = np.array(self.Y)
        self.assertEqual((o.k, o.stan, o.kind, o.n), (1, "y", "continuous", 12))
        want = {"mean": y.mean(), "sd": y.std(ddof=1), "min": y.min(), "max": y.max(), "q10": np.quantile(y, 0.1), "q50": np.median(y),
                "q90": np.quantile(y, 0.9)}
        for name, v in want.items():
            self.assertAlmostEqual(o.observed[name], v, places=12, msg=name)
        # 40 equal bins, the extremes inside, the outer slots empty; the counts are Stan's own expression
        self.assertEqual((o.bins.count, o.bins.slots), (40, 42))
        edges = o.bins.edges()
        self.assertLess(edges[0], y.min())
        self.assertGreater(edges[-1], y.max())
        self.assertEqual(int(o.counts.sum()), 12)
        self.assertEqual((int(o.counts[0]), int(o.counts[-1])), (0, 0))
        slot = np.minimum(np.maximum(np.floor(y * o.bins.scale + o.bins.shift), 1), 42).astype(int)
        np.testing.assert_array_equal(o.counts, np.bincount(slot - 1, minlength=42))
        self.assertEqual(int(o.counts[slot - 1].min()), 1)
        # the checked units: the three with the most rows (a 5, b 3, c 2; d and e one each), the others together after them
        self.assertEqual(o.unit_texts, ["a", "b", "c"])
        self.assertEqual((o.unit_others, o.units), (2, 4))
        self.assertEqual(o.unit_rows.tolist(), [5, 3, 2, 2])
        by = {u: [v for g, v in zip(self.GRP, self.Y) if g == u] for u in "abcde"}
        np.testing.assert_allclose(o.unit_sums, [sum(by["a"]), sum(by["b"]), sum(by["c"]), sum(by["d"]) + sum(by["e"])])
        np.testing.assert_allclose(o.unit_means()[:3], [np.mean(by[u]) for u in "abc"])
        self.assertEqual(o.slots.tolist(), [{"a": 1, "b": 2, "c": 3}.get(g, 4) for g in self.GRP])
        # what was held is gone
        self.assertEqual((o._y, o._codes, o._seen), ([], [], {}))

    def test_a_tie_between_units_goes_to_the_text_order_and_few_units_have_no_others_slot(self):
        data, quantities = extension_entries(0, units=True)
        plan, _, _ = self.prepare(make_task(data, quantities), {"grp": ["z", "y", "z", "y", "x"], "y": [1.0, 2.0, 3.0, 4.0, 5.0]})
        o = plan.outcomes[0]
        self.assertEqual((o.unit_texts, o.unit_others, o.units), (["y", "z", "x"], 0, 3))   # y and z have 2 rows: y first
        self.assertEqual(o.slots.tolist(), [2, 1, 2, 1, 3])

    def test_counts_bins_of_a_whole_width_from_minus_a_half_and_the_zeros(self):
        data, quantities = extension_entries(0, kind="counts", y_kind="int")
        y = [0, 0, 1, 2, 3, 0, 7, 5]
        plan, _, _ = self.prepare(make_task(data, quantities), {"y": y})
        o = plan.outcomes[0]
        self.assertEqual(o.kind, "counts")
        self.assertEqual((o.bins.lo, o.bins.width, o.bins.count), (-0.5, 1.0, 8))      # the values 0…7
        self.assertEqual(o.counts.tolist(), [0, 3, 1, 1, 1, 0, 1, 0, 1, 0])
        self.assertEqual(o.observed["zeros"], 3.0)
        self.assertEqual(o.observed["max"], 7.0)
        self.assertEqual(o.observed["q90"], float(np.quantile(y, 0.9)))
        # a long tail: whole widths, at most 40 bins, the maximum inside
        y = list(range(0, 1000, 7)) + [999]
        data, quantities = extension_entries(0, kind="counts", y_kind="int")
        plan, _, _ = self.prepare(make_task(data, quantities), {"y": y})
        o = plan.outcomes[0]
        self.assertEqual((o.bins.width, o.bins.count), (25.0, 40))
        self.assertEqual((int(o.counts[0]), int(o.counts[-1]), int(o.counts.sum())), (0, 0, len(y)))

    def test_binary_two_bins_and_a_constant_outcome_is_not_a_division_by_zero(self):
        data, quantities = extension_entries(0, kind="binary", y_kind="int")
        plan, _, _ = self.prepare(make_task(data, quantities), {"y": [1, 0, 0, 1, 1]})
        o = plan.outcomes[0]
        self.assertEqual((o.bins.slots, o.counts.tolist(), o.observed), (4, [0, 2, 3, 0], {"mean": 0.6}))
        data, quantities = extension_entries(0)
        plan, _, _ = self.prepare(make_task(data, quantities), {"y": [2.0, 2.0, 2.0]})
        o = plan.outcomes[0]
        self.assertEqual((int(o.counts.sum()), int(o.counts[0]), int(o.counts[-1])), (3, 0, 0))
        self.assertEqual(o.observed["sd"], 0.0)

    def test_a_standardized_outcome_is_observed_as_stan_reads_it(self):
        data, quantities = extension_entries(0)
        next(e for e in data if e["stan"] == "y")["standardize"] = True
        plan, facts, values = self.prepare(make_task(data, quantities), {"y": [1.0, 2.0, 4.0, 8.0]})
        o = plan.outcomes[0]
        self.assertAlmostEqual(o.observed["mean"], 0.0, places=12)
        self.assertAlmostEqual(o.observed["sd"], 1.0, places=12)
        self.assertEqual(values["y"], [(v - 3.75) / np.std([1, 2, 4, 8], ddof=1) for v in [1.0, 2.0, 4.0, 8.0]])

    def test_missing_rows_are_not_observed_and_the_checks_off_observe_nothing(self):
        data, quantities = extension_entries(0)
        plan, facts, values = self.prepare(make_task(data, quantities), {"y": [1.0, None, 3.0, float("nan"), 5.0]})
        self.assertEqual((facts.n, plan.outcomes[0].n, plan.outcomes[0].observed["mean"]), (3, 3, 3.0))
        plan, facts, values = self.prepare(make_task(data, quantities, checks=CHECKS | {"on": False}), {"y": [1.0, 2.0, 3.0]})
        self.assertEqual((plan.observing, plan.outcomes[0].n, plan.outcomes[0].observed), (False, 0, {}))
        self.assertEqual(values["Ppc_on"], 0)

    def test_a_lagged_outcome_is_observed_on_the_rows_that_have_the_lag(self):
        data, quantities = extension_entries(0, series=True)
        y = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0]
        task = make_task(data, quantities, series={"by": None, "time": None})
        plan, facts, values = self.prepare(task, {"y": y})
        o = plan.outcomes[0]
        self.assertEqual((facts.n, facts.series["lag"], o.n), (6, 5, 5))
        self.assertEqual(o.observed["mean"], np.mean(y[1:]))
        _, values = plan.extension("checks", facts)
        self.assertEqual((values["Ppc_o1_n"], values["Ppc_o1_m"], values["Ppc_o1_binned"]), (5, 5, 5))
        self.assertEqual(values["Ppc_o1_rows"].to_list(), [1, 2, 3, 4, 5])      # the positions among the statement's rows


class TestRuns(Case):
    GRP = ["b", "a", "c", "a", "b", "a", "c", "a"]
    Y = [0.5, 1.5, -1.0, 2.0, 3.5, 1.0, 0.0, 2.5]

    def values(self, run, **kw):
        data, quantities = extension_entries(0, units=True, **kw)
        plan, facts, written = self.prepare(make_task(data, quantities), {"grp": self.GRP, "y": self.Y}, run=run)
        return plan, facts, written

    def test_every_run_writes_every_entry_in_the_manifests_order_with_the_invariants(self):
        data, _ = extension_entries(0, units=True)
        order = [e["stan"] for e in data]
        n = len(self.Y)
        for run in bayes_checks.RUNS:
            plan, facts, w = self.values(run)
            self.assertEqual(list(w), order, run)
            on = run in ("checks", "prior")
            o = plan.outcomes[0]
            self.assertEqual(w["Ppc_on"], int(on), run)
            self.assertEqual(w["Ppc_o1_n"], n if run in ("checks", "replicated") else 0)
            self.assertEqual(len(w["Ppc_o1_rows"]), w["Ppc_o1_n"])
            self.assertEqual(w["Prior_o1_n"], n if run == "prior" else 0)
            self.assertEqual(len(w["Prior_o1_rows"]), w["Prior_o1_n"])
            self.assertEqual(w["Ppc_o1_m"], 0 if run == "sample" else n)
            self.assertEqual(w["Ppc_o1_rep"], n if run == "replicated" else 0)
            self.assertEqual(w["Ppc_o1_binned"], n if on else 0)
            self.assertEqual(len(w["Ppc_o1_slot"]), w["Ppc_o1_binned"])
            self.assertEqual(w["Ppc_o1_slots"], 42 if on else 0)
            self.assertEqual((w["Ppc_o1_scale"] != 0.0, w["Ppc_o1_shift"] != 0.0), (on, on))
            self.assertEqual(w["Ppc_o1_units"], 3 if on else 0)
            if on:                                    # Ppc_on 1: at least 2 rows replicated; the slot of every row among the units
                self.assertGreaterEqual(w["Ppc_o1_m"], 2)
                self.assertEqual(sorted(set(w["Ppc_o1_slot"])), [1, 2, 3])
                self.assertEqual(w["Ppc_o1_slot"], [{"a": 1, "b": 2, "c": 3}[g] for g in self.GRP])
                self.assertEqual(w["Ppc_o1_scale"], o.bins.scale)
            else:
                self.assertEqual(w["Ppc_o1_rep"] > 0, run == "replicated")
            if run in ("checks", "replicated"):
                self.assertEqual(w["Ppc_o1_rows"], list(range(1, n + 1)))
            if run == "prior":
                self.assertEqual(w["Prior_o1_rows"], list(range(1, n + 1)))
            # the invariants of P2: binned > 0 only with the checks on, rep > 0 only with them off
            self.assertTrue(w["Ppc_o1_binned"] == 0 or w["Ppc_on"] == 1)
            self.assertTrue(w["Ppc_o1_rep"] == 0 or w["Ppc_on"] == 0)

    def test_the_sizes_are_the_integers_a_dimension_may_name(self):
        plan, facts, _ = self.values("checks")
        sizes, values = plan.extension("checks", facts)
        self.assertEqual({k: sizes[k] for k in ("Ppc_on", "Ppc_o1_slots", "Ppc_o1_units", "Ppc_o1_rep")},
                         {"Ppc_on": 1, "Ppc_o1_slots": 42, "Ppc_o1_units": 3, "Ppc_o1_rep": 0})
        self.assertEqual(facts.sizes["Ppc_o1_slots"], 42)                     # what build_layout expands the checks' columns with
        sizes, _ = plan.extension("sample", facts)
        self.assertEqual((sizes["Ppc_on"], sizes["Ppc_o1_slots"], sizes["Ppc_o1_units"]), (0, 0, 0))

    def test_a_version_without_the_extension_has_nothing(self):
        data = [e for e in extension_entries(0)[0] if not e["stan"].startswith(("Ppc", "Prior"))]
        task = make_task(data, [])
        plan, facts, written = self.prepare(task, {"y": [1.0, 2.0]})
        self.assertEqual((plan.has_extension, plan.outcomes, plan.extension("checks", facts)), (False, [], ({}, {})))
        self.assertNotIn("Ppc_on", written)

    def test_no_prior_predictive_without_prior_entries_and_too_few_rows_refuse(self):
        data, quantities = extension_entries(0, prior=False)
        plan, facts, _ = self.prepare(make_task(data, quantities), {"y": [1.0, 2.0, 3.0]})
        self.assertFalse(plan.has_prior)
        with self.assertRaises(Refusal) as e:
            plan.extension("prior", facts)
        self.assertEqual(e.exception.code, "task_value")
        plan, facts, _ = self.prepare(make_task(data, quantities), {"y": [1.0]})           # one row: sd of one element is an error in Stan
        with self.assertRaises(Refusal) as e:
            plan.extension("checks", facts)
        self.assertEqual(e.exception.code, "data_shape")
        self.assertEqual(plan.extension("sample", facts)[1]["Ppc_o1_m"], 0)               # the sampling run does not need them

    def test_a_run_before_the_observation_is_a_fault(self):
        data, quantities = extension_entries(0)
        plan = bayes_checks.plan(make_task(data, quantities))
        with self.assertRaises(Refusal) as e:
            plan.extension("checks", bayes_data.Facts(n=3))
        self.assertEqual(e.exception.code, "task_value")

    def test_the_replicated_run_does_not_need_the_observations(self):
        data, quantities = extension_entries(0)
        task = make_task(data, quantities, checks=CHECKS | {"on": False})
        plan, facts, w = self.prepare(task, {"y": [1.0, 2.0, 3.0]}, run="replicated")
        self.assertEqual((w["Ppc_on"], w["Ppc_o1_n"], w["Ppc_o1_m"], w["Ppc_o1_rep"], w["Ppc_o1_slots"], w["Ppc_o1_binned"]), (0, 3, 3, 3, 0, 0))


class TestStreams(unittest.TestCase):
    def test_a_long_array_is_written_in_pieces_never_as_a_list(self):
        n = 3 * bayes_checks.PIECE + 7
        s = bayes_checks.Counting(n)
        pieces = list(s.pieces({}))
        self.assertEqual(len(pieces), 4)
        self.assertTrue(all(len(p) < 3 * bayes_checks.PIECE * 8 for p in pieces))
        self.assertEqual(s.to_list()[:3] + s.to_list()[-2:], [1, 2, 3, n - 1, n])
        self.assertEqual(bayes_checks.Counting(0).to_list(), [])
        sl = bayes_checks.Slots(np.array([1, 3, 2, 1], dtype=np.uint16))
        self.assertEqual((sl.length, sl.to_list()), (4, [1, 3, 2, 1]))


# ── the golden programs ───────────────────────────────────────────────────────
DECLARATION = re.compile(r"^\s*(?:array\[(\w+)\]\s+)?(int|real)(?:<[^>]*>)?\s+(\w+);\s*$")


def declared_data(stan_text):
    """The data block of a golden program: [(name, size name or None)] in order."""
    block = stan_text.split("data {", 1)[1].split("\n}", 1)[0]
    out = []
    for line in block.splitlines():
        m = DECLARATION.match(line)
        if m:
            out.append((m.group(3), m.group(1)))
    return out


def golden_input(data, n=30, seed=7):
    """An input table for a manifest: random values for every column bound, nested where a lift says so."""
    rng = np.random.default_rng(seed)
    columns, texts = {}, {}
    for e in data:
        if e["kind"] == "group_index":
            texts[e["target"]] = rng.integers(0, 4, size=n)
    for e in data:
        if e["kind"] == "lift":                       # the outer unit is a function of the inner one
            texts[e["target"]] = texts[e["plate"]] % 2
    for e in data:
        k = e["kind"]
        if e.get("plate") not in (None, "rows") and k in ("real", "int"):      # a value a unit of its group plate
            columns[e["stan"]] = texts[e["plate"]] * 1.5 + 0.25 if k == "real" else texts[e["plate"]]
        elif k == "real":
            columns[e["stan"]] = rng.normal(size=n)
        elif k == "int":
            sup = e.get("support")
            columns[e["stan"]] = rng.integers(0, 2, size=n) if sup == "binary" else rng.poisson(3, size=n) if sup == "nonnegative" else rng.integers(-3, 4, size=n)
        elif k == "category":
            columns[e["stan"]] = [f"lv{v}" for v in rng.integers(0, 3, size=n)]
    for e in data:
        if e["kind"] == "group_index":
            columns[e["stan"]] = [f"{e['target']}{v}" for v in texts[e["target"]]]
    return columns


def golden_task(manifest):
    data = []
    for e in manifest["data"]:
        e = dict(e)
        if e["kind"] in ("real", "int", "group_index", "category"):
            e["column"] = e["stan"]
        data.append(e)
    series = {"by": None, "time": None} if any(e["kind"] in ("rows_subset", "first") for e in data) else None
    return make_task(data, manifest["quantities"], series=series), data


@unittest.skipUnless(GOLDEN.is_dir(), "the Go golden files are not beside the runner")
class TestGoldenPrograms(Case):
    def test_every_run_of_every_golden_program_has_the_dimensions_the_program_declares(self):
        cases = sorted(GOLDEN.glob("checks_*.manifest.json"))
        self.assertGreaterEqual(len(cases), 28)
        runs = 0
        for path in cases:
            name = path.name[:-len(".manifest.json")]
            manifest = json.loads(path.read_text(encoding="utf-8"))
            task, data = golden_task(manifest)
            declared = declared_data((GOLDEN / f"{name}.stan").read_text(encoding="utf-8"))
            names = [e["stan"] for e in data]            # the program may leave out a datum nothing reads (checks_counts' x and y)
            self.assertEqual([n for n in names if n in {d[0] for d in declared}], [d[0] for d in declared], name)
            columns = golden_input(data)
            for run in bayes_checks.RUNS:
                if run == "prior" and not any(e["kind"] == "prior_count" for e in data):
                    continue
                with self.subTest(case=name, run=run):
                    plan, facts, written = self.prepare(task, columns, run=run, batch_rows=7)
                    self.assertEqual(list(written), names)
                    for stan, size in declared:
                        if size is not None:
                            self.assertEqual(len(written[stan]), written[size], f"{stan}[{size}]")
                    runs += 1
                    self.check_invariants(written, plan, run)
        self.assertGreater(runs, 100)

    def check_invariants(self, w, plan, run):
        if "Ppc_on" not in w:
            return
        for o in plan.outcomes:
            p, q = f"Ppc_o{o.k}_", f"Prior_o{o.k}_"
            if w["Ppc_on"]:
                self.assertGreaterEqual(w[p + "m"], 2)
                self.assertEqual(w[p + "slots"], o.bins.slots)
                self.assertEqual(w[p + "binned"], w[p + "m"])
            else:
                self.assertEqual((w[p + "slots"], w[p + "binned"]), (0, 0))
            self.assertEqual(w[p + "m"], w[p + "n"] + w.get(q + "n", 0) if run != "sample" else 0)
            self.assertTrue(w[p + "rep"] == 0 or w["Ppc_on"] == 0)
            if p + "units" in w:
                self.assertEqual(w[p + "units"] > 0, bool(w["Ppc_on"]))
        if run == "prior" and "Prior_first_n" in w:
            self.assertEqual(len(w["Prior_prev"]), w["Prior_next_n"])
            self.assertGreater(w["Prior_first_n"] + w["Prior_next_n"], 0)
        elif "Prior_first_n" in w:
            self.assertEqual((w["Prior_first_n"], w["Prior_next_n"], w["Prior_first"], w["Prior_next"], w["Prior_prev"]), (0, 0, [], [], []))

    def test_the_prior_runs_copy_a_states_rows_from_the_data_file(self):
        manifest = json.loads((GOLDEN / "checks_ts_local_level.manifest.json").read_text(encoding="utf-8"))
        task, data = golden_task(manifest)
        plan, facts, w = self.prepare(task, golden_input(data), run="prior")
        stans = {e["kind"]: e["stan"] for e in data if e["kind"] in ("first", "next", "previous")}
        self.assertEqual((w["Prior_first"], w["Prior_next"], w["Prior_prev"]), (w[stans["first"]], w[stans["next"]], w[stans["previous"]]))
        self.assertGreater(len(w["Prior_next"]), 0)
        # the lagged outcome of a series: its statement's rows are those with every lag
        manifest = json.loads((GOLDEN / "checks_lagged.manifest.json").read_text(encoding="utf-8"))
        task, data = golden_task(manifest)
        plan, facts, w = self.prepare(task, golden_input(data), run="checks")
        self.assertEqual((w["N_lag"], w["Ppc_o1_n"]), (29, 29))


if __name__ == "__main__":
    unittest.main()
