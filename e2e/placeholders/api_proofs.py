"""End-to-end proofs of the placeholders, the ranges and the «S3 folder» datasource
through the gateway API of the dev stack (gateway :8000, its workers), against the
throwaway servers of engine_proofs.py (zzph-minio, zzph-sftp, zzph-smtp, zzph-api).

Runs INSIDE the gateway container (it has httpx):

    docker cp e2e/placeholders/api_proofs.py dataprep-gateway:/tmp/
    docker exec -e BASE=http://localhost:8000 -e ADMIN_EMAIL=... -e ADMIN_PASSWORD=... \\
        -e S3_ENDPOINT=http://zzph-minio:9000 -e S3_KEY=... -e S3_SECRET=... \\
        -e SFTP_HOST=zzph-sftp -e SFTP_PORT=2222 -e SFTP_USER=tab -e SFTP_PASSWORD=... \\
        -e SMTP_HOST=zzph-smtp -e API_BASE=http://zzph-api:8080/v1 \\
        dataprep-gateway python /tmp/api_proofs.py main|cleanup

`main` needs the objects engine_proofs.py leaves in the bucket zzph-in (daily/<day>/sales.csv
for today-3 and today-1, two rows each after its append proof; mixed/*) and on the SFTP
server (ph/in/sales_<day>.csv). It writes /tmp/zz_ph_state.json with what the caller
checks on the servers' disks (the SFTP file, the S3 object).
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx

BASE = os.environ.get("BASE", "http://localhost:8000")
STATE = "/tmp/zz_ph_state.json"
FAR = "Pacific/Kiritimati"
results: list[tuple[str, bool]] = []


def check(name: str, cond: bool, detail=None) -> bool:
    results.append((name, bool(cond)))
    print(("  ok  " if cond else "  KO  ") + name + (f" — {str(detail)[:500]}" if detail is not None and not cond else ""),
          flush=True)
    return bool(cond)


class Api:
    def __init__(self, token: str | None = None):
        self.h = {"Accept-Language": "en", **({"Authorization": f"Bearer {token}"} if token else {})}

    def r(self, method: str, path: str, **kw) -> httpx.Response:
        with httpx.Client(base_url=BASE, timeout=120, headers=self.h) as c:
            return c.request(method, path, **kw)

    def j(self, method: str, path: str, ok=(200, 201, 202, 204), **kw):
        resp = self.r(method, path, **kw)
        if resp.status_code not in ok:
            raise RuntimeError(f"{method} {path} → {resp.status_code} {resp.text[:400]}")
        return resp.json() if resp.content and "json" in resp.headers.get("content-type", "") else None


def login() -> str:
    return Api().j("POST", "/auth/login", json={"email": os.environ["ADMIN_EMAIL"],
                                                "password": os.environ["ADMIN_PASSWORD"]})["access_token"]


def wait_run(api: Api, path: str, seconds: int = 180) -> dict:
    end = time.time() + seconds
    run: dict = {}
    while time.time() < end:
        got = api.j("GET", path)
        run = (got[0] if got else {}) if isinstance(got, list) else got
        if run.get("status") in ("SUCCESS", "FAILURE"):
            return run
        time.sleep(1.5)
    return run


def ds_of(api: Api, pid: int, dsid: int) -> dict:
    return next(d for d in api.j("GET", f"/projects/{pid}/datasources") if d["id"] == dsid)


def save(state: dict) -> None:
    json.dump(state, open(STATE, "w"))


def main_phase(api: Api) -> None:
    today = datetime.now(timezone.utc).date()
    d = lambda n: today - timedelta(days=n)  # noqa: E731
    days = {"name": "day", "type": "range", "start": "today-3", "end": "today", "step": "1d"}
    pid = api.j("POST", "/projects", json={"name": f"zzph-{int(time.time())}"})["id"]
    state: dict = {"project_id": pid}
    save(state)

    print("— the live preview")
    pv = api.j("POST", "/placeholders/preview", json={"templates": {"pattern": "s_{day:yyyyMMdd}.csv"}, "variables": [days]})
    check("an input preview: 3 values, the first and the last", pv["expansions"] == 3
          and pv["fields"]["pattern"]["value"] == f"s_{d(3):%Y%m%d}.csv" and pv["fields"]["pattern"]["last"] == f"s_{d(1):%Y%m%d}.csv", pv)
    pv = api.j("POST", "/placeholders/preview", json={"kind": "output", "templates": {"f": "x_{date}_{run_id}"}, "targets": {"f": "file"},
                                                      "timezone": FAR})
    far_today = datetime.now(timezone.utc).astimezone(ZoneInfo(FAR)).date()
    check("an output preview in a far zone, {run_id} left as it is", pv["fields"]["f"]["value"] == f"x_{far_today}_{{run_id}}", pv)
    pv = api.j("POST", "/placeholders/preview", json={"templates": {"p": "{tomorrow}"}})
    check("a wrong placeholder says why", "unknown placeholder {tomorrow}" in (pv["fields"]["p"]["error"] or ""), pv)

    print("— S3 connection and «S3 folder» datasources")
    conn = api.j("POST", f"/projects/{pid}/connections", json={
        "name": "zz lake", "db_type": "s3", "host": os.environ["S3_ENDPOINT"], "username": os.environ["S3_KEY"],
        "password": os.environ["S3_SECRET"], "database": "zzph-in", "db_schema": ""})
    s3id = state["s3_connection_id"] = conn["id"]
    save(state)
    t = api.j("POST", f"/connections/{s3id}/test")
    check("the S3 connection tests", t.get("ok") is not False, t)
    files = api.j("POST", f"/connections/{s3id}/s3/files", json={"folder": "daily/{day}", "pattern": "sales.csv", "variables": [days]})
    check("Check the files: 2 files and the missing day", files["total"] == 2 and files.get("missing_total") == 1
          and files["missing"][0]["params"]["day"] == d(2).isoformat(), files)
    r = api.r("POST", f"/projects/{pid}/datasources/s3", json={"name": "bad", "connection_id": s3id, "folder": "daily",
                                                                "pattern": "s_{tomorrow}.csv"})
    check("an unknown placeholder is refused at save time (422)", r.status_code == 422 and "tomorrow" in r.text, r.text)
    r = api.r("POST", f"/projects/{pid}/datasources/s3", json={"name": "bad", "connection_id": s3id, "folder": "../x",
                                                                "pattern": "*.csv"})
    check("'..' is refused at save time (422)", r.status_code == 422, r.text)
    r = api.r("POST", f"/projects/{pid}/datasources/s3", json={"name": "bad", "connection_id": s3id, "folder": "daily",
                                                                "pattern": "{day}.csv", "variables": [{**days, "end": "today-5"}]})
    check("an empty range is refused at save time (422)", r.status_code == 422 and "empty" in r.text, r.text)

    ds = api.j("POST", f"/projects/{pid}/datasources/s3", json={
        "name": "vendite giornaliere", "connection_id": s3id, "folder": "daily/{day}", "pattern": "sales.csv",
        "variables": [days], "mode": "append", "keys": ["_param_day", "id"]})
    dsid = state["s3_datasource_id"] = ds["id"]
    save(state)
    run = wait_run(api, f"/datasources/{dsid}/runs")
    notes = json.loads(run.get("notes") or "{}")
    check("the import succeeds with the missing day in the run's notes", run.get("status") == "SUCCESS"
          and notes.get("missing", [{}])[0].get("params") == {"day": d(2).isoformat()}, run)
    meta = ds_of(api, pid, dsid)
    cols = [c["name"] for c in meta.get("columns") or []]
    check("the datasource: rows of the two days, _param_day then _file, _file_modified_at",
          meta["rows"] == 4 and cols[-3:] == ["_param_day", "_file", "_file_modified_at"], meta)
    api.j("POST", f"/datasources/{dsid}/refresh")
    run = wait_run(api, f"/datasources/{dsid}/runs")
    meta = ds_of(api, pid, dsid)
    check("an append refresh re-reads the days and duplicates nothing", run.get("status") == "SUCCESS" and meta["rows"] == 4,
          (run, meta.get("rows")))
    state["s3_key"] = meta["key"]
    state["s3_bucket"] = meta["bucket"]

    none = api.j("POST", f"/projects/{pid}/datasources/s3", json={
        "name": "nessun file", "connection_id": s3id, "folder": "daily", "pattern": "none_{day}.csv", "variables": [days]})
    run = wait_run(api, f"/datasources/{none['id']}/runs")
    check("no file across the whole range: the refresh FAILS, saying so", run.get("status") == "FAILURE"
          and "any of the 3 values" in (run.get("error") or ""), run)

    mixed = api.j("POST", f"/projects/{pid}/datasources/s3", json={"name": "misti", "connection_id": s3id, "folder": "mixed",
                                                                    "pattern": "*.*"})
    run = wait_run(api, f"/datasources/{mixed['id']}/runs")
    check("a glob over CSV + parquet + Excel + NDJSON in a bucket", run.get("status") == "SUCCESS" and run.get("rows_written") == 4, run)

    print("— SFTP connection, a datasource with a range")
    draft = {"name": "zz drop", "db_type": "sftp", "host": os.environ["SFTP_HOST"], "port": int(os.environ["SFTP_PORT"]),
             "username": os.environ["SFTP_USER"], "password": os.environ["SFTP_PASSWORD"], "extra": json.dumps({"auth": "password"})}
    sconn = api.j("POST", f"/projects/{pid}/connections", json=draft)
    sid = state["sftp_connection_id"] = sconn["id"]
    save(state)
    seen = api.j("POST", f"/connections/{sid}/test")
    acc = api.j("POST", f"/connections/{sid}/test", json={"expected_fingerprint": seen["host_key"]["fingerprint"],
                                                            "expected_type": seen["host_key"]["type"]})
    check("the SFTP host key is accepted", acc.get("accepted"), acc)
    sds = api.j("POST", f"/projects/{pid}/datasources/sftp", json={"name": "sftp giorni", "connection_id": sid, "folder": "ph/in",
                                                                    "pattern": "sales_{day:yyyyMMdd}.csv", "variables": [days],
                                                                    "timezone": "UTC"})
    run = wait_run(api, f"/datasources/{sds['id']}/runs")
    notes = json.loads(run.get("notes") or "{}")
    check("SFTP: two days read, the missing one in the notes", run.get("status") == "SUCCESS" and run.get("rows_written") == 3
          and notes.get("missing_count") == 1, run)

    print("— API datasource with a range")
    aconn = api.j("POST", f"/projects/{pid}/connections", json={"name": "zz api", "db_type": "api", "host": os.environ["API_BASE"],
                                                                 "username": "", "password": "", "extra": json.dumps({"auth": "none"})})
    state["api_connection_id"] = aconn["id"]
    save(state)
    ads = api.j("POST", f"/projects/{pid}/datasources/api", json={
        "name": "api giorni", "connection_id": aconn["id"], "path": "/echo",
        "query": [{"name": "from", "value": "{day}"}, {"name": "to", "value": "{day_end}"}], "variables": [days]})
    run = wait_run(api, f"/datasources/{ads['id']}/runs")
    check("API: one call per day (3 rows)", run.get("status") == "SUCCESS" and run.get("rows_written") == 3, run)

    print("— a flow: the S3 datasource out to SFTP, S3 and email, in a far zone")
    engines = [e["id"] for e in api.j("GET", "/engines") if e.get("available") and e.get("allowed", True)]
    engine = "polars" if "polars" in engines else engines[0]
    src = {"id": "s", "type": "source", "position": {"x": 0, "y": 0},
           "data": {"datasourceId": dsid, "bucket": state["s3_bucket"], "parquetKey": state["s3_key"]}}
    nodes = [src,
             {"id": "o1", "type": "output", "position": {"x": 300, "y": 0},
              "data": {"destType": "sftp", "connectionId": sid, "sftpFolder": "/config/ph/out/{today:yyyy}/{today:MM}",
                       "sftpFileName": "giorni_{today:yyyyMMdd}_{run_id}", "sftpFormat": "csv", "timezone": FAR}},
             {"id": "o2", "type": "output", "position": {"x": 300, "y": 100},
              "data": {"destType": "s3", "connectionId": s3id, "s3Bucket": "zzph-out",
                       "s3Key": "flussi/{today:yyyy/MM}/{flow}_{date}.parquet", "s3Format": "parquet", "timezone": FAR}}]
    edges = [{"id": "e1", "source": "s", "target": "o1", "targetHandle": "left"},
             {"id": "e2", "source": "s", "target": "o2", "targetHandle": "left"}]
    flow = api.j("POST", f"/projects/{pid}/flows", json={"name": "Giorni fuori", "definition": json.dumps({"nodes": nodes, "edges": edges}),
                                                         "engine": engine})
    fid = state["flow_id"] = flow["id"]
    save(state)
    orch = api.j("POST", f"/flows/{fid}/run-now")
    run = wait_run(api, f"/runs/{orch['run_id']}", 240)
    check("the server-side orchestration (the scheduler's road) succeeds", run.get("status") == "SUCCESS", run)
    outs = [x for x in api.j("GET", f"/flows/{fid}/runs") if x.get("kind") == "flow" and x["id"] > orch["run_id"]]
    far = datetime.now(timezone.utc).astimezone(ZoneInfo(FAR))
    sftp_run = next((x for x in outs if json.loads(x.get("destination") or "{}").get("type") == "sftp"), {})
    path = json.loads(sftp_run.get("destination") or "{}").get("path", "")
    expected = f"/config/ph/out/{far:%Y}/{far:%m}/giorni_{far:%Y%m%d}_{sftp_run.get('id')}.csv"
    check(f"SFTP Output: the file named in {FAR}, in its dated folder, with the run's number", path == expected, (path, expected))
    s3_run = next((x for x in outs if json.loads(x.get("destination") or "{}").get("type") == "s3"), {})
    dest = json.loads(s3_run.get("destination") or "{}")
    expected_key = f"flussi/{far:%Y/%m}/Giorni_fuori_{far:%Y-%m-%d}.parquet"
    check("S3 Output: the key resolved in the zone, recorded on the run", dest.get("written_key") == expected_key
          and dest.get("key") == "flussi/{today:yyyy/MM}/{flow}_{date}.parquet", dest)
    state["sftp_path"], state["s3_written"] = path, expected_key
    save(state)

    print("— the editor's run: the old SFTP placeholders, byte-identical")
    launched = api.j("POST", f"/flows/{fid}/runs", json={
        "bucket": state["s3_bucket"], "input_key": state["s3_key"], "operations": [],
        "destination": {"type": "sftp", "connection_id": sid, "folder": "/config/ph/out", "file_name": "v_{date}_{datetime}_{run_id}_{flow}",
                        "format": "csv"}})
    run = wait_run(api, f"/runs/{launched['id']}")
    path = json.loads(run.get("destination") or "{}").get("path", "")
    m = re.fullmatch(r"/config/ph/out/v_(\d{4}-\d{2}-\d{2})_(\d{4}-\d{2}-\d{2})_(\d{6})_(\d+)_Giorni_fuori\.csv", path)
    started = datetime.fromisoformat(run["started_at"]).astimezone(timezone.utc)
    check("{date}_{datetime}_{run_id}_{flow} as before: UTC date, date_HHMMSS, run, safe flow name",
          bool(m) and m.group(1) == m.group(2) == started.date().isoformat() and m.group(4) == str(launched["id"]), path)
    state["legacy_path"] = path
    save(state)

    print("— a template the engine refuses: refused before the run")
    r = api.r("POST", f"/flows/{fid}/runs", json={
        "bucket": state["s3_bucket"], "input_key": state["s3_key"], "operations": [],
        "destination": {"type": "s3", "connection_id": s3id, "bucket": "zzph-out", "key": "x/{week}.parquet", "format": "parquet"}})
    check("422 with the engine's message, no run", r.status_code == 422 and "unknown placeholder {week}" in r.text, r.text)

    print("— email Output: subject and attachment name in the zone")
    smtp = api.j("POST", f"/projects/{pid}/connections", json={
        "name": "zz smtp", "db_type": "smtp", "host": os.environ["SMTP_HOST"], "port": 1025, "username": "", "password": "",
        "extra": json.dumps({"from_address": "zz@tabularia.test", "tls": "none"})})
    state["smtp_connection_id"] = smtp["id"]
    save(state)
    httpx.delete(f"http://{os.environ['SMTP_HOST']}:8025/api/v1/messages")
    launched = api.j("POST", f"/flows/{fid}/runs", json={
        "bucket": state["s3_bucket"], "input_key": state["s3_key"], "operations": [],
        "email": {"connection_id": smtp["id"], "to": ["capo@tabularia.test"], "subject": "Giorni {flow} {today:dd/MM/yyyy} #{run_id}",
                  "attachment_name": "giorni_{date}", "attachment_format": "csv", "timezone": FAR}})
    run = wait_run(api, f"/runs/{launched['id']}")
    msgs = httpx.get(f"http://{os.environ['SMTP_HOST']}:8025/api/v1/messages").json()["messages"]
    far = datetime.now(timezone.utc).astimezone(ZoneInfo(FAR))
    subject = msgs[0]["Subject"] if msgs else ""
    check("the email left with the subject resolved", run.get("status") == "SUCCESS"
          and subject == f"Giorni Giorni fuori {far:%d/%m/%Y} #{launched['id']}", (run.get("status"), subject, run.get("error")))
    full = httpx.get(f"http://{os.environ['SMTP_HOST']}:8025/api/v1/message/{msgs[0]['ID']}").json() if msgs else {}
    names = [a.get("FileName") for a in full.get("Attachments", [])]
    check("…and the attachment named in the zone", names == [f"giorni_{far:%Y-%m-%d}.csv"], names)
    sent = json.loads(run.get("email") or "{}")
    check("the run records the subject actually sent", sent.get("subject") == subject, sent)


def cleanup_phase(api: Api) -> None:
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
    {"main": main_phase, "cleanup": cleanup_phase}[phase](api)
    bad = [n for n, okay in results if not okay]
    print(f"\n{len(results) - len(bad)}/{len(results)} ok" + (f" — failed: {bad}" if bad else ""))
    sys.exit(1 if bad else 0)
