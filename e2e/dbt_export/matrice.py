"""Matrice dell'export dbt: OGNI operazione dell'editor (con le sue varianti) su dati
difficili (NULL, testo con spazi, decimali, date, booleani, nomi di colonna con spazi,
accenti, parole riservate). Un flusso con un ramo per caso, eseguito in Tabularia
(produzione, motore ClickHouse = il riferimento: è quello dei clienti), esportato nei due target, fatto girare
con dbt VERO; ogni modello confrontato col suo risultato in Tabularia: COLONNE IN ORDINE
e righe (come multiinsieme, oppure in ordine dove l'ordine è il punto).
Le tabelle dei casi (schema.sql) stanno nel Postgres d'esempio; esegui.py le crea.
uso: python3 matrice.py [--motore clickhouse|duckdb|polars] [--solo duckdb|native|clickhouse] [--caso PREFISSO] [--tieni]"""
import csv, io, json, math, os, re, subprocess, sys, time, urllib.error, urllib.request, zipfile

from ambiente import CH_CONTENITORE, CH_PORTA, DOVE, IMMAGINE, QUI, RETE
from comune import (api, database_clickhouse, entra, esiti, esporta, ok, password_delle_connessioni, postgres_esempio,
                    pulisci_clickhouse)

CH_HOST, CH_PORT = CH_CONTENITORE, CH_PORTA
PG_CONN = {}   # la connessione Postgres d'esempio in Tabularia: host, database, utente (per leggere le tabelle di dbt)




