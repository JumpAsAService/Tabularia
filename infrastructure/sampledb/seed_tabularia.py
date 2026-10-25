#!/usr/bin/env python3
"""Sets up Tabularia's «Sample» folder on the sample databases.

Runs in the `sampledb-init` container (compose profile `samples`), once,
after the gateway and the sample databases are healthy. In order, it:

  1. if the databases are empty, fills them at the requested scale (SAMPLES_SCALE)
     with `generate.py`; if they are already filled it leaves them alone and only
     adds the public tables if they are missing;
  2. creates the folders:  Sample / Flows, Private tables, Public tables;
  3. creates the two connections (Postgres ERP, ClickHouse CRM);
  4. imports the tables: the company's in «Private tables», the open reference
     ones in «Public tables» — and describes SOME of them, not all, so that the
     difference shows between a datasource ready for the assistant and one that
     is not;
  5. builds eleven flows in «Flows» — with joins between private and public data,
     a sql node, pivots, calculations — and schedules three of them;
  6. runs them once, so that the tables they produce exist, and saves the views
     (the reports) on those in the Sample folder.

Idempotent: every object is looked up by name before being created, so starting
the container again duplicates nothing. It grants NO permissions: who can see
the folder is up to the administrator.

Only the standard library towards the gateway (urllib): the image already has
what the generator needs, and it is not worth adding anything else to it.
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

GATEWAY = os.getenv("GATEWAY_URL", "http://gateway:8000").rstrip("/")
ADMIN_EMAIL = os.getenv("AUTH__ADMIN_EMAIL", "")
ADMIN_PASSWORD = os.getenv("AUTH__ADMIN_PASSWORD", "")
# with no administrator password (the try-it stack, nothing configured): the start-up
# token the gateway leaves in the shared keys directory, exchanged for a session as the
# administrator once someone has created it at the sign-in page (POST /auth/bootstrap)
BOOTSTRAP_TOKEN_FILE = os.getenv("SAMPLES_BOOTSTRAP_TOKEN_FILE", "/var/lib/tabularia/keys/bootstrap.token")
SCALE = (os.getenv("SAMPLES_SCALE") or "small").lower()
WANTED_ENGINE = (os.getenv("SAMPLES_ENGINE") or "").strip().lower()
RUN_FLOWS = (os.getenv("SAMPLES_RUN_FLOWS") or "true").lower() in ("1", "true", "yes", "si", "sì")

PG = dict(host=os.getenv("PG_HOST", "sampledb-postgres"), port=int(os.getenv("PG_PORT", "5432")),
          dbname=os.getenv("PG_DB", "shop"), user=os.getenv("PG_USER", "shop"), password=os.getenv("PG_PASSWORD", "shop"))
CH = dict(host=os.getenv("CH_HOST", "sampledb-clickhouse"), port=int(os.getenv("CH_PORT", "8123")),
          db=os.getenv("CH_DB", "analytics"), user=os.getenv("CH_USER", "analytics"), password=os.getenv("CH_PASSWORD", "analytics"))

T0 = time.time()


def log(msg: str) -> None:
    print(f"[{int(time.time() - T0):4d}s] {msg}", flush=True)


# ── the gateway ───────────────────────────────────────────────────────────────
class Gateway:
    def __init__(self) -> None:
        self.token: str | None = None

    def call(self, method: str, path: str, body=None, timeout: int = 120):
        req = urllib.request.Request(GATEWAY + path, method=method)
        if self.token:
            req.add_header("Authorization", "Bearer " + self.token)
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, data, timeout=timeout) as r:
                text = r.read().decode()
                return r.status, (json.loads(text) if text else None)
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()[:300]

    def wait(self, minutes: int = 10) -> None:
        deadline = time.time() + minutes * 60
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(GATEWAY + "/health", timeout=5) as r:
                    if r.status == 200:
                        return
            except Exception:
                pass
            time.sleep(3)
        sys.exit(f"the gateway does not answer at {GATEWAY} after {minutes} minutes")

    def login(self) -> None:
        if not ADMIN_PASSWORD:
            self.login_with_bootstrap_token()
            return
        if not ADMIN_EMAIL:
            sys.exit("AUTH__ADMIN_EMAIL is needed with AUTH__ADMIN_PASSWORD (the same as the gateway's)")
        s, r = self.call("POST", "/auth/login", {"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD})
        if s != 200:
            sys.exit(f"the administrator's sign-in failed: {s} {r}")
        self.token = r["access_token"]

    def login_with_bootstrap_token(self) -> None:
        """No administrator password: wait for the administrator to be created at the
        sign-in page, then sign in as it with the gateway's start-up token."""
        try:
            token = open(BOOTSTRAP_TOKEN_FILE).read().strip()
        except OSError as e:
            sys.exit(f"no AUTH__ADMIN_PASSWORD and no start-up token ({e}): set AUTH__ADMIN_PASSWORD in "
                     "infrastructure/.env, or mount the gateway's SECURITY__KEYS_DIR here")
        waited = 0
        while True:
            s, r = self.call("POST", "/auth/bootstrap", {"token": token})
            if s == 200:
                self.token = r["access_token"]
                log("signed in as the administrator (start-up token)")
                return
            if s != 409:
                sys.exit(f"the start-up token was refused: {s} {r}")
            if waited % 30 == 0:
                log("waiting for the administrator: open http://localhost:3000 and create it, "
                    "then the sample data is loaded")
            time.sleep(5)
            waited += 5

    def must(self, method: str, path: str, body=None, **kw):
        s, r = self.call(method, path, body, **kw)
        if s >= 400:
            sys.exit(f"{method} {path} → {s}: {r}")
        return r


gw = Gateway()


