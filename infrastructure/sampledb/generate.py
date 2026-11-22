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

THE CUSTOMER LIFECYCLE. Customers are not a fixed list that orders at random: each one is
simulated, in a Pareto/NBD process (the model behind «buy till you die» analyses):
  · acquisition: ~30% of the customers are in the portfolio when the window opens, the
    others arrive over time at a rate that grows, with a season (more in January, few in
    August) and a pace of its own for each channel; `clienti.data_acquisizione` is that day;
  · frequency: each customer orders at a Poisson rate of its own (gamma across customers,
    the mean set by the channel), shaped by the month, weekday and channel seasons;
  · churn: each customer's lifetime is exponential, with a hazard that depends on channel,
    macro-area and agent, times a gamma frailty. NOBODY WRITES THE CHURN DOWN: the database
    only has `clienti.stato`, computed like a CRM would (no order for six months = inattivo),
    and an inactive customer can be a slow one, not a lost one;
  · spend: an average order size of its own (gamma across customers, independent of the
    rate), the order's noise on top: the assumption of the Gamma–Gamma model.
The random seed is fixed, so the data is the same at every run.
"""
from __future__ import annotations

import io
import math
import os
import sys
import time
from datetime import date, datetime
from decimal import Decimal

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
# the company grows because customers ARRIVE (see the lifecycle below), not because every
# customer orders more: the order rate of a customer has no trend of its own
# annual growth of the average order value (a basket that widens over time)
ANNUAL_BASKET_GROWTH = 0.05
# monthly seasonality of pasta and bakery goods (index 1..12; 0 = placeholder):
# Christmas very strong, Easter (Apr) up, August down (holidays), autumn recovering.
# This weighs the total VOLUME; the MIX (product/channel) and the fulfilment
# anomalies have tables of their own further down (CATEGORY_SEASON, CHANNEL_SEASON,
# CANCEL_*).
MONTH_MULT = np.array([0.0, 0.90, 0.82, 1.05, 1.15, 1.00, 0.95, 0.90, 0.62, 1.05, 1.12, 1.30, 1.80])

# ── the customer lifecycle ───────────────────────────────────────────────────
# share of the customers already in the portfolio when the window opens (the others are
# acquired over the window); their acquisition date lies before it, and what they ordered
# before is not in the data
INITIAL_SHARE = 0.30
# a share of the customers per channel (the order is CHANNELS' one)
CHANNEL_SHARE = [0.20, 0.26, 0.12, 0.27, 0.15]
# the growth of the ACQUISITIONS from one year to the next, per channel: the online channel
# is the one that takes off, the supermarkets are a slow, contracted business
ACQ_GROWTH = {"Supermercati": 1.10, "Ristorazione": 1.20, "Ingrosso": 1.05, "Negozi": 1.00, "Online": 1.60}
# acquisitions by month (index 1..12; 0 = placeholder): a push at the start of the year,
# a dead August, a quiet December
ACQ_SEASON = np.array([0.0, 1.40, 1.25, 1.10, 1.00, 0.95, 0.90, 0.75, 0.30, 1.20, 1.15, 0.90, 0.55])
# orders a year of a live customer, on average, by channel: the supermarkets and the
# restaurants order often, the wholesalers rarely and in bulk. The scale is then
# calibrated, so that the orders add up to the preset's; what stays is the proportion
CHANNEL_RATE = {"Supermercati": 16.0, "Ristorazione": 14.0, "Ingrosso": 5.0, "Negozi": 7.0, "Online": 5.0}
RATE_SHAPE = 1.5            # gamma shape of the rate across customers (a CV of ~0.8)
# churn: a hazard a year = base × channel × macro-area × the agent's × frailty
BASE_CHURN = 0.25
CHANNEL_CHURN = {"Supermercati": 0.55, "Ristorazione": 1.35, "Ingrosso": 0.85, "Negozi": 1.25, "Online": 1.60}
AREA_CHURN = {"Nord-Ovest": 0.75, "Nord-Est": 0.80, "Centro": 1.00, "Sud": 1.35, "Isole": 1.70}
AGENT_CHURN_SIGMA = 0.30    # a lognormal factor per agent: some lose more customers than others
FRAILTY_SHAPE = 3.0         # gamma, mean 1: what the covariates do not explain
SPEND_SHAPE = 4.0           # gamma, mean 1: the size of a customer's orders (a CV of 0.5)
FIRST_ORDER_LAG_DAYS = 5.0  # the first order follows the acquisition by this many days, on average
INACTIVE_AFTER_DAYS = 183   # the CRM calls a customer «inattivo» after six months with no order
IN_PROGRESS_DAYS = 21       # an order is still «in_lavorazione» at the window's end only if it is this recent
ORDER_OUTCOME_DAYS = 14     # a CRM contact ends in «ordine» only if an order follows within these days
# the CRM activity of a customer who has churned: a fraction of a live one's, the attempts
# to win it back
POST_CHURN_ACTIVITY = 0.12

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
# The shelf life goes into `conservazione_mesi`: the months a product keeps.
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
                conservazione_mesi=shelf_life,
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
        gen_quotazioni(cur)
    pg.commit()
    return True


def gen_agents() -> pd.DataFrame:
    """Hierarchical sales network: area managers → inspectors → agents. Every macro-area has its
    manager (the presets have at least as many managers as AREAS) and its inspectors (dealt to the
    managers in turn), so that every customer can be followed by an agent of its own macro-area."""
    rows = []
    aid = 0
    managers, insp = [], []
    for _ in range(P["managers"]):
        aid += 1
        area = AREAS[(aid - 1) % len(AREAS)]
        rows.append(_agent_row(aid, "capo_area", 0, area, rng.uniform(0.8, 1.5)))
        managers.append((aid, area))
    for k in range(P["inspectors"]):
        aid += 1
        manager_id, area = managers[k % len(managers)]
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


def build_day_intensity() -> np.ndarray:
    """The intensity of each day of the data window, mean 1: the yearly season (Christmas
    and Easter up, August down — see MONTH_MULT), the weekly B2B one (weekdays up, Saturday
    down, Sunday almost zero) and a little daily noise, so that the curves are not too smooth.
    There is NO growth in it: the company grows by acquiring customers."""
    ndays = DAYS + 1
    dates = pd.to_datetime(np.datetime64(START_DATE) + np.arange(ndays).astype("timedelta64[D]"))
    dow = dates.dayofweek.to_numpy()  # 0=Mon … 6=Sun
    weekly = np.where(dow < 5, 1.0, np.where(dow == 5, 0.45, 0.12))
    w = MONTH_MULT[dates.month.to_numpy()] * weekly * rng.uniform(0.85, 1.15, ndays)
    return w / w.mean()


def _boundaries(intensity: np.ndarray) -> np.ndarray:
    """Cumulative intensity at the START of each day (and after the last): the integral that
    turns an interval of days into an expected number of events, and, inverted with
    searchsorted, a uniform draw into a day."""
    return np.concatenate([[0.0], np.cumsum(intensity)])


def _draw_days(cum: np.ndarray, lo: np.ndarray, hi: np.ndarray) -> np.ndarray:
    """One day for each interval of boundaries [lo, hi], with probability proportional to the
    intensity inside it: the days of the data window (0 = START_DATE)."""
    u = cum[lo] + rng.random(len(lo)) * (cum[hi] - cum[lo])
    return np.clip(np.searchsorted(cum, u, side="left") - 1, lo, hi - 1)


def simulate_lifecycles(agents: pd.DataFrame, intensity: np.ndarray) -> dict:
    """Who the customers are and WHEN: acquisition, order rate, lifetime, size of the orders.

    All the arrays are indexed by customer 0..C-1 (the id is index + 1), the times are days from
    START_DATE. `death` is the day the customer stops buying for good (inf: never, in the
    data's horizon): it lives only in this dictionary and never reaches the databases —
    the point is that analyses must infer it."""
    C = P["customers"]
    ndays = DAYS + 1
    agent_pool = agents.loc[agents.ruolo == "agente", "id"].to_numpy()
    n_init = int(round(C * INITIAL_SHARE))
    initial = np.zeros(C, dtype=bool)
    initial[:n_init] = True

    geo_idx = rng.integers(0, len(GEO), C)
    channel_idx = rng.choice(len(CHANNELS), size=C, p=np.array(CHANNEL_SHARE) / sum(CHANNEL_SHARE))
    macro_of = {r[0]: r[1] for r in REGIONS}
    area_mult = np.array([AREA_CHURN[macro_of[GEO[g][0]]] for g in range(len(GEO))])[geo_idx]
    ch_churn = np.array([CHANNEL_CHURN[c] for c in CHANNELS])[channel_idx]
    ch_rate = np.array([CHANNEL_RATE[c] for c in CHANNELS])[channel_idx]

    # ── acquisition: the first customers are there already, the others arrive over time ──
    acq = np.zeros(C, dtype=np.int64)
    acq_date_before = np.zeros(C, dtype=np.int64)       # days before START_DATE, for the first ones
    t = np.arange(ndays)
    month = pd.to_datetime(np.datetime64(START_DATE) + t.astype("timedelta64[D]")).month.to_numpy()
    dow = pd.to_datetime(np.datetime64(START_DATE) + t.astype("timedelta64[D]")).dayofweek.to_numpy()
    weekly = np.where(dow < 5, 1.0, np.where(dow == 5, 0.10, 0.02))   # contracts are signed on working days
    for c, name in enumerate(CHANNELS):
        sel = np.where((channel_idx == c) & ~initial)[0]
        if sel.size:
            cum = _boundaries(ACQ_GROWTH[name] ** (t / 365.25) * ACQ_SEASON[month] * weekly)
            acq[sel] = _draw_days(cum, np.zeros(sel.size, dtype=np.int64), np.full(sel.size, ndays, dtype=np.int64))
    # the portfolio of the start: acquired up to a few years before the window
    back = np.minimum(np.floor(rng.exponential(450.0, n_init)), 8 * 365) + 1
    acq_date_before[:n_init] = back.astype(np.int64)
    acq[:n_init] = -acq_date_before[:n_init]

    # ── the agent: one of the customer's macro-area, already hired on the day it was acquired ──
    area_of_customer = np.array([macro_of[GEO[g][0]] for g in range(len(GEO))])[geo_idx]
    agent_id = assign_agents(agents, area_of_customer, acq)

    # ── churn: an exponential lifetime, with a hazard that depends on covariates ──
    agent_effect = {int(a): float(e) for a, e in zip(agent_pool, rng.lognormal(0.0, AGENT_CHURN_SIGMA, len(agent_pool)))}
    frailty = rng.gamma(FRAILTY_SHAPE, 1.0 / FRAILTY_SHAPE, C)
    hazard = BASE_CHURN * ch_churn * area_mult * np.array([agent_effect[int(a)] for a in agent_id]) * frailty  # a year
    lifetime = rng.exponential(1.0 / hazard) * 365.25
    # a customer of the start is observed from day 0: its remaining life is exponential too
    death = np.where(initial, 0, acq) + lifetime

    # ── frequency and spend: a customer's own, independent of each other ──
    rate = rng.gamma(RATE_SHAPE, ch_rate / RATE_SHAPE)      # orders a year while alive
    spend = rng.gamma(SPEND_SHAPE, 1.0 / SPEND_SHAPE, C)    # the size of its orders, mean 1

    # the first order follows the acquisition by a few days, and falls on a working day
    lag = np.minimum(np.floor(rng.exponential(FIRST_ORDER_LAG_DAYS, C)), 40).astype(np.int64)
    first = acq + lag
    wd = dow[np.clip(first, 0, DAYS)]
    first = np.where(initial, -1, first + np.where(wd == 5, 2, np.where(wd == 6, 1, 0)))

    return dict(initial=initial, geo_idx=geo_idx, channel_idx=channel_idx, agent_id=agent_id, acq=acq,
                death=death, rate=rate, spend=spend, first=first, hazard=hazard)


def assign_agents(agents: pd.DataFrame, area: np.ndarray, acq: np.ndarray) -> np.ndarray:
    """The agent of each customer: one of the agents of its macro-area (`area`) already hired on
    the day it was acquired (`acq`, days from START_DATE), drawn alike among them; so no order,
    CRM activity or acquisition of a customer comes before its agent was hired. Where no agent of
    the area was hired yet (a customer of the start acquired years before the window), the area's
    earliest hire takes it, and that agent's `data_assunzione` moves back to the acquisition:
    `agents` is changed in place."""
    pool = agents[agents.ruolo == "agente"]
    hired = (pd.to_datetime(pool.data_assunzione) - pd.Timestamp(START_DATE)).dt.days.to_numpy()
    out = np.zeros(len(area), dtype=np.int64)
    for a in AREAS:
        cust = np.where(area == a)[0]
        if not cust.size:
            continue
        mine = np.where(pool.area.to_numpy() == a)[0]
        if not mine.size:
            raise ValueError(f"no agent in the macro-area {a}: the preset needs an area manager for each of {AREAS}")
        mine = mine[np.argsort(hired[mine], kind="stable")]
        ready = np.searchsorted(hired[mine], acq[cust], side="right")      # agents hired by the acquisition
        pick = np.floor(rng.random(cust.size) * np.maximum(ready, 1)).astype(np.int64)
        out[cust] = pool.id.to_numpy()[mine[pick]]
    # the earliest hire of an area takes the customers no one was hired for yet: hired by then
    first_acq = pd.Series(acq).groupby(out).min()
    ids = agents.id.to_numpy()
    for aid, day in first_acq.items():
        row = np.where(ids == aid)[0][0]
        start = (pd.Timestamp(agents.at[row, "data_assunzione"]) - pd.Timestamp(START_DATE)).days
        if start > day:
            agents.at[row, "data_assunzione"] = (pd.Timestamp(START_DATE) + pd.Timedelta(days=int(day))).date()
    return out


def gen_customers(agents: pd.DataFrame, life: dict) -> pd.DataFrame:
    """Customer registry (without `stato`: the CRM computes it from the orders, later), each one
    assigned to an AGENT (not an inspector/manager)."""
    C = P["customers"]
    log(f"  customers: generating {C:,} records with Faker…")
    base = np.datetime64(START_DATE)
    acq_date = pd.to_datetime(base + life["acq"].astype("timedelta64[D]"))

    rows = []
    for i in range(C):
        reg, prov, city = GEO[life["geo_idx"][i]]
        rows.append((
            i + 1,
            fake.company(),
            fake.numerify("###########"),      # VAT number, 11 digits
            CHANNELS[life["channel_idx"][i]],
            fake.street_address().replace("\n", " "),
            fake.numerify("#####"),
            city, prov, reg,
            int(life["agent_id"][i]),
        ))
    cols = ["id", "ragione_sociale", "partita_iva", "canale", "indirizzo", "cap",
            "citta", "provincia", "regione", "agente_id"]
    df = pd.DataFrame(rows, columns=cols)
    df["data_acquisizione"] = acq_date
    # helper arrays for the facts (indexed by cliente_id 1..C)
    df.attrs["agent_of"] = np.concatenate([[0], df.agente_id.to_numpy()])
    df.attrs["channel_of"] = np.concatenate([[0], life["channel_idx"] + 1])  # 1-based in CHANNELS
    return df


def crm_status(life: dict, last_order: np.ndarray, delivered: np.ndarray | None = None) -> pd.DataFrame:
    """`stato` and `attivo` as a CRM would compute them at END_DATE, from what it can see: the
    last order that was not cancelled, and whether one was delivered (`delivered`; None: every
    customer with an order has one). «nuovo»: acquired in the last INACTIVE_AFTER_DAYS and nothing
    delivered yet (no order, or only orders still in progress); «attivo»: it ordered within
    INACTIVE_AFTER_DAYS; «inattivo»: it did not (lost, or only slow: the data does not say)."""
    if delivered is None:
        delivered = last_order >= 0
    since_last = DAYS - last_order
    since_acq = DAYS - life["acq"]
    stato = np.where(~delivered & (since_acq <= INACTIVE_AFTER_DAYS), "nuovo",
                     np.where((last_order >= 0) & (since_last <= INACTIVE_AFTER_DAYS), "attivo", "inattivo"))
    return pd.DataFrame({"stato": stato, "attivo": (stato != "inattivo").astype(np.uint8)})


def channel_multipliers() -> np.ndarray:
    """[len(CHANNELS) × 13]: the channel mix × season as a multiplier of each channel's order
    rate. The monthly VOLUME is still MONTH_MULT's: here the table is divided by the weighted
    mean of the channels, so that it only moves the SHARE of each channel month by month."""
    ch = np.array([CHANNEL_SEASON[c] for c in CHANNELS])
    w = np.array([CHANNEL_RATE[c] * CHANNEL_SHARE[i] for i, c in enumerate(CHANNELS)])
    mean = (w[:, None] * ch).sum(axis=0) / w.sum()
    mean[0] = 1.0
    return ch / mean


def simulate_orders(life: dict, intensity: np.ndarray, day_month: np.ndarray):
    """The orders of every customer: (customer index, day), in chronological order.

    A customer's first order follows its acquisition (the customers of the start have none:
    it was before the window); the following ones are a Poisson process of the customer's own
    rate, modulated by the seasons (month, weekday, channel), until its death or the end of
    the window. The rates are scaled so that the orders add up to the preset's."""
    O, C = P["orders"], P["customers"]
    ndays = DAYS + 1
    ch_mult = channel_multipliers()
    cum_ch = [_boundaries(intensity * ch_mult[c][day_month]) for c in range(len(CHANNELS))]
    channel, first, rate = life["channel_idx"], life["first"], life["rate"]

    ordered = (~life["initial"]) & (first <= DAYS)            # has its first order in the window
    last_day = np.minimum(np.floor(life["death"]), DAYS).astype(np.int64)
    lo = np.where(life["initial"], 0, first + 1)              # the repeat orders: from the day after
    hi = last_day + 1                                         # …to the death (boundary after its day)
    live = (lo < hi) & (life["initial"] | ordered)

    # expected repeat orders per customer, then the scale that makes the total the preset's
    expected = np.zeros(C)
    for c in range(len(CHANNELS)):
        sel = np.where(live & (channel == c))[0]
        expected[sel] = rate[sel] / 365.25 * (cum_ch[c][hi[sel]] - cum_ch[c][lo[sel]])
    n_first = int(ordered.sum())
    want = max(O - n_first, 0)
    k = 1.03 * want / max(expected.sum(), 1e-9)               # 3% over, then thinned to the exact total
    n_rep = rng.poisson(k * expected)

    cust_rep = np.repeat(np.arange(C), n_rep)
    day_rep = np.empty(len(cust_rep), dtype=np.int64)
    ch_rep = channel[cust_rep]
    for c in range(len(CHANNELS)):
        sel = np.where(ch_rep == c)[0]
        if sel.size:
            day_rep[sel] = _draw_days(cum_ch[c], lo[cust_rep[sel]], hi[cust_rep[sel]])
    if len(cust_rep) > want:                                  # thinning a Poisson process leaves a Poisson process
        keep = np.sort(rng.choice(len(cust_rep), size=want, replace=False))
        cust_rep, day_rep = cust_rep[keep], day_rep[keep]
    log(f"  order rate scale ×{k:.3f} · {n_first:,} first orders + {len(cust_rep):,} repeat orders")

    cust = np.concatenate([np.where(ordered)[0], cust_rep])
    day = np.concatenate([first[ordered], day_rep])
    o = np.argsort(day, kind="stable")
    return cust[o], day[o]


