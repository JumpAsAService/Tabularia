"""The data passes (lot K1): the input is read batch by batch and data.json written in a stream; every refusal of «Refusals» on a tiny input;
the standardization against numpy; the units in text order; lags and subsets; NULLs and non-finite values counted. Small tables only."""
import copy
import json
import math
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE.parents[3] / "common" / "py"), str(HERE.parent / "py")]
GO_TESTDATA = HERE.parents[4] / "bayescontract" / "testdata"

import numpy as np  # noqa: E402
import pyarrow as pa  # noqa: E402
import pyarrow.parquet as pq  # noqa: E402

import bayes_data  # noqa: E402
import bayes_task  # noqa: E402
from tab_protocol import Refusal  # noqa: E402

PLATES = [{"id": "rows", "kind": "rows", "label": "Rows"}, {"id": "school", "kind": "group", "label": "School"},
          {"id": "district", "kind": "group", "label": "District"}, {"id": "levels:ch", "kind": "levels", "label": "Channel"}]
HIER = [
    {"stan": "N", "kind": "size", "of": "rows"}, {"stan": "J_school", "kind": "size", "of": "school"},
    {"stan": "J_district", "kind": "size", "of": "district"}, {"stan": "K_ch", "kind": "size", "of": "levels:ch"},
    {"stan": "ch", "kind": "category", "plate": "rows", "column": "channel", "node": "ch"},
    {"stan": "sc", "kind": "group_index", "plate": "rows", "column": "school", "node": "sc", "target": "school"},
    {"stan": "di", "kind": "group_index", "plate": "rows", "column": "district", "node": "di", "target": "district"},
    {"stan": "x", "kind": "real", "plate": "rows", "column": "x", "node": "x", "standardize": True},
    {"stan": "y", "kind": "real", "plate": "rows", "column": "y", "node": "y"},
    {"stan": "visits", "kind": "int", "plate": "rows", "column": "visits", "node": "visits", "support": "nonnegative"},
    {"stan": "income", "kind": "real", "plate": "school", "column": "income", "node": "income", "standardize": True},
    {"stan": "Up_sc_district", "kind": "lift", "plate": "school", "node": "sc", "target": "district"},
    {"stan": "Mode", "kind": "mode"}]

SCHOOLS = ["b", "a", "c", "a", "b", "c", "c", "a", "b", "d"]
INPUT = {
    "school": SCHOOLS,
    "district": [{"a": "D2", "b": "D1", "c": "D1", "d": "D2"}[s] for s in SCHOOLS],
    "channel": ["tv", "radio", "email", "tv", "tv", "radio", "email", "tv", "radio", "email"],
    "x": [1.0, 2.0, 3.5, 4.0, 5.0, 6.5, 7.0, 8.0, 9.5, 10.0],
    "y": [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0],
    "visits": [0, 1, 2, 3, 4, 5, 6, 7, 8, 9],
    "income": [{"a": 10.0, "b": 20.0, "c": 40.0, "d": 80.0}[s] for s in SCHOOLS],
}


def make_task(data, plates=PLATES, series=None, limits=None, mode="sample"):
    raw = json.loads((GO_TESTDATA / "tasks" / "sample.json").read_text(encoding="utf-8"))
    raw["bayes"]["data"], raw["bayes"]["plates"], raw["bayes"]["quantities"], raw["bayes"]["mapping"] = data, plates, [], []
    if series is not None:
        raw["bayes"]["series"] = series
    raw["limits"].update(limits or {})
    return bayes_task.parse_task(raw)


class DataCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def write_input(self, columns, name="input.parquet", schema=None):
        path = self.dir / name
        pq.write_table(pa.table(columns, schema=schema), str(path), row_group_size=3)
        return str(path)

    def run_prepare(self, task, columns, batch_rows=4, **kw):
        path = self.write_input(columns)
        work = self.dir / "run"
        facts = bayes_data.prepare(task, path, work, batch_rows=batch_rows, **kw)
        return facts, json.loads((work / "data.json").read_text(encoding="utf-8")), work

    def refusal(self, task, columns, code, batch_rows=4):
        with self.assertRaises(Refusal) as e:
            self.run_prepare(task, columns, batch_rows)
        self.assertEqual(e.exception.code, code)
        return e.exception


