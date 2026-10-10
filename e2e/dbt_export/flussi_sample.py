"""I flussi d'esempio esportati in dbt e fatti girare con dbt VERO, confrontati con
quello che Tabularia pubblica. Per ogni flusso della cartella «Flows» del campione e
per ogni target: si rilancia il flusso in produzione, si esporta, dbt seed/run/test,
e ogni uscita del flusso deve dare gli stessi dati della sua datasource.

Un target che il piano dell'export dice non disponibile (un pivot fuori da DuckDB,
sorgenti su più connessioni…) si salta e lo si dice; gli unici rifiuti che solo
l'engine scopre e che sono attesi sono il pivot e il ClickHouse del CRM d'esempio,
che non è quello del motore.
uso: python3 flussi_sample.py [clickhouse] [duckdb] [native] [--flusso NOME]"""
import json, re, sys

from comune import (aspetta_run, api, confronta, dbt, entra, esiti, esporta, ok, password_delle_connessioni,
                    pulisci_clickhouse, riepilogo, slug_modello)

TARGET = [a for a in sys.argv[1:] if a in ("clickhouse", "duckdb", "native")] or ["clickhouse", "duckdb", "native"]
SOLO = sys.argv[sys.argv.index("--flusso") + 1] if "--flusso" in sys.argv else None
# rifiuti che solo l'engine conosce, attesi sul campione (il gateway risponde in inglese senza Accept-Language)
ATTESI = ("A «pivot» node", "is ANOTHER ClickHouse")


def prova(flusso, target, variabili, ds):
    cartella, nomi = esporta(flusso, target)
    if cartella is None and any(a in str(nomi) for a in ATTESI):
        print(f"  · {target}: rifiuto atteso — {str(nomi[1])[:140]}")
        return
    if not ok(f"{flusso['name']} · {target}: l'export risponde con uno zip", cartella is not None, nomi):
        return
    if "macros/create_tabularia_sources.sql" in nomi:
        # target clickhouse: il passo che il team data fa una volta, i database federati verso le sorgenti
        pulisci_clickhouse()
        rc, out = dbt(cartella, "run-operation", "create_tabularia_sources", "--log-level-file", "none", variabili=variabili)
        ok(f"{flusso['name']} · {target}: dbt run-operation create_tabularia_sources", rc == 0, out[-1500:])
    if any(n.startswith("seeds/") and n.endswith(".csv") for n in nomi):
        rc, out = dbt(cartella, "seed", variabili=variabili)
        ok(f"{flusso['name']} · {target}: dbt seed", rc == 0 and "Completed successfully" in out, out[-1500:])
    rc, out = dbt(cartella, "run", variabili=variabili)
    if not ok(f"{flusso['name']} · {target}: dbt run", rc == 0 and "Completed successfully" in out, out[-1500:]):
        return
    if any(n.startswith("tests/") for n in nomi) or "data_tests:" in open(f"{cartella}/models/schema.yml").read():
        rc, out = dbt(cartella, "test", variabili=variabili)
        ok(f"{flusso['name']} · {target}: dbt test", rc == 0, out[-1500:])
    defn = api("GET", f"/flows/{flusso['id']}")[1]["definition"]
    defn = defn if isinstance(defn, dict) else json.loads(defn)
    for n in defn["nodes"]:
        if n["type"] == "output" and n["data"].get("destType", "datasource") == "datasource":
            nome = n["data"]["name"]
            if nome in ds:
                confronta(cartella, slug_modello(nome), ds[nome], variabili)
            else:
                ok(f"la datasource «{nome}» esiste in Tabularia", False, list(ds)[:5])


entra()
cartelle = {x["id"] for x in api("GET", "/projects")[1] if x["name"] == "Flows"}
flussi = [f for f in api("GET", "/flows")[1] if f["project_id"] in cartelle and (SOLO is None or f["name"] == SOLO)]
ok(f"i flussi d'esempio ci sono ({len(flussi)})", flussi, "serve lo stack col profilo samples")
variabili = password_delle_connessioni()
for f in flussi:
    print(f"\n== «{f['name']}»", flush=True)
    run = aspetta_run(api("POST", f"/flows/{f['id']}/run-now?mode=production")[1]["run_id"])
    if not ok(f"{f['name']}: il flusso gira in Tabularia", run.get("status") == "SUCCESS", (run.get("error") or "")[:300]):
        continue
    ds = {d["name"]: d for d in api("GET", "/datasources")[1]}
    piano = api("GET", f"/flows/{f['id']}/export/dbt/plan")[1]
    for target in TARGET:
        t = next(x for x in piano["targets"] if x["id"] == target)
        if not t["available"]:
            print(f"  · {target}: non disponibile secondo il piano — {(t['reason'] or '')[:140]}")
            continue
        prova(f, target, variabili, ds)
riepilogo()
