#!/usr/bin/env python3
"""Fills the two sample databases of the PASTA AND BAKERY company with realistic data.

  Postgres «shop»       → ORDER MANAGEMENT: product catalogue, price list,
                          production costs, orders, lines, commercial costs.
  ClickHouse «analytics»→ CRM: hierarchical sales network (agent→inspector→
                          area manager), customer registry, CRM activities.

Realistic sizes with Faker (names, companies, addresses); large fact tables
with numpy (fast), loaded in blocks through COPY (Postgres) and insert_df
(ClickHouse). The scale is set with SAMPLES_SCALE=small|medium|robust|massive.

The line margin is built like this (in Tabularia, joining the two sources):
    revenue  = quantita * prezzo_unitario * (1 - sconto_pct/100)
    cost_p   = from catalogo.costi_produzione (per product, at the date)
    cost_c   = from vendite.costi_commerciali (per line)
    margin   = revenue - cost_p - cost_c
and the attribution by agent/inspector/area manager comes from the cross-DB join
on cliente_id/agente_id with the ClickHouse CRM.
"""
from __future__ import annotations

import io
import os
import sys
import time
from datetime import date, datetime

import numpy as np
import pandas as pd
import psycopg2
from faker import Faker

import scales

# ── configuration ────────────────────────────────────────────────────────────
PG = dict(
    host=os.getenv("PG_HOST", "localhost"),
    port=int(os.getenv("PG_PORT", "5434")),
    dbname=os.getenv("PG_DB", "shop"),
    user=os.getenv("PG_USER", "shop"),
    password=os.getenv("PG_PASSWORD", "shop"),
)
CH = dict(
    host=os.getenv("CH_HOST", "localhost"),
    port=int(os.getenv("CH_PORT", "8124")),
    username=os.getenv("CH_USER", "analytics"),
    password=os.getenv("CH_PASSWORD", "analytics"),
    database=os.getenv("CH_DB", "analytics"),
)
# SAMPLES_SCALE is the new name (the compose's); SAMPLEDB_SCALE stays for whoever used it
SCALE = (os.getenv("SAMPLES_SCALE") or os.getenv("SAMPLEDB_SCALE") or "small").lower()
SEED = int(os.getenv("SAMPLEDB_SEED", "42"))

# the data's time window (the app's "today" is mid 2026): ~3 years up to then
END_DATE = date(2026, 7, 15)
START_DATE = date(2023, 7, 1)
DAYS = (END_DATE - START_DATE).days

# ── trends over time (so that the data is NOT flat/uniform) ──────────────────
# compound annual company growth: more orders as time goes by
ANNUAL_GROWTH = 0.18
# annual growth of the average order value (a basket that widens over time)
ANNUAL_BASKET_GROWTH = 0.05
# monthly seasonality of pasta and bakery goods (index 1..12; 0 = placeholder):
# Christmas very strong, Easter (Apr) up, August down (holidays), autumn recovering.
# This weighs the total VOLUME; the MIX (product/channel) and the fulfilment
# anomalies have tables of their own further down (CATEGORY_SEASON, CHANNEL_SEASON,
# CANCEL_*).
MONTH_MULT = np.array([0.0, 0.90, 0.82, 1.05, 1.15, 1.00, 0.95, 0.90, 0.62, 1.05, 1.12, 1.30, 1.80])

HERE = os.path.dirname(os.path.abspath(__file__))

try:
    SCALE = scales.resolve(SCALE)
except ValueError as e:
    sys.exit(str(e))
P = scales.PRESETS[SCALE]

rng = np.random.default_rng(SEED)
fake = Faker("it_IT")
Faker.seed(SEED)

ORDERS_CHUNK = 1_000_000      # orders per COPY block (caps the memory)
# a line's quantity is a gamma of shape 2 with this scale (kilos or pieces): pasta and
# biscuits cost a few euros a kilo and move by the pallet; and what a unit costs to ship
QTY_BULK, QTY_BASE = 45.0, 20.0
LOGISTICS_PER_UNIT = 0.02
ACTIVITIES_CHUNK = 2_000_000  # CRM activity rows per insert


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


