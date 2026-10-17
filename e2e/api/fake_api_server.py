"""A throwaway HTTP API for the real proofs of the «API call» datasource (stdlib only).

Run it in a container on the dev network under TWO names, so that a redirect to
the other name is a redirect to another host:

    docker run -d --name zzapi-fake --network dataprep-network \\
        --network-alias zzapi-a --network-alias zzapi-b \\
        -v "$PWD/e2e/api:/srv:ro" python:3.12-slim python -u /srv/fake_api_server.py

Every request is logged (method, host, path, query, headers) and served back on
GET /_log; POST /_reset clears the log and the counters. The credentials it
accepts are below; nothing here is a real secret.
"""
from __future__ import annotations

import base64
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qsl, urlsplit

API_KEY = "zz-api-key-123456"
TOKEN = "zz-bearer-token-654321"
USER, PASSWORD = "zzuser", "zz-basic-pw-777"
CLIENT_ID, CLIENT_SECRET = "zz-client", "zz-client-secret-999"
PORT = 8080

lock = threading.Lock()
state: dict = {}


def reset() -> None:
    with lock:
        state.clear()
        state.update(log=[], counters={}, tokens=set(), issued=0, series_version=0)


reset()


def bump(name: str) -> int:
    with lock:
        state["counters"][name] = state["counters"].get(name, 0) + 1
        return state["counters"][name]