class TestHierarchicalData(DataCase):
    def test_the_data_file_the_units_and_the_standardization(self):
        task = make_task(HIER)
        facts, data, work = self.run_prepare(task, INPUT)
        self.assertEqual(facts.n, 10)
        self.assertEqual(facts.units["school"], ["a", "b", "c", "d"])             # the units in text order
        self.assertEqual(facts.units["district"], ["D1", "D2"])
        self.assertEqual(facts.units["levels:ch"], ["email", "radio", "tv"])      # the first is the reference
        self.assertEqual(data["N"], 10)
        self.assertEqual((data["J_school"], data["J_district"], data["K_ch"]), (4, 2, 3))
        self.assertEqual(data["sc"], [{"a": 1, "b": 2, "c": 3, "d": 4}[s] for s in SCHOOLS])
        self.assertEqual(data["ch"], [{"email": 1, "radio": 2, "tv": 3}[c] for c in INPUT["channel"]])
        self.assertEqual(data["visits"], list(range(10)))
        self.assertEqual(data["y"], INPUT["y"])
        self.assertEqual(data["Mode"], 0)
        x = np.array(INPUT["x"])
        self.assertAlmostEqual(facts.standardized["x"]["mean"], x.mean(), places=12)
        self.assertAlmostEqual(facts.standardized["x"]["sd"], x.std(ddof=1), places=12)
        np.testing.assert_allclose(data["x"], (x - x.mean()) / x.std(ddof=1), rtol=1e-12)
        # a column of a group plate: one value a unit, in the units' order, standardized over the units
        income = np.array([10.0, 20.0, 40.0, 80.0])
        np.testing.assert_allclose(data["income"], (income - income.mean()) / income.std(ddof=1), rtol=1e-12)
        # a lift: for each school, its district's index
        self.assertEqual(data["Up_sc_district"], [2, 1, 1, 2])                    # a→D2, b→D1, c→D1, d→D2
        self.assertEqual(list(data), [e["stan"] for e in HIER])                   # the manifest's order
        self.assertEqual(facts.sizes["rows"], 10)
        self.assertEqual((facts.sizes["school"], facts.sizes["levels:ch"], facts.sizes["K_ch"]), (4, 3, 3))
        self.assertFalse((work / "data").exists())                                # the parts are deleted

    def test_every_batch_size_gives_the_same_data(self):
        # the same bytes for the same batch size (a Job's is one constant); across sizes the accumulators merge in another order, so the
        # standardized values may differ in their last bits and nothing else may
        task = make_task(HIER)
        runs = []
        for rows in (1, 3, 4, 10, 65_536):
            _, data, work = self.run_prepare(task, INPUT, rows)
            runs.append(data)
            first = (work / "data.json").read_bytes()
            import shutil
            shutil.rmtree(work)
            self.run_prepare(task, INPUT, rows)
            self.assertEqual((work / "data.json").read_bytes(), first)
            shutil.rmtree(work)
        for data in runs[1:]:
            self.assertEqual(list(data), list(runs[0]))
            for key, value in data.items():
                np.testing.assert_allclose(value, runs[0][key], rtol=1e-12, err_msg=key)

    def test_the_mode_flag_of_the_prior_predictive(self):
        task = make_task(HIER)
        task = type(task)(**{**task.__dict__, "mode": "prior_predictive"})
        _, data, _ = self.run_prepare(task, INPUT)
        self.assertEqual(data["Mode"], 1)

    def test_the_input_is_never_read_whole(self):
        task = make_task(HIER)
        with mock.patch.object(pq, "read_table", side_effect=AssertionError("read_table")), \
                mock.patch.object(pq.ParquetFile, "read", side_effect=AssertionError("ParquetFile.read")), \
                mock.patch.object(pq.ParquetFile, "read_row_groups", side_effect=AssertionError("read_row_groups")):
            self.run_prepare(task, INPUT, 2)
        seen = []
        real = pq.ParquetFile.iter_batches

        def spy(self, batch_size=None, **kw):
            for b in real(self, batch_size=batch_size, **kw):
                seen.append(b.num_rows)
                yield b

        with mock.patch.object(pq.ParquetFile, "iter_batches", spy):
            self.run_prepare(task, INPUT, 2)
        self.assertTrue(seen and max(seen) <= 2)
        self.assertEqual(sum(seen), 20)                                           # two passes over ten rows

    def test_nulls_nans_and_infinities_leave_rows_out_and_are_counted(self):
        columns = copy.deepcopy(INPUT)
        columns["x"][1] = None                  # a NULL
        columns["x"][2] = float("nan")          # a NaN
        columns["y"][3] = float("inf")          # an infinity
        columns["school"][4] = None             # a missing text
        columns["y"][5] = None
        columns["x"][5] = float("-inf")         # a row with both
        facts, data, _ = self.run_prepare(make_task(HIER[:9] + [HIER[-1]], plates=PLATES[:1] + [PLATES[1]] + [PLATES[3]]), columns)
        self.assertEqual(facts.n, 5)
        self.assertEqual(len(data["x"]), 5)
        self.assertEqual((facts.missing["x"], facts.not_finite["x"]), (2, 1))
        self.assertEqual((facts.missing["y"], facts.not_finite["y"]), (1, 1))
        self.assertEqual((facts.missing["school"], facts.not_finite["school"]), (1, 0))
        self.assertEqual(facts.rows_missing, 4)          # rows 2, 3, 5 and 6
        self.assertEqual(facts.rows_not_finite, 1)       # row 4 alone has an infinity
        self.assertEqual(facts.n + facts.rows_missing + facts.rows_not_finite, facts.n_input)
        # the standardization rests on the kept rows alone
        kept_x = np.array([INPUT["x"][i] for i in (0, 6, 7, 8, 9)])
        np.testing.assert_allclose(data["x"], (kept_x - kept_x.mean()) / kept_x.std(ddof=1), rtol=1e-12)

    def test_what_is_left_after_the_missing_values(self):
        columns = copy.deepcopy(INPUT)
        columns["x"] = [None] * 10
        e = self.refusal(make_task(HIER), columns, "data_empty")
        self.assertIn("10 rows have a missing value", e.message)