def gen_catalogue(cur, prod: pd.DataFrame) -> None:
    """Writes products + price list history + production cost history."""
    cols = ["id", "codice", "nome", "categoria", "tipo", "formato",
            "conservazione_mesi", "peso_medio_kg", "unita_misura"]
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


def gen_orders_and_lines(cur, prod: pd.DataFrame, life: dict, intensity: np.ndarray, agent_of, channel_of,
                         agents: pd.DataFrame) -> dict:
    """Orders + lines + commercial costs, in blocks. Header → customer (and their
    agent); lines → products with price≈list±noise and a discount per channel. The orders
    come from the customers' lifecycles (`simulate_orders`). Returns what the CRM sees of them:
    `last_order`, the day of each customer's last order that was not cancelled (-1: none), and
    `delivered`, whether it has one evaso (for the CRM status); `customer` and `day`, the orders
    that were not cancelled (customer index, day), for the CRM activities' outcomes."""
    C = P["customers"]
    P_n = len(prod)
    prices = prod._list_price.to_numpy()
    base_comm = np.concatenate([[0.0], agents.provvigione_base_pct.to_numpy().astype(float)])
    base_d = np.datetime64(START_DATE)

    # the month of each day of the window: days → month without going through pandas again
    days64 = base_d + np.arange(DAYS + 1).astype("timedelta64[D]")
    day_month = pd.to_datetime(days64).month.to_numpy()

    # the customers' orders: who orders, and when
    o_cust, o_day = simulate_orders(life, intensity, day_month)
    O = len(o_cust)
    last_order = np.full(C, -1, dtype=np.int64)
    delivered = np.zeros(C, dtype=bool)
    kept_customer, kept_day = [], []
    spend = np.concatenate([[1.0], life["spend"]])     # by cliente_id: the size of the customer's orders

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
        days = o_day[done:done + n]
        order_date = (base_d + days.astype("timedelta64[D]")).astype(str)

        customer = o_cust[done:done + n] + 1               # cliente_id
        agent = agent_of[customer]
        channel_i = channel_of[customer]                   # 1..len(CHANNELS)
        channel = np.array(CHANNELS)[channel_i - 1]
        bulk = np.isin(channel_i, [1, 3])                  # Supermercati/Ingrosso: qty↑ discounts↑
        depot = np.array(DEPOTS)[rng.integers(0, len(DEPOTS), n)]
        number = np.char.add("ORD-", oid.astype(str))

        # order state: cancelled share = baseline + depot anomalies (chronic /
        # incident); "in_lavorazione" only for the orders of the window's last weeks, most of
        # those of the last days (an order is fulfilled in a few days: the older ones are evasi)
        p_cancel = np.full(n, CANCEL_BASE)
        for dep, mx in CANCEL_CHRONIC.items():
            mask = depot == dep
            p_cancel[mask] += mx * chronic_ramp[days[mask]]
        for dep, (_start, _end, peak) in CANCEL_INCIDENT.items():
            mask = depot == dep
            p_cancel[mask] += peak * incident_flag[dep][days[mask]]
        p_cancel = np.clip(p_cancel, 0.0, 0.95)
        cancelled = rng.random(n) < p_cancel
        age = DAYS - days                                   # days from the order to the end of the window
        p_inprog = np.where(age <= IN_PROGRESS_DAYS, 0.7 * np.exp(-age / 4.0), 0.0)
        in_prog = (~cancelled) & (rng.random(n) < p_inprog)
        state = np.where(cancelled, "annullato", np.where(in_prog, "in_lavorazione", "evaso"))
        np.maximum.at(last_order, customer[~cancelled] - 1, days[~cancelled])
        delivered[customer[state == "evaso"] - 1] = True
        kept_customer.append(customer[~cancelled] - 1)
        kept_day.append(days[~cancelled])

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
        r_spend = np.repeat(spend[customer], n_lines)
        # product chosen per month: the MIX follows the season (panettone at Christmas,
        # taralli in summer, …), it is no longer uniform
        r_months = day_month[r_days]
        prod_i = np.empty(tot, dtype=np.int64)
        for m in range(1, 13):
            sel = np.where(r_months == m)[0]
            if sel.size:
                prod_i[sel] = rng.choice(P_n, size=sel.size, p=prod_w[m])
        list_price = prices[prod_i]
        # quantity (kilos or pieces): gamma (skewed), higher for Supermercati/Ingrosso, the
        # customer's own size (independent of how often it orders) and slowly growing over
        # time (the average basket widens year on year)
        basket = (1.0 + ANNUAL_BASKET_GROWTH) ** (r_days / 365.25)
        qty = (rng.gamma(2.0, np.where(r_channel_bulk, QTY_BULK, QTY_BASE)) + 2.0) * basket * r_spend
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
    return dict(last_order=last_order, delivered=delivered, customer=np.concatenate(kept_customer),
                day=np.concatenate(kept_day))