# ── pasta and bakery catalogue (curated) → SKUs per format ───────────────────
# (name, category, kind, shelf life in months, price per kg, raw material share).
# The shelf life goes into `stagionatura_mesi`: the column keeps its name, and for a
# pasta maker it holds the months a product keeps (nothing here is aged).
BASE_PRODUCTS = [
    ("Tortellini di carne", "Pasta fresca", "fresca", 2, 8.50, 0.50),
    ("Ravioli ricotta e spinaci", "Pasta fresca", "fresca", 2, 7.80, 0.49),
    ("Spaghetti di semola di grano duro", "Pasta di semola", "secca", 36, 1.60, 0.58),
    ("Tagliatelle all'uovo", "Pasta fresca", "fresca", 1, 5.20, 0.47),
    ("Gnocchi di patate", "Pasta fresca", "fresca", 2, 3.40, 0.44),
    ("Penne rigate di semola di grano duro", "Pasta di semola", "secca", 36, 1.65, 0.58),
    ("Frollini al burro", "Biscotti", "da forno", 12, 4.20, 0.52),
    ("Frollini al cacao", "Biscotti", "da forno", 12, 4.40, 0.52),
    ("Biscotti integrali ai cereali", "Biscotti", "da forno", 10, 4.60, 0.53),
    ("Cantucci alle mandorle", "Biscotti", "da forno", 12, 9.50, 0.57),
    ("Savoiardi", "Biscotti", "da forno", 9, 5.80, 0.50),
    ("Grissini stirati", "Sostitutivi del pane", "da forno", 8, 5.20, 0.52),
    ("Grissini al sesamo", "Sostitutivi del pane", "da forno", 8, 5.60, 0.53),
    ("Cracker salati", "Sostitutivi del pane", "da forno", 10, 3.40, 0.50),
    ("Fette biscottate", "Sostitutivi del pane", "da forno", 10, 3.90, 0.51),
    ("Crostini tostati", "Sostitutivi del pane", "da forno", 9, 4.80, 0.52),
    ("Taralli all'olio d'oliva", "Sostitutivi del pane", "da forno", 8, 5.90, 0.54),
    ("Panettone classico", "Dolci da ricorrenza", "lievitato", 6, 9.00, 0.48),
    ("Pandoro classico", "Dolci da ricorrenza", "lievitato", 6, 8.50, 0.48),
    ("Amaretti", "Biscotti", "da forno", 12, 10.50, 0.56),
    ("Biscotti secchi da colazione", "Biscotti", "da forno", 12, 3.60, 0.51),
    ("Fusilli di semola di grano duro", "Pasta di semola", "secca", 36, 1.65, 0.57),
    ("Rigatoni trafilati al bronzo", "Pasta di semola", "secca", 36, 2.60, 0.55),
    ("Spaghetti integrali", "Pasta di semola", "secca", 24, 2.10, 0.56),
]
# kind → its formats: (format, unit, weight of a piece in kg, price markup, packaging
# cost per piece). The large ones are for catering and are sold by the kilo, the small
# ones by the piece; the seasonal cakes come in three sizes, always by the piece.
FORMATS = {
    "secca": [
        ("sacco 5 kg", "kg", 5.000, 1.00, 0.12),
        ("sacco 3 kg", "kg", 3.000, 1.02, 0.09),
        ("pacco 1 kg", "kg", 1.000, 1.05, 0.05),
        ("pacco 500 g", "pz", 0.500, 1.20, 0.04),
    ],
    "fresca": [
        ("vaschetta 2 kg", "kg", 2.000, 1.00, 0.18),
        ("vaschetta 1 kg", "kg", 1.000, 1.03, 0.12),
        ("confezione 500 g", "pz", 0.500, 1.15, 0.09),
        ("confezione 250 g", "pz", 0.250, 1.25, 0.07),
    ],
    "da forno": [
        ("cartone 5 kg", "kg", 5.000, 1.00, 0.30),
        ("cartone 2 kg", "kg", 2.000, 1.03, 0.16),
        ("sacchetto 500 g", "pz", 0.500, 1.15, 0.07),
        ("sacchetto 250 g", "pz", 0.250, 1.25, 0.05),
    ],
    "lievitato": [
        ("scatola 1 kg", "pz", 1.000, 1.00, 0.45),
        ("scatola 750 g", "pz", 0.750, 1.04, 0.40),
        ("incarto 500 g", "pz", 0.500, 1.10, 0.22),
    ],
}
CHANNELS = ["Supermercati", "Ristorazione", "Ingrosso", "Negozi", "Online"]
DEPOTS = ["Verona", "Torino", "Bari", "Roma", "Ancona"]
AREAS = ["Nord-Ovest", "Nord-Est", "Centro", "Sud", "Isole"]

