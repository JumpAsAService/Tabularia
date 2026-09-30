#!/usr/bin/env python3
"""Inizializza la cartella «Sample» di Tabularia sui database di esempio.

Gira nel contenitore `sampledb-init` (profilo compose `samples`), una volta,
dopo che gateway e database di esempio sono sani. Fa, in ordine:

  1. se i database sono vuoti, li riempie alla scala richiesta (SAMPLES_SCALE)
     con `generate.py`; se sono già pieni li lascia stare e aggiunge solo le
     tabelle pubbliche se mancano;
  2. crea le cartelle:  Sample / Flows, Private tables, Public tables;
  3. crea le due connessioni (gestionale Postgres, CRM ClickHouse);
  4. importa le tabelle: quelle aziendali in «Private tables», quelle di
     riferimento aperte in «Public tables» — e ne descrive ALCUNE, non tutte,
     così si vede la differenza fra una datasource pronta per l'assistente e una
     che non lo è;
  5. costruisce undici flussi in «Flows» — con join fra dati privati e pubblici,
     nodo sql, pivot, calcoli — e ne schedula tre;
  6. li esegue una volta, così le tabelle prodotte esistono, e su quelle salva
     le viste (i report) nella cartella Sample.

Idempotente: ogni oggetto si cerca per nome prima di crearlo, quindi rilanciare
il contenitore non duplica niente. NON concede permessi: chi può vedere la
cartella lo decide l'amministratore.

Solo libreria standard verso il gateway (urllib): l'immagine ha già quello che
serve al generatore, e non vale la pena aggiungerci altro.
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
SCALE = (os.getenv("SAMPLES_SCALE") or "small").lower()
ENGINE_VOLUTO = (os.getenv("SAMPLES_ENGINE") or "").strip().lower()
ESEGUI_FLUSSI = (os.getenv("SAMPLES_RUN_FLOWS") or "true").lower() in ("1", "true", "yes", "si", "sì")

PG = dict(host=os.getenv("PG_HOST", "sampledb-postgres"), port=int(os.getenv("PG_PORT", "5432")),
          dbname=os.getenv("PG_DB", "shop"), user=os.getenv("PG_USER", "shop"), password=os.getenv("PG_PASSWORD", "shop"))
CH = dict(host=os.getenv("CH_HOST", "sampledb-clickhouse"), port=int(os.getenv("CH_PORT", "8123")),
          db=os.getenv("CH_DB", "analytics"), user=os.getenv("CH_USER", "analytics"), password=os.getenv("CH_PASSWORD", "analytics"))

T0 = time.time()


def log(msg: str) -> None:
    print(f"[{int(time.time() - T0):4d}s] {msg}", flush=True)


# ── il gateway ────────────────────────────────────────────────────────────────
class Gateway:
    def __init__(self) -> None:
        self.token: str | None = None

    def call(self, metodo: str, path: str, body=None, timeout: int = 120):
        req = urllib.request.Request(GATEWAY + path, method=metodo)
        if self.token:
            req.add_header("Authorization", "Bearer " + self.token)
        dati = None
        if body is not None:
            dati = json.dumps(body).encode()
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, dati, timeout=timeout) as r:
                testo = r.read().decode()
                return r.status, (json.loads(testo) if testo else None)
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()[:300]

    def aspetta(self, minuti: int = 10) -> None:
        fine = time.time() + minuti * 60
        while time.time() < fine:
            try:
                with urllib.request.urlopen(GATEWAY + "/health", timeout=5) as r:
                    if r.status == 200:
                        return
            except Exception:
                pass
            time.sleep(3)
        sys.exit(f"il gateway non risponde su {GATEWAY} dopo {minuti} minuti")

    def login(self) -> None:
        if not ADMIN_EMAIL or not ADMIN_PASSWORD:
            sys.exit("servono AUTH__ADMIN_EMAIL e AUTH__ADMIN_PASSWORD (le stesse del gateway)")
        s, r = self.call("POST", "/auth/login", {"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD})
        if s != 200:
            sys.exit(f"login dell'amministratore fallito: {s} {r}")
        self.token = r["access_token"]

    def deve(self, metodo: str, path: str, body=None, **kw):
        s, r = self.call(metodo, path, body, **kw)
        if s >= 400:
            sys.exit(f"{metodo} {path} → {s}: {r}")
        return r


gw = Gateway()


# ── 1. i database di esempio ──────────────────────────────────────────────────
def prepara_database() -> None:
    import psycopg2

    pg = psycopg2.connect(**PG)
    with pg.cursor() as cur:
        cur.execute("SELECT to_regclass('vendite.ordini')")
        esiste = cur.fetchone()[0] is not None
        righe = 0
        if esiste:
            cur.execute("SELECT count(*) FROM vendite.ordini")
            righe = cur.fetchone()[0]
    if righe == 0:
        log(f"database vuoti: genero i dati alla scala «{SCALE}» (può volerci: small secondi, robusta minuti)")
        pg.close()
        os.environ["SAMPLES_SCALE"] = SCALE
        import generate  # noqa: WPS433 — stesso contenitore, stesso codice

        generate.main()
        return
    log(f"database già pieni ({righe:,} ordini): non rigenero")
    import generate

    if generate.ensure_ref(pg):
        log("aggiunte le tabelle pubbliche (calendario, regioni) che mancavano")
    pg.close()


# ── 2. le cartelle ────────────────────────────────────────────────────────────
def cartella(nome: str, parent_id: int | None, descrizione: str) -> int:
    for p in gw.deve("GET", "/projects"):
        if p["name"] == nome and p.get("parent_id") == parent_id:
            return p["id"]
    r = gw.deve("POST", "/projects", {"name": nome, "description": descrizione, "parent_id": parent_id})
    log(f"cartella creata: {nome}")
    return r["id"]


# ── 3. le connessioni ─────────────────────────────────────────────────────────
def connessione(progetto: int, nome: str, corpo: dict) -> int:
    for c in gw.deve("GET", "/connections"):
        if c["name"] == nome and c.get("project_id") == progetto:
            return c["id"]
    r = gw.deve("POST", f"/projects/{progetto}/connections", {"name": nome, **corpo})
    log(f"connessione creata: {nome}")
    return r["id"]


# ── 4. le tabelle ─────────────────────────────────────────────────────────────
PRIVATE = [  # (nome, connessione, tabella, descrizione o None = volutamente non descritta)
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
COLONNE = {
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


def datasource(progetto: int, nome: str, conn_id: int, tabella: str, descrizione: str | None) -> int:
    for d in gw.deve("GET", "/datasources"):
        if d["name"] == nome and d["project_id"] == progetto:
            return d["id"]
    corpo = {"name": nome, "connection_id": conn_id, "source_type": "table", "source_ref": tabella}
    if descrizione:
        corpo["description"] = descrizione
    r = gw.deve("POST", f"/projects/{progetto}/datasources/database", corpo)
    log(f"tabella importata: {nome} ← {tabella}")
    return r["id"]


def aspetta_ingest(ids: list[int], minuti: int = 60) -> dict[int, dict]:
    fine = time.time() + minuti * 60
    while time.time() < fine:
        per_id = {d["id"]: d for d in gw.deve("GET", "/datasources")}
        pronte = [i for i in ids if per_id.get(i) and per_id[i].get("rows") is not None and not per_id[i].get("refreshing")]
        if len(pronte) == len(ids):
            return per_id
        # righe assenti e nessun import in corso: il primo import è fallito
        for i in ids:
            d = per_id.get(i)
            if d and d.get("rows") is None and not d.get("refreshing"):
                s, runs = gw.call("GET", f"/datasources/{i}/runs")
                ultimo = (runs or [{}])[0] if isinstance(runs, list) else {}
                if ultimo.get("status") == "FAILURE":
                    sys.exit(f"import fallito per «{d['name']}»: {ultimo.get('error')}")
        time.sleep(10)
    sys.exit("l'importazione delle tabelle non è finita in tempo")


def descrivi(per_nome: dict[str, dict]) -> None:
    for nome, colonne in COLONNE.items():
        d = per_nome.get(nome)
        if not d or d.get("column_descriptions"):
            continue
        presenti = {c["name"] for c in (d.get("columns") or [])}
        gw.deve("PATCH", f"/datasources/{d['id']}", {"column_descriptions": {k: v for k, v in colonne.items() if k in presenti}})
    log(f"descritte {len(COLONNE)} tabelle su {len(PRIVATE) + len(PUBLIC)}: le altre restano senza, di proposito")


# ── 5. i flussi ───────────────────────────────────────────────────────────────
def scegli_engine() -> str:
    if ENGINE_VOLUTO:
        return ENGINE_VOLUTO
    consentiti = {e["engine_id"] for e in gw.deve("GET", "/engine-policy") if e.get("allowed")}
    for e in ("polars", "duckdb", "chdb", "clickhouse", "bigquery"):
        if e in consentiti:
            return e
    return "polars"


class Costruttore:
    """Nodi e archi come li scriverebbe l'editor, con le sorgenti prese dal catalogo.

    Regola dei join, valida su tutti i motori: la chiave di destra si rinomina
    come quella di sinistra PRIMA di unire (`id` → `ordine_id`) e si unisce con
    `on`, così resta UNA colonna chiave (come SQL USING). Con `left_on`/`right_on`
    restano entrambe, e un secondo join sulla stessa chiave si scontra
    (`prodotto_id_right` già esistente): ClickHouse lo tollera, Polars no."""

    def __init__(self, ds: dict[str, dict], uscite_in: int):
        self.ds, self.uscite_in = ds, uscite_in
        self.nodi, self.archi = [], []
        self.nome_uscita = ""

    def sorgente(self, nid, nome, x, y):
        d = self.ds[nome]
        self.nodi.append({"id": nid, "type": "source", "position": {"x": x, "y": y}, "data": {
            "bucket": d["bucket"], "datasetId": None, "parquetKey": d["key"], "filename": d["name"],
            "rows": d["rows"], "columns": d["columns"], "datasourceId": d["id"], "sample": None}})
        return nid

    def op(self, nid, tipo, params, x, y):
        self.nodi.append({"id": nid, "type": "operation", "position": {"x": x, "y": y},
                          "data": {"opType": tipo, "params": params}})
        return nid

    def tabella(self, nid, nome, colonne, rinomina, x, y):
        """Sorgente + select delle sole colonne che servono + rename della chiave.
        Torna l'ultimo nodo della catenella, pronto per il lato destro di un join."""
        ultimo = self.sorgente(f"src-{nid}", nome, x, y)
        if colonne:
            self.op(f"sel-{nid}", "select", {"columns": colonne}, x + 260, y)
            self.arco(ultimo, f"sel-{nid}"); ultimo = f"sel-{nid}"
        if rinomina:
            self.op(f"ren-{nid}", "rename", {"mapping": rinomina}, x + 520, y)
            self.arco(ultimo, f"ren-{nid}"); ultimo = f"ren-{nid}"
        return ultimo

    def uscita(self, nome, x, y):
        self.nome_uscita = nome
        self.nodi.append({"id": "out", "type": "output", "position": {"x": x, "y": y}, "data": {
            "destType": "datasource", "mode": "append", "projectId": self.uscite_in, "name": nome, "overwrite": True}})
        return "out"

    def arco(self, a, b, presa="left"):
        self.archi.append({"id": f"e-{a}-{presa}-{b}", "source": a, "target": b, "sourceHandle": "out", "targetHandle": presa})

    def catena(self, *ids):
        for a, b in zip(ids, ids[1:]):
            self.arco(a, b)

    def definizione(self):
        return json.dumps({"nodes": self.nodi, "edges": self.archi})