# ── 1. the sample databases ───────────────────────────────────────────────────
def prepare_databases() -> None:
    import psycopg2

    pg = psycopg2.connect(**PG)
    with pg.cursor() as cur:
        cur.execute("SELECT to_regclass('vendite.ordini')")
        exists = cur.fetchone()[0] is not None
        rows = 0
        if exists:
            cur.execute("SELECT count(*) FROM vendite.ordini")
            rows = cur.fetchone()[0]
    if rows == 0:
        log(f"empty databases: generating the data at scale «{SCALE}» (small takes seconds, robusta minutes)")
        pg.close()
        os.environ["SAMPLES_SCALE"] = SCALE
        import generate  # noqa: WPS433 — same container, same code

        generate.main()
        return
    log(f"databases already filled ({rows:,} orders): nothing to generate")
    import generate

    if generate.ensure_ref(pg):
        log("added the missing public tables (calendar, regions)")
    pg.close()


# ── 2. the folders ────────────────────────────────────────────────────────────
def folder(name: str, parent_id: int | None, description: str) -> int:
    for p in gw.must("GET", "/projects"):
        if p["name"] == name and p.get("parent_id") == parent_id:
            return p["id"]
    r = gw.must("POST", "/projects", {"name": name, "description": description, "parent_id": parent_id})
    log(f"folder created: {name}")
    return r["id"]


# ── 3. the connections ────────────────────────────────────────────────────────
def connection(project: int, name: str, body: dict) -> int:
    for c in gw.must("GET", "/connections"):
        if c["name"] == name and c.get("project_id") == project:
            return c["id"]
    r = gw.must("POST", f"/projects/{project}/connections", {"name": name, **body})
    log(f"connection created: {name}")
    return r["id"]


# ── 4. the tables ─────────────────────────────────────────────────────────────
PRIVATE = [  # (name, connection, table, description or None = left undescribed on purpose)
    ("orders", "pg", "vendite.ordini",
     "Order headers: one row per order, with customer, agent, sales channel and dispatching depot."),
    ("order_rows", "pg", "vendite.righe_ordine",
     "Order lines: one per product ordered. The fact table."),
    ("products", "pg", "catalogo.prodotti",
     "Product catalogue: cured-meat SKUs, with format and average weight."),
    ("price_list", "pg", "catalogo.listino_prezzi",
     "Price list history: for each product, the price and the window it applies to."),
    ("production_costs", "pg", "catalogo.costi_produzione", None),
    ("commercial_costs", "pg", "vendite.costi_commerciali", None),
    ("customers", "ch", "clienti",
     "CRM customer base, with territory and the agent who covers them."),
    ("sales_network", "ch", "agenti",
     "Sales force: agents, area managers and inspectors, with the reporting line."),
    ("crm_activities", "ch", "attivita_crm", None),
]
PUBLIC = [
    ("calendar", "pg", "ref.calendario",
     "One row per day: year, quarter, month, ISO week, weekday, weekend and Italian public holidays."),
    ("regions", "pg", "ref.regioni",
     "The twenty Italian regions with macro-area, capital, population and surface."),
]
COLUMNS = {
    "orders": {"id": "Order identifier. Order rows link back to this.", "numero": "Human-readable order number.",
               "data_ordine": "Day the order was entered.", "cliente_id": "Customer who ordered. Refers to customers.id.",
               "agente_id": "Agent who followed the order. Refers to sales_network.id.",
               "canale": "Sales channel: GDO, HoReCa, Grossista, Dettaglio, Gastronomia.",
               "stato": "Order state: evaso (fulfilled), in_lavorazione (in progress), annullato (cancelled).",
               "deposito": "Depot the goods ship from."},
    "order_rows": {"id": "Line identifier.", "ordine_id": "Order this line belongs to. Refers to orders.id.",
                   "prodotto_id": "Product ordered. Refers to products.id.",
                   "quantita": "Quantity, in kilos or pieces depending on the product's unit. Stored as text.",
                   "prezzo_unitario": "Price charged, per kilo or per piece. Stored as text.",
                   "sconto_pct": "Discount on the line, in percent."},
    "products": {"id": "Product identifier.", "codice": "SKU code.", "nome": "Commercial name, including the format.",
                 "categoria": "Product family.", "tipo": "Curing type: crudo, cotto, fresco, stagionato.",
                 "formato": "Selling format.", "stagionatura_mesi": "Months of curing; zero for fresh or cooked.",
                 "peso_medio_kg": "Average weight of one piece, in kilos.",
                 "unita_misura": "Unit the quantity is expressed in: kg or pz."},
    "price_list": {"id": "Row identifier.", "prodotto_id": "Product the price applies to. Refers to products.id.",
                   "valido_dal": "First day the price is in force.", "valido_al": "Last day; empty means still current.",
                   "prezzo_listino": "List price, per kilo or per piece. Stored as text."},
    "customers": {"id": "Customer identifier.", "ragione_sociale": "Company name.", "partita_iva": "VAT number.",
                  "canale": "Channel the customer belongs to.", "indirizzo": "Street address.", "cap": "Postal code.",
                  "citta": "Town.", "provincia": "Province code.", "regione": "Italian region. Joins to regions.regione.",
                  "agente_id": "Agent covering the customer. Refers to sales_network.id.",
                  "data_acquisizione": "Date the customer entered the portfolio.", "attivo": "1 if still active."},
    "sales_network": {"id": "Person identifier.", "nome": "First name.", "cognome": "Surname.",
                      "ruolo": "Role: agente, capo area, ispettore.", "responsabile_id": "Person they report to.",
                      "area": "Territory covered.", "provvigione_base_pct": "Base commission rate, in percent.",
                      "data_assunzione": "Date they joined.", "email": "Work email address."},
    "calendar": {"giorno": "The day. Joins to orders.data_ordine.", "anno": "Year.", "trimestre": "Quarter, 1 to 4.",
                 "mese": "Month, 1 to 12.", "anno_mese": "Year and month as YYYYMM, the key reports aggregate on.",
                 "settimana_iso": "ISO week number.", "giorno_settimana": "1 = Monday … 7 = Sunday.",
                 "nome_giorno": "Weekday name, in Italian.", "fine_settimana": "True on Saturday and Sunday.",
                 "festivo": "True on an Italian public holiday.", "nome_festa": "Name of the holiday, when it is one."},
    "regions": {"regione": "Region name. Joins to customers.regione.", "macroarea": "Nord-Ovest, Nord-Est, Centro, Sud, Isole.",
                "capoluogo": "Regional capital.", "popolazione": "Inhabitants, rounded.", "superficie_kmq": "Surface in km²."},
}