SYMBOLS = {"AAPL": 230.0, "MSFT": 410.0, "ENI.MI": 14.5}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    # -- helpers --
    def send(self, status: int, payload, headers: dict | None = None, content_type: str = "application/json") -> None:
        data = payload if isinstance(payload, bytes) else (payload.encode() if isinstance(payload, str) else json.dumps(payload).encode())
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def q(self, name: str, default=None):
        return dict(self.query).get(name, default)

    def denied(self) -> None:
        self.send(401, {"error": "unauthorized"}, {"WWW-Authenticate": "Bearer"})

    def handle_any(self) -> None:
        parts = urlsplit(self.path)
        self.query = parse_qsl(parts.query, keep_blank_values=True)
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n) if n else b""
        host = (self.headers.get("Host") or "").split(":")[0]
        entry = {"method": self.command, "host": host, "path": parts.path, "query": self.query,
                 "headers": {k.lower(): v for k, v in self.headers.items()}, "body": body.decode("utf-8", "replace")}
        p = parts.path
        if p not in ("/_log", "/_reset"):
            with lock:
                state["log"].append(entry)
        h = entry["headers"]

        if p == "/_log":
            with lock:
                return self.send(200, {"log": state["log"], "issued": state["issued"], "counters": state["counters"]})
        if p == "/_reset":
            reset()
            return self.send(200, {"ok": True})
        if p == "/_revoke":  # every OAuth2 token stops working: the next call gets a 401
            with lock:
                state["tokens"].clear()
            return self.send(200, {"ok": True})

        # ── authentication ──
        if p == "/v1":
            return self.send(200, {"name": "zz fake api", "ok": True})
        if p == "/v1/none/items":
            return self.send(200, [{"id": 1, "name": "alpha", "nested": {"a": {"b": 1.5}}, "tags": ["x", "y"]},
                                   {"id": 2, "name": "beta", "nested": {"a": {"b": 2}}, "tags": []}])
        if p == "/v1/key-header/items":
            return self.send(200, [{"id": 1}]) if h.get("x-api-key") == API_KEY else self.denied()
        if p == "/v1/key-query/items":
            return self.send(200, [{"id": 1}]) if self.q("apikey") == API_KEY else self.denied()
        if p == "/v1/bearer/items":
            return self.send(200, [{"id": 1}]) if h.get("authorization") == f"Bearer {TOKEN}" else self.denied()
        if p == "/v1/basic/items":
            ok = h.get("authorization") == "Basic " + base64.b64encode(f"{USER}:{PASSWORD}".encode()).decode()
            return self.send(200, [{"id": 1}]) if ok else self.denied()
        if p == "/oauth/token" and self.command == "POST":
            form = dict(parse_qsl(body.decode()))
            if form.get("grant_type") != "client_credentials" or form.get("client_id") != CLIENT_ID \
                    or form.get("client_secret") != CLIENT_SECRET:
                return self.send(401, {"error": "invalid_client"})
            with lock:
                state["issued"] += 1
                token = f"zz-access-{state['issued']}"
                state["tokens"].add(token)
            return self.send(200, {"access_token": token, "token_type": "Bearer", "expires_in": 3600})
        if p == "/v1/oauth/items":
            auth = h.get("authorization", "")
            with lock:
                ok = auth.startswith("Bearer ") and auth[7:] in state["tokens"]
            return self.send(200, [{"id": 1, "token": auth[7:]}]) if ok else self.denied()

        # ── pagination ──
        if p == "/v1/pages/page":
            page = int(self.q("page", "1"))
            data = {1: [{"id": 1}, {"id": 2}], 2: [{"id": 3}, {"id": 4}], 3: [{"id": 5}]}.get(page, [])
            return self.send(200, {"data": data, "page": page})
        if p == "/v1/pages/offset":
            rows = [{"id": i} for i in range(1, 8)]
            o, lim = int(self.q("offset", "0")), int(self.q("limit", "3"))
            return self.send(200, {"results": rows[o:o + lim]})
        if p == "/v1/pages/cursor":
            pages = {None: ([{"id": 1}, {"id": 2}], "c2"), "c2": ([{"id": 3}], "c3"), "c3": ([{"id": 4}], None)}
            items, nxt = pages.get(self.q("cursor"), ([], None))
            return self.send(200, {"items": items, "meta": {"next_cursor": nxt}})
        if p == "/v1/pages/link":
            page = int(self.q("page", "1"))
            headers = {"Link": f'<http://{host}:{PORT}/v1/pages/link?page={page + 1}>; rel="next"'} if page < 3 else {}
            return self.send(200, [{"id": page}], headers)
        if p == "/v1/pages/nextjson":
            page = int(self.q("p", "1"))
            nxt = f"/v1/pages/nextjson?p={page + 1}" if page < 3 else None
            return self.send(200, {"rows": [{"id": page}], "links": {"next": nxt}})

        # ── manners ──
        if p == "/v1/flaky/429":
            if bump(p) == 1:
                return self.send(429, {"error": "slow down"}, {"Retry-After": "2"})
            return self.send(200, [{"id": 1, "attempt": state["counters"][p]}])
        if p == "/v1/flaky/503":
            if bump(p) == 1:
                return self.send(503, b"temporarily down", content_type="text/plain")
            return self.send(200, [{"id": 1, "attempt": state["counters"][p]}])
        if p == "/v1/redirect/other":
            return self.send(302, b"", {"Location": f"http://zzapi-b:{PORT}/landing"})
        if p == "/v1/redirect/same":
            return self.send(302, b"", {"Location": "/v1/key-header/items"})
        if p == "/landing":
            return self.send(200, [{"landed": True, "had_auth": "authorization" in h or "x-api-key" in h
                                    or "apikey" in dict(self.query)}])
        if p == "/v1/big":
            # 120 MB declared: over the default per-response cap (API__MAX_RESPONSE_BYTES = 100 MB)
            size = 120 * 1024 * 1024
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(size))
            self.end_headers()
            try:
                chunk = b" " * 65536
                for _ in range(size // 65536):
                    self.wfile.write(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass
            return

        # ── servers that copy the request into the next address (review 2026-10-18) ──
        other = "zzapi-b" if host == "zzapi-a" else "zzapi-a"
        if p == "/v1/leak/redirect-query":  # nginx's `return 301 http://other$request_uri`
            if host == "zzapi-b":
                return self.send(200, [{"landed": "query"}])
            return self.send(301, b"", {"Location": f"http://{other}:{PORT}{self.path}"})
        if p == "/v1/leak/redirect-path":
            return self.send(302, b"", {"Location": f"http://{other}:{PORT}/landing/{self.q('apikey', 'none')}"})
        if p == "/v1/leak/next-json":
            if host == "zzapi-b":
                return self.send(200, {"rows": [{"id": 2}], "next": None})
            return self.send(200, {"rows": [{"id": 1}], "next": f"http://{other}:{PORT}{self.path}"})
        if p == "/v1/leak/link":
            if host == "zzapi-b":
                return self.send(200, [{"id": 2}])
            return self.send(200, [{"id": 1}], {"Link": f'<http://{other}:{PORT}{self.path}>; rel="next"'})
        if p == "/v1/echo-auth":  # echoes the credentials, JSON-escaped
            return self.send(200, {"items": [{"auth": h.get("authorization"), "key": h.get("x-api-key")}]})
        if p == "/v1/pages/capped":  # the API caps the page size at 2, whatever limit is asked
            rows = [{"id": i} for i in range(1, 8)]
            o, lim = int(self.q("offset", "0")), min(int(self.q("limit", "5")), 2)
            return self.send(200, {"results": rows[o:o + lim]})
        if p == "/v1/pages/self":
            return self.send(200, {"rows": [{"id": 1}], "next": f"http://{host}:{PORT}{self.path}"})

        # ── list variables, append, placeholders ──
        if p.startswith("/v1/quote/"):
            symbol = p.rsplit("/", 1)[-1]
            base = SYMBOLS.get(symbol)
            if base is None:
                return self.send(404, {"error": f"unknown symbol {symbol}"})
            return self.send(200, {"symbol": symbol, "prices": {"2026-10-14": {"close": base}, "2026-10-15": {"close": base + 1},
                                                                "2026-10-16": {"close": base + 2}}})
        if p == "/v1/series":
            with lock:
                state["series_version"] += 1
                v = state["series_version"]
            symbol = self.q("symbol", "?")
            if v <= 2:  # the first refresh (two symbols = two calls)
                rows = [{"date": "2026-10-01", "close": 10.0}, {"date": "2026-10-02", "close": 11.0}]
            else:  # later: one day corrected, one new
                rows = [{"date": "2026-10-02", "close": 11.5}, {"date": "2026-10-03", "close": 12.0}]
            return self.send(200, {"symbol": symbol, "rows": rows})
        if p == "/v1/echo":
            return self.send(200, [{"query": dict(self.query), "x_day": h.get("x-day"), "body": body.decode() or None}])

        return self.send(404, {"error": "not found", "path": p})

    do_GET = do_POST = do_HEAD = handle_any


if __name__ == "__main__":
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    server.daemon_threads = True
    print(f"fake API on :{PORT}", flush=True)
    server.serve_forever()
