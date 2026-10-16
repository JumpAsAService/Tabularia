"""Sicurezza degli export (dbt e OpenLineage) sullo stack di scarto: un utente con VIEW
sulla SOLA cartella condivisa può farsi dare, attraverso l'export di un flusso che
vede, i dati o le definizioni di oggetti di una cartella che NON vede?
  · cartella privata: un flusso che pubblica «zzsec dati segreti» (poi cancellato: la
    datasource resta senza origine → l'export la fa diventare un SEED, cioè dati) e una
    datasource da QUERY con un testo riconoscibile;
  · cartella condivisa: un flusso che legge le due datasource private.
Controllo: la preview della sorgente privata deve dare 403 (il recinto normale).
Jinja typed in a flow (#2): a native export whose filter value, compute expression, column
name and output table name carry Jinja goes through a real `dbt compile` and `dbt run`. Each
piece of Jinja would print a marker that its literal text does not contain ('zz' ~ 'filter'
gives «zzfilter», 7 ** 13 gives 96889010407), so a piece that ran shows on the console, in logs/
or in target/; the model's alias and schema in the manifest are the table typed in the flow; the
table dbt writes in the sample Postgres has that very name, and holds the texts as typed.
A quote in a native output's table name is refused with name_not_quotable (dbt-postgres writes a
model's table unescaped in its own SQL), by the plan before the download and by the download.
Crea tutto e lo toglie alla fine. uso: python3 sicurezza.py"""
import io, json, os, sys, time, zipfile
from comune import api as _api, dbt, entra, esporta, ok, esiti, password_delle_connessioni, postgres_esempio
import comune

CAP_VIEW = sys.argv[1] if len(sys.argv) > 1 else "view"
ADMIN = None

# ── Jinja typed in a flow (#2) ────────────────────────────────────────────────
# The schema the output table lands in: the oracles' own schema in the sample Postgres (schema.sql)
SCHEMA_JINJA = "zzdbt"
# The output's table, with a backslash and Jinja: dbt's static parser keeps the escapes of a string
# literal as part of the value, so a `config(alias='t\\x')` made dbt write another table. No quote:
# dbt-postgres cannot have one in a model's table (TABELLA_CON_APICE), and none is needed to close the
# alias' literal early — a name written unneutralised anywhere else (a header, a comment) would run
# the log() and print 7 ** 13
TABELLA_JINJA = "t\\x }}{{ log(7 ** 13, info=True) }}{{ config(a="
# a model's table dbt-postgres cannot write: refused before dbt sees it (name_not_quotable)
TABELLA_CON_APICE = "o'brien"
FILTRO_JINJA = "{% do run_query('select 1') %}{% do log('zz' ~ 'filter', info=True) %}{# x"
ESPRESSIONE_JINJA = "'{{ log(''zz'' ~ ''expr'', info=True) }}{{ env_var(''ZZSEC_SEGRETO'') }}'"
COLONNA_JINJA = "c{{ 'zz' ~ 'col' }}lc"
SEGRETO_JINJA = {"ZZSEC_SEGRETO": "zz-segreto-9431"}
# what only a piece of Jinja that RAN would write: log() at info level goes to the console, and
# every log() call goes to logs/dbt.log at debug level, so the files are searched too
SEGNI_JINJA = ("96889010407", "zzfilter", "zzexpr", "zzcol", "zz-segreto-9431")


def operazioni_jinja() -> list[dict]:
    """The flow's steps after the source: a filter, a compute, each with Jinja in its texts."""
    return [
        {"id": "l", "type": "operation", "data": {"opType": "limit", "params": {"n": 5}}},
        {"id": "f", "type": "operation", "data": {"opType": "filter", "params": {"column": "canale", "operator": "ne", "value": FILTRO_JINJA}}},
        {"id": "c", "type": "operation", "data": {"opType": "compute", "params": {"columns": [
            {"name": "x", "expr": ESPRESSIONE_JINJA}, {"name": COLONNA_JINJA, "expr": "'{{ 7 * 7 }}'"}]}}},
    ]


def _ident(nome: str) -> str:
    return '"' + nome.replace('"', '""') + '"'


def _senza_tabelle_jinja(schema: str, sql=postgres_esempio):
    """Drop the table of the check, and any table dbt may have created under another name."""
    sql(f"""DO $zz$ DECLARE r record; BEGIN
              FOR r IN SELECT table_name FROM information_schema.tables WHERE table_schema = '{schema}' AND table_name LIKE '%config(a=%' LOOP
                EXECUTE format('DROP TABLE %I.%I', '{schema}', r.table_name);
              END LOOP; END $zz$;""")