def gen_activities(ch, agent_of, life: dict, intensity: np.ndarray, orders: dict) -> None:
    """CRM activities (ClickHouse's big table), in blocks. They follow the customers' lives:
    none before the acquisition (the customers of the start: from the window's start); while
    the customer buys, the normal pace; after it has churned, a fraction of it (POST_CHURN_ACTIVITY):
    the reminders and the attempts to win it back, the reminders more often. The outcome «ordine»
    is a contact an order follows: it stays only where the customer has an order that was not
    cancelled (`orders`, gen_orders_and_lines') on that day or within ORDER_OUTCOME_DAYS after it,
    and never on a complaint; elsewhere the contact went well without an order («positivo»)."""
    A = P["activities"]
    C = P["customers"]
    KINDS = np.array(["visita", "telefonata", "email", "sollecito", "reclamo"])
    OUTCOMES = np.array(["positivo", "neutro", "negativo", "ordine"])
    base = np.datetime64(f"{START_DATE}T00:00:00")
    ndays = DAYS + 1
    cum = _boundaries(intensity)

    # each customer's two stretches of the window: alive and churned, and the weight of each
    # (the time in the season's intensity, times a propensity of its own to be contacted)
    start = np.maximum(life["acq"], 0)
    death = np.minimum(np.floor(life["death"]) + 1, ndays).astype(np.int64)   # boundary where the churn starts
    alive_hi = np.maximum(death, start)
    propensity = rng.gamma(2.0, 0.5, C)
    w_alive = propensity * (cum[alive_hi] - cum[start])
    w_dead = propensity * POST_CHURN_ACTIVITY * (cum[ndays] - cum[alive_hi])
    cum_w = np.cumsum(w_alive + w_dead)
    # the orders as one sorted key (customer, day): the next order of a customer from a day on
    order_key = np.sort(orders["customer"].astype(np.int64) * (ndays + 1) + orders["day"])

    done = 0
    while done < A:
        n = min(ACTIVITIES_CHUNK, A - done)
        idx = np.minimum(np.searchsorted(cum_w, rng.random(n) * cum_w[-1], side="right"), C - 1)
        customer = idx + 1
        agent = agent_of[customer]
        dead = rng.random(n) * (w_alive[idx] + w_dead[idx]) >= w_alive[idx]
        lo = np.where(dead, alive_hi[idx], start[idx])
        hi = np.where(dead, ndays, alive_hi[idx])
        day = _draw_days(cum, lo, np.maximum(hi, lo + 1))
        sec_of_day = np.clip(rng.normal(13 * 3600, 2.5 * 3600, n), 7 * 3600, 20 * 3600)
        offset = (day * 86400 + sec_of_day).astype(np.int64).astype("timedelta64[s]")
        when = base + offset
        # a live customer's contacts are as before; a churned one gets reminders and calls, and says no
        tipo = np.where(dead, rng.choice(len(KINDS), n, p=[0.10, 0.30, 0.25, 0.30, 0.05]), rng.integers(0, len(KINDS), n))
        esito = np.where(dead, rng.choice(len(OUTCOMES), n, p=[0.15, 0.30, 0.50, 0.05]), rng.integers(0, len(OUTCOMES), n))
        key = idx.astype(np.int64) * (ndays + 1) + day
        at = np.searchsorted(order_key, key, side="left")
        nxt = order_key[np.minimum(at, len(order_key) - 1)] if len(order_key) else np.full(n, -1)
        followed = (at < len(order_key)) & (nxt // (ndays + 1) == idx) & (nxt - key <= ORDER_OUTCOME_DAYS)
        esito = np.where((esito == 3) & (~followed | (tipo == 4)), 0, esito)
        df = pd.DataFrame({
            "id": np.arange(done + 1, done + n + 1, dtype=np.int64),
            "cliente_id": customer.astype(np.uint32),
            "agente_id": agent.astype(np.uint32),
            "data": pd.to_datetime(when),
            "tipo": KINDS[tipo],
            "esito": OUTCOMES[esito],
            "durata_min": rng.integers(2, 90, n).astype(np.uint16),
            "note": "",
        })
        ch.insert_df("analytics.attivita_crm", df)
        done += n
        log(f"    CRM activities {done:,}/{A:,}")


# ── the web shop's experiments ──────────────────────────────────────────────────
# A separate part of the generator (docs/specs/ab-testing.md, «Sample data»): the company's web
# shop runs three experiments on its visitors, in ClickHouse `analytics`. It draws from a generator
# of its OWN (a PCG64 on SEED + 1000) and never from `rng` or Faker, so every other table is
# byte for byte what it was (test_generate_experiments pins their hash at small).
#
#   checkout_2026        A/B, 50/50, 6 weeks: B +5% conversion (relative), a converter's revenue unchanged
#   banner_home          A/B/C/D, 25% each, 4 weeks: clicks per impression, B +6%, C -4%, D 0
#   spedizione_gratuita  A/B, 50/50 planned, 5 weeks: B +3% revenue per visitor AND a real SRM: from day
#                        12 a bot filter, deployed on B only, drops its least engaged visitors (7% of B's)
#
# A visitor has an engagement (lognormal) that drives its sessions, impressions, clicks, conversion
# and what it spends; the spend of a converter is a lognormal and, once in fifty, a Pareto(1.5) bulk
# order. The 28 days before the experiment come from the same engagement (the CUPED covariate),
# unknown for the 40% of visitors that are new. `utente_id` is a BIGINT, a few above 2^31.
EXP_SEED_OFFSET = 1000
EXP_ENGAGEMENT_SIGMA = 0.7      # the lognormal engagement, mean 1
EXP_NEW_SHARE = 0.40            # visitors without a past: no 28-day covariate
EXP_CROSSOVER_SHARE = 0.001     # checkout visitors seen in both variants
EXP_HIGH_ID_SHARE = 0.05        # visitors whose id is above 2^31
EXP_CONV_EXPONENT = 1.0         # conversion ∝ engagement^this; spend per order ∝ engagement^EXP_SPEND_EXPONENT
EXP_SPEND_EXPONENT = 1.0
EXP_ORDER_SIGMA = 0.5           # the noise of an order's size (lognormal)
EXP_ORDER_MEDIAN = 35.0         # euro
EXP_BULK_CHANCE = 0.02          # a converter's order is a bulk one, a Pareto(1.5) multiple of the normal size
EXP_BULK_SCALE = 0.5
EXP_BOT_FROM_DAY = 12           # the shipping experiment's bot filter acts from this day (1-based) …
EXP_BOT_SHARE = 0.07            # … on B, dropping the least engaged arrivals: this share of ALL of B's visitors
EXP_WEEK = np.array([0.90, 0.92, 0.96, 1.00, 1.08, 1.25, 1.18])   # entries by weekday, Monday first
EXP_HISTORY_DAYS = 56           # the history: the eight weeks before the first experiment
EXP_SHARES = {"checkout_2026": 30, "banner_home": 14, "spedizione_gratuita": 16}   # of 60 (parts of `users`)

EXPERIMENTS = [
    dict(id="checkout_2026", nome="Nuovo checkout", inizio=date(2026, 3, 2), giorni=42, varianti=["A", "B"],
         quote=[0.5, 0.5], conv=0.38, ctr=0.05, conv_lift={"B": 0.05}, ctr_lift={}, rev_lift={},
         descrizione="Checkout a pagina unica (B) contro il checkout a due passi (A, controllo): sei settimane, metà dei visitatori ciascuno."),
    dict(id="banner_home", nome="Banner della home", inizio=date(2026, 4, 20), giorni=28, varianti=["A", "B", "C", "D"],
         quote=[0.25] * 4, conv=0.30, ctr=0.05, conv_lift={}, ctr_lift={"B": 0.06, "C": -0.04}, rev_lift={},
         descrizione="Quattro banner della home page (A è quello attuale): quattro settimane, un quarto dei visitatori per banner; la misura sono i click per impression."),
    dict(id="spedizione_gratuita", nome="Soglia di spedizione gratuita", inizio=date(2026, 5, 25), giorni=35, varianti=["A", "B"],
         quote=[0.5, 0.5], conv=0.30, ctr=0.05, conv_lift={}, ctr_lift={}, rev_lift={"B": 0.03},
         descrizione="Spedizione gratuita a una soglia più bassa (B) contro quella attuale (A): cinque settimane, metà dei visitatori ciascuno."),
]
EXP_BOT_EXPERIMENT = "spedizione_gratuita"
EXP_USER_COLUMNS = ["esperimento_id", "utente_id", "variante", "data_ingresso", "sessioni", "impression", "click",
                    "convertito", "ricavo", "ricavo_pre_28g", "sessioni_pre_28g"]
EXP_HISTORY_COLUMNS = ["utente_id", "data_prima_visita", "sessioni", "impression", "click", "convertito", "ricavo", "ricavo_pre_28g"]


def experiment_rng() -> np.random.Generator:
    """The experiments' own random generator: the same at every run, and apart from `rng`."""
    return np.random.Generator(np.random.PCG64(SEED + EXP_SEED_OFFSET))


def _experiment_ids(r: np.random.Generator, n: int) -> np.ndarray:
    """n distinct visitor ids (int64), in no order: most below 2^31, EXP_HIGH_ID_SHARE of them above it
    (up to 2^33), so that the column needs 64 bits and a narrower type would show."""
    high = max(1, round(n * EXP_HIGH_ID_SHARE))
    low = r.choice(2_000_000_000 - 10_000_000, size=n - high, replace=False).astype(np.int64) + 10_000_000
    top = r.choice(2**33 - 2**31 - 1, size=high, replace=False).astype(np.int64) + 2**31 + 1
    return r.permutation(np.concatenate([low, top]))


def _engagement(r: np.random.Generator, n: int) -> np.ndarray:
    s = EXP_ENGAGEMENT_SIGMA
    return r.lognormal(-s * s / 2, s, size=n)


def _orders(r: np.random.Generator, eng: np.ndarray, bulk: float) -> np.ndarray:
    """The size of an order, per visitor: a lognormal around the visitor's own level; with chance
    `bulk` a Pareto(1.5) multiple of it (the heavy tail the cap is for)."""
    size = np.exp(np.log(EXP_ORDER_MEDIAN) + EXP_SPEND_EXPONENT * np.log(eng) + r.normal(0, EXP_ORDER_SIGMA, size=len(eng)))
    big = r.random(len(eng)) < bulk
    return np.where(big, size * (1 + EXP_BULK_SCALE * r.pareto(1.5, size=len(eng))), size)


def _conversion_probability(eng: np.ndarray, base: float) -> np.ndarray:
    """∝ engagement^EXP_CONV_EXPONENT, with the mean `base` (before the 0.95 ceiling)."""
    s, k = EXP_ENGAGEMENT_SIGMA, EXP_CONV_EXPONENT
    return base * eng ** k / np.exp(s * s * (k * k - k) / 2)


def _behaviour(r: np.random.Generator, spec: dict, eng: np.ndarray, fresh: np.ndarray, variant: np.ndarray | None) -> dict:
    """What the visitors do in the experiment (variant None: the history's, with no effect), and in the
    28 days before it. `fresh`: the visitors with no past (their covariates are null)."""
    n = len(eng)
    pick = (lambda eff: np.array([1.0 + eff.get(v, 0.0) for v in variant])) if variant is not None else (lambda eff: np.ones(n))
    sessions = 1 + r.poisson(2.0 * eng)
    impressions = 1 + r.poisson(8.0 * sessions)
    ctr = np.minimum(spec["ctr"] * eng ** 0.2 * pick(spec["ctr_lift"]), 0.5)
    clicks = r.binomial(impressions, ctr)
    p = np.minimum(_conversion_probability(eng, spec["conv"]) * pick(spec["conv_lift"]), 0.95)
    converted = r.random(n) < p
    revenue = np.where(converted, _orders(r, eng, EXP_BULK_CHANCE) * pick(spec["rev_lift"]), 0.0)
    # the 28 days before: the same engagement, its own noise, one order's size where it bought
    pre_bought = r.random(n) < np.minimum(_conversion_probability(eng, spec["conv"]), 0.95)
    pre_revenue = np.where(pre_bought, _orders(r, eng, EXP_BULK_CHANCE / 2), 0.0)
    pre_sessions = 1 + r.poisson(2.0 * eng)
    return dict(sessioni=sessions, impression=impressions, click=clicks, convertito=converted,
                ricavo=np.round(revenue, 2),
                ricavo_pre_28g=np.where(fresh, np.nan, np.round(pre_revenue, 2)),
                sessioni_pre_28g=np.where(fresh, np.nan, pre_sessions))


def _entry_days(r: np.random.Generator, start: date, days: int, n: int) -> np.ndarray:
    """The day (0-based) each visitor enters, with the week's cycle: more at the weekend."""
    weekday = (np.datetime64(start) + np.arange(days).astype("timedelta64[D]")).astype("datetime64[D]").astype(int) % 7
    # 1970-01-01 was a Thursday: weekday 0 = Monday needs +3
    weights = EXP_WEEK[(weekday + 3) % 7]
    return r.choice(days, size=n, p=weights / weights.sum())


def _experiment(r: np.random.Generator, spec: dict, ids: np.ndarray) -> pd.DataFrame:
    """One experiment's visitors, one row each (and the checkout's few seen in both variants)."""
    n = len(ids)
    eng = _engagement(r, n)
    fresh = r.random(n) < EXP_NEW_SHARE
    day = _entry_days(r, spec["inizio"], spec["giorni"], n)
    variants = np.array(spec["varianti"])
    variant = variants[r.choice(len(variants), size=n, p=spec["quote"])]
    # the crossover: a few visitors of the checkout see the other variant too, on a later day
    if spec["id"] == "checkout_2026":
        k = round(n * EXP_CROSSOVER_SHARE)
        twin = r.choice(n, size=k, replace=False)
        ids = np.concatenate([ids, ids[twin]])
        eng, fresh = np.concatenate([eng, eng[twin]]), np.concatenate([fresh, fresh[twin]])
        other = np.where(variant[twin] == variants[0], variants[1], variants[0])
        day = np.concatenate([day, np.minimum(day[twin] + r.integers(0, 4, size=k), spec["giorni"] - 1)])
        variant = np.concatenate([variant, other])
    what = _behaviour(r, spec, eng, fresh, variant)
    if spec["id"] == EXP_BOT_EXPERIMENT:
        # the bot filter: from its day, on B only, it drops the least engaged arrivals (it took them for bots)
        in_b = variant == spec["varianti"][1]
        late = in_b & (day >= EXP_BOT_FROM_DAY - 1)
        cut = np.quantile(eng[late], EXP_BOT_SHARE * in_b.sum() / late.sum())
        keep = ~(late & (eng < cut))
        ids, day, variant = ids[keep], day[keep], variant[keep]
        what = {k: v[keep] for k, v in what.items()}
    df = pd.DataFrame({"esperimento_id": spec["id"], "utente_id": ids.astype(np.int64), "variante": variant,
                       "data_ingresso": pd.to_datetime(np.datetime64(spec["inizio"]) + day.astype("timedelta64[D]")), **what})
    return df.sort_values(["data_ingresso", "utente_id"], kind="stable", ignore_index=True)


def _exp_table(df: pd.DataFrame) -> pd.DataFrame:
    """The columns as ClickHouse stores them: whole numbers, a boolean, money to the cent, nulls where unknown."""
    df = df.copy()
    for c in ("sessioni", "impression", "click"):
        df[c] = df[c].astype(np.uint32)
    df["convertito"] = df["convertito"].astype(bool)
    df["ricavo"] = df["ricavo"].map(lambda v: Decimal(f"{v:.2f}"))
    df["ricavo_pre_28g"] = df["ricavo_pre_28g"].map(lambda v: None if v != v else Decimal(f"{v:.2f}")).astype(object)
    if "sessioni_pre_28g" in df:
        df["sessioni_pre_28g"] = pd.array(df["sessioni_pre_28g"].map(lambda v: None if v != v else int(v)), dtype="UInt32")
    return df


def build_experiments(scale: str = SCALE) -> dict[str, pd.DataFrame]:
    """The four tables (`esperimenti`, `esperimenti_utenti`, `esperimenti_storico`, `esperimenti_giornaliero`),
    in memory, as raw numbers (`_exp_table` turns them into what ClickHouse takes)."""
    sizes = scales.EXPERIMENTS[scale]
    r = experiment_rng()
    parts = {s["id"]: round(sizes["users"] * EXP_SHARES[s["id"]] / sum(EXP_SHARES.values())) for s in EXPERIMENTS}
    last = EXPERIMENTS[-1]["id"]
    parts[last] = sizes["users"] - sum(v for k, v in parts.items() if k != last)
    ids = _experiment_ids(r, sizes["users"] + sizes["history"])
    users, at = [], 0
    for spec in EXPERIMENTS:
        users.append(_experiment(r, spec, ids[at:at + parts[spec["id"]]]))
        at += parts[spec["id"]]
    users = pd.concat(users, ignore_index=True)
    # the history: the same visitors' mechanics with no effect, the eight weeks before the first experiment
    hist_ids = ids[at:]
    eng = _engagement(r, len(hist_ids))
    fresh = r.random(len(hist_ids)) < EXP_NEW_SHARE
    what = _behaviour(r, EXPERIMENTS[0], eng, fresh, None)
    first = EXPERIMENTS[0]["inizio"]
    day = _entry_days(r, first - pd.Timedelta(days=EXP_HISTORY_DAYS).to_pytimedelta(), EXP_HISTORY_DAYS, len(hist_ids))
    history = pd.DataFrame({"utente_id": hist_ids.astype(np.int64),
                            "data_prima_visita": pd.to_datetime(np.datetime64(first) - np.timedelta64(EXP_HISTORY_DAYS, "D") + day.astype("timedelta64[D]")),
                            **{k: v for k, v in what.items() if k != "sessioni_pre_28g"}})
    history = history.sort_values(["data_prima_visita", "utente_id"], kind="stable", ignore_index=True)
    daily = users.assign(ricavo_quadrati=users["ricavo"] ** 2, utenti=1, conversioni=users["convertito"].astype(int)).groupby(
        ["esperimento_id", "variante", "data_ingresso"], as_index=False).agg(
        utenti=("utenti", "sum"), conversioni=("conversioni", "sum"), ricavo_totale=("ricavo", "sum"), ricavo_quadrati=("ricavo_quadrati", "sum"))
    daily = daily.rename(columns={"data_ingresso": "giorno"})[["esperimento_id", "variante", "giorno", "utenti", "conversioni", "ricavo_totale", "ricavo_quadrati"]]
    experiments = pd.DataFrame([{"id": s["id"], "nome": s["nome"], "descrizione": s["descrizione"],
                                 "inizio": pd.Timestamp(s["inizio"]), "fine": pd.Timestamp(s["inizio"] + pd.Timedelta(days=s["giorni"] - 1).to_pytimedelta()),
                                 "varianti": ",".join(s["varianti"]), "quote_previste": ",".join(f"{q:g}" for q in s["quote"])}
                                for s in EXPERIMENTS])
    return {"esperimenti": experiments, "esperimenti_utenti": users, "esperimenti_storico": history,
            "esperimenti_giornaliero": daily}


def gen_experiments(ch, scale: str = SCALE) -> None:
    """Creates the experiments' tables in ClickHouse and fills them."""
    with open(os.path.join(HERE, "schema_experiments.sql"), encoding="utf-8") as f:
        script = "\n".join(line.split("--")[0] for line in f.read().splitlines())    # a comment may hold a semicolon
    for stmt in script.split(";"):
        if stmt.strip():
            ch.command(stmt)
    t = build_experiments(scale)
    t["esperimenti_utenti"] = _exp_table(t["esperimenti_utenti"])
    t["esperimenti_storico"] = _exp_table(t["esperimenti_storico"])
    t["esperimenti_giornaliero"] = t["esperimenti_giornaliero"].assign(
        ricavo_totale=lambda d: d["ricavo_totale"].round(2).map(lambda v: Decimal(f"{v:.2f}")),
        ricavo_quadrati=lambda d: d["ricavo_quadrati"].round(2).map(lambda v: Decimal(f"{v:.2f}")))
    for name, df in t.items():
        ch.insert_df(f"analytics.{name}", df)
        log(f"  experiments: {name} {len(df):,} rows")


def ensure_experiments(ch) -> bool:
    """For databases filled before the experiments existed: adds them if their tables are missing.
    True when it added them."""
    if ch.command("EXISTS TABLE analytics.esperimenti_utenti") == 1:
        return False
    gen_experiments(ch)
    return True


# ── the raw materials' prices ───────────────────────────────────────────────────
# A separate part of the generator (docs/specs/garch-risk.md, «Sample data»): the daily prices the
# company pays for three raw materials, in the public `ref` schema (market prices are public data).
# A series with clustered volatility is what the volatility and risk nodes need and no other table has.
# It draws from a generator of its OWN (a PCG64 on SEED + 2000) and never from `rng` or Faker, so
# every other table is byte for byte what it was (test_generate_quotazioni pins their hash at small).
#
# The log returns of each material follow a GJR-GARCH(1,1) with skewed Student t innovations
# (Fernández & Steel, standardized: mean 0, variance 1):
#     r_t = mu + e_t,  e_t = s_t z_t,  s_t^2 = omega + (alpha + gamma 1[e_{t-1} < 0]) e_{t-1}^2 + beta s_{t-1}^2
# `persistence` = alpha + gamma/2 + beta (0.96 to 0.99); omega is set from the long-run daily volatility
# `vol`; the olive oil starts at `vol0`, well above it, and calms down: the intercept carries the excess
# variance (vol0^2 - vol^2, times 1 - persistence) and loses QUOT_CALM_DECAY of it a day (a half-life of 69 days), because the
# variance recursion alone forgets its start in a few days. The series is the same size at every scale
# (about 2,300 rows): the owner's rule of small data.
QUOT_SEED_OFFSET = 2000
QUOT_START = date(2023, 7, 3)
QUOT_END = END_DATE
QUOT_MATERIALS = [
    #   material        unit price at the start, drift, long-run daily vol, starting vol, alpha, gamma, beta, nu, xi
    dict(materia="grano_duro",  p0=395.0, mu=0.00010, vol=0.0130, vol0=0.0130, alpha=0.040, gamma=0.100, beta=0.890, nu=6.0, xi=0.90),
    dict(materia="olio_evo",    p0=7.20,  mu=0.00030, vol=0.0100, vol0=0.0350, alpha=0.060, gamma=0.050, beta=0.900, nu=5.0, xi=0.92),
    dict(materia="caffe_verde", p0=4.10,  mu=0.00020, vol=0.0180, vol0=0.0180, alpha=0.080, gamma=0.050, beta=0.865, nu=8.0, xi=0.95),
]
QUOT_CALM_DECAY = 0.99
QUOT_COLUMNS = ["data", "materia", "prezzo"]


def quotazioni_rng() -> np.random.Generator:
    """The prices' own random generator: the same at every run, and apart from `rng`."""
    return np.random.Generator(np.random.PCG64(SEED + QUOT_SEED_OFFSET))


def trading_days(start: date = QUOT_START, end: date = QUOT_END) -> list[date]:
    """Monday to Friday, without 1 January, Good Friday, Easter Monday, 1 May, 25 and 26 December."""
    from datetime import timedelta

    closed = set()
    for year in range(start.year, end.year + 1):
        easter = _easter(year)
        closed |= {date(year, 1, 1), date(year, 5, 1), date(year, 12, 25), date(year, 12, 26),
                   easter - timedelta(days=2), easter + timedelta(days=1)}
    days, d = [], start
    while d <= end:
        if d.weekday() < 5 and d not in closed:
            days.append(d)
        d += timedelta(days=1)
    return days


def _skew_t(r: np.random.Generator, n: int, nu: float, xi: float) -> np.ndarray:
    """n draws of the standardized skewed Student t (Fernández & Steel 1998: the t split at 0 and the
    two sides scaled by xi and 1/xi), with mean 0 and variance 1."""
    t = np.abs(r.standard_t(nu, size=n))
    positive = r.random(n) < xi * xi / (1 + xi * xi)
    x = np.where(positive, xi * t, -t / xi)
    e_abs = 2 * np.sqrt(nu) * math.gamma((nu + 1) / 2) / (np.sqrt(np.pi) * (nu - 1) * math.gamma(nu / 2))
    mean = e_abs * (xi - 1 / xi)
    second = (xi ** 3 + xi ** -3) / (xi + 1 / xi) * nu / (nu - 2)
    return (x - mean) / np.sqrt(second - mean * mean)


def _gjr_returns(r: np.random.Generator, spec: dict, n: int) -> np.ndarray:
    """n log returns of the material's GJR-GARCH(1,1)."""
    a, g, b = spec["alpha"], spec["gamma"], spec["beta"]
    omega = spec["vol"] ** 2 * (1 - (a + g / 2 + b))
    z = _skew_t(r, n, spec["nu"], spec["xi"])
    out = np.empty(n)
    excess = (spec["vol0"] ** 2 - spec["vol"] ** 2) * (1 - (a + g / 2 + b))   # what the recursion turns into the start's excess variance
    var = spec["vol0"] ** 2
    for i in range(n):
        e = math.sqrt(var) * z[i]
        out[i] = spec["mu"] + e
        var = omega + excess * QUOT_CALM_DECAY ** (i + 1) + (a + (g if e < 0 else 0.0)) * e * e + b * var
    return out


def build_quotazioni() -> pd.DataFrame:
    """The prices, by day and material: data, materia, prezzo (4 decimals)."""
    r = quotazioni_rng()
    days = trading_days()
    frames = []
    for spec in QUOT_MATERIALS:
        returns = _gjr_returns(r, spec, len(days) - 1)
        price = spec["p0"] * np.exp(np.concatenate([[0.0], np.cumsum(returns)]))
        frames.append(pd.DataFrame({"data": days, "materia": spec["materia"], "prezzo": np.round(price, 4)}))
    return pd.concat(frames, ignore_index=True)[QUOT_COLUMNS]


def gen_quotazioni(cur) -> None:
    """Fills ref.quotazioni_materie_prime (its table exists: schema_ref.sql)."""
    df = build_quotazioni()
    copy_df(cur, "ref.quotazioni_materie_prime", df)
    log(f"  raw materials' prices: {len(df):,} rows")


def ensure_quotazioni(pg) -> bool:
    """For databases filled before the prices existed: creates and fills their table when it is missing, without
    touching the others. True when it did."""
    with pg.cursor() as cur:
        cur.execute("SELECT to_regclass('ref.quotazioni_materie_prime')")
        if cur.fetchone()[0] is not None:
            return False
        with open(os.path.join(HERE, "schema_ref.sql"), encoding="utf-8") as f:
            script = "\n".join(line.split("--")[0] for line in f.read().splitlines())
        cur.execute("CREATE SCHEMA IF NOT EXISTS ref")
        for stmt in script.split(";"):
            if "quotazioni_materie_prime" in stmt:
                cur.execute(stmt)
        gen_quotazioni(cur)
    pg.commit()
    return True


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

    log("Simulating the customers' lives (acquisition, order rate, churn, spend)…")
    intensity = build_day_intensity()
    life = simulate_lifecycles(agents, intensity)
    # after the lives: a customer's agent is hired by its acquisition (assign_agents may move a hire back)
    ch.insert_df("analytics.agenti", agents)
    log(f"  {len(agents):,} agents ({P['managers']} area managers, {P['inspectors']} supervisors, {P['agents']} agents)")
    customers = gen_customers(agents, life)
    agent_of = customers.attrs["agent_of"]
    channel_of = customers.attrs["channel_of"]

    log("Generating the public reference data (calendar, regions)…")
    with pg.cursor() as cur:
        gen_ref(cur)
        gen_quotazioni(cur)
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
        orders = gen_orders_and_lines(cur, prod, life, intensity, agent_of, channel_of, agents)
    pg.commit()

    # the registry goes in now: its CRM status needs the orders (and never the churn, which no
    # one has written down)
    customers = pd.concat([customers, crm_status(life, orders["last_order"], orders["delivered"])], axis=1)
    ch.insert_df("analytics.clienti", customers[[
        "id", "ragione_sociale", "partita_iva", "canale", "indirizzo", "cap",
        "citta", "provincia", "regione", "agente_id", "data_acquisizione", "stato", "attivo"]])
    log(f"  {len(customers):,} customers inserted: " + ", ".join(
        f"{k} {v:,}" for k, v in customers.stato.value_counts().items()))

    log("Building the secondary Postgres indexes…")
    with pg.cursor() as cur:
        run_sql_file(cur, os.path.join(HERE, "indexes_postgres.sql"))
    pg.commit()

    log("Generating the CRM activities (ClickHouse)…")
    gen_activities(ch, agent_of, life, intensity, orders)

    log("Generating the web shop's experiments (ClickHouse)…")
    gen_experiments(ch)

    pg.close()
    log(f"Done in {int(time.time() - t0)}s. Scale «{SCALE}».")
    log("Tabularia connections → Postgres sampledb-postgres:5432 / ClickHouse sampledb-clickhouse:8123")


if __name__ == "__main__":
    main()
