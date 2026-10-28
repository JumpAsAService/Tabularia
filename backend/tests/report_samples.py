"""A report and its data for the tests of app/reports: orders by region and customer, credit
notes (negative amounts), and customers with hostile names on purpose — Typst markup, HTML,
a spreadsheet formula, a backslash, very long text, accents."""
from __future__ import annotations

import copy
import random
from datetime import date, timedelta

REGIONS = ["Nord-Ovest", "Nord-Est", "Centro", "Sud", "Isole"]
PRODUCTS = ["Prosciutto crudo", "Salame Milano", "Mortadella IGP", "Coppa", "Bresaola", "Speck"]
HOSTILE = ['ACME #panic("boom") *bold* $x^2$ <lab> @ref [link](x)', "<script>alert(1)</script> S.p.A.",
           '=HYPERLINK("http://x")', "Back\\slash \\n #set page(width: 1mm)",
           "Società Cooperativa Agricola Allevatori dell'Appennino Tosco-Emiliano e delle Valli a.r.l.",
           "Café & Bistrò «Ölçü» — €"]


def orders(n: int = 300, seed: int = 7) -> list[dict]:
    rnd = random.Random(seed)
    customers = {r: [f"Cliente {r[:3].upper()}-{i + 1:02d}" for i in range(4)] for r in REGIONS}
    for i, name in enumerate(HOSTILE):
        customers[REGIONS[i % len(REGIONS)]][1 + i // len(REGIONS)] = name
    rows = []
    for i in range(n):
        region = rnd.choices(REGIONS, weights=[30, 25, 20, 15, 10])[0]
        qty = rnd.randint(1, 120)
        amount = round(qty * rnd.uniform(4.5, 38.0), 2)
        if rnd.random() < 0.06:
            amount = -round(amount * rnd.uniform(0.1, 0.6), 2)
        rows.append({"order": f"ORD-{i + 1:05d}", "day": date(2026, 1, 1) + timedelta(days=rnd.randint(0, 270)),
                     "region": region, "customer": rnd.choice(customers[region]), "product": rnd.choice(PRODUCTS),
                     "qty": qty, "amount": amount})
    return rows


COLUMNS = [{"name": "order", "type": "String"}, {"name": "day", "type": "Date"}, {"name": "region", "type": "String"},
           {"name": "customer", "type": "String"}, {"name": "product", "type": "String"},
           {"name": "qty", "type": "Int64"}, {"name": "amount", "type": "Float64"}]

DEFINITION = {
    "v": 1,
    "page": {"size": "A4", "orientation": "portrait", "margins": [15, 12, 15, 12]},
    "locale": "it",
    "timezone": "Europe/Rome",
    "theme": {"font": "IBM Plex Sans", "size": 8, "color": "#1f2933", "accent": "#3b5bdb"},
    "parameters": [
        {"name": "region", "label": "Regione", "type": "text", "multi": True, "default": None},
        {"name": "since", "label": "Dal", "type": "date", "default": "{start_of_year}"},
    ],
    "datasets": [{"name": "orders", "source": {"kind": "sql", "sql": "SELECT * FROM orders"},
                  "fields": [{"name": c["name"], "type": c["type"]} for c in COLUMNS],
                  "calculated": [{"name": "Credit", "expr": "IF([amount] < 0, -[amount], 0)"}]}],
    "sections": {
        "page_header": {"height": 14, "items": [
            {"id": "title", "type": "text", "x": 0, "y": 0, "w": 120, "h": 8,
             "value": '="Vendite per regione dal " & FORMAT(PARAM("since"), "d MMMM yyyy")',
             "style": {"size": 13, "weight": "bold"}},
            {"id": "rule", "type": "line", "x": 0, "y": 11, "w": 186, "h": 0, "style": {"border": {"width": 0.3}}},
        ]},
        "body": {"items": [
            {"id": "k1", "type": "kpi", "dataset": "orders", "x": 0, "y": 0, "w": 58, "h": 24, "label": "Ricavo",
             "value": "SUM([amount])", "compare": "SUM([amount]) * 0.9", "format": "€ #,##0",
             "sparkline": {"category": "MONTH([day])", "value": "SUM([amount])"}},
            {"id": "k2", "type": "kpi", "dataset": "orders", "x": 64, "y": 0, "w": 58, "h": 24, "label": "Ordini",
             "value": "COUNT()", "format": "#,##0"},
            {"id": "k3", "type": "kpi", "dataset": "orders", "x": 128, "y": 0, "w": 58, "h": 24, "label": "Note di credito",
             "value": "SUM([Credit])", "format": "€ #,##0.00"},
            {"id": "bars", "type": "chart", "dataset": "orders", "kind": "column", "x": 0, "y": 28, "w": 120, "h": 60,
             "title": "Ricavo per regione", "category": "[region]",
             "series": [{"value": "SUM([amount])", "name": "Ricavo"}], "options": {"labels": True, "format": "#,##0"}},
            {"id": "gauge", "type": "chart", "dataset": "orders", "kind": "gauge", "x": 126, "y": 28, "w": 60, "h": 60,
             "series": [{"value": "SUM([amount])", "name": "Obiettivo"}],
             "options": {"target": 80000, "format": "€ #,##0"}},
            {"id": "note", "type": "text", "x": 0, "y": 92, "w": 186, "h": 6,
             "value": "Le note di credito hanno ricavo negativo e sono in rosso.", "style": {"italic": True}},
            {"id": "detail", "type": "table", "dataset": "orders", "x": 0, "y": 100, "w": 186, "h": 30,
             "filter": "[day] >= PARAM(\"since\")",
             "columns": [
                 {"header": "Ordine", "value": "[order]", "width": 24},
                 {"header": "Data", "value": "[day]", "width": 22, "format": "dd/MM/yyyy"},
                 {"header": "Prodotto", "value": "[product]"},
                 {"header": "Qtà", "value": "[qty]", "width": 16, "total": "SUM([qty])", "format": "#,##0"},
                 {"header": "Ricavo", "value": "[amount]", "width": 28, "total": "SUM([amount])", "format": "#,##0.00",
                  "rules": [{"when": "[amount] < 0", "style": {"color": "#c92a2a", "weight": "bold"}}]},
             ],
             "groups": [
                 {"by": "[region]", "label": '="Regione: " & [region]', "page_break": "between"},
                 {"by": "[customer]", "label": "[customer]"},
             ],
             "sort": [{"by": "[day]"}, {"by": "[order]"}],
             "grand_total": True, "zebra": True,
             "interactive": {"sort": True, "toggle": True}},
        ]},
        "page_footer": {"height": 8, "items": [
            {"id": "pages", "type": "text", "x": 126, "y": 2, "w": 60, "h": 5,
             "value": '="Pagina " & PAGE() & " di " & PAGES()', "style": {"align": "right", "size": 7}},
            {"id": "when", "type": "text", "x": 0, "y": 2, "w": 100, "h": 5,
             "value": '="Generato il " & FORMAT(NOW(), "d MMMM yyyy HH:mm")', "style": {"size": 7}},
        ]},
    },
}


def definition() -> dict:
    return copy.deepcopy(DEFINITION)


def datasets(n: int = 300, seed: int = 7) -> dict:
    return {"orders": {"rows": orders(n, seed), "columns": COLUMNS}}


def chart_presets() -> list[dict]:
    """One chart of every kind as the engine builds its option: the sample the chart service's
    own test draws (frontend/chart-renderer/presets.sample.json). Written again with

        cd backend && python -m tests.report_samples ../frontend/chart-renderer/presets.sample.json
    """
    from app.reports import definition as rd
    from tests.test_reports_model import build, report

    out = []
    for kind in rd.CHART_KINDS:
        item = {"id": kind, "type": "chart", "dataset": "d", "kind": kind, "x": 0, "y": 0, "w": 90, "h": 55,
                "title": f"{kind} <script>", "category": "[customer]",
                "series": [{"value": "SUM([amount])", "name": "Amount"}],
                "options": {"bands": [10, 40], "target": 35, "labels": True, "format": "#,##0.00"}}
        if kind in ("heatmap", "treemap", "scatter", "line", "area"):
            item["split_by"] = "[kind]"
        if kind == "scatter":
            item.update(category="[qty]", series=[{"value": "[amount]"}])
        if kind == "combo":
            item["series"].append({"value": "SUM([qty])", "name": "Qty", "as": "line", "axis": "right"})
        job = build(report(item)).charts[0]
        out.append({"name": kind, "width": job.width, "height": job.height, "option": job.option})
    return out


if __name__ == "__main__":
    import json
    import sys

    with open(sys.argv[1], "w", encoding="utf-8") as f:
        f.write(json.dumps(chart_presets(), ensure_ascii=False, indent=1) + "\n")