def datasource(project: int, name: str, conn_id: int, table: str, description: str | None) -> int:
    for d in gw.must("GET", "/datasources"):
        if d["name"] == name and d["project_id"] == project:
            return d["id"]
    body = {"name": name, "connection_id": conn_id, "source_type": "table", "source_ref": table}
    if description:
        body["description"] = description
    r = gw.must("POST", f"/projects/{project}/datasources/database", body)
    log(f"table imported: {name} ← {table}")
    return r["id"]


def wait_for_ingest(ids: list[int], minutes: int = 60) -> dict[int, dict]:
    deadline = time.time() + minutes * 60
    while time.time() < deadline:
        by_id = {d["id"]: d for d in gw.must("GET", "/datasources")}
        ready = [i for i in ids if by_id.get(i) and by_id[i].get("rows") is not None and not by_id[i].get("refreshing")]
        if len(ready) == len(ids):
            return by_id
        # no rows and no import running: the first import failed
        for i in ids:
            d = by_id.get(i)
            if d and d.get("rows") is None and not d.get("refreshing"):
                s, runs = gw.call("GET", f"/datasources/{i}/runs")
                last = (runs or [{}])[0] if isinstance(runs, list) else {}
                if last.get("status") == "FAILURE":
                    sys.exit(f"import failed for «{d['name']}»: {last.get('error')}")
        time.sleep(10)
    sys.exit("the tables did not finish importing in time")


def describe(by_name: dict[str, dict]) -> None:
    for name, columns in COLUMNS.items():
        d = by_name.get(name)
        if not d or d.get("column_descriptions"):
            continue
        present = {c["name"] for c in (d.get("columns") or [])}
        gw.must("PATCH", f"/datasources/{d['id']}", {"column_descriptions": {k: v for k, v in columns.items() if k in present}})
    log(f"described {len(COLUMNS)} tables of {len(PRIVATE) + len(PUBLIC)}: the others stay undescribed, on purpose")


# ── 5. the flows ──────────────────────────────────────────────────────────────
def choose_engine() -> str:
    if WANTED_ENGINE:
        return WANTED_ENGINE
    allowed = {e["engine_id"] for e in gw.must("GET", "/engine-policy") if e.get("allowed")}
    for e in ("polars", "duckdb", "chdb", "clickhouse", "bigquery"):
        if e in allowed:
            return e
    return "polars"


class Builder:
    """Nodes and edges as the editor would write them, with the sources taken from the catalogue.

    The join rule, valid on every engine: the right-hand key is renamed like the
    left-hand one BEFORE joining (`id` → `ordine_id`) and the join uses `on`, so ONE
    key column remains (like SQL USING). With `left_on`/`right_on` both remain, and
    a second join on the same key clashes (`prodotto_id_right` already exists):
    ClickHouse puts up with it, Polars does not."""

    def __init__(self, ds: dict[str, dict], outputs_in: int):
        self.ds, self.outputs_in = ds, outputs_in
        self.nodes, self.edges = [], []
        self.output_name = ""

    def source(self, nid, name, x, y):
        d = self.ds[name]
        self.nodes.append({"id": nid, "type": "source", "position": {"x": x, "y": y}, "data": {
            "bucket": d["bucket"], "datasetId": None, "parquetKey": d["key"], "filename": d["name"],
            "rows": d["rows"], "columns": d["columns"], "datasourceId": d["id"], "sample": None}})
        return nid

    def op(self, nid, op_type, params, x, y):
        self.nodes.append({"id": nid, "type": "operation", "position": {"x": x, "y": y},
                           "data": {"opType": op_type, "params": params}})
        return nid

    def table(self, nid, name, columns, rename_map, x, y):
        """Source + select of only the columns needed + rename of the key.
        Returns the last node of the little chain, ready for the right side of a join."""
        last = self.source(f"src-{nid}", name, x, y)
        if columns:
            self.op(f"sel-{nid}", "select", {"columns": columns}, x + 260, y)
            self.edge(last, f"sel-{nid}"); last = f"sel-{nid}"
        if rename_map:
            self.op(f"ren-{nid}", "rename", {"mapping": rename_map}, x + 520, y)
            self.edge(last, f"ren-{nid}"); last = f"ren-{nid}"
        return last

    def output(self, name, x, y):
        self.output_name = name
        self.nodes.append({"id": "out", "type": "output", "position": {"x": x, "y": y}, "data": {
            "destType": "datasource", "mode": "append", "projectId": self.outputs_in, "name": name, "overwrite": True}})
        return "out"

    def edge(self, a, b, handle="left"):
        self.edges.append({"id": f"e-{a}-{handle}-{b}", "source": a, "target": b, "sourceHandle": "out", "targetHandle": handle})

    def chain(self, *ids):
        for a, b in zip(ids, ids[1:]):
            self.edge(a, b)

    def definition(self):
        return json.dumps({"nodes": self.nodes, "edges": self.edges})


# the parameters have the shape the editor writes and can show (the sort: ONE
# column and a boolean); the engine would also take lists, but the node's panel
# would show them empty
MONTH = "EXTRACT(YEAR FROM data_ordine) * 100 + EXTRACT(MONTH FROM data_ordine)"
REVENUE = "quantita * prezzo_unitario * (1 - sconto_pct / 100)"
CAST_LINES = {"columns": {"quantita": "float", "prezzo_unitario": "float", "sconto_pct": "float"}}
ORDER = (["id", "data_ordine", "cliente_id", "agente_id", "canale", "stato", "deposito"], {"id": "ordine_id"})
# a key with the same name on both sides → `on` (a single column, like SQL USING);
# `left_on`/`right_on` would keep BOTH columns and they clash at the second join
JOIN = lambda key: {"how": "inner", "on": [key]}  # noqa: E731


