"""L'export dbt è DETERMINISTICO: stesso flusso, stessa versione, stesse opzioni →
lo stesso zip, byte per byte. Esporta ogni flusso d'esempio × 3 target × (progetto
completo di default | cartella con opzioni), due volte di fila, poi RIAVVIA gateway
ed engine (processi nuovi: l'ordine degli insiemi Python cambia da un processo
all'altro) ed esporta di nuovo. Gli esiti devono coincidere: stesso hash dello zip,
o lo stesso rifiuto. uso: python3 deterministico.py [--senza-riavvio]
Il riavvio è il comando in E2E_RIAVVIO (ambiente.py); senza, quella parte si salta."""
import hashlib, json, os, subprocess, sys, time
from ambiente import RIAVVIO
from comune import api, entra, ok, esiti


def esporta(f, target, opzioni):
    if opzioni is None:
        st, dati = api("GET", f"/flows/{f['id']}/export/dbt?target={target}", grezzo=True)
    else:
        st, dati = api("POST", f"/flows/{f['id']}/export/dbt", {"target": target, **opzioni}, grezzo=True)
    if st == 200 and isinstance(dati, bytes) and dati[:2] == b"PK":
        return "zip " + hashlib.sha256(dati).hexdigest()[:16]
    return f"rifiuto {st} {dati if isinstance(dati, str) else dati[:300]!r}"


def giro(flussi):
    out = {}
    for f in flussi:
        piano = api("GET", f"/flows/{f['id']}/export/dbt/plan")[1]
        for target in ("clickhouse", "duckdb", "native"):
            t = next(x for x in piano["targets"] if x["id"] == target)
            fonti = {s["key"]: {"name": f"src_{i}", "declared": i == 0} for i, s in enumerate(t["sources"])}
            mats = {o["key"]: "view" for o in piano["outputs"][:1]}
            for nome, opzioni in (("progetto", None),
                                  ("cartella", {"package": "folder", "folder": "x", "prefix": "tab_", "layers": True, "sources": fonti,
                                                "schema": "analisi", "materializations": mats, "tests": False, "emails": False})):
                out[f"{f['name']} · {target} · {nome}"] = esporta(f, target, opzioni)
    return out


def riavvia():
    """Gateway ed engine riavviati col comando di E2E_RIAVVIO, poi si aspetta che l'API risponda."""
    subprocess.run(RIAVVIO, shell=True, capture_output=True)
    for _ in range(120):
        time.sleep(3)
        try:
            entra()
            return True
        except Exception:  # noqa: BLE001 — l'API non risponde ancora
            continue
    return False


entra()
cartelle = {x["id"] for x in api("GET", "/projects")[1] if x["name"] == "Flows"}
flussi = [f for f in api("GET", "/flows")[1] if f["project_id"] in cartelle]
print(f"{len(flussi)} flussi × 3 target × 2 forme", flush=True)
primo = giro(flussi)
secondo = giro(flussi)
ok(f"due export di fila danno gli stessi zip ({sum(v.startswith('zip') for v in primo.values())} zip, "
   f"{sum(v.startswith('rifiuto') for v in primo.values())} rifiuti)", primo == secondo,
   {k: (primo[k], secondo.get(k)) for k in primo if primo[k] != secondo.get(k)})
if "--senza-riavvio" not in sys.argv and RIAVVIO:
    ok("gateway ed engine riavviati (processi nuovi)", riavvia())
    entra()
    terzo = giro(flussi)
    diversi = {k: (primo[k], terzo.get(k)) for k in primo if primo[k] != terzo.get(k)}
    ok("dopo il riavvio, gli stessi zip e gli stessi rifiuti", not diversi, diversi)
ok("almeno un rifiuto atteso c'è (il pivot fuori da DuckDB), e dice perché",
   any("pivot" in v for v in primo.values()), [v for v in primo.values() if v.startswith("rifiuto")][:2])
print(f"\n{sum(esiti)}/{len(esiti)} verifiche passate")
sys.exit(0 if all(esiti) else 1)
