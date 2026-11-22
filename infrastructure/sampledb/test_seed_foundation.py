"""The pre-trained forecast's sample flow in `seed_tabularia.py`, on a gateway written here: no container, no numpy.

    python3 -m unittest infrastructure/sampledb/test_seed_foundation.py

What they pin: the flow exists only when the foundation_forecast group is enabled and usable, comes after the other
flows, is never scheduled, asks for the size 28M when the version in use holds it and otherwise the smallest it holds,
crosses the calendar with the channels so that every channel has a row for every day, leaves the days after the data
empty (the future rows), and its node's parameters are accepted by the backend's catalogue.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import seed_tabularia as seed  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
GROUP = {"id": "foundation_forecast", "enabled": True, "usable": True, "ready": True, "sizes": ["5m", "28m"]}
NAME = "Orders by channel: four weeks ahead"


def datasources():
    return {n: {"bucket": "b", "key": f"k/{n}", "name": n, "rows": 10, "columns": [], "id": i}
            for i, n in enumerate(("orders", "order_rows", "products", "price_list", "production_costs", "commercial_costs",
                                   "customers", "sales_network", "crm_activities", "calendar", "regions"), 1)}


class Flow(unittest.TestCase):
    def names(self, groups):
        return [f["name"] for f in seed.flows(datasources(), 99, groups)]

    def flow(self, group=GROUP):
        return next(f for f in seed.flows(datasources(), 99, {"foundation_forecast": group}) if f["name"] == NAME)

    def test_only_with_the_group_enabled(self):
        self.assertNotIn(NAME, self.names(None))
        self.assertNotIn(NAME, self.names({}))
        self.assertNotIn(NAME, self.names({"timeseries": {"id": "timeseries"}}))
        self.assertEqual(self.names({"foundation_forecast": GROUP})[-1], NAME)
        self.assertEqual(self.names({"foundation_forecast": GROUP})[:-1], self.names(None))

    def test_never_scheduled_and_it_writes_its_results(self):
        f = self.flow()
        self.assertIsNone(f["cron"])
        self.assertTrue(f["c"].output_name)
        self.assertEqual(f["group"], GROUP)

    def test_the_size_is_28m_when_present_else_the_smallest(self):
        self.assertEqual(seed.foundation_size({"sizes": ["5m", "28m", "123m"]}), "28m")
        self.assertEqual(seed.foundation_size({"sizes": ["123m", "209m"]}), "123m")
        self.assertEqual(seed.foundation_size({"sizes": ["209m", "123m", "5m"]}), "5m")
        self.assertEqual(seed.foundation_size({"sizes": ["123m"]}), "123m")
        self.assertEqual(seed.foundation_size({"sizes": []}), "28m")
        self.assertEqual(seed.foundation_size({"sizes": None}), "28m")
        self.assertEqual(seed.foundation_size({}), "28m")
        node = next(n for n in self.flow({**GROUP, "sizes": ["123m", "209m"]})["c"].nodes if n["type"] == "model")
        self.assertEqual(node["data"]["params"]["size"], "123m")

    def test_the_calendar_crossed_with_the_channels_is_the_left_side(self):
        f = self.flow()
        nodes = {n["id"]: n for n in f["c"].nodes}
        self.assertEqual(nodes["src-cal"]["data"]["filename"], "calendar")
        self.assertEqual(nodes["x"]["data"]["params"], {"how": "cross"})
        self.assertEqual(nodes["j"]["data"]["params"], {"how": "left", "on": ["data_ordine", "canale"]})
        self.assertEqual(nodes["periodo"]["data"]["params"]["value"], ["2023-07-01", "2026-08-12"])
        edges = {(e["source"], e["target"], e["targetHandle"]) for e in f["c"].edges}
        self.assertIn(("periodo", "x", "left"), edges)
        self.assertIn(("canali", "x", "right"), edges)
        self.assertIn(("x", "j", "left"), edges)
        self.assertIn(("agg", "j", "right"), edges)
        self.assertIn(("zero", "ts", "left"), edges)

    def test_the_days_after_the_data_stay_empty_and_the_earlier_ones_are_zero(self):
        expr = {n["id"]: n for n in self.flow()["c"].nodes}["zero"]["data"]["params"]["columns"][0]
        self.assertEqual(expr["name"], "ordini")
        self.assertIn("data_ordine > DATE '2026-07-15' THEN NULL", expr["expr"])
        self.assertIn("COALESCE(ordini, 0)", expr["expr"])

    def test_the_node_has_the_holidays_known_and_the_horizon_is_the_future_rows(self):
        # with known covariates the horizon is the future rows' count (28 days), so none is given
        p = {n["id"]: n for n in self.flow()["c"].nodes}["ts"]["data"]["params"]
        self.assertEqual((p["series"], p["frequency"], p["holdout"], p["known_covariates"]), ("canale", "day", 28, ["festivo"]))
        self.assertNotIn("horizon", p)

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
        check_params("timestral", node["data"]["params"],
                     {"data_ordine": "Date", "canale": "String", "ordini": "Int64", "festivo": "Boolean"})


if __name__ == "__main__":
    unittest.main()
