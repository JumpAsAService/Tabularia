"""End-to-end proof of the SFTP connection through the gateway API of the dev stack.

Runs INSIDE the gateway container (it has httpx), against a throwaway OpenSSH
container on the same network:

    docker cp e2e/sftp/api_proofs.py dataprep-gateway:/tmp/
    docker exec -e BASE=http://localhost:8000 -e ADMIN_EMAIL=... -e ADMIN_PASSWORD=... \\
        -e SFTP_HOST=zzsftp-a -e SFTP_PORT=2222 -e SFTP_USER=tab -e SFTP_PASSWORD=... \\
        dataprep-gateway python /tmp/api_proofs.py main|changed|cleanup

`main`: folder, connection (draft test shows the key → accept it in the form → create →
accept the key on the saved connection), file list,
datasource «SFTP folder» + refresh, a flow with an SFTP Output run from the editor
path and from the server-side orchestration. `changed` (after the server was
recreated with new keys): refresh refused, file list explained in Italian, a new
test shows the key, accepting it stores it (audit), refresh works again. `mitm` (the
server's name now answers with a man in the middle): nothing is accepted and no
password is offered. `cleanup` deletes everything.
The checks on the server's disk are done by the caller (docker exec ls).
"""
from __future__ import annotations

import json
import os
import sys
import time

import httpx

BASE = os.environ.get("BASE", "http://localhost:8000")
STATE = "/tmp/zz_sftp_api_state.json"
results: list[tuple[str, bool]] = []


def check(name: str, cond: bool, detail="") -> bool:
    results.append((name, bool(cond)))
    print(("  ok  " if cond else "  KO  ") + name + (f" — {str(detail)[:400]}" if detail and not cond else ""), flush=True)
    return bool(cond)


class Api:
    def __init__(self, token: str | None = None, lang: str = "en"):
        self.h = {"Accept-Language": lang, **({"Authorization": f"Bearer {token}"} if token else {})}

    def r(self, method: str, path: str, **kw) -> httpx.Response:
        with httpx.Client(base_url=BASE, timeout=120, headers=self.h) as c:
            return c.request(method, path, **kw)

    def j(self, method: str, path: str, ok=(200, 201, 202, 204), **kw):
        resp = self.r(method, path, **kw)
        if resp.status_code not in ok:
            raise RuntimeError(f"{method} {path} → {resp.status_code} {resp.text[:400]}")
        return resp.json() if resp.content and "json" in resp.headers.get("content-type", "") else None


def login() -> str:
    return Api().j("POST", "/auth/login", json={"email": os.environ["ADMIN_EMAIL"], "password": os.environ["ADMIN_PASSWORD"]})["access_token"]


def wait_run(api: Api, path: str, seconds: int = 180) -> dict:
    end = time.time() + seconds
    while time.time() < end:
        run = api.j("GET", path)
        if isinstance(run, list):
            run = run[0] if run else {}
        if run.get("status") in ("SUCCESS", "FAILURE"):
            return run
        time.sleep(1.5)
    return run


def ds_of(api: Api, project_id: int, ds_id: int) -> dict:
    return next(d for d in api.j("GET", f"/projects/{project_id}/datasources") if d["id"] == ds_id)