# ── product mix × season ─────────────────────────────────────────────────────
# Besides the total VOLUME (MONTH_MULT), the MIX changes too: each category has a
# monthly profile (index 1..12; 0 = placeholder) that weighs the chance that one
# of its SKUs lands on a line, month by month.
#   · Dolci da ricorrenza (panettone/pandoro): a Christmas spike, almost nothing
#     the rest of the year.  · Sostitutivi del pane (grissini/cracker): summer up
#     (aperitifs, picnics).  · Pasta fresca: the tables of Christmas and Easter.
#   · Biscotti: low in summer, up again when the schools open.
CATEGORY_SEASON = {
    "Pasta fresca":         [0.0, 0.90, 0.85, 1.00, 1.18, 1.00, 0.88, 0.82, 0.68, 1.00, 1.18, 1.40, 1.95],
    "Sostitutivi del pane": [0.0, 0.85, 0.85, 1.00, 1.05, 1.18, 1.30, 1.40, 1.10, 1.00, 0.95, 1.00, 1.10],
    "Biscotti":             [0.0, 1.05, 1.00, 1.00, 1.00, 0.95, 0.85, 0.80, 0.75, 1.10, 1.15, 1.18, 1.25],
    "Pasta di semola":      [0.0, 1.05, 1.00, 1.00, 1.00, 1.02, 1.05, 1.02, 0.88, 1.05, 1.05, 1.08, 1.10],
    "Dolci da ricorrenza":  [0.0, 0.40, 0.12, 0.10, 0.10, 0.08, 0.08, 0.08, 0.08, 0.20, 0.60, 2.00, 4.20],
}
# override for a single product (key = substring of the name): taralli are the
# aperitif of summer and lead the hot months.
PRODUCT_SEASON = {
    "Taralli": [0.0, 0.85, 0.85, 1.00, 1.15, 1.40, 1.65, 1.75, 1.35, 1.10, 0.90, 0.85, 0.90],
}
# base popularity per category (season being equal, how much it "sells")
CATEGORY_POP = {
    "Pasta fresca": 1.3, "Pasta di semola": 1.6, "Biscotti": 1.4,
    "Sostitutivi del pane": 1.0, "Dolci da ricorrenza": 0.5,
}

# ── channel mix × season ─────────────────────────────────────────────────────
# Moves each channel's SHARE month by month (the month's total volume is still
# set by MONTH_MULT). Ristorazione booms in summer (tourism/restaurants),
# Supermercati and Negozi at Christmas; Ingrosso/Online flatter.
CHANNEL_SEASON = {
    "Supermercati": [0.0, 0.90, 0.85, 0.95, 1.05, 1.00, 0.95, 0.90, 0.95, 1.05, 1.20, 1.45, 1.60],
    "Ristorazione": [0.0, 0.70, 0.72, 0.90, 1.10, 1.35, 1.70, 1.85, 1.55, 1.10, 0.85, 0.85, 1.05],
    "Ingrosso":     [0.0, 0.95, 0.95, 1.00, 1.00, 1.05, 1.05, 1.00, 1.00, 1.05, 1.15, 1.25, 1.35],
    "Negozi":       [0.0, 0.95, 0.90, 1.00, 1.15, 1.00, 0.92, 0.85, 0.90, 1.05, 1.15, 1.35, 1.70],
    "Online":       [0.0, 1.05, 0.95, 1.00, 1.05, 1.00, 0.95, 0.90, 0.85, 1.05, 1.15, 1.40, 1.65],
}

# ── FULFILMENT anomalies by depot ────────────────────────────────────────────
# Baseline: ~4% of orders cancelled everywhere. On two depots we inject a
# pathology to analyse on the fulfilled share:
#   · "Bari"   → CHRONIC decline: the cancelled share grows over time (a
#      structural logistics problem) from ~4% up to ~24% at the end of the window.
#   · "Torino" → a one-off INCIDENT: a peak of cancellations in a "black" season
#      (spring 2025) up to ~32%, then back to normal.
CANCEL_BASE = 0.04
CANCEL_CHRONIC = {"Bari": 0.20}    # highest increase, reached at the end of the window
CANCEL_INCIDENT = {                # depot → (from, to, peak extra)
    "Torino": (date(2025, 2, 10), date(2025, 5, 31), 0.28),
}

# region → (province code, city) for geographically consistent registries
GEO = [
    ("Lombardia", "MI", "Milano"), ("Lombardia", "BG", "Bergamo"), ("Lombardia", "BS", "Brescia"),
    ("Emilia-Romagna", "RE", "Reggio Emilia"), ("Emilia-Romagna", "RN", "Rimini"), ("Emilia-Romagna", "BO", "Bologna"),
    ("Veneto", "VR", "Verona"), ("Veneto", "PD", "Padova"), ("Veneto", "VE", "Venezia"),
    ("Piemonte", "TO", "Torino"), ("Piemonte", "CN", "Cuneo"),
    ("Toscana", "FI", "Firenze"), ("Toscana", "SI", "Siena"),
    ("Lazio", "RM", "Roma"), ("Lazio", "LT", "Latina"),
    ("Campania", "NA", "Napoli"), ("Campania", "SA", "Salerno"),
    ("Puglia", "BA", "Bari"), ("Puglia", "FG", "Foggia"),
    ("Sicilia", "PA", "Palermo"), ("Sicilia", "CT", "Catania"),
    ("Calabria", "CS", "Cosenza"), ("Sardegna", "CA", "Cagliari"),
    ("Friuli-Venezia Giulia", "UD", "Udine"), ("Trentino-Alto Adige", "BZ", "Bolzano"),
    ("Marche", "AN", "Ancona"), ("Liguria", "GE", "Genova"),
]