# i parametri hanno la forma che l'editor scrive e sa mostrare (il sort: UNA
# colonna e un booleano); il motore accetterebbe anche liste, ma il pannello
# del nodo le mostrerebbe vuote
MESE = "EXTRACT(YEAR FROM data_ordine) * 100 + EXTRACT(MONTH FROM data_ordine)"
RICAVO = "quantita * prezzo_unitario * (1 - sconto_pct / 100)"
CAST_RIGHE = {"columns": {"quantita": "float", "prezzo_unitario": "float", "sconto_pct": "float"}}
ORDINE = (["id", "data_ordine", "cliente_id", "agente_id", "canale", "stato", "deposito"], {"id": "ordine_id"})
# chiave con lo stesso nome dai due lati → `on` (una colonna sola, come SQL USING);
# `left_on`/`right_on` terrebbe ENTRAMBE le colonne e al secondo join si scontrano
JOIN = lambda chiave: {"how": "inner", "on": [chiave]}  # noqa: E731


def righe_con_ricavo(c: Costruttore, y: int = 260) -> str:
    """L'inizio di quasi tutti i flussi: righe d'ordine, numeri veri, ricavo netto."""
    c.sorgente("src-righe", "order_rows", 40, y)
    c.op("cast", "cast", CAST_RIGHE, 300, y)
    c.op("riga", "rename", {"mapping": {"id": "riga_id"}}, 560, y)
    c.op("ricavo", "compute", {"columns": [{"name": "ricavo_netto", "expr": RICAVO}]}, 820, y)
    c.catena("src-righe", "cast", "riga", "ricavo")
    return "ricavo"


