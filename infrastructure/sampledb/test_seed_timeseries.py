"""The decomposition's sample flow in `seed_tabularia.py`, on a gateway written here: no container, no numpy.

    python3 -m unittest infrastructure/sampledb/test_seed_timeseries.py

What they pin: the flow exists only when the time series group is enabled and usable by the administrator, comes
after the other flows, is never scheduled, and it fills the calendar's gaps (a Left join of the calendar and the orders
per day, then Fill nulls with 0) before the node, because the node refuses a series with gaps.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import seed_tabularia as seed  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
GROUP = {"id": "timeseries", "enabled": True, "usable": True, "ready": True}
NAME = "Orders: weekly and yearly seasons"


def datasources():
    return {n: {"bucket": "b", "key": f"k/{n}", "name": n, "rows": 10, "columns": [], "id": i}
            for i, n in enumerate(("orders", "order_rows", "products", "price_list", "production_costs", "commercial_costs",
                                   "customers", "sales_network", "crm_activities", "calendar", "regions"), 1)}


class Flow(unittest.TestCase):
    def names(self, groups):
        return [f["name"] for f in seed.flows(datasources(), 99, groups)]

    def test_only_with_the_group_enabled(self):
        self.assertNotIn(NAME, self.names(None))
        self.assertNotIn(NAME, self.names({}))
        self.assertNotIn(NAME, self.names({"boosting": {"id": "boosting"}}))
        self.assertEqual(self.names({"timeseries": GROUP})[-1], NAME)
        self.assertEqual(self.names({"timeseries": GROUP})[:-1], self.names(None))

    def flow(self):
        return next(f for f in seed.flows(datasources(), 99, {"timeseries": GROUP}) if f["name"] == NAME)

    def test_never_scheduled_and_it_writes_its_results(self):
        f = self.flow()
        self.assertIsNone(f["cron"])
        self.assertTrue(f["c"].output_name)
        self.assertEqual(f["group"], GROUP)

    def test_the_calendar_is_the_left_side_and_the_empty_days_are_zero(self):
        f = self.flow()
        nodes = {n["id"]: n for n in f["c"].nodes}
        self.assertEqual(nodes["src-cal"]["data"]["filename"], "calendar")
        self.assertEqual(nodes["j"]["data"]["params"], {"how": "left", "on": ["data_ordine"]})
        self.assertEqual(nodes["zero"]["data"]["params"], {"columns": {"ordini": 0}})
        edges = {(e["source"], e["target"], e["targetHandle"]) for e in f["c"].edges}
        self.assertIn(("periodo", "j", "left"), edges)
        self.assertIn(("agg", "j", "right"), edges)
        self.assertIn(("j", "zero", "left"), edges)
        self.assertIn(("zero", "dec", "left"), edges)

    def test_the_node_is_mstl_of_a_week_and_a_year(self):
        p = {n["id"]: n for n in self.flow()["c"].nodes}["dec"]["data"]["params"]
        self.assertEqual((p["method"], p["periods"], p["frequency"], p["robust"]), ("mstl", [7, 365], "day", True))

    def test_the_nodes_parameters_are_accepted_by_the_catalogue(self):
        backend = os.path.join(HERE, "..", "..", "backend")
        sys.path.insert(0, backend)
        try:
            from app.engine.models.catalog import check_params
        except Exception:  # no backend (the CI's sampledb image) or its libraries: the backend's own tests ask it
            self.skipTest("the backend's catalogue is not importable here")
        finally:
            sys.path.remove(backend)
        node = next(n for n in self.flow()["c"].nodes if n["type"] == "model")
        check_params("decomposition", node["data"]["params"], {"data_ordine": "Date", "ordini": "Int64"})


if __name__ == "__main__":
    unittest.main()