def build_products() -> pd.DataFrame:
    """Expands the base products by format → SKUs with list price and costs."""
    rows = []
    pid = 0
    for name, cat, kind, shelf_life, price_kg, mp_frac in BASE_PRODUCTS:
        # 3–4 formats per product: the ones of its kind
        for fmt, unit, weight, markup, conf in FORMATS[kind]:
            pid += 1
            if unit == "kg":
                list_price = round(price_kg * markup, 4)           # per kg
            else:
                list_price = round(price_kg * weight * markup, 4)  # per piece
            rows.append(dict(
                id=pid,
                codice=f"SKU{pid:04d}",
                nome=f"{name} — {fmt}",
                categoria=cat,
                tipo=kind,
                formato=fmt,
                stagionatura_mesi=shelf_life,
                peso_medio_kg=weight,
                unita_misura=unit,
                _price_kg=price_kg,
                _mp_frac=mp_frac,
                _conf=conf,
                _list_price=list_price,
                _weight=weight,
                _unit=unit,
            ))
    return pd.DataFrame(rows)


# ── Postgres helpers ─────────────────────────────────────────────────────────
def copy_df(cur, table: str, df: pd.DataFrame) -> None:
    """COPY of a DataFrame (columns in the order of `df`) into `table`."""
    buf = io.StringIO()
    df.to_csv(buf, index=False, header=False)
    buf.seek(0)
    cols = ", ".join(df.columns)
    cur.copy_expert(f"COPY {table} ({cols}) FROM STDIN WITH (FORMAT csv)", buf)


def run_sql_file(cur, path: str) -> None:
    with open(path, encoding="utf-8") as f:
        cur.execute(f.read())


# ── generation ───────────────────────────────────────────────────────────────
REGIONS = [
    # region, macro-area, capital, inhabitants (rounded), km²
    ("Piemonte", "Nord-Ovest", "Torino", 4_250_000, 25_387), ("Valle d'Aosta", "Nord-Ovest", "Aosta", 123_000, 3_261),
    ("Lombardia", "Nord-Ovest", "Milano", 9_950_000, 23_864), ("Liguria", "Nord-Ovest", "Genova", 1_500_000, 5_416),
    ("Trentino-Alto Adige", "Nord-Est", "Trento", 1_080_000, 13_606), ("Veneto", "Nord-Est", "Venezia", 4_850_000, 18_345),
    ("Friuli-Venezia Giulia", "Nord-Est", "Trieste", 1_190_000, 7_924), ("Emilia-Romagna", "Nord-Est", "Bologna", 4_440_000, 22_453),
    ("Toscana", "Centro", "Firenze", 3_660_000, 22_987), ("Umbria", "Centro", "Perugia", 855_000, 8_464),
    ("Marche", "Centro", "Ancona", 1_480_000, 9_401), ("Lazio", "Centro", "Roma", 5_710_000, 17_232),
    ("Abruzzo", "Sud", "L'Aquila", 1_270_000, 10_832), ("Molise", "Sud", "Campobasso", 290_000, 4_461),
    ("Campania", "Sud", "Napoli", 5_600_000, 13_671), ("Puglia", "Sud", "Bari", 3_900_000, 19_541),
    ("Basilicata", "Sud", "Potenza", 535_000, 10_073), ("Calabria", "Sud", "Catanzaro", 1_840_000, 15_222),
    ("Sicilia", "Isole", "Palermo", 4_800_000, 25_832), ("Sardegna", "Isole", "Cagliari", 1_570_000, 24_100),
]
FIXED_HOLIDAYS = {(1, 1): "Capodanno", (1, 6): "Epifania", (4, 25): "Liberazione", (5, 1): "Festa del lavoro",
                  (6, 2): "Festa della Repubblica", (8, 15): "Ferragosto", (11, 1): "Ognissanti",
                  (12, 8): "Immacolata", (12, 25): "Natale", (12, 26): "Santo Stefano"}
DAY_NAMES = ["lunedì", "martedì", "mercoledì", "giovedì", "venerdì", "sabato", "domenica"]


def _easter(year: int) -> date:
    """Gauss/Meeus algorithm for the Gregorian Easter."""
    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def gen_ref(cur) -> None:
    """The PUBLIC tables: a calendar and the regions. Data anyone could
    publish — the opposite of orders and customers — that the flows join to
    the company data (the month, the macro-area)."""
    from datetime import timedelta

    rows = []
    g = date(START_DATE.year, 1, 1)
    end = date(END_DATE.year + 1, 12, 31)   # one year past the data: the schedules look ahead
    easter_mondays = {a: _easter(a) + timedelta(days=1) for a in range(g.year, end.year + 1)}
    while g <= end:
        holiday = FIXED_HOLIDAYS.get((g.month, g.day))
        if holiday is None and easter_mondays[g.year] == g:
            holiday = "Lunedì dell'Angelo"
        iso = g.isocalendar()
        rows.append((g, g.year, (g.month - 1) // 3 + 1, g.month, g.year * 100 + g.month, iso[1],
                     g.isoweekday(), DAY_NAMES[g.weekday()], g.isoweekday() >= 6, holiday is not None, holiday))
        g += timedelta(days=1)
    copy_df(cur, "ref.calendario", pd.DataFrame(rows, columns=[
        "giorno", "anno", "trimestre", "mese", "anno_mese", "settimana_iso", "giorno_settimana",
        "nome_giorno", "fine_settimana", "festivo", "nome_festa"]))
    copy_df(cur, "ref.regioni", pd.DataFrame(REGIONS, columns=["regione", "macroarea", "capoluogo", "popolazione", "superficie_kmq"]))