def flussi(ds: dict[str, dict], uscite_in: int) -> list[dict]:
    """Undici flussi che hanno un senso sul modello dei salumi. Le espressioni
    usano solo SQL portabile (EXTRACT, CASE WHEN, ||, round, coalesce) così
    girano sullo stesso motore che l'installazione consente, quale che sia."""
    out = []

    # 1. margine per categoria e mese: 5 sorgenti, 6 join, un nodo sql
    c = Costruttore(ds, uscite_in)
    righe_con_ricavo(c)
    ordini = c.tabella("ordini", "orders", *ORDINE, 40, 470)
    c.op("j-ordini", "join", JOIN("ordine_id"), 1080, 260)
    c.op("evasi", "filter", {"column": "stato", "operator": "eq", "value": "evaso"}, 1340, 260)
    prodotti = c.tabella("prodotti", "products", ["id", "codice", "nome", "categoria", "tipo", "peso_medio_kg", "unita_misura"], {"id": "prodotto_id"}, 40, 660)
    c.op("j-prod", "join", JOIN("prodotto_id"), 1600, 260)
    listino = c.tabella("listino", "price_list", ["prodotto_id", "valido_dal", "valido_al", "prezzo_listino"], {"valido_dal": "listino_dal", "valido_al": "listino_al"}, 40, 850)
    c.op("j-list", "join", JOIN("prodotto_id"), 1860, 260)
    # i costi hanno più periodi per prodotto e nessuna data di fine: la fine di
    # un periodo è l'inizio del successivo dello stesso prodotto
    costi = c.tabella("costi", "production_costs", ["prodotto_id", "valido_dal", "costo_materia_prima_kg", "costo_lavorazione_kg", "costo_confezionamento_pz"], {"valido_dal": "costo_dal"}, 40, 1040)
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
        {"name": "anno_mese", "expr": MESE}]}, 2900, 260)
    c.op("agg", "group_by", {"by": ["categoria", "anno_mese"], "aggregations": [
        {"column": "ricavo_netto", "func": "sum", "alias": "ricavo"}, {"column": "costo_industriale", "func": "sum", "alias": "costo"},
        {"column": "margine", "func": "sum", "alias": "margine"}, {"column": "chili", "func": "sum", "alias": "chili"},
        {"column": "ordine_id", "func": "n_unique", "alias": "ordini"}]}, 3160, 260)
    c.op("pct", "compute", {"columns": [{"name": "margine_pct", "expr": "round(margine / ricavo * 100, 2)"}]}, 3420, 260)
    c.op("ord", "sort", {"by": "anno_mese", "descending": False}, 3680, 260)
    c.uscita("Margin by Category and Month", 3940, 260)
    c.catena("ricavo", "j-ordini", "evasi", "j-prod", "j-list", "j-costi", "valido", "cast-peso", "calc", "agg", "pct", "ord", "out")
    c.arco(ordini, "j-ordini", "right"); c.arco(prodotti, "j-prod", "right"); c.arco(listino, "j-list", "right")
    c.catena(costi, "cast-costi", "j-ultimo", "costo-al"); c.arco("cast-costi", "ultimo"); c.arco("ultimo", "j-ultimo", "right")
    c.arco("costo-al", "j-costi", "right")
    out.append(dict(name="Margin by Category and Month", cron="0 6 1 * *", c=c,
                    description="The real margin per category and month: revenue net of discount minus industrial cost per kilo, using the list price and the production cost in force on the day of the order. Five sources, six joins and a SQL step."))

    # 2. costo di servizio per canale e deposito
    c = Costruttore(ds, uscite_in)
    righe_con_ricavo(c)
    costi = c.tabella("costi", "commercial_costs", ["riga_ordine_id", "provvigione_importo", "costo_logistica", "costo_promozionale"], {"riga_ordine_id": "riga_id"}, 40, 560)
    c.op("cast-c", "cast", {"columns": {"provvigione_importo": "float", "costo_logistica": "float", "costo_promozionale": "float"}}, 820, 560)
    c.op("j-costi", "join", JOIN("riga_id"), 1080, 260)
    ordini = c.tabella("ordini", "orders", *ORDINE, 40, 800)
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
    c.uscita("Cost to Serve by Channel", 2900, 260)
    c.catena("ricavo", "j-costi", "j-ordini", "evasi", "servizio", "agg", "pct", "ord", "out")
    c.catena(costi, "cast-c"); c.arco("cast-c", "j-costi", "right"); c.arco(ordini, "j-ordini", "right")
    out.append(dict(name="Cost to Serve by Channel", cron="0 7 * * 1", c=c,
                    description="What it costs to serve each channel and depot: commissions, logistics and promotions on the single order line."))

    # 3. segmenti RFM per regione (aggrega PRIMA di unire l'anagrafica)
    c = Costruttore(ds, uscite_in)
    righe_con_ricavo(c)
    ordini = c.tabella("ordini", "orders", *ORDINE, 40, 520)
    c.op("j-ordini", "join", JOIN("ordine_id"), 1080, 260)
    c.op("evasi", "filter", {"column": "stato", "operator": "eq", "value": "evaso"}, 1340, 260)
    c.op("per-cliente", "group_by", {"by": ["cliente_id"], "aggregations": [
        {"column": "ricavo_netto", "func": "sum", "alias": "valore"}, {"column": "ordine_id", "func": "n_unique", "alias": "ordini"},
        {"column": "data_ordine", "func": "max", "alias": "ultimo_ordine"}]}, 1600, 260)
    clienti = c.tabella("clienti", "customers", ["id", "ragione_sociale", "canale", "provincia", "regione", "agente_id"], {"id": "cliente_id"}, 40, 780)
    c.op("j-cl", "join", JOIN("cliente_id"), 1860, 260)
    c.op("segmento", "sql", {"query": (
        "SELECT *, CASE WHEN ordini >= 12 AND EXTRACT(YEAR FROM ultimo_ordine) = 2026 AND EXTRACT(MONTH FROM ultimo_ordine) >= 4 THEN 'Champion' "
        "WHEN ordini >= 6 AND EXTRACT(YEAR FROM ultimo_ordine) = 2026 THEN 'Loyal' "
        "WHEN EXTRACT(YEAR FROM ultimo_ordine) < 2025 THEN 'Dormant' ELSE 'At risk' END AS segmento FROM self")}, 2120, 260)
    c.op("agg", "group_by", {"by": ["regione", "segmento"], "aggregations": [
        {"column": "valore", "func": "sum", "alias": "valore"}, {"column": "cliente_id", "func": "n_unique", "alias": "clienti"}]}, 2380, 260)
    c.op("piv", "pivot", {"index": ["regione"], "on": ["segmento"], "values": "valore", "func": "sum"}, 2640, 260)
    c.uscita("Customer Segments by Region", 2900, 260)
    c.catena("ricavo", "j-ordini", "evasi", "per-cliente", "j-cl", "segmento", "agg", "piv", "out")
    c.arco(ordini, "j-ordini", "right"); c.arco(clienti, "j-cl", "right")
    out.append(dict(name="Customer Segments by Region (RFM)", cron=None, c=c,
                    description="Recency, frequency and monetary per customer, the segmentation rule written in plain SQL, the result pivoted region × segment."))

    # 4. carico CRM per agente e mezzo
    c = Costruttore(ds, uscite_in)
    c.sorgente("src-crm", "crm_activities", 40, 260)
    c.op("agg", "group_by", {"by": ["agente_id", "tipo"], "aggregations": [{"column": "id", "func": "count", "alias": "contatti"}]}, 300, 260)
    rete = c.tabella("rete", "sales_network", ["id", "nome", "cognome", "ruolo", "area"], {"id": "agente_id"}, 40, 560)
    c.op("j-r", "join", JOIN("agente_id"), 820, 260)
    c.op("nome", "compute", {"columns": [{"name": "agente", "expr": "cognome || ' ' || nome"}]}, 1080, 260)
    c.op("solo-agenti", "filter", {"column": "ruolo", "operator": "eq", "value": "agente"}, 1340, 260)
    c.op("piv", "pivot", {"index": ["area", "agente"], "on": ["tipo"], "values": "contatti", "func": "sum"}, 1600, 260)
    c.op("tot", "compute", {"columns": [{"name": "totale", "expr": "coalesce(email, 0) + coalesce(reclamo, 0) + coalesce(sollecito, 0) + coalesce(telefonata, 0) + coalesce(visita, 0)"},
                                        {"name": "quota_visite_pct", "expr": "round(coalesce(visita, 0) / totale * 100, 1)"}]}, 1860, 260)
    c.op("ord", "sort", {"by": "totale", "descending": True}, 2120, 260)
    c.uscita("CRM Workload by Agent and Contact Type", 2380, 260)
    c.catena("src-crm", "agg", "j-r", "nome", "solo-agenti", "piv", "tot", "ord", "out"); c.arco(rete, "j-r", "right")
    out.append(dict(name="CRM Workload by Agent and Contact Type", cron=None, c=c,
                    description="The CRM contact log reduced to a readable matrix: how many contacts each agent makes, by which means, with the share of field visits."))

    # 5. quantità vendute per canale e deposito, mese per mese (pivot)
    c = Costruttore(ds, uscite_in)
    c.sorgente("src-righe", "order_rows", 40, 260)
    c.op("cast", "cast", CAST_RIGHE, 300, 260)
    c.op("riga", "rename", {"mapping": {"id": "riga_id"}}, 560, 260)
    ordini = c.tabella("ordini", "orders", *ORDINE, 40, 520)
    c.op("j", "join", JOIN("ordine_id"), 820, 260)
    c.op("evasi", "filter", {"column": "stato", "operator": "eq", "value": "evaso"}, 1080, 260)
    c.op("mese", "compute", {"columns": [{"name": "anno_mese", "expr": MESE}]}, 1340, 260)
    c.op("piv", "pivot", {"index": ["canale", "anno_mese"], "on": ["deposito"], "values": "quantita", "func": "sum"}, 1600, 260)
    c.op("ord", "sort", {"by": "anno_mese", "descending": False}, 1860, 260)
    c.uscita("Quantities by Channel and Depot", 2120, 260)
    c.catena("src-righe", "cast", "riga", "j", "evasi", "mese", "piv", "ord", "out"); c.arco(ordini, "j", "right")
    out.append(dict(name="Quantities by Channel and Depot", cron=None, c=c,
                    description="Fulfilled quantities per channel and month, one column per depot: a pivot straight from the order lines."))

    # 6. i 20 prodotti che fatturano di più
    c = Costruttore(ds, uscite_in)
    righe_con_ricavo(c)
    prodotti = c.tabella("prodotti", "products", ["id", "codice", "nome", "categoria"], {"id": "prodotto_id"}, 40, 520)
    c.op("j", "join", JOIN("prodotto_id"), 1080, 260)
    c.op("agg", "group_by", {"by": ["codice", "nome", "categoria"], "aggregations": [
        {"column": "ricavo_netto", "func": "sum", "alias": "ricavo"}, {"column": "quantita", "func": "sum", "alias": "quantita"},
        {"column": "ordine_id", "func": "n_unique", "alias": "ordini"}]}, 1340, 260)
    c.op("ord", "sort", {"by": "ricavo", "descending": True}, 1600, 260)
    c.op("top", "limit", {"n": 20}, 1860, 260)
    c.uscita("Top 20 Products by Revenue", 2120, 260)
    c.catena("ricavo", "j", "agg", "ord", "top", "out"); c.arco(prodotti, "j", "right")
    out.append(dict(name="Top 20 Products by Revenue", cron="30 5 * * *", c=c,
                    description="The twenty products that bring in the most revenue, with quantity and number of orders. A sort and a limit, nothing more."))

    # 7. classifica degli agenti
    c = Costruttore(ds, uscite_in)
    righe_con_ricavo(c)
    ordini = c.tabella("ordini", "orders", *ORDINE, 40, 520)
    c.op("j-o", "join", JOIN("ordine_id"), 1080, 260)
    c.op("evasi", "filter", {"column": "stato", "operator": "eq", "value": "evaso"}, 1340, 260)
    c.op("agg", "group_by", {"by": ["agente_id"], "aggregations": [
        {"column": "ricavo_netto", "func": "sum", "alias": "ricavo"}, {"column": "ordine_id", "func": "n_unique", "alias": "ordini"},
        {"column": "cliente_id", "func": "n_unique", "alias": "clienti"}]}, 1600, 260)
    rete = c.tabella("rete", "sales_network", ["id", "nome", "cognome", "area"], {"id": "agente_id"}, 40, 780)
    c.op("j-r", "join", JOIN("agente_id"), 1860, 260)
    c.op("nome", "compute", {"columns": [{"name": "agente", "expr": "cognome || ' ' || nome"},
                                         {"name": "ricavo_per_ordine", "expr": "round(ricavo / ordini, 2)"}]}, 2120, 260)
    c.op("sel", "select", {"columns": ["area", "agente", "ricavo", "ordini", "clienti", "ricavo_per_ordine"]}, 2380, 260)
    c.op("ord", "sort", {"by": "ricavo", "descending": True}, 2640, 260)
    c.uscita("Agent Leaderboard", 2900, 260)
    c.catena("ricavo", "j-o", "evasi", "agg", "j-r", "nome", "sel", "ord", "out")
    c.arco(ordini, "j-o", "right"); c.arco(rete, "j-r", "right")
    out.append(dict(name="Agent Leaderboard", cron=None, c=c,
                    description="Revenue, orders and customers per agent, best first. The aggregation comes before the join with the sales force, so the join is on hundreds of rows, not millions."))

    # 8. stato degli ordini mese per mese
    c = Costruttore(ds, uscite_in)
    c.sorgente("src-ordini", "orders", 40, 260)
    c.op("mese", "compute", {"columns": [{"name": "anno_mese", "expr": MESE}]}, 300, 260)
    c.op("agg", "group_by", {"by": ["anno_mese", "stato"], "aggregations": [{"column": "id", "func": "count", "alias": "ordini"}]}, 560, 260)
    c.op("piv", "pivot", {"index": ["anno_mese"], "on": ["stato"], "values": "ordini", "func": "sum"}, 820, 260)
    c.op("ord", "sort", {"by": "anno_mese", "descending": True}, 1080, 260)
    c.uscita("Order Fulfilment by Month", 1340, 260)
    c.catena("src-ordini", "mese", "agg", "piv", "ord", "out")
    out.append(dict(name="Order Fulfilment by Month", cron=None, c=c,
                    description="How many orders were fulfilled, in progress or cancelled, month by month: the order book's health at a glance."))

    # 9. clienti acquisiti per macroarea e anno — PRIVATO ⋈ PUBBLICO (regioni)
    c = Costruttore(ds, uscite_in)
    c.sorgente("src-clienti", "customers", 40, 260)
    c.op("anno", "compute", {"columns": [{"name": "anno", "expr": "EXTRACT(YEAR FROM data_acquisizione)"}]}, 300, 260)
    c.sorgente("src-regioni", "regions", 40, 520)
    c.op("j", "join", JOIN("regione"), 560, 260)
    c.op("agg", "group_by", {"by": ["macroarea", "regione", "anno"], "aggregations": [{"column": "id", "func": "count", "alias": "clienti"}]}, 820, 260)
    c.op("piv", "pivot", {"index": ["macroarea", "regione"], "on": ["anno"], "values": "clienti", "func": "sum"}, 1080, 260)
    c.op("ord", "sort", {"by": "regione", "descending": False}, 1340, 260)
    c.uscita("Customer Acquisition by Region and Year", 1600, 260)
    c.catena("src-clienti", "anno", "j", "agg", "piv", "ord", "out"); c.arco("src-regioni", "j", "right")
    out.append(dict(name="Customer Acquisition by Region and Year", cron=None, c=c,
                    description="New customers per region and year, grouped by macro-area: the CRM's private data joined with the public regions table."))

    # 10. vendite per giorno della settimana — PRIVATO ⋈ PUBBLICO (calendario)
    c = Costruttore(ds, uscite_in)
    righe_con_ricavo(c)
    ordini = c.tabella("ordini", "orders", *ORDINE, 40, 520)
    c.op("j-o", "join", JOIN("ordine_id"), 1080, 260)
    cal = c.tabella("cal", "calendar", ["giorno", "fine_settimana", "festivo", "nome_giorno"], {"giorno": "data_ordine"}, 40, 780)
    c.op("j-cal", "join", JOIN("data_ordine"), 1340, 260)
    c.op("agg", "group_by", {"by": ["canale", "nome_giorno", "fine_settimana"], "aggregations": [
        {"column": "ricavo_netto", "func": "sum", "alias": "ricavo"}, {"column": "ordine_id", "func": "n_unique", "alias": "ordini"}]}, 1600, 260)
    c.op("medio", "compute", {"columns": [{"name": "ricavo_per_ordine", "expr": "round(ricavo / ordini, 2)"}]}, 1860, 260)
    c.op("ord", "sort", {"by": "ricavo", "descending": True}, 2120, 260)
    c.uscita("Sales by Weekday and Channel", 2380, 260)
    c.catena("ricavo", "j-o", "j-cal", "agg", "medio", "ord", "out")
    c.arco(ordini, "j-o", "right"); c.arco(cal, "j-cal", "right")
    out.append(dict(name="Sales by Weekday and Channel", cron=None, c=c,
                    description="Revenue per weekday and channel, with weekend and holiday flags from the public calendar table."))

    # 11. sconto medio per canale e trimestre — PRIVATO ⋈ PUBBLICO (calendario)
    c = Costruttore(ds, uscite_in)
    righe_con_ricavo(c)
    ordini = c.tabella("ordini", "orders", *ORDINE, 40, 520)
    c.op("j-o", "join", JOIN("ordine_id"), 1080, 260)
    cal = c.tabella("cal", "calendar", ["giorno", "anno", "trimestre"], {"giorno": "data_ordine"}, 40, 780)
    c.op("j-cal", "join", JOIN("data_ordine"), 1340, 260)
    c.op("agg", "group_by", {"by": ["anno", "trimestre", "canale"], "aggregations": [
        {"column": "sconto_pct", "func": "mean", "alias": "sconto_medio_pct"}, {"column": "ricavo_netto", "func": "sum", "alias": "ricavo"}]}, 1600, 260)
    c.op("arr", "compute", {"columns": [{"name": "sconto_medio_pct", "expr": "round(sconto_medio_pct, 2)"},
                                        {"name": "anno_trimestre", "expr": "anno * 10 + trimestre"}]}, 1860, 260)
    c.op("ord", "sort", {"by": "anno_trimestre", "descending": False}, 2120, 260)
    c.uscita("Discount by Channel and Quarter", 2380, 260)
    c.catena("ricavo", "j-o", "j-cal", "agg", "arr", "ord", "out")
    c.arco(ordini, "j-o", "right"); c.arco(cal, "j-cal", "right")
    out.append(dict(name="Discount by Channel and Quarter", cron=None, c=c,
                    description="Average discount granted per channel and quarter, next to the revenue it bought."))
    return out


