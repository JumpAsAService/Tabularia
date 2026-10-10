"""Il resto dell'export dbt, oltre alla matrice delle operazioni (stack di scarto):
  A. CONTRATTO violato: per ogni regola, l'esito del controllo di Tabularia e quello
     del test dbt devono coincidere (passa / avviso / errore), nei due target;
  B. CICLO DI VITA: parse, compile, docs generate; una tabella in APPEND è un modello
     incremental — due run la raddoppiano (come due run di Tabularia), --full-refresh riparte;
  C. CLICKHOUSE nativo: un flusso sul CRM d'esempio, confrontato con Tabularia.
Il target clickhouse in A e B (ClickHouse del motore che legge il Postgres d'esempio dal vivo).
uso: python3 ciclo.py"""
import json, os, re, subprocess, sys, time

from ambiente import CH_ESEMPIO_CONTENITORE
from comune import api, entra, esiti, esporta, ok, password_delle_connessioni, postgres_esempio, righe_della_datasource
from matrice import aspetta_run, confronta, dbt, leggi_dbt, norm

entra()
conn = {c["db_type"]: c for c in api("GET", "/connections")[1]}
pg, ch = conn["postgresql"], conn["clickhouse"]
import matrice
matrice.PG_ID = pg["id"]
matrice.PG_CONN.update(host=pg["host"], database=pg["database"], username=pg["username"])
variabili = password_delle_connessioni()
st, cart = api("POST", "/projects", {"name": "zzdbt ciclo"}); C = cart["id"]


def psql(sql):
    return postgres_esempio(sql)


def flusso(nome, nodi, archi):
    st, f = api("POST", f"/projects/{C}/flows", {"name": nome, "definition": json.dumps({"nodes": nodi, "edges": archi}), "engine": matrice.MOTORE})
    assert st in (200, 201), (st, f)
    return f


def lancia(f):
    return aspetta_run(api("POST", f"/flows/{f['id']}/run-now?mode=production")[1]["run_id"])


def ds(nome):
    for _ in range(60):
        d = next((x for x in api("GET", "/datasources")[1] if x["name"] == nome and x["project_id"] == C), None)
        if d and d.get("rows") is not None and not d.get("refreshing"):
            return d
        time.sleep(2)
    return None


