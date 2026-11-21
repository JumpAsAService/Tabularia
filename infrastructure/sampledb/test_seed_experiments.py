"""The experiments in `seed_tabularia.py`, on a gateway written here: no container, no numpy.

    python3 -m unittest infrastructure/sampledb/test_seed_experiments.py

What they pin: the four tables are imported as private tables with a description for every column the
schema has, the two flows exist only when the experiments group is enabled and usable by the
administrator, they read the tables' columns, they are never scheduled, and a group with no version
ready yet makes the seed leave them unrun, not fail. (That the nodes' parameters are accepted by the
catalogue is asked of the catalogue itself, when the backend is next to this folder.)
"""
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import seed_tabularia as seed  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
ELEVEN = 11
GROUP = {"id": "experiments", "enabled": True, "usable": True, "ready": True}
TABLES = ("experiments", "experiment_users", "experiment_history", "experiment_daily")


def datasources():
    return {n: {"bucket": "b", "key": f"k/{n}", "name": n, "rows": 10, "columns": [], "id": i}
            for i, n in enumerate(("orders", "order_rows", "products", "price_list", "production_costs", "commercial_costs",
                                   "customers", "sales_network", "crm_activities", "calendar", "regions", *TABLES), 1)}


class Gateway:
    def __init__(self, groups, status=200):
        self.groups, self.status, self.calls = groups, status, []

    def call(self, method, path, body=None, **_):
        self.calls.append((method, path))
        return self.status, ({"disabled_reason": None, "groups": self.groups} if self.status < 400 else "no")


class ExperimentTables(unittest.TestCase):
    def test_the_four_tables_are_private_tables_of_the_experiments_schema(self):
        with open(os.path.join(HERE, "schema_experiments.sql"), encoding="utf-8") as f:
            sql = f.read()
        mine = {name: table for name, conn, table, _ in seed.PRIVATE if name in TABLES}
        self.assertEqual(set(mine), set(TABLES))
        for name, table in mine.items():
            self.assertIn(f"CREATE TABLE analytics.{table} (", sql)
            self.assertTrue(all(c == "ch" for n, c, t, d in seed.PRIVATE if n == name))
            self.assertTrue(all(d for n, c, t, d in seed.PRIVATE if n == name), "described")

    def test_every_column_of_the_schema_is_described(self):
        with open(os.path.join(HERE, "schema_experiments.sql"), encoding="utf-8") as f:
            sql = f.read()
        for name in TABLES:
            table = next(t for n, c, t, d in seed.PRIVATE if n == name)
            body = re.search(rf"CREATE TABLE analytics\.{table} \((.*?)\n\) ENGINE", sql, re.S).group(1)
            columns = [m.group(1) for line in body.splitlines() if (m := re.match(r"\s+(\w+)\s+\S", line.split("--")[0]))]
            self.assertEqual(set(columns), set(seed.COLUMNS[name]), name)


