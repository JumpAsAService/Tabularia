"""Sicurezza degli export (dbt e OpenLineage) sullo stack di scarto: un utente con VIEW
sulla SOLA cartella condivisa può farsi dare, attraverso l'export di un flusso che
vede, i dati o le definizioni di oggetti di una cartella che NON vede?
  · cartella privata: un flusso che pubblica «zzsec dati segreti» (poi cancellato: la
    datasource resta senza origine → l'export la fa diventare un SEED, cioè dati) e una
    datasource da QUERY con un testo riconoscibile;
  · cartella condivisa: un flusso che legge le due datasource private.
Controllo: la preview della sorgente privata deve dare 403 (il recinto normale).
Crea tutto e lo toglie alla fine. uso: python3 sicurezza_export.py"""
import io, json, sys, time, zipfile
from ambiente import env
from comune import api as _api, entra, ok, esiti
import comune

entra()
ADMIN = comune.TOK
CAP_VIEW = sys.argv[1] if len(sys.argv) > 1 else "view"


def nota(cosa, presente):
    """Un comportamento NOTO e ACCETTATO dall'utente (2026-10-10): si registra, non fa fallire."""
    print(f"  ·  nota (accettato): {cosa} → {'sì' if presente else 'no'}", flush=True)
creati = {"flussi": [], "ds": [], "cartelle": [], "utenti": []}


def come(token, *a, **k):
    vecchio = comune.TOK
    comune.TOK = token
    try:
        return _api(*a, **k)
    finally:
        comune.TOK = vecchio


api = lambda *a, **k: come(ADMIN, *a, **k)


def aspetta_run(rid, secondi=300):
    fine = time.time() + secondi
    while time.time() < fine:
        st, r = api("GET", f"/runs/{rid}")
        if st == 200 and r["status"] in ("SUCCESS", "FAILURE"):
            return r
        time.sleep(2)
    return r