# ── i casi ───────────────────────────────────────────────────────────────────
F = lambda col, op, val=None: {"type": "filter", "params": {"column": col, "operator": op, "value": val}}
J = lambda how, **kw: {"type": "join", "params": {"how": how, **kw}}
CASI = [
    # colonne
    ("c01 select", [{"type": "select", "params": {"columns": ["città", "select", "Nome Strano", "id"]}}]),
    ("c02 reorder", [{"type": "reorder", "params": {"columns": ["città", "id"]}}]),
    ("c03 drop", [{"type": "drop", "params": {"columns": ["testo", "quando", "select"]}}]),
    ("c04 rename", [{"type": "rename", "params": {"mapping": {"Nome Strano": "nome_strano", "select": "sel", "città": "Città"}}}]),
    # cast
    ("c05 cast testo int", [{"type": "cast", "params": {"columns": {"testo": "int"}}}]),
    ("c06 cast testo float", [{"type": "cast", "params": {"columns": {"testo": "float"}}}]),
    ("c07 cast numero int", [{"type": "cast", "params": {"columns": {"numero": "int"}}}]),
    ("c08 cast importo int", [{"type": "cast", "params": {"columns": {"importo": "int"}}}]),
    ("c09 cast intero str", [{"type": "cast", "params": {"columns": {"intero": "str", "flag": "str"}}}]),
    ("c10 cast quando date", [{"type": "cast", "params": {"columns": {"quando": "date"}}}]),
    ("c11 cast data datetime", [{"type": "cast", "params": {"columns": {"data": "datetime"}}}]),
    ("c12 cast intero float", [{"type": "cast", "params": {"columns": {"intero": "float", "importo": "float"}}}]),
    # filtri
    ("c13 filtro eq", [F("categoria", "eq", "A")]),
    ("c14 filtro ne", [F("categoria", "ne", "A")]),
    ("c15 filtro gt", [F("numero", "gt", 0)]),
    ("c16 filtro ge", [F("intero", "ge", 3)]),
    ("c17 filtro lt data", [F("data", "lt", "2024-03-01")]),
    ("c18 filtro le importo", [F("importo", "le", 3.33)]),
    ("c19 filtro in", [F("categoria", "in", ["A", "C"])]),
    ("c20 filtro not in", [F("categoria", "not_in", ["A", "C"])]),
    ("c21 filtro between", [F("intero", "between", [2, 10])]),
    ("c22 filtro contains", [F("città", "contains", "il")]),
    ("c23 filtro starts with", [F("città", "starts_with", "M")]),
    ("c24 filtro ends with", [F("città", "ends_with", "a")]),
    ("c25 filtro is null", [F("categoria", "is_null")]),
    ("c26 filtro not null", [F("numero", "is_not_null")]),
    ("c27 filtro eq bool", [F("flag", "eq", True)]),
    # righe
    ("c28 sort asc", [{"type": "sort", "params": {"by": "numero"}}], {"ordine": ["numero"]}),
    ("c29 sort desc", [{"type": "sort", "params": {"by": "numero", "descending": True}}], {"ordine": ["numero"]}),
    ("c30 sort multi", [{"type": "sort", "params": {"by": ["categoria", "intero"], "descending": True}}], {"ordine": ["categoria", "intero"]}),
    ("c31 sort limit", [{"type": "sort", "params": {"by": "id"}}, {"type": "limit", "params": {"n": 3}}], {"ordine": ["id"]}),
    ("c32 unique tutto", [{"type": "select", "params": {"columns": ["categoria", "città"]}}, {"type": "unique", "params": {}}]),
    ("c33 unique subset", [{"type": "unique", "params": {"subset": ["categoria"]}}], {"confronta": ["categoria"]}),
    ("c34 fill null", [{"type": "fill_null", "params": {"columns": {"categoria": "Z", "intero": 0, "numero": 0.5}}}]),
    ("c35 drop nulls subset", [{"type": "drop_nulls", "params": {"subset": ["categoria"]}}]),
    ("c36 drop nulls tutto", [{"type": "drop_nulls", "params": {}}]),
    # aggregazioni
    ("c37 group by", [{"type": "group_by", "params": {"by": ["categoria"], "aggregations": [
        {"column": "intero", "func": "sum", "alias": "somma"}, {"column": "numero", "func": "mean", "alias": "media"},
        {"column": "data", "func": "min", "alias": "prima"}, {"column": "quando", "func": "max", "alias": "ultima"},
        {"column": "intero", "func": "count", "alias": "conta"}, {"column": "numero", "func": "median", "alias": "mediana"},
        {"column": "città", "func": "n_unique", "alias": "citta_diverse"}]}}]),
    ("c38 group by std var", [{"type": "group_by", "params": {"by": ["flag"], "aggregations": [
        {"column": "numero", "func": "std", "alias": "dev"}, {"column": "numero", "func": "var", "alias": "varianza"}, {"column": "importo", "func": "sum", "alias": "tot"}]}}]),
    # compute
    ("c39 compute nuova", [{"type": "compute", "params": {"columns": [{"name": "doppio", "expr": "numero * 2"}]}}]),
    ("c40 compute sostituisce", [{"type": "compute", "params": {"columns": [{"name": "intero", "expr": "intero * 10"}]}}]),
    ("c41 compute case coalesce", [{"type": "compute", "params": {"columns": [
        {"name": "segno", "expr": "CASE WHEN numero > 0 THEN 'pos' WHEN numero < 0 THEN 'neg' ELSE 'zero' END"},
        {"name": "cat", "expr": "COALESCE(categoria, 'nessuna')"}]}}]),
    ("c42 compute round", [{"type": "compute", "params": {"columns": [{"name": "terzo", "expr": "ROUND(numero / 3, 2)"}]}}]),
    ("c43 compute anno", [{"type": "compute", "params": {"columns": [{"name": "anno", "expr": "EXTRACT(YEAR FROM data)"}]}}]),
    ("c44 compute testo", [{"type": "compute", "params": {"columns": [{"name": "etichetta", "expr": "UPPER(categoria) || '-' || \"città\""}]}}]),
    # join / union
    ("c45 join inner", [J("inner", on="categoria")], {"destra": []}),
    ("c46 join left", [J("left", on="categoria")], {"destra": []}),
    ("c47 join right", [J("right", on="categoria")], {"destra": []}),
    ("c48 join full", [J("full", on="categoria")], {"destra": []}),
    ("c49 join semi", [J("semi", on="categoria")], {"destra": []}),
    ("c50 join anti", [J("anti", on="categoria")], {"destra": []}),
    ("c51 join left on", [J("left", left_on="id", right_on="id")], {"destra": []}),
    ("c52 join full on", [J("full", left_on="id", right_on="id")], {"destra": []}),
    ("c53 join cross", [{"type": "select", "params": {"columns": ["id", "categoria"]}}, J("cross")], {"destra": [{"type": "select", "params": {"columns": ["extra"]}}]}),
    ("c54 union by name", [{"type": "select", "params": {"columns": ["id", "categoria", "numero"]}}, {"type": "union", "params": {}}], {"destra": []}),
    ("c55 union strict", [{"type": "select", "params": {"columns": ["id", "categoria"]}}, {"type": "union", "params": {"strategy": "strict"}}], {"destra": [{"type": "select", "params": {"columns": ["id", "categoria"]}}]}),
    # forma
    ("c56 unpivot", [{"type": "unpivot", "params": {"index": ["id"], "on": ["numero", "importo"], "variable_name": "misura", "value_name": "valore"}}]),
    # sql
    ("c57 sql", [{"type": "sql", "params": {"query": "SELECT categoria, COUNT(*) AS n FROM self GROUP BY categoria"}}]),
    ("c58 sql con with", [{"type": "sql", "params": {"query": "WITH x AS (SELECT * FROM input WHERE numero > 0) SELECT id, numero FROM x"}}]),
    # nomi con maiuscole che nascono a metà flusso e vanno usati dopo (sqlglot li perdeva)
    ("c59 colonna maiuscola", [{"type": "compute", "params": {"columns": [{"name": "Margine", "expr": "numero * 2"}]}}, F("Margine", "gt", 0),
                               {"type": "sort", "params": {"by": "Margine"}}], {"ordine": ["Margine"]}),
    ("c60 alias maiuscolo", [{"type": "group_by", "params": {"by": ["categoria"], "aggregations": [{"column": "numero", "func": "sum", "alias": "Totale"}]}},
                             {"type": "fill_null", "params": {"columns": {"Totale": 0}}}, {"type": "sort", "params": {"by": "Totale", "descending": True}}], {"ordine": ["Totale"]}),
    # una colonna NATA a destra (non dalla sorgente): su ClickHouse non è Nullable, e senza
    # join_use_nulls = 1 il lato mancante di un join esterno sarebbe '' invece di NULL
    ("c61 join left colonna calcolata a destra", [J("left", on="categoria")],
     {"destra": [{"type": "compute", "params": {"columns": [{"name": "etichetta", "expr": "'fissa'"}]}}]}),
]
SOLO_FEDERATO = [
    ("p01 pivot", [{"type": "pivot", "params": {"index": ["flag"], "on": ["categoria"], "values": "intero", "func": "sum"}}]),
    ("p02 pivot due colonne", [{"type": "pivot", "params": {"index": ["id"], "on": ["categoria", "flag"], "values": "numero", "func": "max"}}]),
]

