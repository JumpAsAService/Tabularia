"""The sample dashboard and reports of `seed_tabularia.py`, on a gateway written here: no container needed.

    python3 -m unittest infrastructure/sampledb/test_seed_dashboard.py

What the real gateway accepts is asked of its own code by gateway/tests/test_sample_reports.py and
backend/tests/test_sample_reports.py, and what the numbers are is proved on a running stack; here, what
the seed asks: running it again changes nothing, and a table a flow did not produce stops only what reads it.
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
    {"name": "costo", "dtype": "Float64"}, {"name": "margine", "dtype": "Float64"}, {"name": "chili", "dtype": "Float64"},
    {"name": "ordini", "dtype": "UInt32"}, {"name": "margine_pct", "dtype": "Float64"}]}
CRM = {"id": 15, "name": "CRM Workload by Agent and Contact Type", "project_id": TABLES, "columns": [
    {"name": "area", "dtype": "String"}, {"name": "agente", "dtype": "String"}, {"name": "email", "dtype": "UInt32"},
    {"name": "reclamo", "dtype": "UInt32"}, {"name": "sollecito", "dtype": "UInt32"}, {"name": "telefonata", "dtype": "UInt32"},
    {"name": "visita", "dtype": "UInt32"}, {"name": "totale", "dtype": "UInt32"}, {"name": "quota_visite_pct", "dtype": "Float64"}]}


class FakeGateway:
    def __init__(self, datasources, dashboards=(), reports=()):
        self.datasources, self.dashboards, self.reports, self.writes = list(datasources), list(dashboards), list(reports), []

    def must(self, method, path, body=None, **_):
        if method == "GET" and path == "/datasources":
            return copy.deepcopy(self.datasources)
        if method == "GET" and path == f"/projects/{SAMPLE}/dashboards":
            return copy.deepcopy(self.dashboards)
        if method == "GET" and path == f"/projects/{SAMPLE}/reports":
            return copy.deepcopy(self.reports)
        self.writes.append((method, path, body))
        if method == "POST" and path == f"/projects/{SAMPLE}/reports":
            self.reports.append({"id": 100 + len(self.reports), "name": body["name"], "project_id": SAMPLE})
            return {"id": self.reports[-1]["id"]}
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


def expressions(value):
    """Every text of a definition, wherever it stands."""
    if isinstance(value, dict):
        return [t for v in value.values() for t in expressions(v)]
    if isinstance(value, list):
        return [t for v in value for t in expressions(v)]
    return [value] if isinstance(value, str) else []


def items(content):
    return content["sections"]["body"]["items"]


class SampleReports(unittest.TestCase):
    def seed_on(self, gateway):
        original, seed.gw = seed.gw, gateway
        self.addCleanup(setattr, seed, "gw", original)
        seed.log = lambda _: None
        seed.create_reports(SAMPLE, TABLES)

    def created(self, gateway, name):
        return next(body["content"] for _, _, body in gateway.writes if body["name"] == name)

    def both(self):
        gateway = FakeGateway([AGENTS, MONTHS, CRM])
        self.seed_on(gateway)
        return gateway

    def test_two_reports_are_created_in_the_sample_folder_and_running_the_seed_again_writes_nothing(self):
        gateway = self.both()
        self.assertEqual(sorted((m, p, b["name"]) for m, p, b in gateway.writes), [
            ("POST", f"/projects/{SAMPLE}/reports", "Agent scorecard"), ("POST", f"/projects/{SAMPLE}/reports", "Margin review")])
        self.seed_on(gateway)
        self.assertEqual(len(gateway.writes), 2)

    def test_one_someone_changed_stays_as_it_is_and_only_the_other_is_made(self):
        gateway = FakeGateway([AGENTS, MONTHS, CRM], reports=[{"id": 3, "name": "Margin review", "project_id": SAMPLE}])
        self.seed_on(gateway)
        self.assertEqual([b["name"] for _, _, b in gateway.writes], ["Agent scorecard"])

    def test_a_table_no_flow_produced_stops_only_the_report_that_reads_it(self):
        gateway = FakeGateway([AGENTS, MONTHS])
        self.seed_on(gateway)
        self.assertEqual([b["name"] for _, _, b in gateway.writes], ["Margin review"])

    def test_a_table_of_the_same_name_in_another_folder_is_not_read(self):
        gateway = FakeGateway([AGENTS, CRM, {**MONTHS, "project_id": 99}])
        self.seed_on(gateway)
        self.assertEqual([b["name"] for _, _, b in gateway.writes], ["Agent scorecard"])

    def test_no_report_holds_an_ai_comment_nor_takes_a_name_of_the_owners_objects(self):
        gateway = self.both()
        for _, _, body in gateway.writes:
            self.assertNotIn(body["name"], ("Monthly margin", "New report"))
            self.assertNotIn("comment", {it["type"] for it in items(body["content"])})

    def test_they_read_the_tables_by_their_ids_and_take_their_columns_as_this_engine_wrote_them(self):
        duckdb = {**AGENTS, "id": 40, "columns": [{**c, "dtype": "Int64"} if c["dtype"] == "UInt32" else c for c in AGENTS["columns"]]}
        gateway = FakeGateway([duckdb, MONTHS, CRM])
        self.seed_on(gateway)
        agents = next(d for d in self.created(gateway, "Agent scorecard")["datasets"] if d["source"].get("datasource_id") == 40)
        self.assertEqual({f["name"]: f["type"] for f in agents["fields"]}["ordini"], "integer")
        for _, _, body in gateway.writes:
            self.assertEqual({d["source"]["kind"] for d in body["content"]["datasets"]}, {"datasource"})

    def test_the_margin_review_compares_shares_and_filters_its_own_data(self):
        content = self.created(self.both(), "Margin review")
        kpis = [it for it in items(content) if it["type"] == "kpi"]
        self.assertTrue(any(k.get("compare") for k in kpis))
        self.assertTrue(any(it["type"] == "chart" for it in items(content)))
        table = next(it for it in items(content) if it["type"] == "table")
        self.assertTrue(table["grand_total"] and table["groups"][0]["footer"])
        written = expressions(table["columns"])
        self.assertTrue(any('PERCENT(SUM([ricavo]), "categoria")' in t for t in written))
        self.assertTrue(any("PREVIOUS(" in t for t in written))
        category = next(p for p in content["parameters"] if p["name"] == "category")
        self.assertEqual(category["choices"]["value"], "[categoria]")
        filtered = next(d for d in content["datasets"] if d["name"] == category["choices"]["dataset"])
        self.assertIn({"column": "categoria", "op": "=", "value": "{category}"}, filtered["source"]["filters"])

    def test_the_period_a_year_earlier_is_read_whole_and_only_the_category_narrows_it(self):
        content = self.created(self.both(), "Margin review")
        history = next(d for d in content["datasets"] if d["name"] == "history")
        self.assertEqual([f["value"] for f in history["source"]["filters"]], ["{category}"])

    def test_the_agents_list_follows_the_area_because_both_filter_one_dataset(self):
        content = self.created(self.both(), "Agent scorecard")
        names = [p["name"] for p in content["parameters"]]
        self.assertLess(names.index("area"), names.index("agent"))
        by_name = {p["name"]: p for p in content["parameters"]}
        self.assertEqual(by_name["area"]["choices"]["dataset"], by_name["agent"]["choices"]["dataset"])
        filters = {f["value"]: f["column"] for f in next(
            d for d in content["datasets"] if d["name"] == by_name["area"]["choices"]["dataset"])["source"]["filters"]}
        self.assertEqual(filters, {"{area}": "area", "{agent}": "agente"})

    def test_the_agent_scorecard_has_a_matrix_a_share_of_its_group_a_lookup_and_a_colour(self):
        content = self.created(self.both(), "Agent scorecard")
        tables = [it for it in items(content) if it["type"] == "table"]
        self.assertTrue(any(t.get("matrix") for t in tables))
        table = next(t for t in tables if not t.get("matrix"))
        self.assertTrue(any('SUM([ricavo], "area")' in t for t in expressions(table["columns"])))
        self.assertTrue(any(c.get("rules") for c in table["columns"]))
        self.assertTrue(any("LOOKUP(" in t and '"crm"' in t for t in expressions(content["datasets"])))

    def test_every_item_of_a_report_is_on_the_page(self):
        gateway = self.both()
        for _, _, body in gateway.writes:
            top, right, bottom, left = body["content"]["page"]["margins"]
            self.assertEqual((body["content"]["page"]["size"], top + bottom < 297), ("A4", True))
            width = 210 - left - right
            for section in body["content"]["sections"].values():
                for it in section["items"]:
                    self.assertLessEqual(it["x"] + max(it["w"], 0), width, it["id"])

    def test_the_seed_says_what_it_made_and_a_view_is_not_called_a_report(self):
        self.assertNotIn("(the reports)", seed.__doc__)
        self.assertIn("report", seed.READY.lower())
        self.assertIn("dashboard", seed.READY.lower())


if __name__ == "__main__":
    unittest.main()