def crea_flussi(progetto: int, engine: str, ds: dict[str, dict], uscite_in: int) -> list[dict]:
    esistenti = {f["name"]: f for f in gw.deve("GET", "/flows") if f["project_id"] == progetto}
    creati = []
    for spec in flussi(ds, uscite_in):
        if spec["name"] in esistenti:
            creati.append({**spec, "id": esistenti[spec["name"]]["id"]})
            continue
        r = gw.deve("POST", f"/projects/{progetto}/flows", {
            "name": spec["name"], "description": spec["description"],
            "definition": spec["c"].definizione(), "engine": engine})
        log(f"flusso creato: {spec['name']}")
        if spec["cron"]:
            s, err = gw.call("PUT", f"/flows/{r['id']}/schedule", {"cron": spec["cron"], "production_engine": engine})
            if s >= 400:
                log(f"  schedulazione non impostata ({s}): {err}")
            else:
                log(f"  schedulato: {spec['cron']}")
        creati.append({**spec, "id": r["id"]})
    return creati


# ── 6. le esecuzioni e le viste ───────────────────────────────────────────────
def esegui(flussi_creati: list[dict], uscite_in: int) -> list[str]:
    """Esegue una volta i flussi la cui tabella prodotta non c'è ancora. Torna i
    nomi di quelli falliti: rilanciare il contenitore riprova solo quelli."""
    presenti = {d["name"] for d in gw.deve("GET", "/datasources") if d["project_id"] == uscite_in}
    falliti = []
    for f in flussi_creati:
        if f["c"].nome_uscita in presenti:
            continue
        s, r = gw.call("POST", f"/flows/{f['id']}/run-now", {})
        if s >= 400:
            log(f"run non avviato per «{f['name']}» ({s}): {r}")
            falliti.append(f["name"])
            continue
        run_id = r.get("run_id")
        fine = time.time() + 40 * 60
        stato = "?"
        while time.time() < fine:
            s2, run = gw.call("GET", f"/runs/{run_id}")
            stato = run.get("status", "?") if isinstance(run, dict) else "?"
            if stato in ("SUCCESS", "FAILURE"):
                break
            time.sleep(6)
        if stato == "SUCCESS":
            log(f"eseguito: {f['name']}")
        else:
            errore = (run.get("error") or "")[:300] if isinstance(run, dict) else ""
            log(f"FALLITO: {f['name']} — {stato} {errore}")
            falliti.append(f["name"])
    return falliti