def controlla_jinja(cartella: str, variabili: dict, schema: str = SCHEMA_JINJA, sql=postgres_esempio):
    """The exported native project of the Jinja flow: compile, manifest, run, the table in Postgres."""
    variabili = {**variabili, **SEGRETO_JINJA}
    rc, out_compile = dbt(cartella, "compile", variabili=variabili)
    compilati = "\n".join(open(os.path.join(r, f)).read() for r, _, fs in os.walk(f"{cartella}/target/compiled") for f in fs if f.endswith(".sql"))
    testi = ("{{ log(''zz'' ~ ''expr'', info=True) }}{{ env_var(''ZZSEC_SEGRETO'') }}", "{{ 7 * 7 }}",
             "{% do run_query(''select 1'') %}{% do log(''zz'' ~ ''filter'', info=True) %}{# x", COLONNA_JINJA)
    ok("dbt compile passes and gives the flow's texts back as text", rc == 0 and all(t in compilati for t in testi),
       out_compile[-800:] if rc else [t for t in testi if t not in compilati])
    try:
        manifest = json.load(open(f"{cartella}/target/manifest.json"))
    except (OSError, ValueError) as e:
        manifest = {"nodes": {}}
        print(f"  (no manifest: {e})")
    uscite = [n for n in manifest["nodes"].values() if n["resource_type"] == "model" and (n.get("config") or {}).get("alias")]
    ok("the output model's alias and schema in the manifest are the table typed in the flow, to the character",
       len(uscite) == 1 and uscite[0]["alias"] == TABELLA_JINJA and uscite[0]["schema"] == schema,
       {"expected": [schema, TABELLA_JINJA], "manifest": [[n["schema"], n["alias"]] for n in uscite]})
    _senza_tabelle_jinja(schema, sql)
    rc, out_run = dbt(cartella, "run", variabili=variabili)
    ok("dbt run of the project with Jinja in its texts", rc == 0, out_run[-800:])
    console = out_compile + out_run
    nei_file = {s: [os.path.relpath(os.path.join(r, f), cartella) for r, _, fs in os.walk(cartella) for f in fs
                    if s.encode() in open(os.path.join(r, f), "rb").read()] for s in SEGNI_JINJA}
    ok("no Jinja of the flow ran: none of its markers, nor the secret, on the console or in a file (logs/, target/)",
       not any(s in console for s in SEGNI_JINJA) and not any(nei_file.values()),
       {"console": [s for s in SEGNI_JINJA if s in console], "files": {s: f for s, f in nei_file.items() if f}})
    tabella = f"{_ident(schema)}.{_ident(TABELLA_JINJA)}"
    r = sql(f"SELECT json_build_object('x', x, 'col', {_ident(COLONNA_JINJA)}, 'n', count(*) OVER ()) FROM {tabella} LIMIT 1")
    try:
        riga = json.loads(r.stdout.strip() or "null")
    except ValueError:
        riga = None
    altre = sql(f"SELECT table_name FROM information_schema.tables WHERE table_schema = '{schema}' AND table_name LIKE '%config(a=%'").stdout.split("\n")
    ok("dbt wrote the very table typed in the flow, and its values are the texts as typed",
       r.returncode == 0 and isinstance(riga, dict) and riga.get("n", 0) > 0
       and riga.get("x") == "{{ log('zz' ~ 'expr', info=True) }}{{ env_var('ZZSEC_SEGRETO') }}" and riga.get("col") == "{{ 7 * 7 }}",
       {"row": riga, "error": r.stderr[-300:], "tables in the schema": [t for t in altre if t]})
    _senza_tabelle_jinja(schema, sql)


def frase(codice: str, **parametri) -> str:
    """The gateway's sentence for a refused export, in English (the oracles send no Accept-Language),
    from its own catalogue."""
    import importlib.util
    from ambiente import REPO

    for nome, percorso in (("app.core.lingua", "gateway/app/core/lingua.py"), ("messaggi_dbt", "gateway/app/services/messaggi_dbt.py")):
        if nome not in sys.modules:
            spec = importlib.util.spec_from_file_location(nome, f"{REPO}/{percorso}")
            modulo = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(modulo)
            sys.modules[nome] = modulo
    return sys.modules["messaggi_dbt"].traduci(codice, parametri, "en")


def dettaglio(risposta) -> str:
    """The `detail` of an error answer (the API helper keeps its body as text)."""
    testo = risposta.decode(errors="replace") if isinstance(risposta, bytes) else str(risposta)
    try:
        return str(json.loads(testo).get("detail"))
    except (ValueError, AttributeError):
        return testo


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


def api(*a, **k):
    return come(ADMIN, *a, **k)