# ── confronto ────────────────────────────────────────────────────────────────
def norm(v):
    if v is None or v == "":
        return None
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        x = float(v)
    else:
        s = str(v)
        if s.lower() in ("true", "false"):
            return s.lower()
        x = None
        if s == s.strip():
            try:
                x = float(s)
            except ValueError:
                x = None
        if x is None:
            s = re.sub(r"^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}:\d{2})(\.0+)?([+-]00:?00|Z)?$", r"\1 \2", s)
            return s
    if math.isnan(x):
        return "nan"
    return int(x) if x == int(x) and abs(x) < 1e15 else round(x, 6)


def righe(cols, rows):
    return [tuple(norm(v) for v in r) for r in rows]


def confronta(nome, opz, tab, dbtres):
    cols_t, rows_t = tab
    cols_d, rows_d = dbtres["cols"], dbtres["rows"]
    if cols_t != cols_d:
        return False, f"colonne diverse:\n      tabularia {cols_t}\n      dbt       {cols_d}"
    rt, rd = righe(cols_t, rows_t), righe(cols_d, rows_d)
    if "confronta" in opz:
        idx = [cols_t.index(c) for c in opz["confronta"]]
        rt, rd = [tuple(r[i] for i in idx) for r in rt], [tuple(r[i] for i in idx) for r in rd]
    chiave = lambda r: tuple((x is None, str(x)) for x in r)
    if sorted(rt, key=chiave) != sorted(rd, key=chiave):
        solo_t = [r for r in rt if r not in rd][:3]; solo_d = [r for r in rd if r not in rt][:3]
        return False, f"righe diverse ({len(rt)} vs {len(rd)}):\n      solo tabularia {solo_t}\n      solo dbt       {solo_d}"
    if "ordine" in opz:
        idx = [cols_t.index(c) for c in opz["ordine"]]
        kt, kd = [tuple(r[i] for i in idx) for r in rt], [tuple(r[i] for i in idx) for r in rd]
        if kt != kd:
            return False, f"ordine diverso:\n      tabularia {kt}\n      dbt       {kd}"
    return True, ""


