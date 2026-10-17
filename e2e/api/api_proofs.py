"""End-to-end proof of the «API call» datasource through the gateway API of the dev stack.

Runs INSIDE the gateway container (it has httpx), against the throwaway fake API
(e2e/api/fake_api_server.py, container `zzapi-fake`, names zzapi-a and zzapi-b on
the dev network) and the public Frankfurter API (ECB exchange rates, no key):

    docker cp e2e/api/api_proofs.py dataprep-gateway:/tmp/
    docker exec -e BASE=http://localhost:8000 -e ADMIN_EMAIL=... -e ADMIN_PASSWORD=... \\
        dataprep-gateway python /tmp/api_proofs.py main|frankfurter|cleanup

`main`: a connection per authentication (none, API key in a header and in the query,
bearer, basic, OAuth2 client credentials — with a revoked token refreshed once),
«Test connection», «Try the call», a datasource per pagination type, 429 with
Retry-After, a 5xx then success, redirects (same host keeps the key, another host
gets none), a response over the cap, list variables, append with keys (no
duplicates after two refreshes), placeholders in a chosen time zone and
{last_refresh}, validation at save time, and no secret in any answer or run.
`frankfurter`: connection, try, datasource in append mode keyed by date, two
refreshes. `schedule`: a scheduled refresh. `cleanup` deletes everything the proofs made.
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx

BASE = os.environ.get("BASE", "http://localhost:8000")
FAKE = os.environ.get("FAKE", "http://zzapi-a:8080")
STATE = "/tmp/zz_api_proof_state.json"
SECRETS = ["zz-api-key-123456", "zz-bearer-token-654321", "zz-basic-pw-777", "zz-client-secret-999"]
results: list[tuple[str, bool]] = []


def check(name: str, cond: bool, detail="") -> bool:
    results.append((name, bool(cond)))
    print(("  ok  " if cond else "  KO  ") + name + (f" — {str(detail)[:600]}" if detail and not cond else ""), flush=True)
    return bool(cond)


class Api:
    def __init__(self, token: str | None = None, lang: str = "en"):
        self.h = {"Accept-Language": lang, **({"Authorization": f"Bearer {token}"} if token else {})}

    def r(self, method: str, path: str, **kw) -> httpx.Response:
        with httpx.Client(base_url=BASE, timeout=180, headers=self.h) as c:
            return c.request(method, path, **kw)

    def j(self, method: str, path: str, ok=(200, 201, 202, 204), **kw):
        resp = self.r(method, path, **kw)
        if resp.status_code not in ok:
            raise RuntimeError(f"{method} {path} → {resp.status_code} {resp.text[:600]}")
        return resp.json() if resp.content and "json" in resp.headers.get("content-type", "") else None


def login() -> str:
    return Api().j("POST", "/auth/login", json={"email": os.environ["ADMIN_EMAIL"], "password": os.environ["ADMIN_PASSWORD"]})["access_token"]


def fake(method: str, path: str) -> dict:
    return httpx.request(method, FAKE + path, timeout=30).json()


def save(state: dict) -> None:
    json.dump(state, open(STATE, "w"))


def wait_run(api: Api, ds_id: int, after: int = 0, seconds: int = 180) -> dict:
    end, run = time.time() + seconds, {}
    while time.time() < end:
        runs = api.j("GET", f"/datasources/{ds_id}/runs")
        runs = runs["items"] if isinstance(runs, dict) else runs
        run = next((x for x in runs if x["id"] > after), {}) if runs else {}
        if run.get("status") in ("SUCCESS", "FAILURE"):
            return run
        time.sleep(1)
    return run


def ds_of(api: Api, pid: int, ds_id: int) -> dict:
    return next(d for d in api.j("GET", f"/projects/{pid}/datasources") if d["id"] == ds_id)


def rows_of(api: Api, d: dict, limit: int = 500) -> list[dict]:
    engines = [e["id"] for e in api.j("GET", "/engines") if e.get("available") and e.get("allowed", True)]
    engine = "polars" if "polars" in engines else engines[0]
    prev = api.j("POST", "/tasks/preview", json={"bucket": d["bucket"], "input_key": d["key"], "limit": limit,
                                                 "engine": engine, "operations": []})
    cols = [c["name"] if isinstance(c, dict) else c for c in prev["columns"]]
    return [r if isinstance(r, dict) else dict(zip(cols, r)) for r in prev["rows"]]


def connection(api: Api, pid: int, name: str, auth: str, password: str = "", username: str = "", **extra) -> dict:
    body = {"name": name, "db_type": "api", "host": f"{FAKE}/v1", "username": username, "password": password,
            "extra": json.dumps({"auth": auth, **extra})}
    return api.j("POST", f"/projects/{pid}/connections", json=body)


def datasource(api: Api, pid: int, cid: int, name: str, **call) -> tuple[dict, dict]:
    ds = api.j("POST", f"/projects/{pid}/datasources/api", json={"name": name, "connection_id": cid, **call})
    return ds, wait_run(api, ds["id"])


def no_secret(what: str, payload) -> None:
    text = json.dumps(payload, default=str)
    leaked = [s for s in SECRETS if s in text]
    check(f"no secret in {what}", not leaked, leaked)


def main_phase(api: Api) -> None:
    fake("POST", "/_reset")
    folder = api.j("POST", "/projects", json={"name": f"zzapi-{int(time.time())}"})
    pid = folder["id"]
    state: dict = {"project_id": pid}
    save(state)

    print("— connections, one per authentication")
    draft = {"name": "x", "db_type": "api", "host": f"{FAKE}/v1", "password": "", "extra": json.dumps({"auth": "none"})}
    t = api.j("POST", f"/projects/{pid}/connections/test", json=draft)
    check("the form's test calls the base URL", t["ok"] and t["status"] == 200, t)
    conns = {
        "none": connection(api, pid, "zz none", "none"),
        "keyh": connection(api, pid, "zz key header", "api_key", SECRETS[0], key_name="X-Api-Key", key_in="header"),
        "keyq": connection(api, pid, "zz key query", "api_key", SECRETS[0], key_name="apikey", key_in="query"),
        "bearer": connection(api, pid, "zz bearer", "bearer", SECRETS[1]),
        "basic": connection(api, pid, "zz basic", "basic", SECRETS[2], username="zzuser"),
        "oauth": connection(api, pid, "zz oauth", "oauth2", SECRETS[3], username="zz-client",
                            token_url=f"{FAKE}/oauth/token", scope="read"),
    }
    state["connections"] = {k: c["id"] for k, c in conns.items()}
    save(state)
    no_secret("the connections' answers", conns)
    no_secret("the connection list", api.j("GET", f"/projects/{pid}/connections"))
    wrong = connection(api, pid, "zz wrong key", "api_key", "not-the-key", key_name="X-Api-Key", key_in="header",
                       test_path="/key-header/items")
    r = api.r("POST", f"/connections/{wrong['id']}/test")
    check("a wrong key fails the test with the API's 401", r.status_code == 400 and "401" in r.text, r.text)
    for k in ("keyh", "keyq", "bearer", "basic", "oauth"):
        path = {"keyh": "/key-header/items", "keyq": "/key-query/items", "bearer": "/bearer/items", "basic": "/basic/items",
                "oauth": "/oauth/items"}[k]
        api.j("PATCH", f"/connections/{conns[k]['id']}", json={"extra": json.dumps({**json.loads(conns[k]["extra"]), "test_path": path})})
        res = api.r("POST", f"/connections/{conns[k]['id']}/test")
        check(f"«Test connection» with {k}", res.status_code == 200 and res.json().get("ok"), res.text)

    print("— «Try the call»")
    tr = api.j("POST", f"/connections/{conns['none']['id']}/api/try", json={"path": "/none/items"})
    check("try: the first rows, nested objects as a.b columns, lists as JSON",
          tr["ok"] and tr["columns"] == ["id", "name", "nested.a.b", "tags"] and tr["rows"][0]["tags"] == '["x", "y"]', tr)
    tr = api.j("POST", f"/connections/{conns['keyq']['id']}/api/try", json={"path": "/key-query/items"})
    check("try: the request shows the key in the query masked", tr["ok"] and tr["request"]["url"].endswith("apikey=***"), tr)
    no_secret("the try answer", tr)
    tr = api.j("POST", f"/connections/{conns['none']['id']}/api/try", json={"path": "/nope"})
    check("try: an HTTP error shows the status and the answer", not tr["ok"] and tr["status"] == 404 and "not found" in tr["excerpt"], tr)

    print("— a datasource per authentication")
    made: dict[str, int] = {}
    for k, path in (("none", "/none/items"), ("keyh", "/key-header/items"), ("keyq", "/key-query/items"),
                    ("bearer", "/bearer/items"), ("basic", "/basic/items"), ("oauth", "/oauth/items")):
        ds, run = datasource(api, pid, conns[k]["id"], f"zz auth {k}", path=path)
        made[k] = ds["id"]
        check(f"refresh with {k} succeeds", run.get("status") == "SUCCESS" and run.get("rows_written", 0) >= 1, run)
    state["datasources"] = made
    save(state)
    before = fake("GET", "/_log")["issued"]
    fake("POST", "/_revoke")
    api.j("POST", f"/datasources/{made['oauth']}/refresh")
    run = wait_run(api, made["oauth"], after=0)
    runs = api.j("GET", f"/datasources/{made['oauth']}/runs")
    latest = runs[0] if isinstance(runs, list) else runs["items"][0]
    latest = wait_run(api, made["oauth"], after=latest["id"] - 1)
    issued = fake("GET", "/_log")["issued"]
    check("OAuth2: a revoked token gets a 401, a new token is asked once and the refresh succeeds",
          latest.get("status") == "SUCCESS" and issued == before + 1, (latest, before, issued))

    print("— pagination")
    pages = {
        "page": ({"path": "/pages/page", "response": {"records_path": "data"}, "pagination": {"type": "page", "param": "page"}}, [1, 2, 3, 4, 5]),
        "offset": ({"path": "/pages/offset", "response": {"records_path": "results"},
                    "pagination": {"type": "offset", "param": "offset", "limit_param": "limit", "limit": 3}}, list(range(1, 8))),
        "cursor": ({"path": "/pages/cursor", "response": {"records_path": "items"},
                    "pagination": {"type": "cursor", "param": "cursor", "cursor_path": "meta.next_cursor"}}, [1, 2, 3, 4]),
        "link": ({"path": "/pages/link", "pagination": {"type": "next_link"}}, [1, 2, 3]),
        "nextjson": ({"path": "/pages/nextjson", "response": {"records_path": "rows"},
                      "pagination": {"type": "next_link", "next_path": "links.next"}}, [1, 2, 3]),
    }
    for kind, (call, expected) in pages.items():
        ds, run = datasource(api, pid, conns["none"]["id"], f"zz pages {kind}", **call)
        ids = sorted(r["id"] for r in rows_of(api, ds_of(api, pid, ds["id"]))) if run.get("status") == "SUCCESS" else run
        check(f"pagination {kind}: every page read", ids == expected, ids)

    print("— manners and limits")
    fake("POST", "/_reset")
    ds, run = datasource(api, pid, conns["none"]["id"], "zz 429", path="/flaky/429")
    log = [e for e in fake("GET", "/_log")["log"] if e["path"] == "/v1/flaky/429"]
    check("429 with Retry-After: waited and retried, then success", run.get("status") == "SUCCESS" and len(log) == 2, (run, len(log)))
    ds, run = datasource(api, pid, conns["none"]["id"], "zz 503", path="/flaky/503")
    check("a 5xx then success", run.get("status") == "SUCCESS" and rows_of(api, ds_of(api, pid, ds["id"]))[0]["attempt"] == 2, run)
    fake("POST", "/_reset")
    ds, run = datasource(api, pid, conns["keyh"]["id"], "zz redirect other", path="/redirect/other")
    landing = [e for e in fake("GET", "/_log")["log"] if e["path"] == "/landing"]
    check("a redirect to another host is followed", run.get("status") == "SUCCESS", run)
    check("…and the other host receives no credential nor header of the connection",
          landing and landing[0]["host"] == "zzapi-b" and "x-api-key" not in landing[0]["headers"]
          and "authorization" not in landing[0]["headers"] and not rows_of(api, ds_of(api, pid, ds["id"]))[0]["had_auth"], landing)
    ds, run = datasource(api, pid, conns["keyh"]["id"], "zz redirect same", path="/redirect/same")
    check("a redirect on the same host keeps the key", run.get("status") == "SUCCESS", run)
    ds, run = datasource(api, pid, conns["none"]["id"], "zz big", path="/big")
    check("a response over the cap fails the refresh, naming the limit",
          run.get("status") == "FAILURE" and "API__MAX_RESPONSE_BYTES" in (run.get("error") or ""), run.get("error"))

    print("— servers that copy the request (and the key) into an address on another host")
    for path, call, ok_rows in (
        ("/leak/redirect-query", {}, 1),
        ("/leak/next-json", {"response": {"records_path": "rows"}, "pagination": {"type": "next_link", "next_path": "next"}}, 2),
        ("/leak/link", {"pagination": {"type": "next_link"}}, 2),
    ):
        fake("POST", "/_reset")
        ds, run = datasource(api, pid, conns["keyq"]["id"], f"zz leak {path.rsplit('/', 1)[-1]}", path=path, **call)
        on_b = [e for e in fake("GET", "/_log")["log"] if e["host"] == "zzapi-b"]
        check(f"{path}: followed to the other host without the key in the query",
              run.get("status") == "SUCCESS" and run.get("rows_written") == ok_rows and on_b
              and not any(SECRETS[0] in json.dumps(e) for e in on_b), (run.get("error"), on_b))
    fake("POST", "/_reset")
    ds, run = datasource(api, pid, conns["keyq"]["id"], "zz leak path", path="/leak/redirect-path")
    on_b = [e for e in fake("GET", "/_log")["log"] if e["host"] == "zzapi-b"]
    check("a redirect with the key in the path is not followed, and says so",
          run.get("status") == "FAILURE" and "holds the key" in (run.get("error") or "") and not on_b, (run.get("error"), on_b))
    odd = connection(api, pid, "zz odd secret", "bearer", 'zz"odd\\sè/cret-77')
    tr = api.j("POST", f"/connections/{odd['id']}/api/try", json={"path": "/echo-auth", "response": {"records_path": "items"}})
    blob = json.dumps(tr, ensure_ascii=False)
    check("try: an API that echoes an odd secret (quotes, backslash, accent, slash) shows *** in rows and excerpt",
          tr["ok"] and "***" in json.dumps(tr["rows"]) and "***" in tr["excerpt"] and "odd" not in blob, tr)
    ds, run = datasource(api, pid, conns["none"]["id"], "zz pages capped", path="/pages/capped", response={"records_path": "results"},
                         pagination={"type": "offset", "param": "offset", "limit_param": "limit", "limit": 5})
    check("offset pagination with an API that caps the page size reads every row",
          run.get("status") == "SUCCESS" and run.get("rows_written") == 7, run)
    ds, run = datasource(api, pid, conns["none"]["id"], "zz pages self", path="/pages/self", response={"records_path": "rows"},
                         pagination={"type": "next_link", "next_path": "next"})
    check("a next link back to a page already read fails instead of duplicating it",
          run.get("status") == "FAILURE" and "already read" in (run.get("error") or ""), run.get("error"))

    print("— list variables")
    ds, run = datasource(api, pid, conns["none"]["id"], "zz quotes", path="/quote/{symbol}",
                         variables=[{"name": "symbol", "values": ["AAPL", "MSFT", "ENI.MI"]}],
                         response={"records_path": "prices", "keyed": True})
    rows = rows_of(api, ds_of(api, pid, ds["id"])) if run.get("status") == "SUCCESS" else []
    check("one call per value, the value in _param_symbol", len(rows) == 9 and {r["_param_symbol"] for r in rows} == {"AAPL", "MSFT", "ENI.MI"}
          and list(rows[0])[0] == "_key", rows[:3] or run)

    print("— append with keys")
    fake("POST", "/_reset")
    call = {"path": "/series", "query": [{"name": "symbol", "value": "{symbol}"}], "variables": [{"name": "symbol", "values": ["A", "B"]}],
            "response": {"records_path": "rows"}, "mode": "append", "keys": ["date", "_param_symbol"]}
    ds, run = datasource(api, pid, conns["none"]["id"], "zz series", **call)
    first = rows_of(api, ds_of(api, pid, ds["id"])) if run.get("status") == "SUCCESS" else []
    check("append, first refresh", len(first) == 4, first or run)
    api.j("POST", f"/datasources/{ds['id']}/refresh")
    run = wait_run(api, ds["id"], after=run["id"])
    second = rows_of(api, ds_of(api, pid, ds["id"])) if run.get("status") == "SUCCESS" else []
    keys = [(r["date"], r["_param_symbol"]) for r in second]
    check("append, second refresh: replaced and added, no duplicates",
          len(second) == 6 and len(set(keys)) == 6 and {r["close"] for r in second if r["date"] == "2026-10-02"} == {11.5}, second or run)

    print("— placeholders in a time zone, and {last_refresh}")
    tz = ZoneInfo("Pacific/Kiritimati")  # UTC+14: «today» there is often not today in UTC
    call = {"path": "/echo", "timezone": "Pacific/Kiritimati", "last_refresh_initial": "today-7",
            "query": [{"name": "from", "value": "{today-1m:%Y%m%d}"}, {"name": "since", "value": "{last_refresh}"}],
            "headers": [{"name": "X-Day", "value": "{yesterday}"}]}
    ds, run = datasource(api, pid, conns["none"]["id"], "zz echo", **call)
    now = datetime.now(timezone.utc).astimezone(tz)
    row = rows_of(api, ds_of(api, pid, ds["id"]))[0] if run.get("status") == "SUCCESS" else {}
    q = json.loads(row.get("query") or "{}") if isinstance(row.get("query"), str) else {}
    today = now.date()
    month_ago = (today.replace(day=1) - timedelta(days=1)).replace(day=min(today.day, (today.replace(day=1) - timedelta(days=1)).day))
    check("placeholders resolved in the chosen time zone",
          row.get("query.from") == month_ago.strftime("%Y%m%d") and row.get("x_day") == str(today - timedelta(days=1)), row or run)
    check("{last_refresh} on the first refresh = the initial value (midnight in the zone)",
          row.get("query.since") == f"{today - timedelta(days=7)}T00:00:00+14:00", row)
    first_start = datetime.fromisoformat(run["started_at"].replace("Z", "+00:00"))
    api.j("POST", f"/datasources/{ds['id']}/refresh")
    run2 = wait_run(api, ds["id"], after=run["id"])
    row = rows_of(api, ds_of(api, pid, ds["id"]))[0] if run2.get("status") == "SUCCESS" else {}
    since = datetime.fromisoformat(row.get("query.since", "1970-01-01T00:00:00+00:00"))
    check("{last_refresh} on the next refresh = the start of the previous one, in the zone",
          abs((since - first_start).total_seconds()) < 1.5 and row.get("query.since", "").endswith("+14:00"), (row, run["started_at"]))

    print("— refused at save time")
    for name, call, sign in (("bad placeholder", {"path": "/x/{tomorrow}"}, "tomorrow"),
                             ("unused variable", {"path": "/x", "variables": [{"name": "s", "values": ["a"]}]}, "not used"),
                             ("append without keys", {"path": "/x", "mode": "append"}, "key")):
        r = api.r("POST", f"/projects/{pid}/datasources/api", json={"name": f"zz {name}", "connection_id": conns["none"]["id"], **call})
        check(f"{name}: 422 with the reason", r.status_code == 422 and sign in r.text, r.text)

    print("— secrets")
    every = []
    for d in api.j("GET", f"/projects/{pid}/datasources"):
        runs = api.j("GET", f"/datasources/{d['id']}/runs")
        every += runs if isinstance(runs, list) else runs["items"]
    no_secret("any run (error, error_detail, destination)", every)
    no_secret("the datasources", api.j("GET", f"/projects/{pid}/datasources"))
    audit = api.j("GET", "/audit", params={"limit": 200})
    no_secret("the audit log", audit)


def frankfurter_phase(api: Api) -> None:
    state = json.load(open(STATE))
    pid = state["project_id"]
    print("— Frankfurter (ECB rates), public API without a key")
    body = {"name": "zz frankfurter", "db_type": "api", "host": "https://api.frankfurter.dev/v1", "password": "",
            "extra": json.dumps({"auth": "none", "test_path": "/latest"})}
    t = api.j("POST", f"/projects/{pid}/connections/test", json=body)
    check("Frankfurter: test connection", t["ok"] and t["status"] == 200, t)
    conn = api.j("POST", f"/projects/{pid}/connections", json=body)
    call = {"path": "/{today-30}..", "query": [{"name": "base", "value": "EUR"}, {"name": "symbols", "value": "USD,CHF"}],
            "timezone": "Europe/Rome", "response": {"records_path": "rates", "keyed": True}, "mode": "append", "keys": ["_key"]}
    tr = api.j("POST", f"/connections/{conn['id']}/api/try", json=call)
    check("Frankfurter: try shows one row per date with USD and CHF",
          tr["ok"] and tr["columns"] == ["_key", "CHF", "USD"] and tr["records"] >= 15 and "{" not in tr["request"]["url"], tr)
    print(f"  try: {tr['request']['method']} {tr['request']['url']} → {tr['status']}, {tr['records']} rows; first {tr['rows'][0]}")
    ds, run = datasource(api, pid, conn["id"], "zz ecb rates", **call)
    first = rows_of(api, ds_of(api, pid, ds["id"])) if run.get("status") == "SUCCESS" else []
    check("Frankfurter: the first refresh writes a row per business day", len(first) >= 15 and all(r["USD"] for r in first), first[:2] or run)
    api.j("POST", f"/datasources/{ds['id']}/refresh")
    run = wait_run(api, ds["id"], after=run["id"])
    second = rows_of(api, ds_of(api, pid, ds["id"])) if run.get("status") == "SUCCESS" else []
    keys = [r["_key"] for r in second]
    check("Frankfurter: append twice, every date once", len(second) >= len(first) and len(keys) == len(set(keys)), (len(first), len(second)))
    d = ds_of(api, pid, ds["id"])
    print(f"  datasource «{d['name']}»: {d['rows']} rows, columns {[c['name'] for c in d['columns']]}, last {sorted(keys)[-1] if keys else None}")
    state["frankfurter"] = {"connection_id": conn["id"], "datasource_id": ds["id"]}
    save(state)


def schedule_phase(api: Api) -> None:
    """A scheduled refresh goes down the same road: a cron every minute on one of the
    datasources of `main`, a run triggered by the scheduler, then the schedule removed."""
    state = json.load(open(STATE))
    ds_id = state["datasources"]["keyq"]
    runs = api.j("GET", f"/datasources/{ds_id}/runs")
    last = max(r["id"] for r in (runs if isinstance(runs, list) else runs["items"]))
    api.j("PUT", f"/datasources/{ds_id}/schedule", json={"cron": "* * * * *"})
    try:
        end, run = time.time() + 200, {}
        while time.time() < end:
            runs = api.j("GET", f"/datasources/{ds_id}/runs")
            new = [r for r in (runs if isinstance(runs, list) else runs["items"]) if r["id"] > last]
            if new and new[0].get("status") in ("SUCCESS", "FAILURE"):
                run = new[0]
                break
            time.sleep(3)
        check("a scheduled refresh runs and succeeds", run.get("status") == "SUCCESS" and run.get("trigger_type") == "schedule", run)
    finally:
        api.j("PUT", f"/datasources/{ds_id}/schedule", json={"cron": ""})


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
    {"main": main_phase, "frankfurter": frankfurter_phase, "schedule": schedule_phase,
     "cleanup": cleanup_phase}[phase](api)
    bad = [n for n, okay in results if not okay]
    print(f"\n{len(results) - len(bad)}/{len(results)} ok" + (f" — failed: {bad}" if bad else ""))
    sys.exit(1 if bad else 0)