def lines_with_revenue(c: Builder, y: int = 260) -> str:
    """The start of almost every flow: order lines, real numbers, net revenue."""
    c.source("src-righe", "order_rows", 40, y)
    c.op("cast", "cast", CAST_LINES, 300, y)
    c.op("riga", "rename", {"mapping": {"id": "riga_id"}}, 560, y)
    c.op("ricavo", "compute", {"columns": [{"name": "ricavo_netto", "expr": REVENUE}]}, 820, y)
    c.chain("src-righe", "cast", "riga", "ricavo")
    return "ricavo"


def flows(ds: dict[str, dict], outputs_in: int) -> list[dict]:
    """Eleven flows that make sense on the cured-meats model. The expressions use
    only portable SQL (EXTRACT, CASE WHEN, ||, round, coalesce), so they run on
    whichever engine the installation allows."""
    out = []

    # 1. margin by category and month: 5 sources, 6 joins, a sql node
    c = Builder(ds, outputs_in)
    lines_with_revenue(c)
    orders = c.table("ordini", "orders", *ORDER, 40, 470)
    c.op("j-ordini", "join", JOIN("ordine_id"), 1080, 260)
    c.op("evasi", "filter", {"column": "stato", "operator": "eq", "value": "evaso"}, 1340, 260)
    products = c.table("prodotti", "products", ["id", "codice", "nome", "categoria", "tipo", "peso_medio_kg", "unita_misura"], {"id": "prodotto_id"}, 40, 660)
    c.op("j-prod", "join", JOIN("prodotto_id"), 1600, 260)
    price_list = c.table("listino", "price_list", ["prodotto_id", "valido_dal", "valido_al", "prezzo_listino"], {"valido_dal": "listino_dal", "valido_al": "listino_al"}, 40, 850)
    c.op("j-list", "join", JOIN("prodotto_id"), 1860, 260)
    # the costs have several periods per product and no end date: the end of a
    # period is the start of the next one of the same product
    costs = c.table("costi", "production_costs", ["prodotto_id", "valido_dal", "costo_materia_prima_kg", "costo_lavorazione_kg", "costo_confezionamento_pz"], {"valido_dal": "costo_dal"}, 40, 1040)
    c.op("cast-costi", "cast", {"columns": {"costo_materia_prima_kg": "float", "costo_lavorazione_kg": "float", "costo_confezionamento_pz": "float"}}, 820, 1040)
    c.op("ultimo", "group_by", {"by": ["prodotto_id"], "aggregations": [{"column": "costo_dal", "func": "max", "alias": "ultimo_periodo"}]}, 1080, 1230)
    c.op("j-ultimo", "join", JOIN("prodotto_id"), 1080, 1040)
    c.op("costo-al", "compute", {"columns": [{"name": "costo_al", "expr": "CASE WHEN costo_dal < ultimo_periodo THEN ultimo_periodo ELSE NULL END"}]}, 1340, 1040)
    c.op("j-costi", "join", JOIN("prodotto_id"), 2120, 260)
    c.op("valido", "sql", {"query": (
        "SELECT * FROM self WHERE data_ordine >= listino_dal AND (listino_al IS NULL OR data_ordine <= listino_al) "
        "AND data_ordine >= costo_dal AND (costo_al IS NULL OR data_ordine < costo_al)")}, 2380, 260)
    c.op("cast-peso", "cast", {"columns": {"peso_medio_kg": "float"}}, 2640, 260)
    c.op("calc", "compute", {"columns": [
        {"name": "chili", "expr": "CASE WHEN unita_misura = 'kg' THEN quantita ELSE quantita * peso_medio_kg END"},
        {"name": "pezzi", "expr": "CASE WHEN unita_misura = 'kg' THEN quantita / peso_medio_kg ELSE quantita END"},
        {"name": "costo_industriale", "expr": "chili * (costo_materia_prima_kg + costo_lavorazione_kg) + pezzi * costo_confezionamento_pz"},
        {"name": "margine", "expr": "ricavo_netto - costo_industriale"},
        {"name": "anno_mese", "expr": MONTH}]}, 2900, 260)
    c.op("agg", "group_by", {"by": ["categoria", "anno_mese"], "aggregations": [
        {"column": "ricavo_netto", "func": "sum", "alias": "ricavo"}, {"column": "costo_industriale", "func": "sum", "alias": "costo"},
        {"column": "margine", "func": "sum", "alias": "margine"}, {"column": "chili", "func": "sum", "alias": "chili"},
        {"column": "ordine_id", "func": "n_unique", "alias": "ordini"}]}, 3160, 260)
    c.op("pct", "compute", {"columns": [{"name": "margine_pct", "expr": "round(margine / ricavo * 100, 2)"}]}, 3420, 260)
    c.op("ord", "sort", {"by": "anno_mese", "descending": False}, 3680, 260)
    c.output("Margin by Category and Month", 3940, 260)
    c.chain("ricavo", "j-ordini", "evasi", "j-prod", "j-list", "j-costi", "valido", "cast-peso", "calc", "agg", "pct", "ord", "out")
    c.edge(orders, "j-ordini", "right"); c.edge(products, "j-prod", "right"); c.edge(price_list, "j-list", "right")
    c.chain(costs, "cast-costi", "j-ultimo", "costo-al"); c.edge("cast-costi", "ultimo"); c.edge("ultimo", "j-ultimo", "right")
    c.edge("costo-al", "j-costi", "right")
    out.append(dict(name="Margin by Category and Month", cron="0 6 1 * *", c=c,
                    description="The real margin per category and month: revenue net of discount minus industrial cost per kilo, using the list price and the production cost in force on the day of the order. Five sources, six joins and a SQL step."))

    # 2. cost to serve by channel and depot
    c = Builder(ds, outputs_in)
    lines_with_revenue(c)
    costs = c.table("costi", "commercial_costs", ["riga_ordine_id", "provvigione_importo", "costo_logistica", "costo_promozionale"], {"riga_ordine_id": "riga_id"}, 40, 560)
    c.op("cast-c", "cast", {"columns": {"provvigione_importo": "float", "costo_logistica": "float", "costo_promozionale": "float"}}, 820, 560)
    c.op("j-costi", "join", JOIN("riga_id"), 1080, 260)
    orders = c.table("ordini", "orders", *ORDER, 40, 800)
    c.op("j-ordini", "join", JOIN("ordine_id"), 1340, 260)
    c.op("evasi", "filter", {"column": "stato", "operator": "eq", "value": "evaso"}, 1600, 260)
    c.op("servizio", "compute", {"columns": [{"name": "costo_servizio", "expr": "provvigione_importo + costo_logistica + costo_promozionale"}]}, 1860, 260)
    c.op("agg", "group_by", {"by": ["canale", "deposito"], "aggregations": [
        {"column": "ricavo_netto", "func": "sum", "alias": "ricavo"}, {"column": "costo_servizio", "func": "sum", "alias": "costo_servizio"},
        {"column": "provvigione_importo", "func": "sum", "alias": "provvigioni"}, {"column": "costo_logistica", "func": "sum", "alias": "logistica"},
        {"column": "costo_promozionale", "func": "sum", "alias": "promozioni"}, {"column": "ordine_id", "func": "n_unique", "alias": "ordini"}]}, 2120, 260)
    c.op("pct", "compute", {"columns": [{"name": "incidenza_pct", "expr": "round(costo_servizio / ricavo * 100, 2)"},
                                        {"name": "costo_per_ordine", "expr": "round(costo_servizio / ordini, 2)"}]}, 2380, 260)
    c.op("ord", "sort", {"by": "incidenza_pct", "descending": True}, 2640, 260)
    c.output("Cost to Serve by Channel", 2900, 260)
    c.chain("ricavo", "j-costi", "j-ordini", "evasi", "servizio", "agg", "pct", "ord", "out")
    c.chain(costs, "cast-c"); c.edge("cast-c", "j-costi", "right"); c.edge(orders, "j-ordini", "right")
    out.append(dict(name="Cost to Serve by Channel", cron="0 7 * * 1", c=c,
                    description="What it costs to serve each channel and depot: commissions, logistics and promotions on the single order line."))

    # 3. RFM segments by region (aggregates BEFORE joining the customer registry)
    c = Builder(ds, outputs_in)
    lines_with_revenue(c)
    orders = c.table("ordini", "orders", *ORDER, 40, 520)
    c.op("j-ordini", "join", JOIN("ordine_id"), 1080, 260)
    c.op("evasi", "filter", {"column": "stato", "operator": "eq", "value": "evaso"}, 1340, 260)
    c.op("per-cliente", "group_by", {"by": ["cliente_id"], "aggregations": [
        {"column": "ricavo_netto", "func": "sum", "alias": "valore"}, {"column": "ordine_id", "func": "n_unique", "alias": "ordini"},
        {"column": "data_ordine", "func": "max", "alias": "ultimo_ordine"}]}, 1600, 260)
    customers = c.table("clienti", "customers", ["id", "ragione_sociale", "canale", "provincia", "regione", "agente_id"], {"id": "cliente_id"}, 40, 780)
    c.op("j-cl", "join", JOIN("cliente_id"), 1860, 260)
    c.op("segmento", "sql", {"query": (
        "SELECT *, CASE WHEN ordini >= 12 AND EXTRACT(YEAR FROM ultimo_ordine) = 2026 AND EXTRACT(MONTH FROM ultimo_ordine) >= 4 THEN 'Champion' "
        "WHEN ordini >= 6 AND EXTRACT(YEAR FROM ultimo_ordine) = 2026 THEN 'Loyal' "
        "WHEN EXTRACT(YEAR FROM ultimo_ordine) < 2025 THEN 'Dormant' ELSE 'At risk' END AS segmento FROM self")}, 2120, 260)
    c.op("agg", "group_by", {"by": ["regione", "segmento"], "aggregations": [
        {"column": "valore", "func": "sum", "alias": "valore"}, {"column": "cliente_id", "func": "n_unique", "alias": "clienti"}]}, 2380, 260)
    c.op("piv", "pivot", {"index": ["regione"], "on": ["segmento"], "values": "valore", "func": "sum"}, 2640, 260)
    c.output("Customer Segments by Region", 2900, 260)
    c.chain("ricavo", "j-ordini", "evasi", "per-cliente", "j-cl", "segmento", "agg", "piv", "out")
    c.edge(orders, "j-ordini", "right"); c.edge(customers, "j-cl", "right")
    out.append(dict(name="Customer Segments by Region (RFM)", cron=None, c=c,
                    description="Recency, frequency and monetary per customer, the segmentation rule written in plain SQL, the result pivoted region × segment."))

    # 4. CRM workload by agent and contact type
    c = Builder(ds, outputs_in)
    c.source("src-crm", "crm_activities", 40, 260)
    c.op("agg", "group_by", {"by": ["agente_id", "tipo"], "aggregations": [{"column": "id", "func": "count", "alias": "contatti"}]}, 300, 260)
    network = c.table("rete", "sales_network", ["id", "nome", "cognome", "ruolo", "area"], {"id": "agente_id"}, 40, 560)
    c.op("j-r", "join", JOIN("agente_id"), 820, 260)
    c.op("nome", "compute", {"columns": [{"name": "agente", "expr": "cognome || ' ' || nome"}]}, 1080, 260)
    c.op("solo-agenti", "filter", {"column": "ruolo", "operator": "eq", "value": "agente"}, 1340, 260)
    c.op("piv", "pivot", {"index": ["area", "agente"], "on": ["tipo"], "values": "contatti", "func": "sum"}, 1600, 260)
    c.op("tot", "compute", {"columns": [{"name": "totale", "expr": "coalesce(email, 0) + coalesce(reclamo, 0) + coalesce(sollecito, 0) + coalesce(telefonata, 0) + coalesce(visita, 0)"},
                                        {"name": "quota_visite_pct", "expr": "round(coalesce(visita, 0) / totale * 100, 1)"}]}, 1860, 260)
    c.op("ord", "sort", {"by": "totale", "descending": True}, 2120, 260)
    c.output("CRM Workload by Agent and Contact Type", 2380, 260)
    c.chain("src-crm", "agg", "j-r", "nome", "solo-agenti", "piv", "tot", "ord", "out"); c.edge(network, "j-r", "right")
    out.append(dict(name="CRM Workload by Agent and Contact Type", cron=None, c=c,
                    description="The CRM contact log reduced to a readable matrix: how many contacts each agent makes, by which means, with the share of field visits."))

    # 5. quantities sold by channel and depot, month by month (pivot)
    c = Builder(ds, outputs_in)
    c.source("src-righe", "order_rows", 40, 260)
    c.op("cast", "cast", CAST_LINES, 300, 260)
    c.op("riga", "rename", {"mapping": {"id": "riga_id"}}, 560, 260)
    orders = c.table("ordini", "orders", *ORDER, 40, 520)
    c.op("j", "join", JOIN("ordine_id"), 820, 260)
    c.op("evasi", "filter", {"column": "stato", "operator": "eq", "value": "evaso"}, 1080, 260)
    c.op("mese", "compute", {"columns": [{"name": "anno_mese", "expr": MONTH}]}, 1340, 260)
    c.op("piv", "pivot", {"index": ["canale", "anno_mese"], "on": ["deposito"], "values": "quantita", "func": "sum"}, 1600, 260)
    c.op("ord", "sort", {"by": "anno_mese", "descending": False}, 1860, 260)
    c.output("Quantities by Channel and Depot", 2120, 260)
    c.chain("src-righe", "cast", "riga", "j", "evasi", "mese", "piv", "ord", "out"); c.edge(orders, "j", "right")
    out.append(dict(name="Quantities by Channel and Depot", cron=None, c=c,
                    description="Fulfilled quantities per channel and month, one column per depot: a pivot straight from the order lines."))

    # 6. the 20 products with the highest revenue
    c = Builder(ds, outputs_in)
    lines_with_revenue(c)
    products = c.table("prodotti", "products", ["id", "codice", "nome", "categoria"], {"id": "prodotto_id"}, 40, 520)
    c.op("j", "join", JOIN("prodotto_id"), 1080, 260)
    c.op("agg", "group_by", {"by": ["codice", "nome", "categoria"], "aggregations": [
        {"column": "ricavo_netto", "func": "sum", "alias": "ricavo"}, {"column": "quantita", "func": "sum", "alias": "quantita"},
        {"column": "ordine_id", "func": "n_unique", "alias": "ordini"}]}, 1340, 260)
    c.op("ord", "sort", {"by": "ricavo", "descending": True}, 1600, 260)
    c.op("top", "limit", {"n": 20}, 1860, 260)
    c.output("Top 20 Products by Revenue", 2120, 260)
    c.chain("ricavo", "j", "agg", "ord", "top", "out"); c.edge(products, "j", "right")
    out.append(dict(name="Top 20 Products by Revenue", cron="30 5 * * *", c=c,
                    description="The twenty products that bring in the most revenue, with quantity and number of orders. A sort and a limit, nothing more."))

    # 7. agent leaderboard
    c = Builder(ds, outputs_in)
    lines_with_revenue(c)
    orders = c.table("ordini", "orders", *ORDER, 40, 520)
    c.op("j-o", "join", JOIN("ordine_id"), 1080, 260)
    c.op("evasi", "filter", {"column": "stato", "operator": "eq", "value": "evaso"}, 1340, 260)
    c.op("agg", "group_by", {"by": ["agente_id"], "aggregations": [
        {"column": "ricavo_netto", "func": "sum", "alias": "ricavo"}, {"column": "ordine_id", "func": "n_unique", "alias": "ordini"},
        {"column": "cliente_id", "func": "n_unique", "alias": "clienti"}]}, 1600, 260)
    network = c.table("rete", "sales_network", ["id", "nome", "cognome", "area"], {"id": "agente_id"}, 40, 780)
    c.op("j-r", "join", JOIN("agente_id"), 1860, 260)
    c.op("nome", "compute", {"columns": [{"name": "agente", "expr": "cognome || ' ' || nome"},
                                         {"name": "ricavo_per_ordine", "expr": "round(ricavo / ordini, 2)"}]}, 2120, 260)
    c.op("sel", "select", {"columns": ["area", "agente", "ricavo", "ordini", "clienti", "ricavo_per_ordine"]}, 2380, 260)
    c.op("ord", "sort", {"by": "ricavo", "descending": True}, 2640, 260)
    c.output("Agent Leaderboard", 2900, 260)
    c.chain("ricavo", "j-o", "evasi", "agg", "j-r", "nome", "sel", "ord", "out")
    c.edge(orders, "j-o", "right"); c.edge(network, "j-r", "right")
    out.append(dict(name="Agent Leaderboard", cron=None, c=c,
                    description="Revenue, orders and customers per agent, best first. The aggregation comes before the join with the sales force, so the join is on hundreds of rows, not millions."))

    # 8. order state month by month
    c = Builder(ds, outputs_in)
    c.source("src-ordini", "orders", 40, 260)
    c.op("mese", "compute", {"columns": [{"name": "anno_mese", "expr": MONTH}]}, 300, 260)
    c.op("agg", "group_by", {"by": ["anno_mese", "stato"], "aggregations": [{"column": "id", "func": "count", "alias": "ordini"}]}, 560, 260)
    c.op("piv", "pivot", {"index": ["anno_mese"], "on": ["stato"], "values": "ordini", "func": "sum"}, 820, 260)
    c.op("ord", "sort", {"by": "anno_mese", "descending": True}, 1080, 260)
    c.output("Order Fulfilment by Month", 1340, 260)
    c.chain("src-ordini", "mese", "agg", "piv", "ord", "out")
    out.append(dict(name="Order Fulfilment by Month", cron=None, c=c,
                    description="How many orders were fulfilled, in progress or cancelled, month by month: the order book's health at a glance."))

    # 9. customers acquired by macro-area and year — PRIVATE ⋈ PUBLIC (regions)
    c = Builder(ds, outputs_in)
    c.source("src-clienti", "customers", 40, 260)
    c.op("anno", "compute", {"columns": [{"name": "anno", "expr": "EXTRACT(YEAR FROM data_acquisizione)"}]}, 300, 260)
    c.source("src-regioni", "regions", 40, 520)
    c.op("j", "join", JOIN("regione"), 560, 260)
    c.op("agg", "group_by", {"by": ["macroarea", "regione", "anno"], "aggregations": [{"column": "id", "func": "count", "alias": "clienti"}]}, 820, 260)
    c.op("piv", "pivot", {"index": ["macroarea", "regione"], "on": ["anno"], "values": "clienti", "func": "sum"}, 1080, 260)
    c.op("ord", "sort", {"by": "regione", "descending": False}, 1340, 260)
    c.output("Customer Acquisition by Region and Year", 1600, 260)
    c.chain("src-clienti", "anno", "j", "agg", "piv", "ord", "out"); c.edge("src-regioni", "j", "right")
    out.append(dict(name="Customer Acquisition by Region and Year", cron=None, c=c,
                    description="New customers per region and year, grouped by macro-area: the CRM's private data joined with the public regions table."))

    # 10. sales by weekday — PRIVATE ⋈ PUBLIC (calendar)
    c = Builder(ds, outputs_in)
    lines_with_revenue(c)
    orders = c.table("ordini", "orders", *ORDER, 40, 520)
    c.op("j-o", "join", JOIN("ordine_id"), 1080, 260)
    cal = c.table("cal", "calendar", ["giorno", "fine_settimana", "festivo", "nome_giorno"], {"giorno": "data_ordine"}, 40, 780)
    c.op("j-cal", "join", JOIN("data_ordine"), 1340, 260)
    c.op("agg", "group_by", {"by": ["canale", "nome_giorno", "fine_settimana"], "aggregations": [
        {"column": "ricavo_netto", "func": "sum", "alias": "ricavo"}, {"column": "ordine_id", "func": "n_unique", "alias": "ordini"}]}, 1600, 260)
    c.op("medio", "compute", {"columns": [{"name": "ricavo_per_ordine", "expr": "round(ricavo / ordini, 2)"}]}, 1860, 260)
    c.op("ord", "sort", {"by": "ricavo", "descending": True}, 2120, 260)
    c.output("Sales by Weekday and Channel", 2380, 260)
    c.chain("ricavo", "j-o", "j-cal", "agg", "medio", "ord", "out")
    c.edge(orders, "j-o", "right"); c.edge(cal, "j-cal", "right")
    out.append(dict(name="Sales by Weekday and Channel", cron=None, c=c,
                    description="Revenue per weekday and channel, with weekend and holiday flags from the public calendar table."))

    # 11. average discount by channel and quarter — PRIVATE ⋈ PUBLIC (calendar)
    c = Builder(ds, outputs_in)
    lines_with_revenue(c)
    orders = c.table("ordini", "orders", *ORDER, 40, 520)
    c.op("j-o", "join", JOIN("ordine_id"), 1080, 260)
    cal = c.table("cal", "calendar", ["giorno", "anno", "trimestre"], {"giorno": "data_ordine"}, 40, 780)
    c.op("j-cal", "join", JOIN("data_ordine"), 1340, 260)
    c.op("agg", "group_by", {"by": ["anno", "trimestre", "canale"], "aggregations": [
        {"column": "sconto_pct", "func": "mean", "alias": "sconto_medio_pct"}, {"column": "ricavo_netto", "func": "sum", "alias": "ricavo"}]}, 1600, 260)
    c.op("arr", "compute", {"columns": [{"name": "sconto_medio_pct", "expr": "round(sconto_medio_pct, 2)"},
                                        {"name": "anno_trimestre", "expr": "anno * 10 + trimestre"}]}, 1860, 260)
    c.op("ord", "sort", {"by": "anno_trimestre", "descending": False}, 2120, 260)
    c.output("Discount by Channel and Quarter", 2380, 260)
    c.chain("ricavo", "j-o", "j-cal", "agg", "arr", "ord", "out")
    c.edge(orders, "j-o", "right"); c.edge(cal, "j-cal", "right")
    out.append(dict(name="Discount by Channel and Quarter", cron=None, c=c,
                    description="Average discount granted per channel and quarter, next to the revenue it bought."))
    return out