def main_phase(api: Api) -> None:
    host, port = os.environ["SFTP_HOST"], int(os.environ["SFTP_PORT"])
    folder = api.j("POST", "/projects", json={"name": f"zzsftp-{int(time.time())}"})
    pid = folder["id"]
    state = {"project_id": pid}
    json.dump(state, open(STATE, "w"))
    draft = {"name": "drop", "db_type": "sftp", "host": host, "port": port, "username": os.environ["SFTP_USER"],
             "password": os.environ["SFTP_PASSWORD"], "extra": json.dumps({"auth": "password"})}

    print("— connection")
    seen = api.j("POST", f"/projects/{pid}/connections/test", json=draft)
    fp, kt = seen["host_key"]["fingerprint"], seen["host_key"]["type"]
    check("the form's test shows the server key and does not log in", fp.startswith("SHA256:") and seen["needs_acceptance"], seen)
    ok_ = api.j("POST", f"/projects/{pid}/connections/test", json={**draft, "expected_fingerprint": fp, "expected_type": kt})
    check("«Accept this key» in the form logs in with that key", ok_["ok"] and not ok_["needs_acceptance"], ok_)
    conn = api.j("POST", f"/projects/{pid}/connections", json=draft)
    state["connection_id"] = cid = conn["id"]
    json.dump(state, open(STATE, "w"))
    check("created without an accepted key", conn["host_key_fingerprint"] == "" and conn["has_password"], conn)
    r = api.r("POST", f"/connections/{cid}/sftp/files", json={"folder": "in", "pattern": "*"})
    check("before acceptance the file list is refused (409, explained)", r.status_code == 409 and "Test connection" in r.json()["detail"], r.text)
    plain = api.j("POST", f"/connections/{cid}/test")
    check("a plain test of the saved connection only shows the key", plain["needs_acceptance"] and not plain.get("accepted"), plain)
    acc = api.j("POST", f"/connections/{cid}/test", json={"expected_fingerprint": fp, "expected_type": kt})
    check("saving accepts exactly the key seen", acc.get("accepted") and acc["host_key"]["fingerprint"] == fp, acc)
    conn = next(c for c in api.j("GET", f"/projects/{pid}/connections") if c["id"] == cid)
    check("the connection now carries the key", conn["host_key_fingerprint"] == fp and conn["host_key_type"], conn)
    audit = api.j("GET", "/audit", params={"action": "connection.host_key_accept", "limit": 5})
    items = audit["items"] if isinstance(audit, dict) else audit
    check("the acceptance is in the audit log", any(str(cid) == str(i.get("target_id")) for i in items), items[:2])
    check("no secret in the API answer", os.environ["SFTP_PASSWORD"] not in json.dumps(conn))

    print("— datasource «SFTP folder»")
    files = api.j("POST", f"/connections/{cid}/sftp/files", json={"folder": "in", "pattern": "vendite_*"})
    check("Check files lists the 3 fixtures", files["total"] == 3, files)
    ds = api.j("POST", f"/projects/{pid}/datasources/sftp", json={"name": "vendite sftp", "connection_id": cid, "folder": "in",
                                                                  "pattern": "vendite_*"})
    state["datasource_id"] = dsid = ds["id"]
    json.dump(state, open(STATE, "w"))
    run = wait_run(api, f"/datasources/{dsid}/runs")
    check("the first import succeeds", run.get("status") == "SUCCESS" and run.get("rows_written") == 5, run)
    d = ds_of(api, pid, dsid)
    names = [c["name"] for c in d.get("columns") or []]
    check("the datasource has the stacked rows and _file", d["rows"] == 5 and "_file" in names and "_file_modified_at" in names, d)
    api.j("POST", f"/datasources/{dsid}/refresh")
    run = wait_run(api, f"/datasources/{dsid}/runs")
    check("a refresh succeeds too", run.get("status") == "SUCCESS", run)
    d = ds_of(api, pid, dsid)
    engines = [e["id"] for e in api.j("GET", "/engines") if e.get("available") and e.get("allowed", True)]
    engine = "polars" if "polars" in engines else engines[0]
    prev = api.j("POST", "/tasks/preview", json={"bucket": d["bucket"], "input_key": d["key"], "limit": 100, "engine": engine,
                                                 "operations": [{"type": "sort", "params": {"by": "id"}}]})
    cols = [c["name"] if isinstance(c, dict) else c for c in prev["columns"]]
    rows = prev["rows"]
    files = [r["_file"] if isinstance(r, dict) else r[cols.index("_file")] for r in rows]
    print(f"  preview ({engine}): columns {cols}; _file per row {files}")
    check("every row says its file in _file, as the last-but-one column",
          cols[-2:] == ["_file", "_file_modified_at"]
          and files == ["vendite_01.csv", "vendite_01.csv", "vendite_02.xlsx", "vendite_03.parquet", "vendite_03.parquet"], files)
    state["ds_key"] = d["key"]

    print("— flow with an SFTP Output")
    definition = {"nodes": [
        {"id": "s", "type": "source", "position": {"x": 0, "y": 0}, "data": {"datasourceId": dsid, "bucket": d["bucket"], "parquetKey": d["key"]}},
        {"id": "o", "type": "output", "position": {"x": 300, "y": 0},
         "data": {"destType": "sftp", "connectionId": cid, "sftpFolder": "out", "sftpFileName": "vendite_{date}_{run_id}", "sftpFormat": "xlsx"}},
    ], "edges": [{"id": "e", "source": "s", "target": "o", "targetHandle": "left"}]}
    flow = api.j("POST", f"/projects/{pid}/flows", json={"name": "Export SFTP", "definition": json.dumps(definition), "engine": engine})
    state["flow_id"] = fid = flow["id"]
    json.dump(state, open(STATE, "w"))
    launched = api.j("POST", f"/flows/{fid}/runs", json={"bucket": d["bucket"], "input_key": d["key"], "operations": [],
                                                       "destination": {"type": "sftp", "connection_id": cid, "folder": "out",
                                                                       "file_name": "vendite_{date}_{run_id}", "format": "csv"}})
    run = wait_run(api, f"/runs/{launched['id']}")
    dest = json.loads(run.get("destination") or "{}")
    check("the editor's run writes the file and reports it", run.get("status") == "SUCCESS" and dest.get("path", "").endswith(f"_{launched['id']}.csv"),
          run)
    state["manual_path"] = dest.get("path")
    orch = api.j("POST", f"/flows/{fid}/run-now")
    run = wait_run(api, f"/runs/{orch['run_id']}", 240)
    check("the server-side orchestration (the scheduler's road) succeeds", run.get("status") == "SUCCESS", run)
    # the Output's own run (RunOut has no parent id): the flow's run that wrote Excel
    outputs = [x for x in api.j("GET", f"/flows/{fid}/runs") if x.get("kind") == "flow" and x["id"] > orch["run_id"]]
    paths = [(x["id"], json.loads(x.get("destination") or "{}").get("path") or "") for x in outputs]
    check("its Output wrote an Excel file named with its run number", any(p.endswith(f"_{i}.xlsx") for i, p in paths), paths)
    state["orch_paths"] = [p for _, p in paths]
    json.dump(state, open(STATE, "w"))


