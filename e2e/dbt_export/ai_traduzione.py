"""Prova DAL VIVO della traduzione proposta dall'AI (pezzo 4) sullo stack di scarto:
una datasource da query Postgres VALIDA che sqlglot traduce in SQL che ClickHouse non
legge (SIMILAR TO). Senza AI l'export ClickHouse la rifiuta dicendo perché (il
controllo di sintassi di ClickHouse); con l'AI la proposta passa i controlli, dbt la
fa girare su ClickHouse e dà gli stessi dati di Tabularia; riesportare non richiama
l'AI (stesso zip). Serve un modello AI abilitato: chiamate a pagamento al provider. uso: python3 ai_traduzione.py"""
import hashlib, io, json, os, subprocess, sys, time, zipfile
from ambiente import DOVE, IMMAGINE
from comune import aspetta_run, api, confronta, dbt, entra, esiti, ok, password_delle_connessioni, pulisci_clickhouse

QUERY = "SELECT id, canale, stato FROM vendite.ordini WHERE canale SIMILAR TO 'G(DO|astronomia)' AND id < 400"
entra()
pg = next(c for c in api("GET", "/connections")[1] if c["db_type"] == "postgresql")
st, cart = api("POST", "/projects", {"name": "zzai traduzione"})
C = cart["id"]
try:
    st, ds = api("POST", f"/projects/{C}/datasources/database", {"name": "zzai ordini g", "connection_id": pg["id"], "source_type": "sql", "source_ref": QUERY})
    ok("datasource da query Postgres (SIMILAR TO) importata", st in (200, 201), (st, ds))
    for _ in range(60):
        d = next((x for x in api("GET", "/datasources")[1] if x["id"] == ds["id"]), {})
        if d.get("rows") and not d.get("refreshing"):
            break
        time.sleep(2)
    nodi = [{"id": "s", "type": "source", "data": {"datasourceId": ds["id"]}},
            {"id": "o", "type": "output", "data": {"destType": "datasource", "name": "zzai uscita", "projectId": C, "overwrite": True}}]
    st, f = api("POST", f"/projects/{C}/flows", {"name": "zzai flusso", "engine": "clickhouse",
                                                  "definition": json.dumps({"nodes": nodi, "edges": [{"source": "s", "target": "o", "targetHandle": "left"}]})})
    run = aspetta_run(api("POST", f"/flows/{f['id']}/run-now?mode=production")[1]["run_id"])
    ok("il flusso gira in Tabularia", run["status"] == "SUCCESS", run.get("error"))

    st, risposta = api("POST", f"/flows/{f['id']}/export/dbt", {"target": "clickhouse"}, grezzo=True)
    ok("senza AI: l'export ClickHouse rifiuta, e dice che ClickHouse non legge la traduzione",
       st == 422 and "SIMILAR" in str(risposta) and "does not translate to ClickHouse" in str(risposta), (st, str(risposta)[:400]))

    st, primo = api("POST", f"/flows/{f['id']}/export/dbt", {"target": "clickhouse", "ai_translations": True}, grezzo=True)
    if not ok("con l'AI: l'export risponde con uno zip", st == 200 and primo[:2] == b"PK", (st, str(primo)[:500])):
        raise SystemExit
    files = {n: zipfile.ZipFile(io.BytesIO(primo)).read(n).decode() for n in zipfile.ZipFile(io.BytesIO(primo)).namelist()}
    modello = next(n for n in files if n.startswith("models/src_"))
    print("    " + files[modello].replace("\n", "\n    ")[:900])
    ok("il modello tradotto lo dice in testa e il README lo elenca",
       "Translated by AI from postgres" in files[modello] and "translated by an AI model" in files["README.md"] and "tab_src_" in files[modello])

    st, secondo = api("POST", f"/flows/{f['id']}/export/dbt", {"target": "clickhouse", "ai_translations": True}, grezzo=True)
    ok("riesportare la stessa versione: lo stesso zip (la proposta è ricordata)", st == 200 and hashlib.sha256(primo).digest() == hashlib.sha256(secondo).digest())

    cartella = f"{DOVE}/zzai_traduzione"
    subprocess.run(["docker", "run", "--rm", "-v", f"{DOVE}:/x", IMMAGINE, "sh", "-c", "rm -rf /x/zzai_traduzione"], capture_output=True)
    os.makedirs(cartella)
    zipfile.ZipFile(io.BytesIO(primo)).extractall(cartella)
    variabili = password_delle_connessioni()
    pulisci_clickhouse()
    rc, out = dbt(cartella, "run-operation", "create_tabularia_sources", "--log-level-file", "none", variabili=variabili)
    ok("dbt run-operation create_tabularia_sources", rc == 0, out[-600:])
    rc, out = dbt(cartella, "run", variabili=variabili)
    ok("dbt run su ClickHouse col modello tradotto dall'AI", rc == 0 and "Completed successfully" in out, out[-1200:])
    if rc == 0:
        d = next(x for x in api("GET", "/datasources")[1] if x["name"] == "zzai uscita")
        confronta(cartella, "zzai_uscita", d, variabili)
finally:
    for fl in api("GET", "/flows")[1]:
        if fl["name"].startswith("zzai"):
            api("DELETE", f"/flows/{fl['id']}")
    for d in api("GET", "/datasources")[1]:
        if d["project_id"] == C:
            api("DELETE", f"/datasources/{d['id']}")
    api("DELETE", f"/projects/{C}")
    pulisci_clickhouse()
print(f"\n{sum(esiti)}/{len(esiti)} verifiche passate")
sys.exit(0 if all(esiti) else 1)