def create_flows(project: int, engine: str, ds: dict[str, dict], outputs_in: int) -> list[dict]:
    existing = {f["name"]: f for f in gw.must("GET", "/flows") if f["project_id"] == project}
    created = []
    for spec in flows(ds, outputs_in):
        if spec["name"] in existing:
            created.append({**spec, "id": existing[spec["name"]]["id"]})
            continue
        r = gw.must("POST", f"/projects/{project}/flows", {
            "name": spec["name"], "description": spec["description"],
            "definition": spec["c"].definition(), "engine": engine})
        log(f"flow created: {spec['name']}")
        if spec["cron"]:
            s, err = gw.call("PUT", f"/flows/{r['id']}/schedule", {"cron": spec["cron"], "production_engine": engine})
            if s >= 400:
                log(f"  schedule not set ({s}): {err}")
            else:
                log(f"  scheduled: {spec['cron']}")
        created.append({**spec, "id": r["id"]})
    return created


# ── 6. the runs and the views ─────────────────────────────────────────────────
def run_once(created_flows: list[dict], outputs_in: int) -> list[str]:
    """Runs once the flows whose produced table does not exist yet. Returns the
    names of the ones that failed: starting the container again retries only those."""
    present = {d["name"] for d in gw.must("GET", "/datasources") if d["project_id"] == outputs_in}
    failed = []
    for f in created_flows:
        if f["c"].output_name in present:
            continue
        s, r = gw.call("POST", f"/flows/{f['id']}/run-now", {})
        if s >= 400:
            log(f"run not started for «{f['name']}» ({s}): {r}")
            failed.append(f["name"])
            continue
        run_id = r.get("run_id")
        deadline = time.time() + 40 * 60
        status = "?"
        while time.time() < deadline:
            s2, run = gw.call("GET", f"/runs/{run_id}")
            status = run.get("status", "?") if isinstance(run, dict) else "?"
            if status in ("SUCCESS", "FAILURE"):
                break
            time.sleep(6)
        if status == "SUCCESS":
            log(f"ran: {f['name']}")
        else:
            error = (run.get("error") or "")[:300] if isinstance(run, dict) else ""
            log(f"FAILED: {f['name']} — {status} {error}")
            failed.append(f["name"])
    return failed