# ── preparazione in Tabularia ────────────────────────────────────────────────
def slug(n):
    return re.sub(r"[^a-z0-9_]", "_", n.lower()).strip("_")


def costruisci(nome_flusso, casi, cartella, ds_casi, ds_destra):
    nodi = [{"id": "s_casi", "type": "source", "data": {"datasourceId": ds_casi}},
            {"id": "s_destra", "type": "source", "data": {"datasourceId": ds_destra}}]
    archi = []
    for i, caso in enumerate(casi):
        nome, ops, opz = caso[0], caso[1], (caso[2] if len(caso) > 2 else {})
        prec = "s_casi"
        for k, op in enumerate(ops):
            nid = f"n{i}_{k}"
            nodi.append({"id": nid, "type": "operation", "data": {"opType": op["type"], "params": op["params"]}})
            archi.append({"source": prec, "target": nid, "targetHandle": "left"})
            if op["type"] in ("join", "union"):
                dprec = "s_destra"
                for h, dop in enumerate(opz.get("destra", [])):
                    did = f"d{i}_{h}"
                    nodi.append({"id": did, "type": "operation", "data": {"opType": dop["type"], "params": dop["params"]}})
                    archi.append({"source": dprec, "target": did, "targetHandle": "left"})
                    dprec = did
                archi.append({"source": dprec, "target": nid, "targetHandle": "right"})
            prec = nid
        nodi.append({"id": f"o{i}", "type": "output", "data": {"destType": "datasource", "name": nome, "projectId": cartella, "overwrite": True}})
        archi.append({"source": prec, "target": f"o{i}", "targetHandle": "left"})
    st, f = api("POST", f"/projects/{cartella}/flows", {"name": nome_flusso, "definition": json.dumps({"nodes": nodi, "edges": archi}), "engine": MOTORE})
    assert st in (200, 201), (st, f)
    return f


def aspetta_run(rid, secondi=900):
    fine = time.time() + secondi
    while time.time() < fine:
        st, r = api("GET", f"/runs/{rid}")
        if st == 200 and r["status"] in ("SUCCESS", "FAILURE"):
            return r
        time.sleep(3)
    return r


def risultato_tabularia(cartella, nome):
    d = next((x for x in api("GET", "/datasources")[1] if x["name"] == nome and x["project_id"] == cartella), None)
    if d is None:
        return None
    st, b = api("POST", "/tasks/export", {"bucket": d["bucket"], "input_key": d["key"], "operations": [], "format": "csv", "filename": "x.csv"}, grezzo=True)
    r = list(csv.reader(io.StringIO(b.decode())))
    return r[0], r[1:]