class TestRefusals(DataCase):
    def test_a_count_with_a_value_that_does_not_fit(self):
        for visits, what in (([0, 1, 2.5, 3, 4, 5, 6, 7, 8, 9], "row 3 holds 2.5"), ([0, 1, 2, -3, 4, 5, 6, 7, 8, 9], "row 4 holds -3")):
            columns = {**INPUT, "visits": visits}
            e = self.refusal(make_task(HIER), columns, "data_shape")
            self.assertIn(what, e.message)
            self.assertIn("`visits`", e.message)

    def test_a_binary_column_and_a_text_where_a_number_is_bound(self):
        entry = {"stan": "z", "kind": "int", "plate": "rows", "column": "z", "node": "z", "support": "binary"}
        task = make_task([HIER[0], entry, HIER[-1]], plates=PLATES[:1])
        e = self.refusal(task, {"z": [0, 1, 1, 2]}, "data_shape")
        self.assertIn("0 or 1", e.message)
        self.refusal(make_task(HIER), {**INPUT, "x": [str(v) for v in INPUT["x"]]}, "data_shape")

    def test_a_standardized_predictor_with_no_spread(self):
        e = self.refusal(make_task(HIER), {**INPUT, "x": [3.0] * 10}, "data_constant")
        self.assertIn("`x` has the same value on every row", e.message)

    def test_a_column_of_a_group_plate_that_varies_within_a_unit(self):
        income = list(INPUT["income"])
        income[7] = 11.0          # the unit «a» seen twice, in two batches
        e = self.refusal(make_task(HIER), {**INPUT, "income": income}, "data_unit_varies")
        self.assertIn("`income` changes within the unit «a» of School", e.message)

    def test_a_unit_of_a_nested_plate_in_two_parents(self):
        district = list(INPUT["district"])
        district[3] = "D1"        # the school «a» in D2 and in D1
        e = self.refusal(make_task(HIER), {**INPUT, "district": district}, "data_nesting")
        self.assertIn("«a»", e.message)

    def test_too_many_units_the_moment_one_passes_the_limit(self):
        e = self.refusal(make_task(HIER, limits={"max_units": 3}), INPUT, "too_many_units")
        self.assertIn("`school` has more than 3 values", e.message)
        self.assertEqual(e.params["max_units"], 3)
        self.refusal(make_task(HIER, limits={"max_units": 2}), {**INPUT, "school": SCHOOLS[:10], "income": INPUT["income"]}, "too_many_units")

    def test_a_refusal_leaves_no_partial_data_file_from_pass_a(self):
        with self.assertRaises(Refusal):
            self.run_prepare(make_task(HIER), {**INPUT, "x": [3.0] * 10})
        self.assertFalse((self.dir / "run" / "data.json").exists())