def changed_phase(api: Api) -> None:
    state = json.load(open(STATE))
    pid, cid, dsid = state["project_id"], state["connection_id"], state["datasource_id"]
    api.j("POST", f"/datasources/{dsid}/refresh")
    run = wait_run(api, f"/datasources/{dsid}/runs")
    check("with the server's key changed the refresh fails", run.get("status") == "FAILURE", run)
    check("…with the host-key message", (run.get("error") or "").startswith("SFTP: the host key of") and "has changed" in run["error"], run.get("error"))
    r = Api(api.h["Authorization"].split()[1], "it").r("POST", f"/connections/{cid}/sftp/files", json={"folder": "in", "pattern": "*"})
    check("the file list explains it in Italian (409)", r.status_code == 409 and "è cambiata" in r.json()["detail"], r.text)
    shown = api.j("POST", f"/connections/{cid}/test")
    check("a new test shows the new key as changed and accepts nothing", shown["needs_acceptance"] and shown["changed"] and not shown.get("accepted"), shown)
    acc = api.j("POST", f"/connections/{cid}/test", json={"expected_fingerprint": shown["host_key"]["fingerprint"],
                                                            "expected_type": shown["host_key"]["type"]})
    check("accepting it stores it and says it changed", acc.get("accepted") and acc.get("changed") is True, acc)
    audit = api.j("GET", "/audit", params={"action": "connection.host_key_accept", "limit": 10})
    items = audit["items"] if isinstance(audit, dict) else audit
    mine = [i for i in items if str(i.get("target_id")) == str(cid)]
    check("two acceptances in the audit log, the second with the previous key", len(mine) >= 2 and "previous" in json.dumps(mine[0]), mine[:2])
    api.j("POST", f"/datasources/{dsid}/refresh")
    run = wait_run(api, f"/datasources/{dsid}/runs")
    check("then the refresh works again", run.get("status") == "SUCCESS", run)