def ensure_ref(pg) -> bool:
    """Adds the public tables to a database generated BEFORE they existed.
    Returns True if it had to create them."""
    with pg.cursor() as cur:
        cur.execute("SELECT to_regclass('ref.calendario')")
        if cur.fetchone()[0] is not None:
            return False
        run_sql_file(cur, os.path.join(HERE, "schema_ref.sql"))
        gen_ref(cur)
    pg.commit()
    return True


def gen_agents() -> pd.DataFrame:
    """Hierarchical sales network: area managers → inspectors → agents."""
    rows = []
    aid = 0
    managers, insp = [], []
    for _ in range(P["managers"]):
        aid += 1
        area = AREAS[(aid - 1) % len(AREAS)]
        rows.append(_agent_row(aid, "capo_area", 0, area, rng.uniform(0.8, 1.5)))
        managers.append((aid, area))
    for _ in range(P["inspectors"]):
        aid += 1
        manager_id, area = managers[rng.integers(len(managers))]
        rows.append(_agent_row(aid, "ispettore", manager_id, area, rng.uniform(1.5, 2.5)))
        insp.append((aid, area))
    for _ in range(P["agents"]):
        aid += 1
        insp_id, area = insp[rng.integers(len(insp))]
        rows.append(_agent_row(aid, "agente", insp_id, area, rng.uniform(3.0, 6.0)))
    df = pd.DataFrame(rows)
    return df


def _agent_row(aid, role, resp, area, comm):
    first, last = fake.first_name(), fake.last_name()
    email = f"{first}.{last}@pastificio.example".lower().replace(" ", "")
    return dict(
        id=aid, nome=first, cognome=last, ruolo=role, responsabile_id=resp,
        area=area, provvigione_base_pct=round(comm, 2),
        data_assunzione=fake.date_between(date(2010, 1, 1), date(2024, 12, 31)),
        email=email,
    )


def gen_customers(agents: pd.DataFrame) -> pd.DataFrame:
    """Customer registry, each one assigned to an AGENT (not an inspector/manager)."""
    agent_ids_pool = agents.loc[agents.ruolo == "agente", "id"].to_numpy()
    C = P["customers"]
    log(f"  customers: generating {C:,} records with Faker…")
    geo_idx = rng.integers(0, len(GEO), C)
    channel_idx = rng.integers(0, len(CHANNELS), C)
    agent_ids = agent_ids_pool[rng.integers(0, len(agent_ids_pool), C)]
    acq_days = rng.integers(0, DAYS, C)
    base = np.datetime64(START_DATE)
    # ClickHouse Date wants real dates (not strings): vectorised datetime64
    acq = pd.to_datetime(base + acq_days.astype("timedelta64[D]"))

    rows = []
    for i in range(C):
        reg, prov, city = GEO[geo_idx[i]]
        rows.append((
            i + 1,
            fake.company(),
            fake.numerify("###########"),      # VAT number, 11 digits
            CHANNELS[channel_idx[i]],
            fake.street_address().replace("\n", " "),
            fake.numerify("#####"),
            city, prov, reg,
            int(agent_ids[i]),
            int(rng.random() > 0.08),           # ~92% active
        ))
    cols = ["id", "ragione_sociale", "partita_iva", "canale", "indirizzo", "cap",
            "citta", "provincia", "regione", "agente_id", "attivo"]
    df = pd.DataFrame(rows, columns=cols)
    df["data_acquisizione"] = acq
    # helper arrays for the facts (indexed by cliente_id 1..C)
    df.attrs["agent_of"] = np.concatenate([[0], df.agente_id.to_numpy()])
    df.attrs["channel_of"] = np.concatenate([[0], channel_idx + 1])  # 1-based in CHANNELS
    return df