class ExperimentFlows(unittest.TestCase):
    def names(self, groups):
        return [f["name"] for f in seed.flows(datasources(), 99, groups)]

    def test_without_the_group_the_eleven_flows_are_all_there_is(self):
        self.assertEqual(len(self.names(None)), ELEVEN)
        self.assertEqual(len(self.names({})), ELEVEN)
        self.assertEqual(self.names({"boosting": {}}), self.names(None))

    def test_with_the_group_two_flows_come_after_them(self):
        names = self.names({"experiments": GROUP})
        self.assertEqual(names[:ELEVEN], self.names(None))
        self.assertEqual(names[ELEVEN:], ["Experiment: checkout", "Experiment planning"])

    def test_they_are_never_scheduled_and_they_write_their_results(self):
        for f in seed.flows(datasources(), 99, {"experiments": GROUP})[ELEVEN:]:
            self.assertIsNone(f["cron"])
            self.assertTrue(f["c"].output_name)
            self.assertEqual(f["group"], GROUP)

    def test_the_checkout_flow_is_the_one_of_the_specification(self):
        f = seed.flows(datasources(), 99, {"experiments": GROUP})[ELEVEN]
        nodes = {n["id"]: n for n in f["c"].nodes}
        self.assertEqual(nodes["src"]["data"]["filename"], "experiment_users")
        self.assertEqual(nodes["checkout"]["data"]["params"], {"column": "esperimento_id", "operator": "eq", "value": "checkout_2026"})
        p = nodes["ab"]["data"]["params"]
        self.assertEqual((nodes["ab"]["type"], nodes["ab"]["data"]["model"]), ("model", "ab_test"))
        self.assertEqual((p["metric_type"], p["metric"], p["covariate"], p["date"], p["sequential"], p["secondary_metrics"]),
                         ("binary", "convertito", "ricavo_pre_28g", "data_ingresso", True, ["ricavo"]))
        self.assertEqual([(e["source"], e["target"]) for e in f["c"].edges], [("src", "checkout"), ("checkout", "ab"), ("ab", "out")])

    def test_the_planning_flow_reads_the_history(self):
        f = seed.flows(datasources(), 99, {"experiments": GROUP})[ELEVEN + 1]
        nodes = {n["id"]: n for n in f["c"].nodes}
        self.assertEqual(nodes["src"]["data"]["filename"], "experiment_history")
        self.assertEqual(nodes["plan"]["data"]["model"], "ab_plan")
        self.assertEqual(nodes["plan"]["data"]["params"]["date"], "data_prima_visita")

    def test_the_nodes_parameters_are_accepted_by_the_catalogue(self):
        backend = os.path.join(HERE, "..", "..", "backend")
        sys.path.insert(0, backend)
        try:
            from app.engine.models.catalog import check_params
        except Exception:  # no backend (the CI's sampledb image) or its libraries: the backend's own tests ask it
            self.skipTest("the backend's catalogue is not importable here")
        finally:
            sys.path.remove(backend)
        columns = {"esperimento_id": "String", "utente_id": "Int64", "variante": "String", "data_ingresso": "Date", "sessioni": "Int64",
                   "impression": "Int64", "click": "Int64", "convertito": "Boolean", "ricavo": "Float64", "ricavo_pre_28g": "Float64",
                   "sessioni_pre_28g": "Float64", "data_prima_visita": "Date"}
        for f in seed.flows(datasources(), 99, {"experiments": GROUP})[ELEVEN:]:
            (node,) = [n for n in f["c"].nodes if n["type"] == "model"]
            check_params(node["data"]["model"], node["data"]["params"], columns)


class Palette(unittest.TestCase):
    def groups(self, rows, status=200):
        original, seed.gw = seed.gw, Gateway(rows, status)
        self.addCleanup(setattr, seed, "gw", original)
        return seed.model_groups()

    def test_only_a_group_enabled_and_usable_counts(self):
        rows = [GROUP, {"id": "boosting", "enabled": True, "usable": False, "ready": True},
                {"id": "clv", "enabled": False, "usable": False, "ready": False}]
        self.assertEqual(list(self.groups(rows)), ["experiments"])

    def test_a_palette_that_refuses_is_no_group(self):
        self.assertEqual(self.groups([GROUP], status=403), {})


class RunningThem(unittest.TestCase):
    def test_a_group_not_ready_leaves_the_flows_unrun_without_failing(self):
        class Gw:
            def __init__(self): self.calls = []
            def must(self, method, path, body=None, **_): return []
            def call(self, method, path, body=None, **_):
                self.calls.append((method, path))
                return 200, {"run_id": 1, "status": "SUCCESS"}
        original_gw, original_log = seed.gw, seed.log
        seed.gw, seed.log = Gw(), lambda _: None
        self.addCleanup(lambda: (setattr(seed, "gw", original_gw), setattr(seed, "log", original_log)))
        group = {**GROUP, "ready": False}
        made = [{**f, "id": 5} for f in seed.flows(datasources(), 99, {"experiments": group})[ELEVEN:]]
        self.assertEqual(seed.run_once(made, 99), [])
        self.assertEqual(seed.gw.calls, [])


if __name__ == "__main__":
    unittest.main()
