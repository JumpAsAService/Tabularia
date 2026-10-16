"""L'export dbt è DETERMINISTICO: stesso flusso, stessa versione, stesse opzioni →
lo stesso zip, byte per byte. Esporta ogni flusso d'esempio × 3 target × (progetto
completo di default | cartella con opzioni), due volte di fila, poi RIAVVIA gateway
ed engine (processi nuovi: l'ordine degli insiemi Python cambia da un processo
all'altro) ed esporta di nuovo. Gli esiti devono coincidere: stesso hash dello zip,
o lo stesso rifiuto. uso: python3 deterministico.py [--senza-riavvio]
Il riavvio è il comando in E2E_RIAVVIO (ambiente.py); senza, quella parte si salta.
Besides the sample flows, a flow that drops several columns (the order once followed the
process's hash seed, #65). And another administrator exports the same version: the models,
the YAML and the tests are the same bytes; only README.md names who exported it, and with the
emails option off the zip is the same (#42, decision 8)."""
import hashlib, io, json, subprocess, sys, time, zipfile
from ambiente import RIAVVIO
from comune import api, entra, ok, esiti
import comune


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


def zip_di(f, opzioni):
    st, dati = api("POST", f"/flows/{f['id']}/export/dbt", opzioni, grezzo=True)
    if st != 200 or not isinstance(dati, bytes) or dati[:2] != b"PK":
        raise RuntimeError(f"export: {st} {dati if isinstance(dati, str) else dati[:300]!r}")
    with zipfile.ZipFile(io.BytesIO(dati)) as z:
        return dati, {n: z.read(n) for n in z.namelist()}


def un_altro_amministratore(f):
    """The same version exported by another administrator: (files that differ, zips equal with the emails off?)."""
    st, u = api("POST", "/users", {"email": "zzdet-altro@example.com", "password": "Altro-di-prova-9", "full_name": "zzdet", "is_superuser": True})
    if st not in (200, 201):
        raise RuntimeError(f"second administrator: {st} {u}")
    mio = comune.TOK
    try:
        _, mie = zip_di(f, {"target": "duckdb"})
        mio_senza, _ = zip_di(f, {"target": "duckdb", "emails": False})
        comune.TOK = None
        st, t = api("POST", "/auth/login", {"email": "zzdet-altro@example.com", "password": "Altro-di-prova-9"})
        comune.TOK = t["access_token"]
        _, sue = zip_di(f, {"target": "duckdb"})
        suo_senza, _ = zip_di(f, {"target": "duckdb", "emails": False})
    finally:
        comune.TOK = mio
        api("DELETE", f"/users/{u['id']}")
    return sorted(n for n in set(mie) | set(sue) if mie.get(n) != sue.get(n)), mio_senza == suo_senza


entra()
cartelle = {x["id"] for x in api("GET", "/projects")[1] if x["name"] == "Flows"}
flussi = [f for f in api("GET", "/flows")[1] if f["project_id"] in cartelle]
# a drop of several columns: the export once wrote them in the order of a Python set (#65)
st, cart = api("POST", "/projects", {"name": "zzdet"})
try:
    ordini = next(d for d in api("GET", "/datasources")[1] if d["name"] == "orders")
    via = [c["name"] for c in ordini.get("columns") or [] if c["name"] != "id"][:4]
    st, fd = api("POST", f"/projects/{cart['id']}/flows", {"name": "zzdet drop", "engine": "duckdb", "definition": json.dumps({"nodes": [
        {"id": "s", "type": "source", "data": {"datasourceId": ordini["id"]}},
        {"id": "d", "type": "operation", "data": {"opType": "drop", "params": {"columns": via}}},
        {"id": "o", "type": "output", "data": {"destType": "datasource", "name": "zzdet drop", "projectId": cart["id"], "overwrite": True}}],
        "edges": [{"source": "s", "target": "d", "targetHandle": "left"}, {"source": "d", "target": "o", "targetHandle": "left"}]})})
    ok(f"a flow that drops {len(via)} columns", st in (200, 201) and len(via) >= 3, (st, fd, via))
    flussi.append(fd)
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
        ok("dopo il riavvio (another hash seed), gli stessi zip e gli stessi rifiuti", not diversi, diversi)
    ok("almeno un rifiuto atteso c'è (il pivot fuori da DuckDB), e dice perché",
       any("pivot" in v for v in primo.values()), [v for v in primo.values() if v.startswith("rifiuto")][:2])
    diversi, uguali_senza = un_altro_amministratore(fd)
    ok("another administrator: only README.md differs (it names who exported), and with the emails off the zip is the same",
       diversi == ["README.md"] and uguali_senza, (diversi, uguali_senza))
finally:
    for f in api("GET", "/flows")[1]:
        if f["name"].startswith("zzdet"):
            api("DELETE", f"/flows/{f['id']}")
    api("DELETE", f"/projects/{cart['id']}")
print(f"\n{sum(esiti)}/{len(esiti)} verifiche passate")
sys.exit(0 if all(esiti) else 1)
