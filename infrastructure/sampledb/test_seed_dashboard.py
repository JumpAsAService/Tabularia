"""The sample dashboard of `seed_tabularia.py`, on a gateway written here: no container needed.

    python3 -m unittest infrastructure/sampledb/test_seed_dashboard.py

What the real gateway accepts and what the numbers are is proved on a running stack; here, what the
seed asks: running it again changes nothing, and a table a flow did not produce stops only the dashboard.
"""
import copy
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import seed_tabularia as seed  # noqa: E402

SAMPLE, TABLES = 1, 3
AGENTS = {"id": 18, "name": "Agent Leaderboard", "project_id": TABLES, "columns": [
    {"name": "area", "dtype": "String"}, {"name": "agente", "dtype": "String"}, {"name": "ricavo", "dtype": "Float64"},
    {"name": "ordini", "dtype": "UInt32"}, {"name": "clienti", "dtype": "UInt32"}, {"name": "ricavo_per_ordine", "dtype": "Float64"}]}
MONTHS = {"id": 12, "name": "Margin by Category and Month", "project_id": TABLES, "columns": [
    {"name": "categoria", "dtype": "String"}, {"name": "anno_mese", "dtype": "Int32"}, {"name": "ricavo", "dtype": "Float64"},
    {"name": "costo", "dtype": "Float64"}, {"name": "margine", "dtype": "Float64"}, {"name": "ordini", "dtype": "UInt32"}]}


class FakeGateway:
    def __init__(self, datasources, dashboards=()):
        self.datasources, self.dashboards, self.writes = list(datasources), list(dashboards), []

    def must(self, method, path, body=None, **_):
        if method == "GET" and path == "/datasources":
            return copy.deepcopy(self.datasources)
        if method == "GET" and path == f"/projects/{SAMPLE}/dashboards":
            return copy.deepcopy(self.dashboards)
        self.writes.append((method, path, body))
        if method == "POST" and path == f"/projects/{SAMPLE}/dashboards":
            self.dashboards.append({"id": 7, "name": body["name"], "project_id": SAMPLE})
            return {"id": 7}
        raise AssertionError(f"the seed asked {method} {path}")


class SalesOverview(unittest.TestCase):
    def seed_on(self, gateway):
        original, seed.gw = seed.gw, gateway
        self.addCleanup(setattr, seed, "gw", original)
        seed.log = lambda _: None
        seed.create_dashboard(SAMPLE, TABLES)

    def created(self, gateway):
        (_, _, body), = gateway.writes
        return body["content"]

    def test_it_is_created_once_and_running_the_seed_again_writes_nothing(self):
        gateway = FakeGateway([AGENTS, MONTHS])
        self.seed_on(gateway)
        self.assertEqual([(m, p) for m, p, _ in gateway.writes], [("POST", f"/projects/{SAMPLE}/dashboards")])
        self.assertEqual(gateway.writes[0][2]["name"], "Sales overview")
        self.seed_on(gateway)
        self.assertEqual(len(gateway.writes), 1)

    def test_one_someone_changed_stays_as_it_is(self):
        gateway = FakeGateway([AGENTS, MONTHS], [{"id": 3, "name": "Sales overview", "project_id": SAMPLE}])
        self.seed_on(gateway)
        self.assertEqual(gateway.writes, [])

    def test_a_table_no_flow_produced_stops_only_the_dashboard(self):
        gateway = FakeGateway([AGENTS])
        self.seed_on(gateway)
        self.assertEqual(gateway.writes, [])

    def test_a_table_of_the_same_name_in_another_folder_is_not_read(self):
        gateway = FakeGateway([AGENTS, {**MONTHS, "project_id": 99}])
        self.seed_on(gateway)
        self.assertEqual(gateway.writes, [])

    def test_the_agents_list_follows_the_area_because_both_filter_one_dataset(self):
        gateway = FakeGateway([AGENTS, MONTHS])
        self.seed_on(gateway)
        content = self.created(gateway)
        names = [p["name"] for p in content["parameters"]]
        self.assertLess(names.index("area"), names.index("agent"))  # a list follows those before it in the form
        by_name = {p["name"]: p for p in content["parameters"]}
        self.assertEqual(by_name["area"]["choices"]["dataset"], by_name["agent"]["choices"]["dataset"])
        filters = {f["value"]: f["column"] for f in next(
            d for d in content["datasets"] if d["name"] == by_name["area"]["choices"]["dataset"])["source"]["filters"]}
        self.assertEqual(filters, {"{area}": "area", "{agent}": "agente"})

    def test_it_reads_the_tables_by_their_ids_and_takes_their_columns_as_this_engine_wrote_them(self):
        duckdb = {**AGENTS, "id": 40, "columns": [{**c, "dtype": "Int64"} if c["dtype"] == "UInt32" else c for c in AGENTS["columns"]]}
        gateway = FakeGateway([duckdb, MONTHS])
        self.seed_on(gateway)
        content = self.created(gateway)
        agents = next(d for d in content["datasets"] if d["source"]["datasource_id"] == 40)
        self.assertEqual({f["name"]: f["type"] for f in agents["fields"]}["ordini"], "integer")

    def test_a_chart_filters_the_others_and_the_tiles_fit_the_grid(self):
        gateway = FakeGateway([AGENTS, MONTHS])
        self.seed_on(gateway)
        content = self.created(gateway)
        kinds = {t["type"] for t in content["tiles"]}
        self.assertLessEqual({"kpi", "chart", "table"}, kinds)
        for t in content["tiles"]:
            g = t["grid"]
            self.assertLessEqual(g["x"] + g["w"], 12, t["id"])
            self.assertIsNot(t.get("filters_others"), False, t["id"])
        boxes = [t["grid"] for t in content["tiles"]]
        for i, a in enumerate(boxes):
            for b in boxes[i + 1:]:
                self.assertFalse(a["x"] < b["x"] + b["w"] and b["x"] < a["x"] + a["w"]
                                 and a["y"] < b["y"] + b["h"] and b["y"] < a["y"] + a["h"], (a, b))


if __name__ == "__main__":
    unittest.main()
