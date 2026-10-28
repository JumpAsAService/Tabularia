"""A fake Google, faithful to the part of the contract the Google Sheets source uses:
the token endpoint with a service account's signed JWT (the signature checked against
the account's public key), Drive's file list (paged), a spreadsheet's metadata, the
values of a range (UNFORMATTED_VALUE, trailing empty rows and cells left out, as
Google does), the number formats of a range, and the refusals — an API not enabled,
a spreadsheet not shared or missing, a revoked key, a quota (429 + Retry-After), an
Excel file stored in Drive.

There are no real credentials in development: this is what the connector is tested
against. `request` has the signature of `requests.Session().request`; `serve()` puts
the same fake behind a local HTTP server, for the proofs in a browser."""
from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass, field
from urllib.parse import parse_qs, unquote, urlsplit

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

TOKEN_URL = "https://oauth2.googleapis.com/token"
SHEETS = "https://sheets.googleapis.com/v4/spreadsheets"
DRIVE = "https://www.googleapis.com/drive/v3/files"
READONLY = {"https://www.googleapis.com/auth/spreadsheets.readonly",
            "https://www.googleapis.com/auth/drive.metadata.readonly"}
EMAIL = "tabularia-reader@acme-data.iam.gserviceaccount.com"
PROJECT = "acme-data"

_PRIVATE = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PRIVATE_PEM = _PRIVATE.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                     serialization.NoEncryption()).decode()


def service_account_json(**overrides) -> str:
    """A service account's key file, as Google's console downloads it (made up)."""
    key = {
        "type": "service_account", "project_id": PROJECT, "private_key_id": "0123456789abcdef0123456789abcdef01234567",
        "private_key": PRIVATE_PEM, "client_email": EMAIL, "client_id": "123456789012345678901",
        "auth_uri": "https://accounts.google.com/o/oauth2/auth", "token_uri": TOKEN_URL,
        "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
        "client_x509_cert_url": f"https://www.googleapis.com/robot/v1/metadata/x509/{EMAIL}",
        "universe_domain": "googleapis.com",
    }
    key.update(overrides)
    return json.dumps(key)


@dataclass
class Tab:
    title: str
    rows: list[list] = field(default_factory=list)
    sheet_id: int = 0
    # {column index: number format type} for every cell of that column below the header
    formats: dict[int, str] = field(default_factory=dict)
    grid_rows: int | None = None  # the grid's size; default: the rows written + 100 empty
    grid_columns: int | None = None
    chart: bool = False


@dataclass
class Book:
    title: str
    tabs: list[Tab]
    modified: str = "2026-10-20T08:30:00.000Z"
    excel: bool = False  # an .xlsx stored in Drive: the Sheets API refuses it


class _Resp:
    def __init__(self, status=200, body=None, headers=None):
        self.status_code = status
        self._body = body
        self.headers = headers or {}
        self.text = json.dumps(body) if body is not None else ""

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


def _error(code: int, status: str, message: str, details: list | None = None) -> _Resp:
    return _Resp(code, {"error": {"code": code, "message": message, "status": status, **({"details": details} if details else {})}})


def _disabled(service: str) -> _Resp:
    name = "Google Sheets API" if service.startswith("sheets") else "Google Drive API"
    return _error(403, "PERMISSION_DENIED",
                  f"{name} has not been used in project 123456 before or it is disabled. Enable it by visiting "
                  f"https://console.developers.google.com/apis/api/{service}/overview?project=123456 then retry.",
                  [{"@type": "type.googleapis.com/google.rpc.ErrorInfo", "reason": "SERVICE_DISABLED",
                    "domain": "googleapis.com", "metadata": {"service": service, "consumer": "projects/123456"}}])


def _b64decode(part: str) -> bytes:
    return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))


def _col(letters: str) -> int:
    n = 0
    for ch in letters.upper():
        n = n * 26 + ord(ch) - 64
    return n


def split_a1(a1: str) -> tuple[str, str]:
    """'It''s'!A1:B2 → (It's, A1:B2)."""
    if a1.startswith("'"):
        i, title = 1, ""
        while True:
            j = a1.index("'", i)
            if a1[j + 1:j + 2] == "'":
                title += a1[i:j] + "'"
                i = j + 2
                continue
            title += a1[i:j]
            return title, a1[j + 2:]
    title, _, cells = a1.partition("!")
    return title, cells