SERIES = [
    {"stan": "N", "kind": "size", "of": "rows"}, {"stan": "J_s", "kind": "size", "of": "s"},
    {"stan": "N_lag", "kind": "size", "of": "Rows_lag"}, {"stan": "N_first", "kind": "size", "of": "Rows_first"},
    {"stan": "N_next", "kind": "size", "of": "Rows_next"},
    {"stan": "s", "kind": "group_index", "plate": "rows", "column": "s", "node": "s", "target": "s"},
    {"stan": "y", "kind": "real", "plate": "rows", "column": "y", "node": "y"},
    {"stan": "Rows_lag", "kind": "rows_subset", "plate": "rows"}, {"stan": "Lag1", "kind": "lag", "plate": "rows", "lag": 1},
    {"stan": "Lag2", "kind": "lag", "plate": "rows", "lag": 2},
    {"stan": "Rows_first", "kind": "first", "plate": "rows"}, {"stan": "Rows_next", "kind": "next", "plate": "rows"},
    {"stan": "Prev", "kind": "previous", "plate": "rows"}, {"stan": "Mode", "kind": "mode"}]
SPLATES = [PLATES[0], {"id": "s", "kind": "group", "label": "Series"}]


class TestSeries(DataCase):
    columns = {"s": ["A", "A", "A", "A", "B", "B", "B"], "t": [1, 2, 3, 4, 1, 2, 3], "y": [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0]}

    def task(self):
        return make_task(SERIES, SPLATES, series={"by": "s", "time": "t"})

    def test_the_lags_the_subsets_and_the_states_arrays_whatever_the_batches(self):
        for rows in (1, 2, 3, 7, 100):
            facts, data, work = self.run_prepare(self.task(), self.columns, rows)
            self.assertEqual((data["N_lag"], data["N_first"], data["N_next"]), (3, 2, 5), rows)
            self.assertEqual(data["Rows_lag"], [3, 4, 7], rows)
            self.assertEqual(data["Lag1"], [2, 3, 6], rows)
            self.assertEqual(data["Lag2"], [1, 2, 5], rows)
            self.assertEqual(data["Rows_first"], [1, 5], rows)
            self.assertEqual(data["Rows_next"], [2, 3, 4, 6, 7], rows)
            self.assertEqual(data["Prev"], [1, 2, 3, 5, 6], rows)
            self.assertEqual(facts.series, {"first": 2, "next": 5, "lag": 3, "max_lag": 2})
            import shutil
            shutil.rmtree(work)

    def test_rows_left_out_are_not_counted_in_the_rows_numbers(self):
        columns = {**self.columns, "y": [1.0, None, 3.0, 4.0, 5.0, 6.0, 7.0]}      # the second row of A is left out
        _, data, _ = self.run_prepare(self.task(), columns, 2)
        self.assertEqual(data["N"], 6)
        self.assertEqual(data["Rows_first"], [1, 4])
        self.assertEqual(data["Rows_next"], [2, 3, 5, 6])
        self.assertEqual(data["Rows_lag"], [3, 6])        # A has three kept rows, B three: the third of each
        self.assertEqual(data["Lag1"], [2, 5])

    def test_without_a_series_description_there_is_one_unit_and_no_time_check(self):
        task = make_task(SERIES, SPLATES)
        _, data, _ = self.run_prepare(task, {**self.columns, "t": [3, 1, 2, 5, 4, 1, 1]}, 3)
        self.assertEqual(data["Rows_first"], [1])
        self.assertEqual(data["Rows_next"], [2, 3, 4, 5, 6, 7])
        self.assertEqual(data["Rows_lag"], [3, 4, 5, 6, 7])

    def test_a_unit_that_comes_back_is_not_in_order(self):
        e = self.refusal(self.task(), {**self.columns, "s": ["A", "A", "B", "B", "A", "B", "B"]}, "data_order")
        self.assertIn("«A» comes back", e.message)

    def test_two_rows_at_one_time_and_a_time_that_goes_back(self):
        e = self.refusal(self.task(), {**self.columns, "t": [1, 2, 2, 4, 1, 2, 3]}, "data_time_twice")
        self.assertIn("«A»", e.message)
        e = self.refusal(self.task(), {**self.columns, "t": [1, 3, 2, 4, 1, 2, 3]}, "data_order")
        self.assertIn("time order", e.message)
        # across two batches
        self.refusal(self.task(), {**self.columns, "t": [1, 2, 3, 4, 1, 2, 2]}, "data_time_twice", batch_rows=1)

    def test_timestamps_are_a_time_too(self):
        import datetime as dt
        stamps = [dt.datetime(2026, 1, d) for d in (1, 2, 3, 4, 1, 2, 3)]
        _, data, _ = self.run_prepare(self.task(), {**self.columns, "t": pa.array(stamps, pa.timestamp("us"))}, 3)
        self.assertEqual(data["Rows_first"], [1, 5])