def gen_catalogue(cur, prod: pd.DataFrame) -> None:
    """Writes products + price list history + production cost history."""
    cols = ["id", "codice", "nome", "categoria", "tipo", "formato",
            "stagionatura_mesi", "peso_medio_kg", "unita_misura"]
    copy_df(cur, "catalogo.prodotti", prod[cols])

    price_list, costs = [], []
    lid = cid = 0
    for _, r in prod.iterrows():
        # two periods of prices/costs: up to the end of 2024 and from 2025 (a ~6% rise)
        for start, mult in ((START_DATE, 1.0), (date(2025, 1, 1), 1.06)):
            lid += 1
            end = date(2024, 12, 31) if mult == 1.0 else None
            price_list.append((lid, int(r.id), start, end, round(r._list_price * mult, 4)))
            cid += 1
            mp = r._price_kg * r._mp_frac * mult
            proc = r._price_kg * 0.11 * mult
            costs.append((cid, int(r.id), start, round(mp, 4), round(proc, 4), round(r._conf * mult, 4)))
    copy_df(cur, "catalogo.listino_prezzi",
            pd.DataFrame(price_list, columns=["id", "prodotto_id", "valido_dal", "valido_al", "prezzo_listino"]))
    copy_df(cur, "catalogo.costi_produzione",
            pd.DataFrame(costs, columns=["id", "prodotto_id", "valido_dal",
                                         "costo_materia_prima_kg", "costo_lavorazione_kg", "costo_confezionamento_pz"]))


def build_day_weights() -> np.ndarray:
    """The probability of each day of the data window, so that orders and activities
    are NOT spread uniformly but follow a realistic TREND:
      · compound company growth (ANNUAL_GROWTH);
      · yearly seasonality (Christmas/Easter ↑, August ↓ — see MONTH_MULT);
      · weekly B2B seasonality (weekdays ↑, Saturday ↓, Sunday almost zero).
    A little daily noise avoids curves that are too smooth."""
    ndays = DAYS + 1
    idx = np.arange(ndays)
    dates = pd.to_datetime(np.datetime64(START_DATE) + idx.astype("timedelta64[D]"))
    years = idx / 365.25
    growth = (1.0 + ANNUAL_GROWTH) ** years
    seasonal = MONTH_MULT[dates.month.to_numpy()]
    dow = dates.dayofweek.to_numpy()  # 0=Mon … 6=Sun
    weekly = np.where(dow < 5, 1.0, np.where(dow == 5, 0.45, 0.12))
    noise = rng.uniform(0.85, 1.15, ndays)
    w = growth * seasonal * weekly * noise
    return w / w.sum()


def build_seasonality(prod: pd.DataFrame):
    """For each SKU: a monthly seasonal vector [P_n × 13] (category, with an
    override by name) + base popularity [P_n]. The product choice weights month
    by month come from these (a mix that changes over time)."""
    P_n = len(prod)
    season = np.ones((P_n, 13), dtype=float)
    pop = np.ones(P_n, dtype=float)
    names = prod.nome.to_numpy()
    cats = prod.categoria.to_numpy()
    for i in range(P_n):
        prof = CATEGORY_SEASON.get(cats[i])
        for key, override in PRODUCT_SEASON.items():
            if key in names[i]:
                prof = override
                break
        if prof is not None:
            season[i] = prof
        pop[i] = CATEGORY_POP.get(cats[i], 1.0)
    season[:, 0] = 0.0  # placeholder month: never chosen
    return season, pop