def mitm_phase(api: Api) -> None:
    """The server's name now answers with someone else's key (the caller replaced the
    server with a man in the middle that prints every password it is offered)."""
    state = json.load(open(STATE))
    pid, cid, dsid = state["project_id"], state["connection_id"], state["datasource_id"]
    conn = next(c for c in api.j("GET", f"/projects/{pid}/connections") if c["id"] == cid)
    api.j("POST", f"/datasources/{dsid}/refresh")
    run = wait_run(api, f"/datasources/{dsid}/runs")
    check("MITM: the refresh is refused (host key changed)", run.get("status") == "FAILURE" and "has changed" in (run.get("error") or ""), run)
    r = api.r("POST", f"/connections/{cid}/sftp/files", json={"folder": "in", "pattern": "*"})
    check("MITM: the file list is refused (409)", r.status_code == 409, r.text)
    r = api.j("POST", f"/connections/{cid}/test")
    check("MITM: the plain test (the recovery the message suggests) only shows the key", r["needs_acceptance"] and r["changed"], r)
    after = next(c for c in api.j("GET", f"/projects/{pid}/connections") if c["id"] == cid)
    check("MITM: …and the accepted key is unchanged", after["host_key_fingerprint"] == conn["host_key_fingerprint"], after)
    r = api.r("POST", f"/connections/{cid}/test", json={"expected_fingerprint": conn["host_key_fingerprint"], "expected_type": conn["host_key_type"]})
    check("MITM: accepting the OLD key (what saving does) is refused, 409", r.status_code == 409, r.text)
    draft = {"name": "x", "db_type": "sftp", "host": os.environ["SFTP_HOST"], "port": int(os.environ["SFTP_PORT"]), "username": "tab",
             "password": os.environ["SFTP_PASSWORD"], "extra": json.dumps({"auth": "password"})}
    r = api.j("POST", f"/projects/{pid}/connections/test", json=draft)
    check("MITM: the form's test with the password typed only shows the key", r["needs_acceptance"], r)
    r = api.r("POST", f"/projects/{pid}/connections/test", json={**draft, "password": "", "secrets_from": cid})
    check("MITM: a draft test never uses the stored secret (422)", r.status_code == 422, r.text)
    print("  now count the passwords the MITM printed: there must be none")


def cleanup_phase(api: Api) -> None:
    """Everything in the proof's folder (the browser proof adds objects of its own), then the folder."""
    pid = json.load(open(STATE))["project_id"]
    paths = [f"/flows/{f['id']}" for f in api.j("GET", f"/projects/{pid}/flows")]
    paths += [f"/datasources/{d['id']}" for d in api.j("GET", f"/projects/{pid}/datasources")]
    paths += [f"/connections/{c['id']}" for c in api.j("GET", f"/projects/{pid}/connections")]
    for path in paths + [f"/projects/{pid}"]:
        r = api.r("DELETE", path)
        check(f"delete {path}", r.status_code in (200, 204, 404), r.text)


if __name__ == "__main__":
    phase = sys.argv[1] if len(sys.argv) > 1 else "main"
    api = Api(login())
    {"main": main_phase, "changed": changed_phase, "mitm": mitm_phase, "cleanup": cleanup_phase}[phase](api)
    bad = [n for n, okay in results if not okay]
    print(f"\n{len(results) - len(bad)}/{len(results)} ok" + (f" — failed: {bad}" if bad else ""))
    sys.exit(1 if bad else 0)