class TestExtensionData(DataCase):
    entries = [HIER[0], HIER[8], {"stan": "Ppc_bins", "kind": "bins"}, {"stan": "Ppc_edges", "kind": "bin_edges"}, HIER[-1]]
    columns = {"y": [1.0, 2.0, 3.0]}

    def test_an_extension_variable_comes_from_the_caller(self):
        task = make_task(self.entries, plates=PLATES[:1])
        facts, data, _ = self.run_prepare(task, self.columns, extension_sizes={"Ppc_bins": 2},
                                          extension_values={"Ppc_bins": 2, "Ppc_edges": [0.0, 1.5, 3.0]})
        self.assertEqual((data["N"], data["y"], data["Ppc_bins"], data["Ppc_edges"]), (3, [1.0, 2.0, 3.0], 2, [0.0, 1.5, 3.0]))
        self.assertEqual(facts.sizes["Ppc_bins"], 2)

    def test_an_unknown_kind_is_a_fault_of_tabularia(self):
        task = make_task(self.entries, plates=PLATES[:1])
        e = self.refusal(task, self.columns, "task_value")
        self.assertIn("Ppc_bins", e.message)

    def test_numbers_a_data_file_can_hold(self):
        self.assertEqual(bayes_data._array_json([0.0, 1.5]), "[0.0,1.5]")
        self.assertEqual(bayes_data._scalar_json(3), "3")
        with self.assertRaises(ValueError):
            bayes_data._scalar_json(math.nan)


if __name__ == "__main__":
    unittest.main()