class FakeGoogle:
    def __init__(self, books: dict[str, Book], *, shared: set[str] | None = None, sheets_api=True, drive_api=True,
                 revoked=False, drive_page_size=100, public_key=None):
        self.books = books
        self.shared = set(books) if shared is None else shared
        self.sheets_api, self.drive_api, self.revoked = sheets_api, drive_api, revoked
        self.drive_page_size = drive_page_size
        self.public_key = public_key or _PRIVATE.public_key()
        self.calls: list[tuple[str, str]] = []
        self.tokens = 0
        self.assertions: list[dict] = []  # the JWT claims received
        self.throttle_next = 0  # the next N requests answer 429

    # -- transport --
    def request(self, method: str, url: str, params=None, data=None, headers=None, **kw):
        if params:
            from urllib.parse import urlencode

            url = url + ("&" if "?" in url else "?") + urlencode(params, doseq=True)
        self.calls.append((method, url))
        if self.throttle_next:
            self.throttle_next -= 1
            return _Resp(429, {"error": {"code": 429, "message": "Quota exceeded", "status": "RESOURCE_EXHAUSTED"}},
                         {"Retry-After": "1"})
        if url.startswith(TOKEN_URL):
            return self._token(data or {})
        if (headers or {}).get("Authorization") != f"Bearer tok-{self.tokens}" or not self.tokens:
            return _error(401, "UNAUTHENTICATED", "Request had invalid authentication credentials.")
        parts = urlsplit(url)
        query = {k: v for k, v in parse_qs(parts.query).items()}
        if url.startswith(DRIVE):
            return self._drive(query)
        if url.startswith(SHEETS):
            return self._sheets(unquote(parts.path[len("/v4/spreadsheets/"):]) if "/values/" not in parts.path
                                else parts.path[len("/v4/spreadsheets/"):], query)
        return _error(404, "NOT_FOUND", "unknown endpoint")

    def _token(self, form: dict) -> _Resp:
        if form.get("grant_type") != "urn:ietf:params:oauth:grant-type:jwt-bearer":
            return _Resp(400, {"error": "unsupported_grant_type"})
        try:
            header, claims, signature = form["assertion"].split(".")
            self.public_key.verify(_b64decode(signature), f"{header}.{claims}".encode(), padding.PKCS1v15(), hashes.SHA256())
        except Exception:  # noqa: BLE001
            return _Resp(400, {"error": "invalid_grant", "error_description": "Invalid JWT Signature."})
        payload = json.loads(_b64decode(claims))
        self.assertions.append(payload)
        if self.revoked:
            return _Resp(400, {"error": "invalid_grant", "error_description": "Invalid JWT Signature."})
        if payload.get("aud") != TOKEN_URL or not set(payload.get("scope", "").split()) <= READONLY:
            return _Resp(400, {"error": "invalid_scope"})
        self.tokens += 1
        return _Resp(200, {"access_token": f"tok-{self.tokens}", "expires_in": 3599, "token_type": "Bearer"})

    def _drive(self, query: dict) -> _Resp:
        if not self.drive_api:
            return _disabled("drive.googleapis.com")
        assert "application/vnd.google-apps.spreadsheet" in query["q"][0]
        visible = sorted((i for i in self.books if i in self.shared and not self.books[i].excel), key=str)
        start = int(query.get("pageToken", ["0"])[0])
        page = visible[start:start + self.drive_page_size]
        body = {"files": [{"id": i, "name": self.books[i].title, "modifiedTime": self.books[i].modified,
                           "owners": [{"displayName": "Anna Rossi", "emailAddress": "anna@acme.example"}]} for i in page]}
        if start + self.drive_page_size < len(visible):
            body["nextPageToken"] = str(start + self.drive_page_size)
        return _Resp(200, body)

    def _book(self, sid: str) -> Book | _Resp:
        if not self.sheets_api:
            return _disabled("sheets.googleapis.com")
        if sid not in self.books:
            return _error(404, "NOT_FOUND", "Requested entity was not found.")
        if sid not in self.shared:
            return _error(403, "PERMISSION_DENIED", "The caller does not have permission")
        book = self.books[sid]
        if book.excel:
            return _error(400, "FAILED_PRECONDITION", "This operation is not supported for this document")
        return book

    def _sheets(self, rest: str, query: dict) -> _Resp:
        if "/values/" in rest:
            sid, a1 = rest.split("/values/", 1)
            book = self._book(unquote(sid))
            return book if isinstance(book, _Resp) else self._values(book, unquote(a1))
        book = self._book(rest)
        if isinstance(book, _Resp):
            return book
        if "ranges" in query:
            return self._formats(book, query["ranges"][0])
        return _Resp(200, {"spreadsheetId": rest, "properties": {"title": book.title}, "sheets": [
            {"properties": {"sheetId": t.sheet_id, "title": t.title, "index": i,
                            "sheetType": "OBJECT" if t.chart else "GRID",
                            **({} if t.chart else {"gridProperties": {"rowCount": self._grid(t)[0], "columnCount": self._grid(t)[1]}})}}
            for i, t in enumerate(book.tabs)]})

    @staticmethod
    def _grid(t: Tab) -> tuple[int, int]:
        return (t.grid_rows if t.grid_rows is not None else len(t.rows) + 100,
                t.grid_columns if t.grid_columns is not None else max([26] + [len(r) for r in t.rows]))

    def _range(self, book: Book, a1: str) -> tuple[Tab, int, int, int, int] | _Resp:
        title, cells = split_a1(a1)
        tab = next((t for t in book.tabs if t.title == title), None)
        m = re.match(r"^([A-Z]+)(\d+):([A-Z]+)(\d+)$", cells)
        if tab is None or m is None:
            return _error(400, "INVALID_ARGUMENT", f"Unable to parse range: {a1}")
        return tab, _col(m.group(1)), int(m.group(2)), _col(m.group(3)), int(m.group(4))

    def _values(self, book: Book, a1: str) -> _Resp:
        found = self._range(book, a1)
        if isinstance(found, _Resp):
            return found
        tab, c1, r1, c2, r2 = found
        out = []
        for r in range(r1, r2 + 1):
            row = list(tab.rows[r - 1][c1 - 1:c2]) if r - 1 < len(tab.rows) else []
            while row and row[-1] in (None, ""):
                row.pop()
            out.append(["" if v is None else v for v in row])
        while out and not out[-1]:
            out.pop()
        return _Resp(200, {"range": a1, "majorDimension": "ROWS", **({"values": out} if out else {})})

    def _formats(self, book: Book, a1: str) -> _Resp:
        found = self._range(book, a1)
        if isinstance(found, _Resp):
            return found
        tab, c1, r1, c2, r2 = found
        rows = []
        for r in range(r1, min(r2, len(tab.rows)) + 1):
            values = []
            for c in range(c1, c2 + 1):
                kind = tab.formats.get(c - 1)
                values.append({"effectiveFormat": {"numberFormat": {"type": kind}}} if kind else {})
            rows.append({"values": values})
        return _Resp(200, {"sheets": [{"data": [{"rowData": rows}]}]})


def serve(google: FakeGoogle, host: str = "127.0.0.1", port: int = 0):
    """The fake behind a real HTTP server (a thread): for a proof in the browser,
    whose engine reaches it under Google's host names. Returns the server."""
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Handler(BaseHTTPRequestHandler):
        def _answer(self, method: str):
            host_name = (self.headers.get("Host") or "").split(":")[0]
            url = f"https://{host_name}{self.path}"
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length).decode() if length else ""
            data = {k: v[0] for k, v in parse_qs(body).items()} if body else None
            r = google.request(method, url, data=data, headers={"Authorization": self.headers.get("Authorization", "")})
            payload = r.text.encode()
            self.send_response(r.status_code)
            self.send_header("Content-Type", "application/json")
            for k, v in r.headers.items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):
            self._answer("GET")

        def do_POST(self):
            self._answer("POST")

        def log_message(self, *a):
            pass

    server = ThreadingHTTPServer((host, port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server
