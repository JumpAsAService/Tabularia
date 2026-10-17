"""A programmable HTTP API that runs in the test process, so the client speaks
real HTTP (requests, redirects, headers, chunked bodies): what the tests fix is
our side of the contract.

    with FakeApi() as api:
        api.route("GET", "/items", lambda req: (200, {}, [{"id": 1}]))
        api.url("/items")             # http://127.0.0.1:<port>/items
        api.other_host_url("/x")      # the same server as http://localhost:<port>/x
        api.log                       # every request received, headers included

A handler returns (status, headers, body); a body that is not bytes/str is sent
as JSON. Handlers can keep state (a counter for «429 then 200»).
"""
from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import parse_qsl, urlsplit


@dataclass
class Req:
    method: str
    path: str
    query: list[tuple[str, str]]
    headers: dict[str, str]
    body: bytes
    host: str
    raw_path: str = ""
    extra: dict = field(default_factory=dict)

    def q(self, name: str, default: str | None = None) -> str | None:
        for k, v in self.query:
            if k == name:
                return v
        return default

    def json(self) -> Any:
        return json.loads(self.body or b"null")


Handler = Callable[[Req], tuple]


class FakeApi:
    def __init__(self) -> None:
        self.routes: dict[tuple[str, str], Handler] = {}
        self.log: list[Req] = []
        api = self

        class _H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):  # quiet
                pass

            def _serve(self):
                parts = urlsplit(self.path)
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n) if n else b""
                req = Req(method=self.command, path=parts.path, query=parse_qsl(parts.query, keep_blank_values=True),
                          headers={k.lower(): v for k, v in self.headers.items()}, body=body,
                          host=self.headers.get("Host", ""), raw_path=self.path)
                api.log.append(req)
                handler = api.routes.get((self.command, parts.path)) or api.routes.get(("*", parts.path))
                if handler is None:
                    status, headers, payload = 404, {}, {"error": "not found"}
                else:
                    status, headers, payload = handler(req)
                if hasattr(payload, "__next__"):  # a generator of chunks: streamed as they come, then closed
                    self.send_response(status)
                    for k, v in headers.items():
                        self.send_header(k, v)
                    self.send_header("Connection", "close")
                    self.end_headers()
                    try:
                        for chunk in payload:
                            self.wfile.write(chunk)
                            self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError):
                        pass
                    self.close_connection = True
                    return
                if isinstance(payload, (bytes, bytearray)):
                    data = bytes(payload)
                elif isinstance(payload, str):
                    data = payload.encode()
                else:
                    data = json.dumps(payload).encode()
                    headers = {"Content-Type": "application/json", **headers}
                self.send_response(status)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(data)

            do_GET = do_POST = do_PUT = do_HEAD = _serve

        class _Server(ThreadingHTTPServer):
            daemon_threads = True

            def handle_error(self, request, client_address):  # a client that hung up (a cap hit): not news
                pass

        self._server = _Server(("127.0.0.1", 0), _H)
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, kwargs={"poll_interval": 0.02},
                                        daemon=True)

    def __enter__(self) -> "FakeApi":
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._server.shutdown()
        self._server.server_close()

    def route(self, method: str, path: str, handler: Handler) -> None:
        self.routes[(method, path)] = handler

    def url(self, path: str = "") -> str:
        return f"http://127.0.0.1:{self.port}{path}"

    def other_host_url(self, path: str = "") -> str:
        """The same server under another host name: another origin for the client."""
        return f"http://localhost:{self.port}{path}"

    def requests_to(self, path: str) -> list[Req]:
        return [r for r in self.log if r.path == path]