VIEWS = [
    ("Margin watch — categories under 30%", "Margin by Category and Month",
     "The categories whose margin dips under 30%: the list the sales team should go back to.",
     {"filters": [{"column": "margine_pct", "operator": "lt", "value": "30", "value2": ""}]}),
    ("Margin by category — month by month", "Margin by Category and Month",
     "The margin turned into a matrix: categories in rows, months in columns.",
     {"pivotOn": True, "pivot": {"index": ["categoria"], "on": ["anno_mese"], "values": "margine", "func": "sum"}}),
    ("Cost to serve — where it hurts", "Cost to Serve by Channel",
     "Channels and depots where serving costs more than 6% of revenue.",
     {"filters": [{"column": "incidenza_pct", "operator": "gt", "value": "6", "value2": ""}]}),
    ("Top agents — Nord-Ovest", "Agent Leaderboard",
     "The leaderboard restricted to one area.",
     {"filters": [{"column": "area", "operator": "eq", "value": "Nord-Ovest", "value2": ""}]}),
]


def create_views(project: int, engine: str, outputs_in: int) -> None:
    outputs = {d["name"]: d for d in gw.must("GET", "/datasources") if d["project_id"] == outputs_in}
    existing = {v["name"] for v in gw.must("GET", f"/projects/{project}/saved-views")}
    for name, source, descr, extra in VIEWS:
        if name in existing:
            continue
        d = outputs.get(source)
        if not d:
            log(f"view «{name}» skipped: table «{source}» is missing (did its flow run?)")
            continue
        spec = {"engine": engine, "filters": [], "computedFields": [], "pivotOn": False,
                "pivot": {"index": [""], "on": [""], "values": "", "func": "sum"}, "outline": False}
        spec.update(extra)
        gw.must("POST", f"/projects/{project}/saved-views",
                {"name": name, "description": descr, "datasource_id": d["id"], "spec": json.dumps(spec)})
        log(f"view saved: {name}")