def dbt(cartella, *arg, variabili=None):
    cmd = ["docker", "run", "--rm", "--network", RETE, "--user", f"{os.getuid()}:{os.getgid()}", "-e", "HOME=/tmp",
           "-v", f"{cartella}:/progetto", "-v", f"{QUI}/dump.py:/dump.py:ro", "-w", "/progetto", "-e", "DBT_PROFILES_DIR=/progetto"]
    for k, v in (variabili or {}).items():
        cmd += ["-e", f"{k}={v}"]
    r = subprocess.run(cmd + [IMMAGINE, *arg], capture_output=True, text=True)
    return r.returncode, r.stdout + r.stderr


def leggi_dbt(cartella, target, variabili):
    if target == "duckdb":
        f = next(n for n in os.listdir(cartella) if n.endswith(".duckdb"))
        rc, out = dbt(cartella, "python", "/dump.py", "duckdb", f"/progetto/{f}", variabili=variabili)
    elif target == "clickhouse":
        rc, out = dbt(cartella, "python", "/dump.py", "clickhouse", CH_HOST, CH_PORT, "dbt_tabularia", variabili=variabili)
    else:
        pw = variabili[f"TABULARIA_DB_{PG_ID}_PASSWORD"]
        rc, out = dbt(cartella, "python", "/dump.py", "postgres", PG_CONN["host"], PG_CONN["database"], PG_CONN["username"], "dbt_tabularia",
                      variabili={**variabili, "PGPASSWORD": pw})
    if "===DUMP===" not in out:
        raise RuntimeError(out[-1500:])
    return json.loads(out.split("===DUMP===", 1)[1])


MOTORE = sys.argv[sys.argv.index("--motore") + 1] if "--motore" in sys.argv else "clickhouse"   # il motore dei clienti è il riferimento




def prepara_clickhouse(cartella_dbt, variabili):
    """Il passo che il team data fa una volta: i database federati verso le sorgenti."""
    return dbt(cartella_dbt, "dbt", "run-operation", "create_tabularia_sources", "--log-level-file", "none", "--no-use-colors", variabili=variabili)