def gen_orders_and_lines(cur, prod: pd.DataFrame, agent_of, channel_of, agents: pd.DataFrame) -> None:
    """Orders + lines + commercial costs, in blocks. Header → customer (and their
    agent); lines → products with price≈list±noise and a discount per channel."""
    O = P["orders"]
    C = P["customers"]
    P_n = len(prod)
    prices = prod._list_price.to_numpy()
    base_comm = np.concatenate([[0.0], agents.provvigione_base_pct.to_numpy().astype(float)])
    base_d = np.datetime64(START_DATE)
    p_day = build_day_weights()  # growth trend + seasonality (not uniform)

    # ── helper tables for the finer seasonalities (computed once) ──
    # the month of each day of the window: days → month without going through pandas again
    days64 = base_d + np.arange(DAYS + 1).astype("timedelta64[D]")
    day_month = pd.to_datetime(days64).month.to_numpy()

    # CUSTOMER choice weights per month (channel mix × season): the month being equal,
    # the customers of the channels "in season" are more likely to order
    ch_mat = np.array([CHANNEL_SEASON[c] for c in CHANNELS])   # [len(CHANNELS) × 13]
    client_ch0 = (channel_of[1:] - 1).astype(int)              # 0-based channel per customer 1..C
    client_ids = np.arange(1, C + 1, dtype=np.int64)
    client_w = {m: (lambda col: col / col.sum())(ch_mat[client_ch0, m]) for m in range(1, 13)}

    # PRODUCT choice weights per month (product mix × season)
    season, base_pop = build_seasonality(prod)
    prod_w = {m: (lambda col: col / col.sum())(base_pop * season[:, m]) for m in range(1, 13)}

    # fulfilment anomalies by depot: chronic ramp + incident window, per day
    chronic_ramp = np.clip(np.arange(DAYS + 1) / DAYS, 0.0, 1.0)  # 0→1 over the window
    incident_flag = {}
    for dep, (start, end, _peak) in CANCEL_INCIDENT.items():
        incident_flag[dep] = ((days64 >= np.datetime64(start)) & (days64 <= np.datetime64(end))).astype(float)

    line_id = 0
    done = 0
    while done < O:
        n = min(ORDERS_CHUNK, O - done)
        oid = np.arange(done + 1, done + n + 1, dtype=np.int64)
        days = rng.choice(DAYS + 1, size=n, p=p_day)       # date weighted by the trend
        order_date = (base_d + days.astype("timedelta64[D]")).astype(str)
        months = day_month[days]                           # the month of each order

        # customer chosen per month, weighted by the channel's seasonality
        customer = np.empty(n, dtype=np.int64)
        for m in range(1, 13):
            sel = np.where(months == m)[0]
            if sel.size:
                customer[sel] = rng.choice(client_ids, size=sel.size, p=client_w[m])
        agent = agent_of[customer]
        channel_i = channel_of[customer]                   # 1..len(CHANNELS)
        channel = np.array(CHANNELS)[channel_i - 1]
        bulk = np.isin(channel_i, [1, 3])                  # Supermercati/Ingrosso: qty↑ discounts↑
        depot = np.array(DEPOTS)[rng.integers(0, len(DEPOTS), n)]
        number = np.char.add("ORD-", oid.astype(str))

        # order state: cancelled share = baseline + depot anomalies (chronic /
        # incident); "in_lavorazione" concentrated on recent orders
        p_cancel = np.full(n, CANCEL_BASE)
        for dep, mx in CANCEL_CHRONIC.items():
            mask = depot == dep
            p_cancel[mask] += mx * chronic_ramp[days[mask]]
        for dep, (_start, _end, peak) in CANCEL_INCIDENT.items():
            mask = depot == dep
            p_cancel[mask] += peak * incident_flag[dep][days[mask]]
        p_cancel = np.clip(p_cancel, 0.0, 0.95)
        cancelled = rng.random(n) < p_cancel
        p_inprog = 0.03 + 0.10 * (days / DAYS)             # 3%→13% towards the end of the window
        in_prog = (~cancelled) & (rng.random(n) < p_inprog)
        state = np.where(cancelled, "annullato", np.where(in_prog, "in_lavorazione", "evaso"))

        copy_df(cur, "vendite.ordini", pd.DataFrame({
            "id": oid, "numero": number, "data_ordine": order_date, "cliente_id": customer,
            "agente_id": agent, "canale": channel, "stato": state, "deposito": depot,
        }))

        # lines: count per order ~ Poisson+1
        n_lines = rng.poisson(2.5, n) + 1
        tot = int(n_lines.sum())
        r_oid = np.repeat(oid, n_lines)
        r_channel_bulk = np.repeat(bulk, n_lines)
        r_agent = np.repeat(agent, n_lines)
        r_days = np.repeat(days, n_lines)
        # product chosen per month: the MIX follows the season (panettone at Christmas,
        # taralli in summer, …), it is no longer uniform
        r_months = day_month[r_days]
        prod_i = np.empty(tot, dtype=np.int64)
        for m in range(1, 13):
            sel = np.where(r_months == m)[0]
            if sel.size:
                prod_i[sel] = rng.choice(P_n, size=sel.size, p=prod_w[m])
        list_price = prices[prod_i]
        # quantity (kilos or pieces): gamma (skewed), higher for Supermercati/Ingrosso,
        # and slowly growing over time (the average basket widens year on year)
        basket = (1.0 + ANNUAL_BASKET_GROWTH) ** (r_days / 365.25)
        qty = (rng.gamma(2.0, np.where(r_channel_bulk, QTY_BULK, QTY_BASE)) + 2.0) * basket
        qty = np.round(qty, 3)
        unit_price = np.round(list_price * (1 + rng.normal(0, 0.04, tot)), 4)
        unit_price = np.maximum(unit_price, 0.2)
        discount = np.round(np.clip(rng.normal(np.where(r_channel_bulk, 12, 5), 3), 0, 40), 2)
        rid = np.arange(line_id + 1, line_id + tot + 1, dtype=np.int64)

        copy_df(cur, "vendite.righe_ordine", pd.DataFrame({
            "id": rid, "ordine_id": r_oid, "prodotto_id": prod_i + 1,
            "quantita": qty, "prezzo_unitario": unit_price, "sconto_pct": discount,
        }))

        # commercial costs per line: the agent's commission + logistics + promotions
        revenue = qty * unit_price * (1 - discount / 100.0)
        pct = base_comm[r_agent]
        comm_amount = np.round(revenue * pct / 100.0, 4)
        logistics = np.round(qty * LOGISTICS_PER_UNIT + 1.5, 4)
        promo = np.round(np.where(rng.random(tot) < 0.15, revenue * rng.uniform(0.02, 0.08, tot), 0.0), 4)
        copy_df(cur, "vendite.costi_commerciali", pd.DataFrame({
            "id": rid, "riga_ordine_id": rid, "provvigione_pct": np.round(pct, 2),
            "provvigione_importo": comm_amount, "costo_logistica": logistics, "costo_promozionale": promo,
        }))

        line_id += tot
        done += n
        log(f"    orders {done:,}/{O:,} · order lines {line_id:,}")


