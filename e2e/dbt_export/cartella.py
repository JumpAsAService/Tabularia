"""La CARTELLA per un progetto esistente (pezzo 3) dentro il progetto di un team
data: il team ha già un suo modello e un suo sources.yml che dichiara la sorgente
delle vendite (su ClickHouse: con un suo database federato). Si scarica la cartella
dal dialogo (POST con le opzioni: cartella, prefisso, livelli, sorgente «già
dichiarata» col LORO nome), la si copia nel loro progetto e:
  · `dbt build -s +tag:<flusso>` passa e il modello dà gli stessi dati di Tabularia;
  · il loro modello resta al suo posto;
  · controprova: senza «già dichiarata» dbt rifiuta la sorgente doppia.
Target ClickHouse (il ClickHouse del motore) e DuckDB federato.
uso: python3 cartella.py [clickhouse|duckdb]"""
import io, json, os, re, shutil, subprocess, sys, zipfile
from ambiente import CH_CONTENITORE, CH_PORTA, DOVE, IMMAGINE, env
from comune import api, confronta, dbt, entra, esiti, ok, password_delle_connessioni

FLUSSO = "Margin by Category and Month"
TARGETS = [a for a in sys.argv[1:] if not a.startswith("--")] or ["clickhouse", "duckdb"]
CH = CH_CONTENITORE


def ch_sql(sql):
    return subprocess.run(["docker", "exec", "-i", CH, "sh", "-c", 'clickhouse-client --user "$CLICKHOUSE_USER" --password "$CLICKHOUSE_PASSWORD" --multiquery'],
                          input=sql, capture_output=True, text=True)


def scarica(flusso, opzioni):
    st, zipped = api("POST", f"/flows/{flusso['id']}/export/dbt", opzioni, grezzo=True)
    if st != 200 or zipped[:2] != b"PK":
        return None
    with zipfile.ZipFile(io.BytesIO(zipped)) as z:
        return {n: z.read(n) for n in z.namelist()}


def progetto_del_team(cartella, target, pg, sorgente_loro):
    """Un progetto dbt «di un team»: suo nome, suo profilo, un suo modello, un suo sources.yml."""
    if os.path.exists(cartella):
        subprocess.run(["docker", "run", "--rm", "-v", f"{DOVE}:/x", IMMAGINE, "sh", "-c", f"rm -rf /x/{os.path.basename(cartella)}"], capture_output=True)
    os.makedirs(f"{cartella}/models/staging")
    open(f"{cartella}/dbt_project.yml", "w").write(
        "name: 'team'\nversion: '1.0.0'\nconfig-version: 2\nprofile: 'team'\nmodel-paths: [\"models\"]\nseed-paths: [\"seeds\"]\n"
        "test-paths: [\"tests\"]\nmacro-paths: [\"macros\"]\nmodels:\n  team:\n    +materialized: view\n")
    if target == "clickhouse":
        open(f"{cartella}/profiles.yml", "w").write(
            "team:\n  target: dev\n  outputs:\n    dev:\n      type: clickhouse\n"
            f"      host: {CH}\n      port: {CH_PORTA}\n"
            "      user: \"{{ env_var('CLICKHOUSE_USER') }}\"\n      password: \"{{ env_var('CLICKHOUSE_PASSWORD') }}\"\n"
            "      schema: team_analytics\n      custom_settings:\n        join_use_nulls: 1\n")
        sorgente = f"  - name: {sorgente_loro}\n    schema: team_erp\n"
    else:
        dsn = f"host={pg['host']} port={pg.get('port') or 5432} dbname={pg['database']} user={pg['username']} password={{{{ env_var('TABULARIA_DB_{pg['id']}_PASSWORD') }}}}"
        open(f"{cartella}/profiles.yml", "w").write(
            "team:\n  target: dev\n  outputs:\n    dev:\n      type: duckdb\n      path: team.duckdb\n      extensions: [postgres]\n"
            f"      attach:\n        - path: \"{dsn}\"\n          type: postgres\n          alias: db_{pg['id']}\n")
        sorgente = f"  - name: {sorgente_loro}\n    database: db_{pg['id']}\n    schema: vendite\n"
    open(f"{cartella}/models/staging/sources.yml", "w").write(
        "version: 2\n\nsources:\n" + sorgente + "    tables:\n      - name: ordini\n      - name: righe_ordine\n")
    open(f"{cartella}/models/staging/stg_esistente.sql", "w").write("select 1 as gia_qui\n")