if __name__ == "__main__":
    solo = sys.argv[sys.argv.index("--solo") + 1] if "--solo" in sys.argv else None
    filtro = sys.argv[sys.argv.index("--caso") + 1] if "--caso" in sys.argv else None
    entra()
    pg = next(c for c in api("GET", "/connections")[1] if c["db_type"] == "postgresql")
    globals()["PG_ID"] = pg["id"]
    PG_CONN.update(host=pg["host"], database=pg["database"], username=pg["username"])
    st, cart = api("POST", "/projects", {"name": "zzdbt matrice"})
    cartella = cart["id"]
    creati_ds = []
    try:
        ids = {}
        for nome, ref in (("zzdbt casi", "zzdbt.casi"), ("zzdbt destra", "zzdbt.destra")):
            st, d = api("POST", f"/projects/{cartella}/datasources/database", {"name": nome, "connection_id": pg["id"], "source_type": "table", "source_ref": ref})
            assert st in (200, 201), (st, d)
            ids[nome] = d["id"]; creati_ds.append(d["id"])
        for _ in range(90):
            pronte = [x for x in api("GET", "/datasources")[1] if x["id"] in creati_ds and x.get("rows") and not x.get("refreshing")]
            if len(pronte) == 2:
                break
            time.sleep(2)
        casi = [c for c in CASI if not filtro or c[0].startswith(filtro)]
        piv = [c for c in SOLO_FEDERATO if not filtro or c[0].startswith(filtro)]
        flussi = []
        if casi:
            flussi.append(("zzdbt matrice", casi, ("duckdb", "native", "clickhouse")))
        if piv:
            flussi.append(("zzdbt pivot", piv, ("duckdb",)))
        variabili = password_delle_connessioni()
        for nome_f, cs, target in flussi:
            print(f"\n== flusso «{nome_f}»: {len(cs)} casi", flush=True)
            f = costruisci(nome_f, cs, cartella, ids["zzdbt casi"], ids["zzdbt destra"])
            run = aspetta_run(api("POST", f"/flows/{f['id']}/run-now?mode=production")[1]["run_id"])
            if run["status"] != "SUCCESS":
                print("  (il run di Tabularia ha errori:", (run.get("error") or "")[:400], ")")
            tab = {c[0]: risultato_tabularia(cartella, c[0]) for c in cs}
            for t in target:
                if solo and t != solo:
                    continue
                print(f"-- target {t}")
                postgres_esempio("DROP SCHEMA IF EXISTS dbt_tabularia CASCADE")
                cartella_dbt, nomi = esporta(f, t, suffisso="_matrice")
                if not ok(f"{nome_f} · {t}: export", cartella_dbt is not None, nomi):
                    continue
                if t == "clickhouse":
                    pulisci_clickhouse()
                    rc, out = prepara_clickhouse(cartella_dbt, variabili)
                    if not ok(f"{nome_f} · {t}: dbt run-operation create_tabularia_sources", rc == 0, out[-1200:]):
                        continue
                rc, out = dbt(cartella_dbt, "dbt", "run", "--no-use-colors", variabili=variabili)
                # dbt-clickhouse scrive i nomi fra backtick: `db`.`modello`
                falliti = {re.sub(r"^.*\.", "", m).strip("`") for m in re.findall(r"ERROR creating sql table model (\S+)", out)}
                # un ramo che Tabularia stessa rifiuta DEVE fallire anche in dbt: è parità, non un difetto
                attesi = {slug(c[0]) for c in cs if tab[c[0]] is None}
                ok(f"{nome_f} · {t}: dbt run (falliscono solo i modelli che Tabularia rifiuta)", falliti == attesi,
                   f"falliti in dbt {sorted(falliti)}, rifiutati da Tabularia {sorted(attesi)}\n" + "\n".join(l for l in out.splitlines() if "Error" in l)[:1200])
                try:
                    prodotte = leggi_dbt(cartella_dbt, t, variabili)
                except Exception as e:
                    ok(f"{nome_f} · {t}: lettura delle tabelle dbt", False, e); continue
                for c in cs:
                    nome, opz = c[0], (c[2] if len(c) > 2 else {})
                    # un modello fallito nel dbt run è assente, anche se dbt-clickhouse ne ha lasciato la tabella vuota
                    m = prodotte.get(slug(nome)) if slug(nome) not in falliti else None
                    if tab[nome] is None:
                        ok(f"{t} · {nome}: rifiutato da Tabularia E da dbt (parità)", m is None, "Tabularia lo rifiuta ma dbt lo calcola"); continue
                    if m is None:
                        ok(f"{t} · {nome}", False, "modello dbt assente o fallito"); continue
                    uguale, perche = confronta(nome, opz, tab[nome], m)
                    ok(f"{t} · {nome}", uguale, perche)
    finally:
        if "--tieni" not in sys.argv:
            for fl in api("GET", "/flows")[1]:
                if fl["name"].startswith("zzdbt"):
                    api("DELETE", f"/flows/{fl['id']}")
            for d in api("GET", "/datasources")[1]:
                if d["project_id"] == cartella:
                    api("DELETE", f"/datasources/{d['id']}")
            api("DELETE", f"/projects/{cartella}")
            postgres_esempio("DROP SCHEMA IF EXISTS dbt_tabularia CASCADE")
            pulisci_clickhouse()
    print(f"\n{sum(esiti)}/{len(esiti)} verifiche passate")
    sys.exit(0 if all(esiti) else 1)