VISTE = [
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


def crea_viste(progetto: int, engine: str, uscite_in: int) -> None:
    uscite = {d["name"]: d for d in gw.deve("GET", "/datasources") if d["project_id"] == uscite_in}
    esistenti = {v["name"] for v in gw.deve("GET", f"/projects/{progetto}/saved-views")}
    for nome, sorgente, descr, extra in VISTE:
        if nome in esistenti:
            continue
        d = uscite.get(sorgente)
        if not d:
            log(f"vista «{nome}» saltata: la tabella «{sorgente}» non c'è (il flusso non è stato eseguito?)")
            continue
        spec = {"engine": engine, "filters": [], "computedFields": [], "pivotOn": False,
                "pivot": {"index": [""], "on": [""], "values": "", "func": "sum"}, "outline": False}
        spec.update(extra)
        gw.deve("POST", f"/projects/{progetto}/saved-views",
                {"name": nome, "description": descr, "datasource_id": d["id"], "spec": json.dumps(spec)})
        log(f"vista salvata: {nome}")


# ── tutto insieme ─────────────────────────────────────────────────────────────
def main() -> None:
    log(f"inizializzazione dei dati di esempio (scala «{SCALE}»)")
    prepara_database()
    gw.aspetta()
    gw.login()

    sample = cartella("Sample", None,
                      "Sample data: a cured-meat company's ERP (Postgres) and CRM (ClickHouse), plus public reference tables. "
                      "Everything here is generated. Who can see it is up to the administrator.")
    flows_dir = cartella("Flows", sample, "The sample pipelines. Three of them are scheduled.")
    private = cartella("Private tables", sample, "The company's own data from the ERP and the CRM, and what the flows derive from it.")
    public = cartella("Public tables", sample, "Reference data anyone could publish: a calendar with Italian holidays, the twenty regions.")

    conn_pg = connessione(sample, "Sample ERP (Postgres)", {
        "description": "The order management system: orders, order lines, products, prices, costs. Fake data.",
        "db_type": "postgresql", "host": PG["host"], "port": PG["port"], "username": PG["user"],
        "password": PG["password"], "database": PG["dbname"], "db_schema": "vendite"})
    conn_ch = connessione(sample, "Sample CRM (ClickHouse)", {
        "description": "The customer relationship system: customers, sales force, contact log. Fake data.",
        "db_type": "clickhouse", "host": CH["host"], "port": CH["port"], "username": CH["user"],
        "password": CH["password"], "database": CH["db"]})

    ids = []
    for nome, conn, tabella, descr in PRIVATE:
        ids.append(datasource(private, nome, conn_pg if conn == "pg" else conn_ch, tabella, descr))
    for nome, conn, tabella, descr in PUBLIC:
        ids.append(datasource(public, nome, conn_pg, tabella, descr))
    log("aspetto che l'importazione delle tabelle finisca…")
    per_id = aspetta_ingest(ids)
    per_nome = {d["name"]: d for d in per_id.values() if d["project_id"] in (private, public)}
    descrivi(per_nome)

    engine = scegli_engine()
    log(f"motore per i flussi: {engine}")
    creati = crea_flussi(flows_dir, engine, per_nome, private)
    falliti = []
    if ESEGUI_FLUSSI:
        log("eseguo i flussi una volta, così le tabelle prodotte esistono…")
        falliti = esegui(creati, private)
    crea_viste(sample, engine, private)
    log("FATTO. Cartella «Sample» pronta: Flows, Private tables, Public tables, connessioni e viste. "
        "Nessun permesso concesso: decide l'amministratore.")
    if falliti:
        sys.exit(f"{len(falliti)} flussi non sono riusciti ({', '.join(falliti)}): guarda la cronologia dei run; "
                 "rilanciare il contenitore riprova solo quelli")


if __name__ == "__main__":
    main()