E = lambda a, b, h="left": {"source": a, "target": b, "targetHandle": h}
try:
    st, casi = api("POST", f"/projects/{C}/datasources/database", {"name": "zzciclo casi", "connection_id": pg["id"], "source_type": "table", "source_ref": "zzdbt.casi"})
    casi = ds("zzciclo casi")
    ok("sorgente importata", casi is not None)

    # ── A. contratto violato ─────────────────────────────────────────────────
    print("A. contratto violato: Tabularia e dbt devono dire le stesse cose", flush=True)
    fa = flusso("zzciclo contratto", [
        {"id": "s", "type": "source", "data": {"datasourceId": casi["id"]}},
        {"id": "p", "type": "operation", "data": {"opType": "select", "params": {"columns": ["id", "categoria", "numero", "testo"]}}},
        {"id": "o", "type": "output", "data": {"destType": "datasource", "name": "zzciclo dati", "projectId": C, "overwrite": True}}],
        [E("s", "p"), E("p", "o")])
    ok("flusso del contratto eseguito", lancia(fa)["status"] == "SUCCESS")
    dati = ds("zzciclo dati")
    regole = [
        {"id": "r1", "kind": "not_null", "column": "id", "severity": "error"},                 # passa
        {"id": "r2", "kind": "not_null", "column": "categoria", "severity": "error"},          # NULL presenti
        {"id": "r3", "kind": "unique", "columns": ["categoria"], "severity": "warning"},        # doppioni
        {"id": "r4", "kind": "accepted_values", "column": "categoria", "values": ["A", "B"], "severity": "error"},  # c'è C
        {"id": "r5", "kind": "range", "column": "numero", "min": 0, "severity": "warning"},    # negativi
        {"id": "r6", "kind": "range", "column": "id", "min": 1, "max": 100, "severity": "error"},  # passa
        {"id": "r7", "kind": "pattern", "column": "testo", "regex": "^[0-9]+$", "severity": "error"},  # ' 5 ' ecc.
        {"id": "r8", "kind": "row_count", "max": 3, "severity": "warning"},                     # 8 righe
        {"id": "r9", "kind": "expression", "sql": "numero < 1000", "severity": "error"},        # 1e10
        {"id": "r10", "kind": "unique", "columns": ["id"], "severity": "error"},               # passa
    ]
    st, c = api("PUT", f"/datasources/{dati['id']}/contract", {"document": {"rules": regole}, "enabled": True})
    ok("contratto salvato", st == 200, (st, c))
    st, rep = api("POST", f"/datasources/{dati['id']}/contract/check")
    esiti_tab = {}
    for r in (rep.get("report") or {}).get("rules", []):
        esiti_tab[r.get("id")] = "pass" if r.get("passed") else ("warn" if r.get("severity") == "warning" else "fail")
    ok("Tabularia ha valutato tutte le regole", len(esiti_tab) == len(regole), rep if not esiti_tab else esiti_tab)
    for target in ("duckdb", "native", "clickhouse"):
        cartella, nomi = esporta(fa, target, suffisso="_ciclo")
        if not ok(f"A · {target}: export", cartella is not None, nomi):
            continue
        psql("DROP SCHEMA IF EXISTS dbt_tabularia CASCADE")
        if target == "clickhouse":
            matrice.pulisci_clickhouse()
            rc, out = matrice.prepara_clickhouse(cartella, variabili)
            ok(f"A · {target}: dbt run-operation create_tabularia_sources", rc == 0, out[-800:])
        rc, out = dbt(cartella, "dbt", "run", "--no-use-colors", variabili=variabili)
        ok(f"A · {target}: dbt run", rc == 0, out[-800:])
        # il contratto porta accepted_values: la forma con `arguments:` non deve far avvisare dbt 1.12
        ok(f"A · {target}: accepted_values nella forma con arguments", "arguments:" in open(f"{cartella}/models/schema.yml").read())
        avvisi = [l for l in out.splitlines() if re.search(r"(?i)deprecat|\[WARNING\]", l)]
        ok(f"A · {target}: dbt run senza avvisi di deprecazione", not avvisi, "\n".join(avvisi)[:600])
        rc, out = dbt(cartella, "dbt", "test", "--no-use-colors", variabili=variabili)
        risultati = json.load(open(f"{cartella}/target/run_results.json"))["results"]
        esiti_dbt = {}
        for r in risultati:
            uid, stato = r["unique_id"], r["status"]
            m = re.search(r"__(?:range|pattern|row_count|expression|unique_combination)_(r\d+)", uid)
            if m:
                esiti_dbt[m.group(1)] = stato
                continue
            for regola in regole:   # i test generici: per tipo e colonna
                col = regola.get("column") or (regola.get("columns") or [None])[0]
                if regola["kind"] in ("not_null", "unique", "accepted_values") and re.search(rf"\.{regola['kind']}_zzciclo_dati_{col}(\.|__)", uid):
                    esiti_dbt[regola["id"]] = stato
        for regola in regole:
            ok(f"A · {target} · {regola['id']} {regola['kind']} ({regola['severity']}): Tabularia {esiti_tab.get(regola['id'])}, dbt {esiti_dbt.get(regola['id'])}",
               esiti_tab.get(regola["id"]) == esiti_dbt.get(regola["id"]))

    # ── B. ciclo di vita, tabella in append ──────────────────────────────────
    print("B. ciclo di vita: parse, compile, docs, append incrementale, full-refresh", flush=True)
    psql("DROP TABLE IF EXISTS zzdbt.ciclo_append")
    fb = flusso("zzciclo append", [
        {"id": "s", "type": "source", "data": {"datasourceId": casi["id"]}},
        {"id": "p", "type": "operation", "data": {"opType": "select", "params": {"columns": ["id", "categoria"]}}},
        {"id": "o", "type": "output", "data": {"destType": "database", "connectionId": pg["id"], "table": "zzdbt.ciclo_append", "mode": "append"}}],
        [E("s", "p"), E("p", "o")])
    for _ in range(2):
        lancia(fb)
    tab_righe = int(psql("SELECT count(*) FROM zzdbt.ciclo_append").stdout.strip() or 0)
    ok(f"Tabularia: due run in append = {tab_righe} righe", tab_righe == 16, tab_righe)
    for target in ("duckdb", "native", "clickhouse"):
        cartella, nomi = esporta(fb, target, suffisso="_ciclo")
        if not ok(f"B · {target}: export", cartella is not None, nomi):
            continue
        if target == "clickhouse":
            matrice.pulisci_clickhouse()
            ok(f"B · {target}: prima della macro i database federati non ci sono", not any(n.startswith("tab_src_") for n in matrice.database_clickhouse()))
            rc, out = matrice.prepara_clickhouse(cartella, variabili)
            ok(f"B · {target}: dbt run-operation create_tabularia_sources", rc == 0, out[-800:])
            ok(f"B · {target}: la macro crea davvero il database federato", f"tab_src_{pg['id']}_zzdbt" in matrice.database_clickhouse(), sorted(matrice.database_clickhouse()))
            # la password della sorgente non resta in nessun file che dbt scrive. Una password
            # FINTA e riconoscibile (quella vera del campione è anche il nome del database):
            # IF NOT EXISTS non tocca i database già creati, ma lo SQL con la password passa da dbt
            finto = "zz-SEGRETO-4242"
            v2 = {**variabili, f"TABULARIA_DB_{pg['id']}_PASSWORD": finto}
            cerca = lambda: [os.path.join(r, f) for r, _, fs in os.walk(cartella) for f in fs if finto.encode() in open(os.path.join(r, f), "rb").read()]
            rc, out = matrice.prepara_clickhouse(cartella, v2)
            ok(f"B · {target}: col comando del README la password non è in nessun file (logs/, target/) né a video", rc == 0 and not cerca() and finto not in out, (rc, cerca(), out[-300:]))
            rc, out = dbt(cartella, "dbt", "run-operation", "create_tabularia_sources", "--no-use-colors", variabili=v2)
            ok(f"B · {target}: controprova — senza --log-level-file none la password finisce in logs/dbt.log", any("dbt.log" in f for f in cerca()), cerca())
        if target == "native":
            psql("DROP TABLE IF EXISTS zzdbt.ciclo_append")
        for comando in (["dbt", "parse"], ["dbt", "compile"], ["dbt", "docs", "generate"]):
            rc, out = dbt(cartella, *comando, "--no-use-colors", variabili=variabili)
            ok(f"B · {target}: {' '.join(comando)}", rc == 0, out[-600:])
            if comando == ["dbt", "parse"]:
                avvisi = [l for l in out.splitlines() if re.search(r"(?i)deprecat|\[WARNING\]", l)]
                ok(f"B · {target}: dbt parse senza avvisi di deprecazione", not avvisi, "\n".join(avvisi)[:600])
        # pezzo 2: tracciabilità — da dove viene il modello, nel manifest e in testa al file
        manifest = json.load(open(f"{cartella}/target/manifest.json"))
        nodo = next((n for n in manifest["nodes"].values() if n["resource_type"] == "model" and n["name"] == "ciclo_append"), None)
        meta = ((nodo or {}).get("config") or {}).get("meta", {}).get("tabularia", {})
        ok(f"B · {target}: il manifest porta meta.tabularia (flusso, id, versione e quando è stata salvata, uscita) e non l'ora dell'export",
           meta.get("flow") == "zzciclo append" and meta.get("flow_id") == fb["id"] and meta.get("flow_version") and "zzdbt.ciclo_append" in (meta.get("output") or "")
           and (meta.get("version_saved_at") or "").endswith("UTC") and not any(k.startswith("exported") for k in meta), meta)
        ok(f"B · {target}: i tag tabularia + flusso", nodo is not None and {"tabularia", "zzciclo_append"} <= set(nodo.get("tags") or []), (nodo or {}).get("tags"))
        rc, out = dbt(cartella, "dbt", "ls", "-s", "tag:tabularia", "--resource-type", "model", "--quiet", "--no-use-colors", variabili=variabili)
        ok(f"B · {target}: dbt ls -s tag:tabularia trova il modello", rc == 0 and "ciclo_append" in out, out[-400:])
        testa = open(f"{cartella}/models/ciclo_append.sql").read().split("\n")[2:4]
        ok(f"B · {target}: in testa al file da dove viene", testa and testa[0].startswith("-- Exported from Tabularia: flow «zzciclo append»"), testa)
        print("    " + "\n    ".join(testa))
        conteggi = []
        for argomenti in (["dbt", "run"], ["dbt", "run"], ["dbt", "run", "--full-refresh"]):
            rc, out = dbt(cartella, *argomenti, "--no-use-colors", variabili=variabili)
            if target == "native":
                n = int(psql("SELECT count(*) FROM zzdbt.ciclo_append").stdout.strip() or -1)
            else:
                prodotte = leggi_dbt(cartella, target, variabili)
                n = len(prodotte.get("ciclo_append", {}).get("rows", []))
            conteggi.append((rc, n))
        ok(f"B · {target}: run, run, --full-refresh → 8, 16, 8 righe" + (" nella tabella di destinazione del flusso" if target == "native" else ""),
           [n for _, n in conteggi] == [8, 16, 8] and all(rc == 0 for rc, _ in conteggi), conteggi)
    psql("DROP TABLE IF EXISTS zzdbt.ciclo_append")

    # ── C. ClickHouse nativo ─────────────────────────────────────────────────
    print("C. ClickHouse nativo sul CRM d'esempio", flush=True)
    crm = next(x for x in api("GET", "/datasources")[1] if x.get("source_ref") == "attivita_crm")
    fc = flusso("zzciclo crm", [
        {"id": "s", "type": "source", "data": {"datasourceId": crm["id"]}},
        {"id": "f", "type": "operation", "data": {"opType": "filter", "params": {"column": "durata_min", "operator": "gt", "value": 10}}},
        {"id": "g", "type": "operation", "data": {"opType": "group_by", "params": {"by": ["tipo", "esito"], "aggregations": [
            {"column": "id", "func": "count", "alias": "attivita"}, {"column": "durata_min", "func": "mean", "alias": "Durata_media"}]}}},
        {"id": "o", "type": "output", "data": {"destType": "datasource", "name": "zzciclo crm", "projectId": C, "overwrite": True}}],
        [E("s", "f"), E("f", "g"), E("g", "o")])
    ok("flusso CRM eseguito in Tabularia", lancia(fc)["status"] == "SUCCESS")
    cartella, nomi = esporta(fc, "native", suffisso="_ciclo")
    if ok("C · export nativo ClickHouse", cartella is not None, nomi):
        print("    " + open(f"{cartella}/profiles.yml").read().replace("\n", "\n    ")[:400])
        rc, out = dbt(cartella, "dbt", "run", "--no-use-colors", variabili=variabili)
        if ok("C · dbt run su ClickHouse", rc == 0, "\n".join(l for l in out.splitlines() if "rror" in l)[:1200] or out[-1200:]):
            rc, out = dbt(cartella, "dbt", "show", "--inline", "select * from {{ ref('zzciclo_crm') }}", "--limit", "10000", "--output", "json", "--quiet", "--no-use-colors", variabili=variabili)
            righe = json.loads(out[out.index("{"):]).get("show", [])
            cols = list(righe[0].keys()) if righe else []
            tab = righe_della_datasource(ds("zzciclo crm"))
            tab_cols = list(tab[0].keys()) if tab else []
            uguale, perche = confronta("crm", {}, (tab_cols, [list(r.values()) for r in tab]), {"cols": cols, "rows": [list(r.values()) for r in righe]})
            ok(f"C · ClickHouse dà lo stesso risultato di Tabularia ({len(tab)} righe)", uguale, perche)

    # C2. join esterno su colonne NON Nullable (MergeTree): senza join_use_nulls = 1 ClickHouse
    # riempie il lato mancante con 0 / '' invece di NULL. Tre agenti non hanno un responsabile.
    agenti = next(x for x in api("GET", "/datasources")[1] if x.get("source_ref") == "agenti")
    fj = flusso("zzciclo crm join", [
        {"id": "s", "type": "source", "data": {"datasourceId": agenti["id"]}},
        {"id": "r", "type": "source", "data": {"datasourceId": agenti["id"]}},
        {"id": "j", "type": "operation", "data": {"opType": "join", "params": {"how": "left", "left_on": ["responsabile_id"], "right_on": ["id"]}}},
        {"id": "o", "type": "output", "data": {"destType": "datasource", "name": "zzciclo crm join", "projectId": C, "overwrite": True}}],
        [E("s", "j"), {"source": "r", "target": "j", "targetHandle": "right"}, E("j", "o")])
    ok("flusso CRM con left join eseguito in Tabularia", lancia(fj)["status"] == "SUCCESS")
    cartella, nomi = esporta(fj, "native", suffisso="_ciclo")
    if ok("C2 · export nativo ClickHouse del left join", cartella is not None, nomi):
        rc, out = dbt(cartella, "dbt", "run", "--no-use-colors", variabili=variabili)
        if ok("C2 · dbt run su ClickHouse", rc == 0, "\n".join(l for l in out.splitlines() if "rror" in l)[:1200] or out[-1200:]):
            rc, out = dbt(cartella, "dbt", "show", "--inline", "select * from {{ ref('zzciclo_crm_join') }}", "--limit", "10000", "--output", "json", "--quiet", "--no-use-colors", variabili=variabili)
            righe = json.loads(out[out.index("{"):]).get("show", [])
            tab = righe_della_datasource(ds("zzciclo crm join"))
            uguale, perche = confronta("crm join", {}, (list(tab[0].keys()) if tab else [], [list(r.values()) for r in tab]),
                                       {"cols": list(righe[0].keys()) if righe else [], "rows": [list(r.values()) for r in righe]})
            ok(f"C2 · left join: il lato mancante è NULL in dbt come in Tabularia ({len(tab)} righe)", uguale, perche)
finally:
    for f in api("GET", "/flows")[1]:
        if f["name"].startswith("zzciclo"):
            api("DELETE", f"/flows/{f['id']}")
    for d in api("GET", "/datasources")[1]:
        if d["project_id"] == C:
            api("DELETE", f"/datasources/{d['id']}")
    api("DELETE", f"/projects/{C}")
    psql("DROP SCHEMA IF EXISTS dbt_tabularia CASCADE")
    matrice.pulisci_clickhouse()
    subprocess.run(["docker", "exec", "-i", CH_ESEMPIO_CONTENITORE, "sh", "-c", 'clickhouse-client --user "$CLICKHOUSE_USER" --password "$CLICKHOUSE_PASSWORD"'],
                   input="DROP DATABASE IF EXISTS dbt_tabularia", capture_output=True, text=True)
print(f"\n{sum(esiti)}/{len(esiti)} verifiche passate")
sys.exit(0 if all(esiti) else 1)
