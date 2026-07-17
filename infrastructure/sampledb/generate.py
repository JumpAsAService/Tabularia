#!/usr/bin/env python3
"""Popola i due database di esempio dell'azienda di SALUMI con dati simil-reali.

  Postgres «shop»       → GESTIONALE: catalogo prodotti, listino, costi di
                          produzione, ordini, righe, costi commerciali.
  ClickHouse «analytics»→ CRM: rete commerciale gerarchica (agente→ispettore→
                          capo area), anagrafica clienti, attività CRM.

Dimensioni realistiche con Faker (nomi, aziende, indirizzi); tabelle-fatto
grandi con numpy (veloce), caricate a blocchi via COPY (Postgres) e insert_df
(ClickHouse). Scala regolabile con SAMPLEDB_SCALE=small|medium|robusta.

Il margine di riga si costruisce così (in Tabularia, unendo le due sorgenti):
    ricavo   = quantita * prezzo_unitario * (1 - sconto_pct/100)
    costo_p  = da catalogo.costi_produzione (per prodotto, alla data)
    costo_c  = da vendite.costi_commerciali (per riga)
    margine  = ricavo - costo_p - costo_c
e l'attribuzione per agente/ispettore/capo-area arriva dal join cross-DB su
cliente_id/agente_id verso il CRM ClickHouse.
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

# ── configurazione ───────────────────────────────────────────────────────────
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
SCALE = os.getenv("SAMPLEDB_SCALE", "robusta").lower()
SEED = int(os.getenv("SAMPLEDB_SEED", "42"))

# finestra temporale dei dati (l'app "oggi" è metà 2026): ~3 anni fino a lì
DATA_FINE = date(2026, 7, 15)
DATA_INIZIO = date(2023, 7, 1)
GIORNI = (DATA_FINE - DATA_INIZIO).days

# ── trend temporali (perché i dati NON siano piatti/uniformi) ────────────────
# crescita aziendale composta annua: più ordini col passare del tempo
GROWTH_ANNUO = 0.18
# crescita annua del valore medio ordine (carrello che si allarga nel tempo)
BASKET_GROWTH_ANNUO = 0.05
# stagionalità mensile dei salumi (indice 1..12; 0 = segnaposto):
# Natale fortissimo, Pasqua (apr) su, agosto giù (ferie), autunno in ripresa
MONTH_MULT = np.array([0.0, 0.90, 0.82, 1.05, 1.15, 1.00, 0.95, 0.90, 0.62, 1.05, 1.12, 1.30, 1.80])

HERE = os.path.dirname(os.path.abspath(__file__))

# preset di scala: (capi_area, ispettori, agenti, clienti, ordini, attività CRM)
PRESETS = {
    "small":   dict(capi=3, ispettori=12, agenti=90,   clienti=3_000,   ordini=30_000,     attivita=60_000),
    "medium":  dict(capi=5, ispettori=30, agenti=300,  clienti=30_000,  ordini=1_000_000,  attivita=2_000_000),
    "robusta": dict(capi=8, ispettori=56, agenti=700,  clienti=150_000, ordini=12_000_000, attivita=60_000_000),
}
if SCALE not in PRESETS:
    sys.exit(f"SAMPLEDB_SCALE non valido: {SCALE!r} (usa small|medium|robusta)")
P = PRESETS[SCALE]

rng = np.random.default_rng(SEED)
fake = Faker("it_IT")
Faker.seed(SEED)

CHUNK_ORDINI = 1_000_000    # ordini per blocco COPY (limita la memoria)
CHUNK_ATTIVITA = 2_000_000  # righe attività CRM per insert


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


# ── catalogo salumi (curato) → SKU per formato ───────────────────────────────
# (nome, categoria, tipo, stagionatura_mesi, prezzo_kg, frazione_materia_prima)
BASE_SALUMI = [
    ("Prosciutto Crudo di Parma DOP", "Prosciutti", "crudo", 18, 27.0, 0.58),
    ("Prosciutto San Daniele DOP", "Prosciutti", "crudo", 16, 29.0, 0.60),
    ("Prosciutto Cotto Alta Qualità", "Insaccati cotti", "cotto", 0, 12.5, 0.50),
    ("Speck Alto Adige IGP", "Prosciutti", "crudo", 5, 22.0, 0.52),
    ("Culatello di Zibello DOP", "Prosciutti", "crudo", 14, 48.0, 0.62),
    ("Mortadella Bologna IGP", "Insaccati cotti", "cotto", 0, 9.5, 0.46),
    ("Salame Milano", "Salami", "stagionato", 3, 15.5, 0.55),
    ("Salame Napoli", "Salami", "stagionato", 3, 16.0, 0.55),
    ("Salame Felino IGP", "Salami", "stagionato", 4, 19.0, 0.57),
    ("Soppressata Calabra DOP", "Salami", "stagionato", 3, 21.0, 0.56),
    ("Finocchiona IGP", "Salami", "stagionato", 4, 17.5, 0.55),
    ("Coppa Piacentina DOP", "Salumi stagionati", "stagionato", 6, 24.0, 0.58),
    ("Pancetta Arrotolata", "Salumi stagionati", "stagionato", 3, 15.0, 0.53),
    ("Pancetta Tesa", "Salumi stagionati", "stagionato", 2, 13.5, 0.52),
    ("Guanciale", "Salumi stagionati", "stagionato", 3, 14.0, 0.50),
    ("Lardo di Colonnata IGP", "Salumi stagionati", "stagionato", 6, 20.0, 0.55),
    ("Bresaola della Valtellina IGP", "Salumi stagionati", "stagionato", 2, 34.0, 0.64),
    ("Cotechino Modena IGP", "Insaccati da cuocere", "fresco", 0, 11.0, 0.48),
    ("Zampone Modena IGP", "Insaccati da cuocere", "fresco", 0, 12.0, 0.48),
    ("'Nduja di Spilinga", "Salami", "stagionato", 2, 18.0, 0.54),
    ("Salsiccia Stagionata", "Salami", "stagionato", 2, 13.0, 0.50),
    ("Prosciutto di Praga", "Insaccati cotti", "cotto", 0, 13.5, 0.51),
    ("Porchetta di Ariccia IGP", "Insaccati cotti", "cotto", 0, 14.5, 0.52),
    ("Wurstel di Suino", "Insaccati cotti", "cotto", 0, 6.5, 0.42),
]
# (formato, unita, peso_medio_kg, markup_prezzo, costo_confez_pz)
FORMATI = [
    ("intero sottovuoto", "kg", 6.500, 1.00, 0.35),
    ("mezzo sottovuoto", "kg", 3.200, 1.02, 0.30),
    ("banco taglio", "kg", 1.000, 1.05, 0.15),
    ("tranci 500g", "pz", 0.500, 1.20, 0.45),
    ("affettato vaschetta 100g", "pz", 0.100, 1.55, 0.55),
    ("affettato vaschetta 150g", "pz", 0.150, 1.50, 0.55),
]
CANALI = ["GDO", "HoReCa", "Grossista", "Dettaglio", "Gastronomia"]
DEPOSITI = ["Parma", "Modena", "San Daniele", "Milano", "Napoli"]
AREE = ["Nord-Ovest", "Nord-Est", "Centro", "Sud", "Isole"]

# regione → (sigla provincia, città) per anagrafiche geograficamente coerenti
GEO = [
    ("Lombardia", "MI", "Milano"), ("Lombardia", "BG", "Bergamo"), ("Lombardia", "BS", "Brescia"),
    ("Emilia-Romagna", "PR", "Parma"), ("Emilia-Romagna", "MO", "Modena"), ("Emilia-Romagna", "BO", "Bologna"),
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


def build_prodotti() -> pd.DataFrame:
    """Espande i salumi base per formato → SKU con prezzo di listino e costi."""
    rows = []
    pid = 0
    for nome, cat, tipo, stag, prezzo_kg, mp_frac in BASE_SALUMI:
        # 3–4 formati per prodotto (deterministico: i primi n della lista)
        n_fmt = 3 if tipo in ("fresco",) else 4
        for formato, unita, peso, markup, conf in FORMATI[:n_fmt]:
            pid += 1
            if unita == "kg":
                prezzo_listino = round(prezzo_kg * markup, 4)          # per kg
            else:
                prezzo_listino = round(prezzo_kg * peso * markup, 4)   # per pezzo
            rows.append(dict(
                id=pid,
                codice=f"SKU{pid:04d}",
                nome=f"{nome} — {formato}",
                categoria=cat,
                tipo=tipo,
                formato=formato,
                stagionatura_mesi=stag,
                peso_medio_kg=peso,
                unita_misura=unita,
                _prezzo_kg=prezzo_kg,
                _mp_frac=mp_frac,
                _conf=conf,
                _prezzo_listino=prezzo_listino,
                _peso=peso,
                _unita=unita,
            ))
    return pd.DataFrame(rows)


# ── Postgres helpers ─────────────────────────────────────────────────────────
def copy_df(cur, table: str, df: pd.DataFrame) -> None:
    """COPY di un DataFrame (colonne nell'ordine di `df`) in `table`."""
    buf = io.StringIO()
    df.to_csv(buf, index=False, header=False)
    buf.seek(0)
    cols = ", ".join(df.columns)
    cur.copy_expert(f"COPY {table} ({cols}) FROM STDIN WITH (FORMAT csv)", buf)


def run_sql_file(cur, path: str) -> None:
    with open(path, encoding="utf-8") as f:
        cur.execute(f.read())


# ── generazione ──────────────────────────────────────────────────────────────
def gen_agenti() -> pd.DataFrame:
    """Rete commerciale gerarchica: capi_area → ispettori → agenti."""
    rows = []
    aid = 0
    capi, isp = [], []
    for _ in range(P["capi"]):
        aid += 1
        area = AREE[(aid - 1) % len(AREE)]
        rows.append(_agente_row(aid, "capo_area", 0, area, rng.uniform(0.8, 1.5)))
        capi.append((aid, area))
    for _ in range(P["ispettori"]):
        aid += 1
        capo_id, area = capi[rng.integers(len(capi))]
        rows.append(_agente_row(aid, "ispettore", capo_id, area, rng.uniform(1.5, 2.5)))
        isp.append((aid, area))
    for _ in range(P["agenti"]):
        aid += 1
        isp_id, area = isp[rng.integers(len(isp))]
        rows.append(_agente_row(aid, "agente", isp_id, area, rng.uniform(3.0, 6.0)))
    df = pd.DataFrame(rows)
    return df


def _agente_row(aid, ruolo, resp, area, provv):
    nome, cognome = fake.first_name(), fake.last_name()
    email = f"{nome}.{cognome}@salumificio.example".lower().replace(" ", "")
    return dict(
        id=aid, nome=nome, cognome=cognome, ruolo=ruolo, responsabile_id=resp,
        area=area, provvigione_base_pct=round(provv, 2),
        data_assunzione=fake.date_between(date(2010, 1, 1), date(2024, 12, 31)),
        email=email,
    )


def gen_clienti(agenti: pd.DataFrame) -> pd.DataFrame:
    """Anagrafica clienti, ciascuno assegnato a un AGENTE (non ispettore/capo)."""
    ids_agente = agenti.loc[agenti.ruolo == "agente", "id"].to_numpy()
    C = P["clienti"]
    log(f"  clienti: genero {C:,} anagrafiche con Faker…")
    geo_idx = rng.integers(0, len(GEO), C)
    canale_idx = rng.integers(0, len(CANALI), C)
    agente_ids = ids_agente[rng.integers(0, len(ids_agente), C)]
    acq_days = rng.integers(0, GIORNI, C)
    base = np.datetime64(DATA_INIZIO)
    # ClickHouse Date vuole date reali (non stringhe): datetime64 vettorizzato
    acq = pd.to_datetime(base + acq_days.astype("timedelta64[D]"))

    rows = []
    for i in range(C):
        reg, prov, citta = GEO[geo_idx[i]]
        rows.append((
            i + 1,
            fake.company(),
            fake.numerify("###########"),      # p.iva 11 cifre
            CANALI[canale_idx[i]],
            fake.street_address().replace("\n", " "),
            fake.numerify("#####"),
            citta, prov, reg,
            int(agente_ids[i]),
            int(rng.random() > 0.08),           # ~92% attivi
        ))
    cols = ["id", "ragione_sociale", "partita_iva", "canale", "indirizzo", "cap",
            "citta", "provincia", "regione", "agente_id", "attivo"]
    df = pd.DataFrame(rows, columns=cols)
    df["data_acquisizione"] = acq
    # array di supporto per i fatti (indicizzati per cliente_id 1..C)
    df.attrs["agente_of"] = np.concatenate([[0], df.agente_id.to_numpy()])
    df.attrs["canale_of"] = np.concatenate([[0], canale_idx + 1])  # 1-based nei CANALI
    return df


def gen_catalogo(cur, prod: pd.DataFrame) -> None:
    """Scrive prodotti + storico listino + storico costi di produzione."""
    cols = ["id", "codice", "nome", "categoria", "tipo", "formato",
            "stagionatura_mesi", "peso_medio_kg", "unita_misura"]
    copy_df(cur, "catalogo.prodotti", prod[cols])

    listino, costi = [], []
    lid = cid = 0
    for _, r in prod.iterrows():
        # due periodi di listino/costi: fino a fine 2024 e dal 2025 (rincaro ~6%)
        for start, mult in ((DATA_INIZIO, 1.0), (date(2025, 1, 1), 1.06)):
            lid += 1
            fine = date(2024, 12, 31) if mult == 1.0 else None
            listino.append((lid, int(r.id), start, fine, round(r._prezzo_listino * mult, 4)))
            cid += 1
            mp = r._prezzo_kg * r._mp_frac * mult
            lav = r._prezzo_kg * 0.11 * mult
            costi.append((cid, int(r.id), start, round(mp, 4), round(lav, 4), round(r._conf * mult, 4)))
    copy_df(cur, "catalogo.listino_prezzi",
            pd.DataFrame(listino, columns=["id", "prodotto_id", "valido_dal", "valido_al", "prezzo_listino"]))
    copy_df(cur, "catalogo.costi_produzione",
            pd.DataFrame(costi, columns=["id", "prodotto_id", "valido_dal",
                                         "costo_materia_prima_kg", "costo_lavorazione_kg", "costo_confezionamento_pz"]))


def build_day_weights() -> np.ndarray:
    """Probabilità di ciascun giorno della finestra dati, così ordini e attività
    NON sono distribuiti uniformemente ma seguono un TREND realistico:
      · crescita aziendale composta (GROWTH_ANNUO);
      · stagionalità annuale (Natale/Pasqua ↑, agosto ↓ — vedi MONTH_MULT);
      · stagionalità settimanale B2B (feriali ↑, sabato ↓, domenica quasi zero).
    Un po' di rumore giornaliero evita curve troppo lisce."""
    ndays = GIORNI + 1
    idx = np.arange(ndays)
    dates = pd.to_datetime(np.datetime64(DATA_INIZIO) + idx.astype("timedelta64[D]"))
    anni = idx / 365.25
    crescita = (1.0 + GROWTH_ANNUO) ** anni
    stagionale = MONTH_MULT[dates.month.to_numpy()]
    dow = dates.dayofweek.to_numpy()  # 0=lun … 6=dom
    settimanale = np.where(dow < 5, 1.0, np.where(dow == 5, 0.45, 0.12))
    rumore = rng.uniform(0.85, 1.15, ndays)
    w = crescita * stagionale * settimanale * rumore
    return w / w.sum()


def gen_ordini_e_righe(cur, prod: pd.DataFrame, agente_of, canale_of, agenti: pd.DataFrame) -> None:
    """Ordini + righe + costi commerciali, a blocchi. Testata → cliente (e suo
    agente); righe → prodotti con prezzo≈listino±rumore e sconto per canale."""
    O = P["ordini"]
    C = P["clienti"]
    P_n = len(prod)
    prezzi = prod._prezzo_listino.to_numpy()
    provv_base = np.concatenate([[0.0], agenti.provvigione_base_pct.to_numpy().astype(float)])
    base_d = np.datetime64(DATA_INIZIO)
    p_day = build_day_weights()  # trend di crescita + stagionalità (no uniforme)

    riga_id = 0
    done = 0
    while done < O:
        n = min(CHUNK_ORDINI, O - done)
        oid = np.arange(done + 1, done + n + 1, dtype=np.int64)
        cliente = rng.integers(1, C + 1, n)
        agente = agente_of[cliente]
        canale_i = canale_of[cliente]                      # 1..len(CANALI)
        canale = np.array(CANALI)[canale_i - 1]
        gdo_like = np.isin(canale_i, [1, 3])               # GDO/Grossista: qty↑ sconti↑
        giorni = rng.choice(GIORNI + 1, size=n, p=p_day)   # data pesata dal trend
        data_ord = (base_d + giorni.astype("timedelta64[D]")).astype(str)
        stato = np.where(rng.random(n) < 0.90, "evaso",
                         np.where(rng.random(n) < 0.6, "in_lavorazione", "annullato"))
        deposito = np.array(DEPOSITI)[rng.integers(0, len(DEPOSITI), n)]
        numero = np.char.add("ORD-", oid.astype(str))

        copy_df(cur, "vendite.ordini", pd.DataFrame({
            "id": oid, "numero": numero, "data_ordine": data_ord, "cliente_id": cliente,
            "agente_id": agente, "canale": canale, "stato": stato, "deposito": deposito,
        }))

        # righe: numero per ordine ~ Poisson+1
        n_righe = rng.poisson(2.5, n) + 1
        tot = int(n_righe.sum())
        r_oid = np.repeat(oid, n_righe)
        r_canale_gdo = np.repeat(gdo_like, n_righe)
        r_agente = np.repeat(agente, n_righe)
        r_giorni = np.repeat(giorni, n_righe)
        prod_i = rng.integers(0, P_n, tot)
        prezzo_list = prezzi[prod_i]
        # quantità: gamma (asimmetrica), più alta per GDO/Grossista, e in lenta
        # crescita nel tempo (il carrello medio si allarga anno su anno)
        basket = (1.0 + BASKET_GROWTH_ANNUO) ** (r_giorni / 365.25)
        qta = (rng.gamma(2.0, np.where(r_canale_gdo, 9.0, 4.0)) + 0.5) * basket
        qta = np.round(qta, 3)
        prezzo_un = np.round(prezzo_list * (1 + rng.normal(0, 0.04, tot)), 4)
        prezzo_un = np.maximum(prezzo_un, 0.5)
        sconto = np.round(np.clip(rng.normal(np.where(r_canale_gdo, 12, 5), 3), 0, 40), 2)
        rid = np.arange(riga_id + 1, riga_id + tot + 1, dtype=np.int64)

        copy_df(cur, "vendite.righe_ordine", pd.DataFrame({
            "id": rid, "ordine_id": r_oid, "prodotto_id": prod_i + 1,
            "quantita": qta, "prezzo_unitario": prezzo_un, "sconto_pct": sconto,
        }))

        # costi commerciali per riga: provvigione dell'agente + logistica + promo
        ricavo = qta * prezzo_un * (1 - sconto / 100.0)
        pct = provv_base[r_agente]
        provv_imp = np.round(ricavo * pct / 100.0, 4)
        logistica = np.round(qta * 0.12 + 1.5, 4)
        promo = np.round(np.where(rng.random(tot) < 0.15, ricavo * rng.uniform(0.02, 0.08, tot), 0.0), 4)
        copy_df(cur, "vendite.costi_commerciali", pd.DataFrame({
            "id": rid, "riga_ordine_id": rid, "provvigione_pct": np.round(pct, 2),
            "provvigione_importo": provv_imp, "costo_logistica": logistica, "costo_promozionale": promo,
        }))

        riga_id += tot
        done += n
        log(f"    ordini {done:,}/{O:,} · righe totali {riga_id:,}")


def gen_attivita(ch, agente_of) -> None:
    """Attività CRM (la tabella grande di ClickHouse), a blocchi."""
    A = P["attivita"]
    C = P["clienti"]
    TIPI = np.array(["visita", "telefonata", "email", "sollecito", "reclamo"])
    ESITI = np.array(["positivo", "neutro", "negativo", "ordine"])
    base = np.datetime64(f"{DATA_INIZIO}T00:00:00")
    p_day = build_day_weights()  # stesso trend/stagionalità degli ordini
    done = 0
    while done < A:
        n = min(CHUNK_ATTIVITA, A - done)
        cliente = rng.integers(1, C + 1, n)
        agente = agente_of[cliente]
        # giorno pesato dal trend + orario concentrato nella giornata lavorativa
        giorno = rng.choice(GIORNI + 1, size=n, p=p_day)
        sec_giorno = np.clip(rng.normal(13 * 3600, 2.5 * 3600, n), 7 * 3600, 20 * 3600)
        offset = (giorno * 86400 + sec_giorno).astype(np.int64).astype("timedelta64[s]")
        data = base + offset
        df = pd.DataFrame({
            "id": np.arange(done + 1, done + n + 1, dtype=np.int64),
            "cliente_id": cliente.astype(np.uint32),
            "agente_id": agente.astype(np.uint32),
            "data": pd.to_datetime(data),
            "tipo": TIPI[rng.integers(0, len(TIPI), n)],
            "esito": ESITI[rng.integers(0, len(ESITI), n)],
            "durata_min": rng.integers(2, 90, n).astype(np.uint16),
            "note": "",
        })
        ch.insert_df("analytics.attivita_crm", df)
        done += n
        log(f"    attività CRM {done:,}/{A:,}")


# ── main ─────────────────────────────────────────────────────────────────────
def main() -> None:
    t0 = time.time()
    log(f"Scala «{SCALE}» — preset: {P}")
    import clickhouse_connect

    log("Connessione a Postgres e ClickHouse…")
    pg = psycopg2.connect(**PG)
    pg.autocommit = False
    ch = clickhouse_connect.get_client(**CH)

    # schema
    log("Creo lo schema Postgres…")
    with pg.cursor() as cur:
        run_sql_file(cur, os.path.join(HERE, "schema_postgres.sql"))
    pg.commit()
    log("Creo lo schema ClickHouse…")
    for stmt in open(os.path.join(HERE, "schema_clickhouse.sql"), encoding="utf-8").read().split(";"):
        if stmt.strip():
            ch.command(stmt)

    # dimensioni
    log("Genero la rete commerciale (agenti)…")
    agenti = gen_agenti()
    ch.insert_df("analytics.agenti", agenti)
    log(f"  {len(agenti):,} agenti ({P['capi']} capi area, {P['ispettori']} ispettori, {P['agenti']} agenti)")

    clienti = gen_clienti(agenti)
    ch.insert_df("analytics.clienti", clienti[[
        "id", "ragione_sociale", "partita_iva", "canale", "indirizzo", "cap",
        "citta", "provincia", "regione", "agente_id", "data_acquisizione", "attivo"]])
    log(f"  {len(clienti):,} clienti inseriti")
    agente_of = clienti.attrs["agente_of"]
    canale_of = clienti.attrs["canale_of"]

    log("Genero il catalogo prodotti + listino + costi di produzione…")
    prod = build_prodotti()
    with pg.cursor() as cur:
        gen_catalogo(cur, prod)
    pg.commit()
    log(f"  {len(prod)} SKU")

    # fatti
    log("Genero ordini, righe e costi commerciali (Postgres)…")
    with pg.cursor() as cur:
        gen_ordini_e_righe(cur, prod, agente_of, canale_of, agenti)
    pg.commit()

    log("Costruisco gli indici secondari Postgres…")
    with pg.cursor() as cur:
        run_sql_file(cur, os.path.join(HERE, "indexes_postgres.sql"))
    pg.commit()

    log("Genero le attività CRM (ClickHouse)…")
    gen_attivita(ch, agente_of)

    pg.close()
    log(f"FATTO in {int(time.time() - t0)}s. Scala «{SCALE}».")
    log("Connessioni Tabularia → Postgres sampledb-postgres:5432 / ClickHouse sampledb-clickhouse:8123")


if __name__ == "__main__":
    main()
