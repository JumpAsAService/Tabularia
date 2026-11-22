"""The time series group's sample flows in `seed_tabularia.py` (the decomposition's, and the volatility and risk nodes' three), on
a gateway written here: no container, no numpy.

    python3 -m unittest infrastructure/sampledb/test_seed_timeseries.py

What they pin: the flows exist only when the time series group is enabled and usable by the administrator, comes
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
RISK = [seed.DURUM_VOLATILITY, seed.DURUM_RISK, seed.ORDERS_RISK]


def datasources():
    return {n: {"bucket": "b", "key": f"k/{n}", "name": n, "rows": 10, "columns": [], "id": i}
            for i, n in enumerate(("orders", "order_rows", "products", "price_list", "production_costs", "commercial_costs",
                                   "customers", "sales_network", "crm_activities", "calendar", "regions", "commodity_prices"), 1)}


class Flow(unittest.TestCase):
    def names(self, groups):
        return [f["name"] for f in seed.flows(datasources(), 99, groups)]

    def test_only_with_the_group_enabled(self):
        self.assertNotIn(NAME, self.names(None))
        self.assertNotIn(NAME, self.names({}))
        self.assertNotIn(NAME, self.names({"boosting": {"id": "boosting"}}))
        self.assertEqual(self.names({"timeseries": GROUP})[-4:], [NAME, *RISK])
        self.assertEqual(self.names({"timeseries": GROUP})[:-4], self.names(None))
        for name in RISK:
            self.assertNotIn(name, self.names(None))
            self.assertNotIn(name, self.names({"foundation_forecast": {"id": "foundation_forecast"}}))

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


class RiskFlows(unittest.TestCase):
    def flow(self, name):
        return next(f for f in seed.flows(datasources(), 99, {"timeseries": GROUP}) if f["name"] == name)

    def model(self, name):
        return next(n for n in self.flow(name)["c"].nodes if n["type"] == "model")

    def test_never_scheduled_and_they_write_their_results(self):
        for name in RISK:
            f = self.flow(name)
            self.assertIsNone(f["cron"], name)
            self.assertEqual(f["c"].output_name, f"{name} results")
            self.assertEqual(f["group"], GROUP)

    def test_the_durum_wheat_flows_read_the_prices_of_one_material_in_order(self):
        for name in (seed.DURUM_VOLATILITY, seed.DURUM_RISK):
            nodes = {n["id"]: n for n in self.flow(name)["c"].nodes}
            self.assertEqual(nodes["src"]["data"]["filename"], "commodity_prices")
            self.assertEqual(nodes["grano"]["data"]["params"], {"column": "materia", "operator": "eq", "value": "grano_duro"})
            self.assertEqual(nodes["num"]["data"]["params"], {"columns": {"prezzo": "float"}})
            self.assertEqual(nodes["ord"]["data"]["params"], {"by": "data", "descending": False})
            edges = [(e["source"], e["target"]) for e in self.flow(name)["c"].edges]
            self.assertEqual(edges[:4], [("src", "grano"), ("grano", "num"), ("num", "ord"), ("ord", edges[3][1])])
            self.assertEqual(edges[-1][1], "out")

    def test_the_volatility_flow_is_a_gjr_with_skewed_t_on_the_prices(self):
        n = self.model(seed.DURUM_VOLATILITY)
        self.assertEqual(n["data"]["model"], "garch")
        p = n["data"]["params"]
        self.assertEqual((p["frequency"], p["input_kind"], p["returns_type"], p["variance_model"], p["distribution"], p["holdout"]),
                         ("index", "prices", "log", "gjr", "sstd", 250))

    def test_the_risk_flow_simulates_the_same_model(self):
        n = self.model(seed.DURUM_RISK)
        self.assertEqual(n["data"]["model"], "risk_simulation")
        p = n["data"]["params"]
        v = self.model(seed.DURUM_VOLATILITY)["data"]["params"]
        self.assertEqual(p["model"], "garch")
        for k in ("time", "value", "frequency", "input_kind", "returns_type", "variance_model", "arch_order", "garch_order", "distribution", "holdout"):
            self.assertEqual(p[k], v[k], k)
        self.assertEqual((p["paths"], p["horizon"], p["position"], p["output"]), (10000, 21, 100000, "risk_summary"))
        self.assertEqual(p["threshold"], seed.DURUM_THRESHOLD)

    def test_the_orders_flow_fills_the_calendar_and_simulates_an_arima_upper_tail(self):
        f = self.flow(seed.ORDERS_RISK)
        nodes = {n["id"]: n for n in f["c"].nodes}
        self.assertEqual(nodes["src-cal"]["data"]["filename"], "calendar")
        self.assertEqual(nodes["j"]["data"]["params"], {"how": "left", "on": ["data_ordine"]})
        self.assertEqual(nodes["zero"]["data"]["params"], {"columns": {"ordini": 0}})
        edges = {(e["source"], e["target"], e["targetHandle"]) for e in f["c"].edges}
        self.assertIn(("periodo", "j", "left"), edges)
        self.assertIn(("agg", "j", "right"), edges)
        self.assertIn(("zero", "risk", "left"), edges)
        p = nodes["risk"]["data"]["params"]
        self.assertEqual((p["model"], p["s"], p["p"], p["d"], p["q"], p["P"], p["D"], p["Q"]), ("arima", 7, 1, 0, 1, 0, 1, 1))
        self.assertEqual((p["aggregate"], p["tail"], p["threshold"], p["horizon"], p["frequency"]), ("sum", "upper", 1000, 30, "day"))
        self.assertNotIn("position", p)         # a position needs a relative change; a sum of orders has no money

    def test_the_catalogue_accepts_the_three(self):
        backend = os.path.join(HERE, "..", "..", "backend")
        sys.path.insert(0, backend)
        try:
            from app.engine.models.catalog import check_params
        except Exception:  # no backend (the CI's sampledb image) or its libraries: the backend's own tests ask it
            self.skipTest("the backend's catalogue is not importable here")
        finally:
            sys.path.remove(backend)
        prices = {"data": "Date", "prezzo": "Float64"}
        check_params("garch", self.model(seed.DURUM_VOLATILITY)["data"]["params"], prices)
        check_params("risk_simulation", self.model(seed.DURUM_RISK)["data"]["params"], prices)
        check_params("risk_simulation", self.model(seed.ORDERS_RISK)["data"]["params"], {"data_ordine": "Date", "ordini": "Int64"})


class Data(unittest.TestCase):
    def test_the_prices_are_a_public_table_with_described_columns(self):
        row = next(r for r in seed.PUBLIC if r[0] == "commodity_prices")
        self.assertEqual((row[1], row[2]), ("pg", "ref.quotazioni_materie_prime"))
        self.assertEqual(set(seed.COLUMNS["commodity_prices"]), {"data", "materia", "prezzo"})


if __name__ == "__main__":
    unittest.main()