try:
    ds = {d["name"]: d for d in api("GET", "/datasources")[1]}
    pg = next(c for c in api("GET", "/connections")[1] if c["db_type"] == "postgresql")
    st, priv = api("POST", "/projects", {"name": "zzsec privata"}); creati["cartelle"].append(priv["id"])
    st, cond = api("POST", "/projects", {"name": "zzsec condivisa"}); creati["cartelle"].append(cond["id"])
    ok("cartelle create", priv.get("id") and cond.get("id"), (priv, cond))

    # nella privata: dati pubblicati da un flusso poi cancellato
    defin = {"nodes": [{"id": "s", "type": "source", "data": {"datasourceId": ds["orders"]["id"]}},
                       {"id": "l", "type": "operation", "data": {"opType": "limit", "params": {"n": 20}}},
                       {"id": "o", "type": "output", "data": {"destType": "datasource", "name": "zzsec dati segreti", "projectId": priv["id"], "overwrite": True}}],
             "edges": [{"source": "s", "target": "l", "targetHandle": "left"}, {"source": "l", "target": "o", "targetHandle": "left"}]}
    st, fp = api("POST", f"/projects/{priv['id']}/flows", {"name": "zzsec segreto", "definition": json.dumps(defin), "engine": "duckdb"})
    r = aspetta_run(api("POST", f"/flows/{fp['id']}/run-now?mode=production")[1]["run_id"])
    ok("il flusso privato ha pubblicato i dati", r["status"] == "SUCCESS", r)
    api("DELETE", f"/flows/{fp['id']}")
    segreti = next(d for d in api("GET", "/datasources")[1] if d["name"] == "zzsec dati segreti"); creati["ds"].append(segreti["id"])
    ok("cancellato il flusso, la datasource resta senza origine", segreti.get("flow_id") is None, segreti)
    st, q = api("POST", f"/projects/{priv['id']}/datasources/database", {"name": "zzsec query segreta", "connection_id": pg["id"], "source_type": "sql",
                                                                           "source_ref": "SELECT id, canale /* TESTO_SEGRETO_DELLA_QUERY */ FROM " + ds["orders"]["source_ref"]})
    creati["ds"].append(q["id"])
    for _ in range(60):
        if next((x for x in api("GET", "/datasources")[1] if x["id"] == q["id"]), {}).get("rows"):
            break
        time.sleep(2)

    # nella condivisa: un flusso che le legge
    defin2 = {"nodes": [{"id": "a", "type": "source", "data": {"datasourceId": segreti["id"]}},
                        {"id": "b", "type": "source", "data": {"datasourceId": q["id"]}},
                        {"id": "o1", "type": "output", "data": {"destType": "datasource", "name": "zzsec uno", "projectId": cond["id"]}},
                        {"id": "o2", "type": "output", "data": {"destType": "datasource", "name": "zzsec due", "projectId": cond["id"]}}],
              "edges": [{"source": "a", "target": "o1", "targetHandle": "left"}, {"source": "b", "target": "o2", "targetHandle": "left"}]}
    st, fc = api("POST", f"/projects/{cond['id']}/flows", {"name": "zzsec condiviso", "definition": json.dumps(defin2), "engine": "duckdb"})
    creati["flussi"].append(fc["id"])

    # l'utente che vede solo la condivisa
    st, u = api("POST", "/users", {"email": "zzsec-lettore@example.com", "password": "Lettore-di-prova-9", "full_name": "zzsec"})
    creati["utenti"].append(u["id"])
    st, perm = api("POST", f"/projects/{cond['id']}/permissions", {"capability": CAP_VIEW, "user_id": u["id"]})
    ok("permesso VIEW concesso sulla cartella condivisa", st in (200, 201), (st, perm, u))
    tok = come(None, "POST", "/auth/login", {"email": "zzsec-lettore@example.com", "password": "Lettore-di-prova-9"})[1]["access_token"]
    st, _ = come(tok, "GET", f"/flows/{fc['id']}")
    ok("il lettore vede il flusso condiviso", st == 200, st)
    st, r = come(tok, "POST", "/tasks/export", {"bucket": segreti["bucket"], "input_key": segreti["key"], "operations": [], "format": "csv"}, grezzo=True)
    ok("recinto normale: il lettore NON può scaricare la datasource privata (403)", st == 403, (st, r[:120] if isinstance(r, (bytes, str)) else r))

    # export dbt dal lettore
    st, z = come(tok, "GET", f"/flows/{fc['id']}/export/dbt?target=duckdb", grezzo=True)
    if st == 200 and z[:2] == b"PK":
        files = zipfile.ZipFile(io.BytesIO(z))
        seed = [n for n in files.namelist() if n.startswith("seeds/") and n.endswith(".csv")]
        dati = files.read(seed[0]).decode() if seed else ""
        modelli = "\n".join(files.read(n).decode() for n in files.namelist() if n.endswith(".sql"))
        ok("export dbt: NESSUN dato della cartella privata nei seed", not seed, f"{len(dati.splitlines()) - 1} righe di dati privati nel seed {seed}: {dati[:160]!r}")
        ok("export dbt: NESSUN testo di query privata nei modelli", "TESTO_SEGRETO_DELLA_QUERY" not in modelli, "la query privata è nel progetto")
    else:
        ok("export dbt rifiutato al lettore: è solo per gli amministratori (403)", st == 403 and b"amministratori" in (z if isinstance(z, bytes) else str(z).encode()), (st, z[:200] if isinstance(z, (bytes, str)) else z))

    # OpenLineage statico dal lettore
    st, ev = come(tok, "GET", f"/flows/{fc['id']}/openlineage")
    testo = json.dumps(ev)
    ok("OpenLineage: nessun testo di query privata", "TESTO_SEGRETO_DELLA_QUERY" not in testo, "la query privata è negli eventi")
    nota("OpenLineage mostra al lettore nomi e cartella di oggetti privati (solo metadati)",
         [x for x in ("zzsec dati segreti", "zzsec privata", "zzsec query segreta") if x in testo])
    # export dbt con Jinja nell'espressione: chi lo lancia eseguirebbe codice altrui
    st, fj = api("POST", f"/projects/{cond['id']}/flows", {"name": "zzsec jinja", "engine": "duckdb", "definition": json.dumps({"nodes": [
        {"id": "s", "type": "source", "data": {"datasourceId": ds["orders"]["id"]}},
        {"id": "c", "type": "operation", "data": {"opType": "compute", "params": {"columns": [{"name": "x", "expr": "{{ env_var('TABULARIA_DB_1_PASSWORD') }}"}]}}},
        {"id": "o", "type": "output", "data": {"destType": "database", "connectionId": pg["id"], "table": "t') }}{{ log('iniettato') }}{{ config(a='", "mode": "replace"}}],
        "edges": [{"source": "s", "target": "c", "targetHandle": "left"}, {"source": "c", "target": "o", "targetHandle": "left"}]})})
    creati["flussi"].append(fj["id"])
    st, z = api("GET", f"/flows/{fj['id']}/export/dbt?target=native", grezzo=True)
    tutto = "\n".join(zipfile.ZipFile(io.BytesIO(z)).read(n).decode() for n in zipfile.ZipFile(io.BytesIO(z)).namelist()) if st == 200 and z[:2] == b"PK" else ""
    nota("export dbt (admin): il Jinja scritto nel flusso arriva nel progetto", bool(tutto) and "env_var('TABULARIA_DB_1_PASSWORD')" in tutto)
    nota("OpenLineage: il lettore può mandare gli eventi al collector (POST)", come(tok, "POST", f"/flows/{fc['id']}/openlineage")[0] not in (403, 409))
finally:
    for fid in creati["flussi"]:
        api("DELETE", f"/flows/{fid}")
    for d in api("GET", "/datasources")[1]:
        if d["name"].startswith("zzsec"):
            api("DELETE", f"/datasources/{d['id']}")
    for uid in creati["utenti"]:
        api("DELETE", f"/users/{uid}")
    for pid in reversed(creati["cartelle"]):
        api("DELETE", f"/projects/{pid}")
print(f"\n{sum(esiti)}/{len(esiti)} verifiche passate")
sys.exit(0 if all(esiti) else 1)
