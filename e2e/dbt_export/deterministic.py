"""The dbt export is DETERMINISTIC: same flow, same version, same options → the same
zip, byte for byte. Exports every sample flow × 3 targets × (complete project by
default | folder with options), twice in a row, then RESTARTS gateway and engine (new
processes: the order of Python sets changes from one process to the next) and exports
again. The outcomes must match: same hash of the zip, or the same refusal.
usage: python3 deterministic.py [--no-restart]
The restart is the command in E2E_RESTART (environment.py); without it, that part is skipped.
Besides the sample flows, a flow that drops several columns (the order once followed the
process's hash seed, #65). And another administrator exports the same version: the models,
the YAML and the tests are the same bytes; only README.md names who exported it, and with the
emails option off the zip is the same (#42, decision 8)."""
import hashlib, io, json, subprocess, sys, time, zipfile
from environment import RESTART
from common import api, sign_in, ok, outcomes
import common


def export(f, target, options):
    if options is None:
        st, data = api("GET", f"/flows/{f['id']}/export/dbt?target={target}", raw=True)
    else:
        st, data = api("POST", f"/flows/{f['id']}/export/dbt", {"target": target, **options}, raw=True)
    if st == 200 and isinstance(data, bytes) and data[:2] == b"PK":
        return "zip " + hashlib.sha256(data).hexdigest()[:16]
    return f"refusal {st} {data if isinstance(data, str) else data[:300]!r}"


def round_of(flows):
    out = {}
    for f in flows:
        plan = api("GET", f"/flows/{f['id']}/export/dbt/plan")[1]
        for target in ("clickhouse", "duckdb", "native"):
            t = next(x for x in plan["targets"] if x["id"] == target)
            sources = {s["key"]: {"name": f"src_{i}", "declared": i == 0} for i, s in enumerate(t["sources"])}
            mats = {o["key"]: "view" for o in plan["outputs"][:1]}
            for name, options in (("project", None),
                                  ("folder", {"package": "folder", "folder": "x", "prefix": "tab_", "layers": True, "sources": sources,
                                              "schema": "analisi", "materializations": mats, "tests": False, "emails": False})):
                out[f"{f['name']} · {target} · {name}"] = export(f, target, options)
    return out


def restart():
    """Gateway and engine restarted with the command of E2E_RESTART, then wait for the API to answer."""
    subprocess.run(RESTART, shell=True, capture_output=True)
    for _ in range(120):
        time.sleep(3)
        try:
            sign_in()
            return True
        except Exception:  # noqa: BLE001 — the API does not answer yet
            continue
    return False


def zip_of(f, options):
    st, data = api("POST", f"/flows/{f['id']}/export/dbt", options, raw=True)
    if st != 200 or not isinstance(data, bytes) or data[:2] != b"PK":
        raise RuntimeError(f"export: {st} {data if isinstance(data, str) else data[:300]!r}")
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        return data, {n: z.read(n) for n in z.namelist()}


def another_administrator(f):
    """The same version exported by another administrator: (files that differ, zips equal with the emails off?)."""
    st, u = api("POST", "/users", {"email": "zzdet-other@example.com", "password": "Other-for-tests-9", "full_name": "zzdet", "is_superuser": True})
    if st not in (200, 201):
        raise RuntimeError(f"second administrator: {st} {u}")
    my_token = common.TOK
    try:
        _, mine = zip_of(f, {"target": "duckdb"})
        mine_without, _ = zip_of(f, {"target": "duckdb", "emails": False})
        common.TOK = None
        st, t = api("POST", "/auth/login", {"email": "zzdet-other@example.com", "password": "Other-for-tests-9"})
        common.TOK = t["access_token"]
        _, theirs = zip_of(f, {"target": "duckdb"})
        theirs_without, _ = zip_of(f, {"target": "duckdb", "emails": False})
    finally:
        common.TOK = my_token
        api("DELETE", f"/users/{u['id']}")
    return sorted(n for n in set(mine) | set(theirs) if mine.get(n) != theirs.get(n)), mine_without == theirs_without


sign_in()
folders = {x["id"] for x in api("GET", "/projects")[1] if x["name"] == "Flows"}
flows = [f for f in api("GET", "/flows")[1] if f["project_id"] in folders]
# a drop of several columns: the export once wrote them in the order of a Python set (#65)
st, folder = api("POST", "/projects", {"name": "zzdet"})
try:
    orders = next(d for d in api("GET", "/datasources")[1] if d["name"] == "orders")
    dropped = [c["name"] for c in orders.get("columns") or [] if c["name"] != "id"][:4]
    st, fd = api("POST", f"/projects/{folder['id']}/flows", {"name": "zzdet drop", "engine": "duckdb", "definition": json.dumps({"nodes": [
        {"id": "s", "type": "source", "data": {"datasourceId": orders["id"]}},
        {"id": "d", "type": "operation", "data": {"opType": "drop", "params": {"columns": dropped}}},
        {"id": "o", "type": "output", "data": {"destType": "datasource", "name": "zzdet drop", "projectId": folder["id"], "overwrite": True}}],
        "edges": [{"source": "s", "target": "d", "targetHandle": "left"}, {"source": "d", "target": "o", "targetHandle": "left"}]})})
    ok(f"a flow that drops {len(dropped)} columns", st in (200, 201) and len(dropped) >= 3, (st, fd, dropped))
    flows.append(fd)
    print(f"{len(flows)} flows × 3 targets × 2 shapes", flush=True)
    first = round_of(flows)
    second = round_of(flows)
    ok(f"two exports in a row give the same zips ({sum(v.startswith('zip') for v in first.values())} zips, "
       f"{sum(v.startswith('refusal') for v in first.values())} refusals)", first == second,
       {k: (first[k], second.get(k)) for k in first if first[k] != second.get(k)})
    if "--no-restart" not in sys.argv and RESTART:
        ok("gateway and engine restarted (new processes)", restart())
        sign_in()
        third = round_of(flows)
        different = {k: (first[k], third.get(k)) for k in first if first[k] != third.get(k)}
        ok("after the restart (another hash seed), the same zips and the same refusals", not different, different)
    ok("at least one expected refusal is there (the pivot outside DuckDB), and says why",
       any("pivot" in v for v in first.values()), [v for v in first.values() if v.startswith("refusal")][:2])
    different, equal_without = another_administrator(fd)
    ok("another administrator: only README.md differs (it names who exported), and with the emails off the zip is the same",
       different == ["README.md"] and equal_without, (different, equal_without))
finally:
    for f in api("GET", "/flows")[1]:
        if f["name"].startswith("zzdet"):
            api("DELETE", f"/flows/{f['id']}")
    api("DELETE", f"/projects/{folder['id']}")
print(f"\n{sum(outcomes)}/{len(outcomes)} checks passed")
sys.exit(0 if all(outcomes) else 1)