def copia(files, cartella):
    for nome, dati in files.items():
        if nome == "INTEGRATION.md":
            continue
        os.makedirs(os.path.dirname(f"{cartella}/{nome}"), exist_ok=True)
        open(f"{cartella}/{nome}", "wb").write(dati)


entra()
flusso = next(f for f in api("GET", "/flows")[1] if f["name"] == FLUSSO)
piano = api("GET", f"/flows/{flusso['id']}/export/dbt/plan")[1]
pg = next(c for c in api("GET", "/connections")[1] if c["db_type"] == "postgresql")
variabili = password_delle_connessioni()
ds = {d["name"]: d for d in api("GET", "/datasources")[1]}
uscita = piano["outputs"][0]
tag = re.sub(r"[^a-z0-9_]", "_", FLUSSO.lower())
try:
    if "clickhouse" in TARGETS:
        # il team ha già un SUO database federato verso le vendite
        pw = variabili[f"TABULARIA_DB_{pg['id']}_PASSWORD"].replace("\\", "\\\\").replace("'", "\\'")
        r = ch_sql("DROP DATABASE IF EXISTS team_erp; DROP DATABASE IF EXISTS team_analytics; "
                   f"CREATE DATABASE team_erp ENGINE = PostgreSQL('{pg['host']}:{pg.get('port') or 5432}', '{pg['database']}', '{pg['username']}', '{pw}', 'vendite');")
        ok("clickhouse: il database federato del team", r.returncode == 0, r.stderr[-300:])
    for target in TARGETS:
        print(f"\n== target {target}", flush=True)
        t = next(x for x in piano["targets"] if x["id"] == target)
        vendite = next(s for s in t["sources"] if s["schema"] == "vendite")
        opzioni = {"target": target, "package": "folder", "folder": "tabularia_margini", "prefix": "tab_", "layers": True,
                   "sources": {vendite["key"]: {"name": "erp_vendite", "declared": True}}}
        files = scarica(flusso, opzioni)
        if not ok(f"{target}: la cartella si scarica", files is not None):
            continue
        cartella = f"{DOVE}/team_{target}"
        progetto_del_team(cartella, target, pg, "erp_vendite")
        copia(files, cartella)
        if target == "clickhouse":
            ch_sql("DROP DATABASE IF EXISTS tab_src_%d_catalogo;" % pg["id"])
            rc, out = dbt(cartella, "run-operation", "create_tabularia_sources", "--log-level-file", "none", variabili=variabili)
            ok(f"{target}: la macro della cartella crea solo i database delle sorgenti NON dichiarate",
               rc == 0 and "tab_src_%d_catalogo" % pg["id"] in out and "vendite" not in out, out[-500:])
        rc, out = dbt(cartella, "build", "-s", f"+tag:{tag}", variabili=variabili)
        ok(f"{target}: dbt build -s +tag:{tag} nel progetto del team passa", rc == 0 and "Completed successfully" in out,
           "\n".join(l for l in out.splitlines() if "rror" in l or "ERROR" in l)[:1200] or out[-1200:])
        rc, out = dbt(cartella, "ls", "--resource-type", "model", "--quiet", variabili=variabili)
        ok(f"{target}: il modello del team resta, accanto ai nostri", rc == 0 and "team.staging.stg_esistente" in out and f"tab_{uscita['model']}" in out, out[-500:])
        confronta(cartella, f"tab_{uscita['model']}", ds[FLUSSO], variabili)
        # controprova: lo stesso nome SENZA «già dichiarata» → la sorgente è due volte e dbt la rifiuta
        doppia = scarica(flusso, {**opzioni, "sources": {vendite["key"]: {"name": "erp_vendite", "declared": False}}})
        controprova = f"{DOVE}/team_{target}_doppia"
        progetto_del_team(controprova, target, pg, "erp_vendite")
        copia(doppia, controprova)
        rc, out = dbt(controprova, "parse", variabili=variabili)
        ok(f"{target}: controprova — senza «già dichiarata» dbt rifiuta la sorgente doppia", rc != 0 and re.search(r"(?i)duplicate|two sources|same name", out), out[-600:])
finally:
    if "clickhouse" in TARGETS:
        ch_sql(f"DROP DATABASE IF EXISTS team_erp; DROP DATABASE IF EXISTS team_analytics; DROP DATABASE IF EXISTS tab_src_{pg['id']}_catalogo;")
print(f"\n{sum(esiti)}/{len(esiti)} verifiche passate")
sys.exit(0 if all(esiti) else 1)