# ── all together ──────────────────────────────────────────────────────────────
def main() -> None:
    log(f"setting up the sample data (scale «{SCALE}»)")
    prepare_databases()
    gw.wait()
    gw.login()

    sample = folder("Sample", None,
                    "Sample data: a cured-meat company's ERP (Postgres) and CRM (ClickHouse), plus public reference tables. "
                    "Everything here is generated. Who can see it is up to the administrator.")
    flows_dir = folder("Flows", sample, "The sample pipelines. Three of them are scheduled.")
    private = folder("Private tables", sample, "The company's own data from the ERP and the CRM, and what the flows derive from it.")
    public = folder("Public tables", sample, "Reference data anyone could publish: a calendar with Italian holidays, the twenty regions.")

    conn_pg = connection(sample, "Sample ERP (Postgres)", {
        "description": "The order management system: orders, order lines, products, prices, costs. Fake data.",
        "db_type": "postgresql", "host": PG["host"], "port": PG["port"], "username": PG["user"],
        "password": PG["password"], "database": PG["dbname"], "db_schema": "vendite"})
    conn_ch = connection(sample, "Sample CRM (ClickHouse)", {
        "description": "The customer relationship system: customers, sales force, contact log. Fake data.",
        "db_type": "clickhouse", "host": CH["host"], "port": CH["port"], "username": CH["user"],
        "password": CH["password"], "database": CH["db"]})

    ids = []
    for name, conn, table, descr in PRIVATE:
        ids.append(datasource(private, name, conn_pg if conn == "pg" else conn_ch, table, descr))
    for name, conn, table, descr in PUBLIC:
        ids.append(datasource(public, name, conn_pg, table, descr))
    log("waiting for the tables to finish importing…")
    by_id = wait_for_ingest(ids)
    by_name = {d["name"]: d for d in by_id.values() if d["project_id"] in (private, public)}
    describe(by_name)

    engine = choose_engine()
    log(f"engine for the flows: {engine}")
    created = create_flows(flows_dir, engine, by_name, private)
    failed = []
    if RUN_FLOWS:
        log("running the flows once, so the tables they produce exist…")
        failed = run_once(created, private)
    create_views(sample, engine, private)
    log("DONE. The «Sample» folder is ready: Flows, Private tables, Public tables, connections and views. "
        "No permission granted: that is the administrator's call.")
    if failed:
        sys.exit(f"{len(failed)} flows did not succeed ({', '.join(failed)}): see Runs; "
                 "starting the container again retries only those")


if __name__ == "__main__":
    main()