def gen_activities(ch, agent_of) -> None:
    """CRM activities (ClickHouse's big table), in blocks."""
    A = P["activities"]
    C = P["customers"]
    KINDS = np.array(["visita", "telefonata", "email", "sollecito", "reclamo"])
    OUTCOMES = np.array(["positivo", "neutro", "negativo", "ordine"])
    base = np.datetime64(f"{START_DATE}T00:00:00")
    p_day = build_day_weights()  # the same trend/seasonality as the orders
    done = 0
    while done < A:
        n = min(ACTIVITIES_CHUNK, A - done)
        customer = rng.integers(1, C + 1, n)
        agent = agent_of[customer]
        # day weighted by the trend + time of day concentrated in working hours
        day = rng.choice(DAYS + 1, size=n, p=p_day)
        sec_of_day = np.clip(rng.normal(13 * 3600, 2.5 * 3600, n), 7 * 3600, 20 * 3600)
        offset = (day * 86400 + sec_of_day).astype(np.int64).astype("timedelta64[s]")
        when = base + offset
        df = pd.DataFrame({
            "id": np.arange(done + 1, done + n + 1, dtype=np.int64),
            "cliente_id": customer.astype(np.uint32),
            "agente_id": agent.astype(np.uint32),
            "data": pd.to_datetime(when),
            "tipo": KINDS[rng.integers(0, len(KINDS), n)],
            "esito": OUTCOMES[rng.integers(0, len(OUTCOMES), n)],
            "durata_min": rng.integers(2, 90, n).astype(np.uint16),
            "note": "",
        })
        ch.insert_df("analytics.attivita_crm", df)
        done += n
        log(f"    CRM activities {done:,}/{A:,}")


# ── main ─────────────────────────────────────────────────────────────────────
def main() -> None:
    t0 = time.time()
    log(f"Scale «{SCALE}» — preset: {P}")
    import clickhouse_connect

    log("Connecting to Postgres and ClickHouse…")
    pg = psycopg2.connect(**PG)
    pg.autocommit = False
    ch = clickhouse_connect.get_client(**CH)

    # schema
    log("Creating the Postgres schema…")
    with pg.cursor() as cur:
        run_sql_file(cur, os.path.join(HERE, "schema_postgres.sql"))
        run_sql_file(cur, os.path.join(HERE, "schema_ref.sql"))
    pg.commit()
    log("Creating the ClickHouse schema…")
    for stmt in open(os.path.join(HERE, "schema_clickhouse.sql"), encoding="utf-8").read().split(";"):
        if stmt.strip():
            ch.command(stmt)

    # dimensions
    log("Generating the sales network (agents)…")
    agents = gen_agents()
    ch.insert_df("analytics.agenti", agents)
    log(f"  {len(agents):,} agents ({P['managers']} area managers, {P['inspectors']} supervisors, {P['agents']} agents)")

    customers = gen_customers(agents)
    ch.insert_df("analytics.clienti", customers[[
        "id", "ragione_sociale", "partita_iva", "canale", "indirizzo", "cap",
        "citta", "provincia", "regione", "agente_id", "data_acquisizione", "attivo"]])
    log(f"  {len(customers):,} customers inserted")
    agent_of = customers.attrs["agent_of"]
    channel_of = customers.attrs["channel_of"]

    log("Generating the public reference data (calendar, regions)…")
    with pg.cursor() as cur:
        gen_ref(cur)
    pg.commit()

    log("Generating the product catalogue, price list and production costs…")
    prod = build_products()
    with pg.cursor() as cur:
        gen_catalogue(cur, prod)
    pg.commit()
    log(f"  {len(prod)} SKUs")

    # facts
    log("Generating orders, order lines and commercial costs (Postgres)…")
    with pg.cursor() as cur:
        gen_orders_and_lines(cur, prod, agent_of, channel_of, agents)
    pg.commit()

    log("Building the secondary Postgres indexes…")
    with pg.cursor() as cur:
        run_sql_file(cur, os.path.join(HERE, "indexes_postgres.sql"))
    pg.commit()

    log("Generating the CRM activities (ClickHouse)…")
    gen_activities(ch, agent_of)

    pg.close()
    log(f"Done in {int(time.time() - t0)}s. Scale «{SCALE}».")
    log("Tabularia connections → Postgres sampledb-postgres:5432 / ClickHouse sampledb-clickhouse:8123")


if __name__ == "__main__":
    main()