def aspetta_run(rid, secondi=300):
    fine = time.time() + secondi
    while time.time() < fine:
        st, r = api("GET", f"/runs/{rid}")
        if st == 200 and r["status"] in ("SUCCESS", "FAILURE"):
            return r
        time.sleep(2)
    return r


def main():
    global ADMIN
    entra()
    ADMIN = comune.TOK
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
            ok("export dbt rifiutato al lettore: è solo per gli amministratori (403)", st == 403 and b"administrators" in (z if isinstance(z, bytes) else str(z).encode()), (st, z[:200] if isinstance(z, (bytes, str)) else z))

        # OpenLineage statico dal lettore
        st, ev = come(tok, "GET", f"/flows/{fc['id']}/openlineage")
        testo = json.dumps(ev)
        ok("OpenLineage: nessun testo di query privata", "TESTO_SEGRETO_DELLA_QUERY" not in testo, "la query privata è negli eventi")
        nota("OpenLineage mostra al lettore nomi e cartella di oggetti privati (solo metadati)",
             [x for x in ("zzsec dati segreti", "zzsec privata", "zzsec query segreta") if x in testo])

        # export dbt con Jinja nel flusso: chi lo lancia eseguirebbe codice altrui. Since #2 every text of the
        # flow is written so that dbt gives it back as it is: the flow exports, and nothing in it runs
        postgres_esempio(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA_JINJA}")
        nodi = [{"id": "s", "type": "source", "data": {"datasourceId": ds["orders"]["id"]}}, *operazioni_jinja(),
                {"id": "o", "type": "output", "data": {"destType": "database", "connectionId": pg["id"],
                                                       "table": f"{SCHEMA_JINJA}.{TABELLA_JINJA}", "mode": "replace"}}]
        catena = [n["id"] for n in nodi]
        st, fj = api("POST", f"/projects/{cond['id']}/flows", {"name": "zzsec jinja", "engine": "duckdb", "definition": json.dumps({
            "nodes": nodi, "edges": [{"source": a, "target": b, "targetHandle": "left"} for a, b in zip(catena, catena[1:])]})})
        creati["flussi"].append(fj["id"])
        cartella, nomi = esporta(fj, "native", suffisso="_jinja")
        if ok("export dbt (admin) di un flusso con Jinja nei testi: uno zip, non un rifiuto", cartella is not None, nomi):
            controlla_jinja(cartella, password_delle_connessioni())

        # a quote in the native output's table: dbt-postgres writes a model's table between quotes in its own
        # SQL without escaping it (an incremental's second run, `dbt docs generate` failed). Refused, with the
        # gateway's sentence, before the download and at the download: never a project that breaks at dbt run
        nodi = [{"id": "s", "type": "source", "data": {"datasourceId": ds["orders"]["id"]}},
                {"id": "l", "type": "operation", "data": {"opType": "limit", "params": {"n": 5}}},
                {"id": "o", "type": "output", "data": {"destType": "database", "connectionId": pg["id"],
                                                       "table": f"{SCHEMA_JINJA}.{TABELLA_CON_APICE}", "mode": "replace"}}]
        st, fq = api("POST", f"/projects/{cond['id']}/flows", {"name": "zzsec apice", "engine": "duckdb", "definition": json.dumps({
            "nodes": nodi, "edges": [{"source": "s", "target": "l", "targetHandle": "left"}, {"source": "l", "target": "o", "targetHandle": "left"}]})})
        creati["flussi"].append(fq["id"])
        attesa = frase("name_not_quotable", name=TABELLA_CON_APICE, character="'", adapter="dbt-postgres")
        st, z = api("GET", f"/flows/{fq['id']}/export/dbt?target=native", grezzo=True)
        ok("a quote in the native output's table: the download is refused with name_not_quotable (dbt-postgres, ')",
           st == 422 and attesa in dettaglio(z), {"status": st, "answer": z[:300] if isinstance(z, (bytes, str)) else z, "expected": attesa})
        piano = api("GET", f"/flows/{fq['id']}/export/dbt/plan")[1]
        nativo = next((t for t in (piano if isinstance(piano, dict) else {}).get("targets", []) if t["id"] == "native"), {"plan": piano})
        ok("… and the plan says it before the download: native not available, with the same reason",
           nativo.get("available") is False and nativo.get("reason") == attesa, {"plan": nativo, "expected": attesa})
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
        _senza_tabelle_jinja(SCHEMA_JINJA)
    print(f"\n{sum(esiti)}/{len(esiti)} verifiche passate")
    sys.exit(0 if all(esiti) else 1)


if __name__ == "__main__":
    main()
