"""HTTP APIs read as a datasource («API call»): one or more requests to a REST/JSON
API at each refresh, their records flattened into one table and written as the
datasource's parquet — the same road as the database, SharePoint and SFTP sources
(run, snapshot swap, refresh, schedule).

THE CONNECTION is the API: a base URL, default headers and the authentication —
none, an API key (in a header or in a query parameter, with the name the API
wants), a bearer token, HTTP basic, or OAuth2 client credentials (the token is
asked to the token URL, kept until it expires, asked again once on a 401). The
secret arrives encrypted (Fernet, shared with the gateway) and is decrypted only
to be sent; every text a person can read — errors, the «Try the call» answer, the
logs — goes through a Redactor that replaces it with ***.

CREDENTIALS STAY ON THE API'S HOST. A request carries the credentials and the
connection's and call's headers only when it goes to the origin of the base URL
(scheme, host, port). A redirect or a «next» link to another origin is followed
with none of them — a CDN serving a file does not need them, and an API that
redirects elsewhere must not hand them to someone else. TLS is verified.

THE CALL (datasource): method GET/POST, a path relative to the base URL, query
parameters, headers and a JSON body, all of them with dynamic placeholders
(`{today-7d}`, `{last_refresh}`… see api_placeholders) resolved in the datasource's
time zone. List variables (`symbol = AAPL, MSFT`) make one call per value (every
combination of several lists), each with its pagination; every row says the
values in `_param_<name>`.

THE RESPONSE: JSON (NDJSON and CSV too). An optional JMESPath expression is
applied first, then the records path (`data.items`). Nested objects become
columns `a.b.c`; lists become a JSON-text column, or one list field is exploded
into rows; «keyed» records (an object whose keys are e.g. dates) become one row
per key, the key in `_key`. Types are inferred: integers, decimals, booleans,
text; ISO dates stay text (the flow casts them). A column that is an integer on
one page and a decimal on another is a decimal one; any other mix of types makes
the column TEXT (and the refresh says which: `type_changes`) — an API that writes
"N/A" in a number field must not fail the refresh, nor lose the value.

THE PAGINATION: page number, offset/limit, cursor, next link (Link header or a
JSON path). Retries with backoff on 429, 5xx and network errors, honouring
Retry-After; a minimum interval between requests. Every limit (pages, calls,
rows, bytes) fails the refresh naming it: the data is never cut silently.

THE REFRESH MODE: snapshot (the table is rebuilt) or append with key columns —
the rows of the previous snapshot whose key comes again are REPLACED, new keys
are added, nothing is duplicated.

Every message for the user starts with «API:», the prefix the gateway knows.
"""
from __future__ import annotations

import base64
import hashlib
import io
import itertools
import json
import logging
import math
import os
import re
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Callable, Literal, Mapping
from urllib.parse import parse_qsl, quote, quote_plus, unquote, unquote_plus, urlencode, urljoin, urlsplit, urlunsplit

import polars as pl
import urllib3
import urllib3.connection
import urllib3.connectionpool
from pydantic import BaseModel, Field

from app.core.config import get_settings
from app.ingest import api_placeholders as ph
from app.ingest.converters import IngestError

logger = logging.getLogger(__name__)

PREFIX = "API: "
KEY_COLUMN = "_key"
PARAM_PREFIX = "_param_"
USER_AGENT = "Tabularia"
RETRY_STATUSES = (429, 500, 502, 503, 504)
REDIRECT_STATUSES = (301, 302, 303, 307, 308)
TRY_ROWS = 20
EXCERPT_CHARS = 2000
_HEADER_NAME = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")

# patched in the tests: retries and the minimum interval wait through it
_sleep = time.sleep


def _no_netrc(r):
    """An explicit «no auth» for requests: without one, requests reads ~/.netrc and
    would add a password of its own — to any host, a redirect's included."""
    return r


class ApiError(IngestError):
    """A message for the user (it ends up in the run or in the response)."""

    def __init__(self, message: str):
        super().__init__(message if message.startswith(PREFIX) else PREFIX + message)


class _Transient(Exception):
    """A network error worth retrying (the message is already redacted). `connect`:
    it happened before the request was sent — the only case where a POST is retried."""

    def __init__(self, message: str, connect: bool = False):
        super().__init__(message)
        self.connect = connect


# ── connection ─────────────────────────────────────────────────────────────────
class ApiConnectionSpec(BaseModel):
    base_url: str
    auth: Literal["none", "api_key", "bearer", "basic", "oauth2"] = "none"
    # basic: the user; oauth2: the client id
    username: str = ""
    # the API key, the token, the password or the client secret (Fernet)
    secret_encrypted: str = ""
    # api_key: the name of the header or of the query parameter
    key_name: str = ""
    key_in: Literal["header", "query"] = "header"
    # oauth2
    token_url: str = ""
    scope: str = ""
    client_auth: Literal["body", "basic"] = "body"
    # sent with every request to the API's host
    headers: dict[str, str] = Field(default_factory=dict)
    # «Test connection»: a path below the base URL (empty = the base URL itself)
    test_path: str = ""
    timeout_seconds: float = 0  # 0 = API__TIMEOUT_SECONDS

    def timeout(self) -> float:
        return self.timeout_seconds or get_settings().api.timeout_seconds

    def secret(self) -> str:
        if not self.secret_encrypted:
            return ""
        from app.core.crypto import decrypt_secret

        return decrypt_secret(self.secret_encrypted)


# ── the call ───────────────────────────────────────────────────────────────────
class Pair(BaseModel):
    name: str = Field(max_length=200)
    value: str = Field(default="", max_length=4000)


class Variable(BaseModel):
    name: str = Field(max_length=64)
    values: list[str] = Field(min_length=1, max_length=1000)


class ResponseSpec(BaseModel):
    format: Literal["json", "ndjson", "csv"] = "json"
    # dot path to the records (`data.items`; a number is a list index); empty = the root
    records_path: str = Field(default="", max_length=500)
    # applied to the whole response BEFORE the records path, for odd shapes
    jmespath: str = Field(default="", max_length=2000)
    # the records are an object whose keys are rows (`{"2024-01-02": {...}}`): key in `_key`
    keyed: bool = False
    # a list field exploded into one row per element (empty = every list stays JSON text)
    explode: str = Field(default="", max_length=500)
    csv_separator: str = Field(default=",", min_length=1, max_length=1)


class Pagination(BaseModel):
    type: Literal["none", "page", "offset", "cursor", "next_link"] = "none"
    # page: the page parameter; offset: the offset parameter; cursor: the cursor parameter
    param: str = Field(default="", max_length=200)
    # the first page (default 1) or the first offset (default 0)
    start: int | None = Field(default=None, ge=0)
    # offset: the parameter of the page size and its value
    limit_param: str = Field(default="", max_length=200)
    limit: int = Field(default=100, ge=1, le=100_000)
    # cursor: dot path of the next cursor in the JSON response
    cursor_path: str = Field(default="", max_length=500)
    # next link: dot path of the next URL in the JSON response; empty = the Link header
    next_path: str = Field(default="", max_length=500)


class Limits(BaseModel):
    """0 = the deployment's limit (API__…); a value can only lower it."""
    max_pages: int = Field(default=0, ge=0)
    max_calls: int = Field(default=0, ge=0)
    max_rows: int = Field(default=0, ge=0)
    min_interval_seconds: float = Field(default=0, ge=0, le=3600)


class ApiSourceSpec(BaseModel):
    method: Literal["GET", "POST"] = "GET"
    path: str = Field(default="", max_length=2000)
    query: list[Pair] = Field(default_factory=list, max_length=100)
    headers: list[Pair] = Field(default_factory=list, max_length=50)
    body: str = Field(default="", max_length=100_000)  # JSON text (POST)
    variables: list[Variable] = Field(default_factory=list, max_length=10)
    timezone: str = Field(default="UTC", max_length=64)
    # {last_refresh} on the first refresh: `today-365`, `2024-01-01`…
    last_refresh_initial: str = Field(default="", max_length=100)
    response: ResponseSpec = Field(default_factory=ResponseSpec)
    pagination: Pagination = Field(default_factory=Pagination)
    limits: Limits = Field(default_factory=Limits)
    mode: Literal["snapshot", "append"] = "snapshot"
    keys: list[str] = Field(default_factory=list, max_length=20)


@dataclass
class _Caps:
    max_pages: int
    max_calls: int
    max_rows: int
    min_interval: float


def _caps(source: ApiSourceSpec) -> _Caps:
    cfg = get_settings().api
    lim = source.limits

    def lower(mine: int, theirs: int) -> int:
        return min(mine, theirs) if mine else theirs

    return _Caps(lower(lim.max_pages, cfg.max_pages), lower(lim.max_calls, cfg.max_calls),
                 lower(lim.max_rows, cfg.max_rows), max(lim.min_interval_seconds, cfg.min_interval_seconds))


# ── secrets ────────────────────────────────────────────────────────────────────
class Redactor:
    """Replaces every credential with *** in a text a person can read."""

    def __init__(self, conn: ApiConnectionSpec):
        self._secrets: set[str] = set()
        secret = conn.secret() if conn.auth != "none" else ""
        self.add(secret)
        if conn.auth in ("basic",) or (conn.auth == "oauth2" and conn.client_auth == "basic"):
            self.add(base64.b64encode(f"{conn.username}:{secret}".encode()).decode())

    def add(self, secret: str) -> None:
        """The secret and the forms it takes in a URL or in a JSON answer that echoes
        it: percent-encoded, JSON-escaped (with and without \\u escapes), `\\/`."""
        if not secret or len(secret) < 4:
            return
        escaped = json.dumps(secret)[1:-1]
        escaped_utf8 = json.dumps(secret, ensure_ascii=False)[1:-1]
        # \\u00e8 (Python) or \\u00E8 (Java's Jackson): both hex cases
        escaped_upper = re.sub(r"\\u([0-9a-f]{4})", lambda m: "\\u" + m.group(1).upper(), escaped)
        forms = {secret, quote(secret, safe=""), quote_plus(secret), quote(secret), escaped, escaped_utf8, escaped_upper,
                 escaped.replace("/", "\\/"), escaped_utf8.replace("/", "\\/"), escaped_upper.replace("/", "\\/")}
        self._secrets.update(f for f in forms if f)

    def text(self, s: str) -> str:
        for secret in sorted(self._secrets, key=len, reverse=True):
            s = s.replace(secret, "***")
        return s

    def holds(self, s: str) -> bool:
        """Whether a credential is in `s`, in any form (decoded too, any letter case)."""
        if not s:
            return False
        texts = {s.lower(), unquote(s).lower(), unquote_plus(s).lower()}
        return any(secret.lower() in t for secret in self._secrets for t in texts)


# ── URLs ───────────────────────────────────────────────────────────────────────
def check_base_url(url: str) -> str:
    raw = (url or "").strip()
    p = urlsplit(raw)
    if p.scheme not in ("http", "https") or not p.hostname:
        raise ApiError(f"invalid base URL '{raw}': expected http(s)://host[:port][/path]")
    if p.username or p.password:
        raise ApiError("the base URL cannot hold a user or a password: use the connection's authentication")
    if p.query or p.fragment or raw.endswith(("?", "#")):
        raise ApiError(
            "the base URL cannot hold a query (?…) or a fragment (#…): put the parameters in the datasource "
            "and the key in the connection's authentication"
        )
    return raw


def _origin(url: str) -> tuple[str, str, int]:
    p = urlsplit(url)
    scheme = (p.scheme or "").lower()
    try:
        port = p.port
    except ValueError:
        port = None
    return scheme, (p.hostname or "").lower(), port or (443 if scheme == "https" else 80)


def _join(base: str, path: str) -> str:
    path = (path or "").strip()
    return base.rstrip("/") + ("/" + path.lstrip("/") if path else "")


def _quote_path_value(value: str) -> str:
    return quote(value, safe="-._~!$&'()*+,;=:@")


def _with_query(url: str, params: list[tuple[str, str]]) -> str:
    if not params:
        return url
    return url + ("&" if "?" in url else "?") + urlencode(params, quote_via=quote, safe="*")


def _size(n: float) -> str:
    if n >= 1024**2:
        return f"{n / 1024**2:.1f} MB"
    if n >= 1024:
        return f"{n / 1024:.1f} KB"
    return f"{int(n)} bytes"


# ── HTTP ───────────────────────────────────────────────────────────────────────
@dataclass
class HttpResult:
    method: str
    status: int
    reason: str
    headers: Mapping[str, str]
    body: bytes
    url: str  # the final URL without our credentials (for «next» links)
    display: str  # the final URL as a person may read it (credentials masked)
    same_origin: bool

    @property
    def content_type(self) -> str:
        return str(self.headers.get("Content-Type") or "")

    def text(self, redactor: Redactor, limit: int = EXCERPT_CHARS) -> str:
        # redacted BEFORE it is cut: a secret across the cut would leave its first half
        return redactor.text(self.body[: limit * 4 + 512].decode("utf-8", errors="replace"))[:limit]


_TOKENS: dict[str, tuple[str, float]] = {}
_TOKENS_LOCK = threading.Lock()


def forget_tokens() -> None:
    with _TOKENS_LOCK:
        _TOKENS.clear()


def _retry_after(headers: Mapping[str, str]) -> float | None:
    raw = str(headers.get("Retry-After") or "").strip()
    if not raw:
        return None
    if raw.isdigit():
        return float(raw)
    try:
        when = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())


# ── the watchdog: a wall clock over a whole request ────────────────────────────
_watching = threading.local()


class _Watchdog:
    """While active (`with`), every connection that sends a request in this thread is
    noted; when the time is up their sockets are shut down, which interrupts a read
    blocked anywhere — the status line, the headers, the body."""

    def __init__(self, seconds: float):
        self.seconds = seconds
        self.fired = False
        self._connections: list = []
        self._lock = threading.Lock()
        self._timer: threading.Timer | None = None

    def note(self, connection) -> None:
        with self._lock:
            self._connections.append(connection)
            if self.fired:
                self._shut(connection)

    @staticmethod
    def _shut(connection) -> None:
        import socket

        sock = getattr(connection, "sock", None)
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def _fire(self) -> None:
        with self._lock:
            self.fired = True
            for c in self._connections:
                self._shut(c)

    def __enter__(self) -> "_Watchdog":
        _watching.current = self
        self._timer = threading.Timer(self.seconds, self._fire)
        self._timer.daemon = True
        self._timer.start()
        return self

    def __exit__(self, *exc) -> None:
        if self._timer is not None:
            self._timer.cancel()
        _watching.current = None


def _note_connection(connection) -> None:
    watch = getattr(_watching, "current", None)
    if watch is not None:
        watch.note(connection)


class _WatchedHTTPConnection(urllib3.connection.HTTPConnection):
    def request(self, *a, **k):
        _note_connection(self)
        return super().request(*a, **k)


class _WatchedHTTPSConnection(urllib3.connection.HTTPSConnection):
    def request(self, *a, **k):
        _note_connection(self)
        return super().request(*a, **k)


class _WatchedHTTPPool(urllib3.connectionpool.HTTPConnectionPool):
    ConnectionCls = _WatchedHTTPConnection


class _WatchedHTTPSPool(urllib3.connectionpool.HTTPSConnectionPool):
    ConnectionCls = _WatchedHTTPSConnection


def _watched_adapter_class():
    from requests.adapters import HTTPAdapter

    class Adapter(HTTPAdapter):
        def init_poolmanager(self, *a, **k):
            super().init_poolmanager(*a, **k)
            self.poolmanager.pool_classes_by_scheme = {"http": _WatchedHTTPPool, "https": _WatchedHTTPSPool}

    return Adapter


def _watched_adapter():
    """A requests adapter whose connections the watchdog can shut."""
    return _watched_adapter_class()()


def _before_sending(e: Exception) -> bool:
    """Whether a network error happened before the request was sent (connect time)."""
    import requests

    if isinstance(e, requests.exceptions.ConnectTimeout):
        return True
    if isinstance(e, requests.exceptions.Timeout):
        return False  # a read timeout: the request was sent
    reason = getattr(e.args[0], "reason", None) if e.args else None
    return isinstance(reason, urllib3.exceptions.NewConnectionError) or "NewConnectionError" in repr(e)


def _tls_reason(e: Exception) -> str:
    """What is wrong with a certificate, without the URL the HTTP library puts in
    its messages (the query string there can hold the API key)."""
    text = str(e)
    for sign in ("certificate has expired", "self-signed certificate", "self signed certificate",
                 "unable to get local issuer certificate", "Hostname mismatch", "doesn't match", "certificate verify failed"):
        if sign in text:
            return sign
    return "the TLS handshake failed"


def _excerpt_line(text: str, limit: int = 300) -> str:
    one = " ".join(text.split())
    return one[:limit] + ("…" if len(one) > limit else "")


class ApiClient:
    """The requests of one refresh (or one test): credentials, redirects, retries,
    the minimum interval and the byte caps."""

    def __init__(self, conn: ApiConnectionSpec, *, min_interval: float = 0.0, max_retries: int | None = None,
                 session=None):
        import requests

        self.conn = conn
        self.base = check_base_url(conn.base_url)
        self.origin = _origin(self.base)
        self.redactor = Redactor(conn)
        cfg = get_settings().api
        self.max_retries = cfg.max_retries if max_retries is None else max_retries
        self.min_interval = min_interval
        self.total_bytes = 0
        self._last: float | None = None
        self._session = session or requests.Session()
        if session is None:
            adapter = _watched_adapter()
            self._session.mount("http://", adapter)
            self._session.mount("https://", adapter)
        self._secret = conn.secret() if conn.auth != "none" else ""
        if conn.auth == "api_key" and not conn.key_name.strip():
            raise ApiError("the API key needs the name of its header or query parameter")
        if conn.auth != "none" and not self._secret:
            raise ApiError("the connection has no secret for its authentication: enter it in the connection")

    def close(self) -> None:
        self._session.close()

    # -- credentials --
    def auth_headers(self) -> dict[str, str]:
        a = self.conn.auth
        if a == "api_key" and self.conn.key_in == "header":
            return {self.conn.key_name.strip(): self._secret}
        if a == "bearer":
            return {"Authorization": f"Bearer {self._secret}"}
        if a == "basic":
            return {"Authorization": "Basic " + base64.b64encode(f"{self.conn.username}:{self._secret}".encode()).decode()}
        if a == "oauth2":
            return {"Authorization": f"Bearer {self._token()}"}
        return {}

    def masked_auth_headers(self) -> dict[str, str]:
        a = self.conn.auth
        if a == "api_key" and self.conn.key_in == "header":
            return {self.conn.key_name.strip(): "***"}
        if a in ("bearer", "basic", "oauth2"):
            return {"Authorization": "***"}
        return {}

    def key_param(self) -> tuple[str, str] | None:
        if self.conn.auth == "api_key" and self.conn.key_in == "query":
            return self.conn.key_name.strip(), self._secret
        return None

    def display(self, url: str, params: list[tuple[str, str]]) -> str:
        shown = list(params)
        kp = self.key_param()
        if kp and _origin(url) == self.origin and not any(k == kp[0] for k, _ in parse_qsl(urlsplit(url).query)):
            shown.append((kp[0], "***"))
        return self.redactor.text(_with_query(url, shown))

    def _token_key(self) -> str:
        c = self.conn
        return hashlib.sha256("\x00".join((c.token_url, c.username, c.scope, c.client_auth, self._secret)).encode()).hexdigest()

    def _token(self) -> str:
        key = self._token_key()
        with _TOKENS_LOCK:
            cached = _TOKENS.get(key)
        if cached and cached[1] > time.monotonic():
            self.redactor.add(cached[0])
            return cached[0]
        c = self.conn
        url = (c.token_url or "").strip()
        p = urlsplit(url)
        if p.scheme not in ("http", "https") or not p.hostname:
            raise ApiError(f"invalid OAuth2 token URL '{url}'")
        data = {"grant_type": "client_credentials"}
        if c.scope.strip():
            data["scope"] = c.scope.strip()
        auth = None
        if c.client_auth == "basic":
            auth = (c.username, self._secret)
        else:
            data.update(client_id=c.username, client_secret=self._secret)
        import requests

        self._pace()
        try:
            r = self._session.post(url, data=data, auth=auth or _no_netrc, timeout=c.timeout(), allow_redirects=False,
                                   headers={"Accept": "application/json", "User-Agent": USER_AGENT})
        except requests.exceptions.SSLError as e:
            raise ApiError(f"TLS: the certificate of the token endpoint {p.hostname} is not valid ({_tls_reason(e)})") from None
        except requests.exceptions.RequestException as e:
            raise ApiError(f"cannot reach the OAuth2 token endpoint {p.hostname}: {self._network_reason(e)}") from None
        text = self.redactor.text(r.text[:EXCERPT_CHARS])
        if r.status_code != 200:
            raise ApiError(f"the OAuth2 token endpoint refused the client credentials (HTTP {r.status_code}: {_excerpt_line(text)})")
        try:
            body = r.json()
            token = str(body["access_token"])
            expires_in = float(body.get("expires_in") or 3600)
        except (ValueError, KeyError, TypeError) as e:
            raise ApiError(f"the OAuth2 token endpoint answered without an access_token: {_excerpt_line(text)}") from e
        with _TOKENS_LOCK:
            _TOKENS[key] = (token, time.monotonic() + max(0.0, expires_in - 30))
        self.redactor.add(token)
        return token

    def _forget_token(self) -> None:
        with _TOKENS_LOCK:
            _TOKENS.pop(self._token_key(), None)

    # -- manners --
    def _pace(self) -> None:
        if self.min_interval > 0 and self._last is not None:
            wait = self.min_interval - (time.monotonic() - self._last)
            if wait > 0:
                _sleep(wait)
        self._last = time.monotonic()

    def _network_reason(self, e: Exception) -> str:
        import requests

        if isinstance(e, requests.exceptions.Timeout):
            return f"no answer within {self.conn.timeout():g} s (API__TIMEOUT_SECONDS)"
        text = str(e)
        for sign, said in (("Name or service not known", "unknown host name"), ("Temporary failure in name resolution", "unknown host name"),
                           ("Connection refused", "connection refused"), ("No route to host", "no route to host"),
                           ("Network is unreachable", "network unreachable")):
            if sign in text:
                return said
        return self.redactor.text(type(e).__name__)

    def _read(self, r, display: str, deadline: float) -> bytes:
        cfg = get_settings().api
        cap, total_cap = cfg.max_response_bytes, cfg.max_total_bytes
        declared = r.headers.get("Content-Length")
        if declared and declared.isdigit() and int(declared) > cap:
            raise ApiError(f"the response of {display} is {_size(int(declared))}, over the limit of {_size(cap)} "
                           "(API__MAX_RESPONSE_BYTES): narrow the call or paginate it")
        chunks, n = [], 0
        while True:
            if time.monotonic() > deadline:
                raise ApiError(f"{display} took more than {cfg.max_request_seconds:g} s to answer "
                               "(API__MAX_REQUEST_SECONDS): narrow the call or paginate it")
            # read1: whatever has arrived, so a server that drips its answer meets the deadline
            chunk = r.raw.read1(65536, decode_content=True)
            if not chunk:
                break
            n += len(chunk)
            if n > cap:
                raise ApiError(f"the response of {display} is over the limit of {_size(cap)} "
                               "(API__MAX_RESPONSE_BYTES): narrow the call or paginate it")
            if self.total_bytes + n > total_cap:
                raise ApiError(f"the responses of this refresh are over the limit of {_size(total_cap)} "
                               "(API__MAX_TOTAL_BYTES): narrow the calls")
            chunks.append(chunk)
        self.total_bytes += n
        return b"".join(chunks)

    def _send(self, method: str, url: str, params: list[tuple[str, str]], headers: dict[str, str],
              body: bytes | None) -> HttpResult:
        import requests

        cur_method, cur_url, cur_params, cur_body = method, url, list(params), body
        max_redirects = get_settings().api.max_redirects
        for _hop in range(max_redirects + 1):
            p = urlsplit(cur_url)
            if p.scheme not in ("http", "https") or not p.hostname:
                raise ApiError(f"the API sent to an address that is not http(s): {self.redactor.text(cur_url)[:200]}")
            same = _origin(cur_url) == self.origin
            if not same:
                # another host: no credential may travel, not even one the API copied into the address
                cur_url, cur_params = self._cross_origin(cur_url, cur_params)
                p = urlsplit(cur_url)
            h = {"User-Agent": USER_AGENT}
            send_params = list(cur_params)
            if same:
                # credentials and headers only for the API's own host
                h.update(self.conn.headers)
                h.update(headers)
                h.update(self.auth_headers())
                kp = self.key_param()
                if kp and not any(k == kp[0] for k, _ in parse_qsl(p.query)) and not any(k == kp[0] for k, _ in send_params):
                    send_params.append(kp)
            if cur_body is not None and not any(k.lower() == "content-type" for k in h):
                h["Content-Type"] = "application/json"
            display = self.display(cur_url, cur_params)
            self._pace()
            seconds = get_settings().api.max_request_seconds
            deadline = time.monotonic() + seconds
            # the wall clock of the WHOLE request (connect, headers, body): a server that
            # drips bytes resets the read timeout at each one; the watchdog shuts its socket
            watch = _Watchdog(seconds)
            try:
                with watch:
                    try:
                        r = self._session.request(cur_method, cur_url, params=send_params or None, headers=h, data=cur_body,
                                                  allow_redirects=False, timeout=self.conn.timeout(), stream=True, verify=True,
                                                  auth=_no_netrc)
                    except requests.exceptions.SSLError as e:
                        raise ApiError(f"TLS: the certificate of {p.hostname} is not valid ({_tls_reason(e)})") from None
                    except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
                        raise _Transient(f"cannot reach {p.hostname}{':' + str(p.port) if p.port else ''}: {self._network_reason(e)}",
                                         connect=_before_sending(e)) from None
                    except requests.exceptions.RequestException as e:
                        raise ApiError(f"{cur_method} {display} failed: {type(e).__name__}") from None
                    try:
                        location = r.headers.get("Location")
                        if r.status_code in REDIRECT_STATUSES and location:
                            clean = _with_query(cur_url, cur_params)
                            cur_url = urljoin(clean, location)
                            if r.status_code == 303 or (r.status_code in (301, 302) and cur_method == "POST"):
                                cur_method, cur_body = "GET", None
                            cur_params = []
                            continue
                        try:
                            data = self._read(r, display, deadline)
                        except (requests.exceptions.RequestException, urllib3.exceptions.HTTPError, OSError) as e:
                            # the HTTP library's message can hold the URL: not chained
                            raise _Transient(f"the connection to {p.hostname} broke while reading the answer "
                                             f"({type(e).__name__})") from None
                        return HttpResult(method=cur_method, status=r.status_code, reason=r.reason or "", headers=r.headers,
                                          body=data, url=_with_query(cur_url, cur_params), display=display, same_origin=same)
                    finally:
                        r.close()
            except (ApiError, _Transient):
                if watch.fired:
                    raise ApiError(f"{display} took more than {seconds:g} s to answer (API__MAX_REQUEST_SECONDS): "
                                   "narrow the call or paginate it") from None
                raise
        raise ApiError(f"more than {max_redirects} redirects from {self.display(url, params)} (API__MAX_REDIRECTS)")

    def _cross_origin(self, url: str, params: list[tuple[str, str]]) -> tuple[str, list[tuple[str, str]]]:
        """An address on another host, without any credential: the query pairs named
        like the API key or holding a secret are dropped; a secret left anywhere else
        (the path, a fragment) refuses the hop."""
        p = urlsplit(url)
        key_name = self.conn.key_name.strip() if self.conn.auth == "api_key" and self.conn.key_in == "query" else None
        pairs = parse_qsl(p.query, keep_blank_values=True)
        kept = [(k, v) for k, v in pairs if k != key_name and not self.redactor.holds(k) and not self.redactor.holds(v)]
        if len(kept) != len(pairs):
            url = urlunsplit(p._replace(query=urlencode(kept)))
        params = [(k, v) for k, v in params if k != key_name and not self.redactor.holds(k) and not self.redactor.holds(v)]
        if self.redactor.holds(url):
            raise ApiError(f"the API sends to another host ({p.hostname}) an address that holds the key: not followed, "
                           "so that the key does not leave the API's host")
        return url, params

    def request(self, method: str, url: str, params: list[tuple[str, str]], headers: dict[str, str],
                body: bytes | None) -> HttpResult:
        attempt, refreshed = 0, False
        cap = get_settings().api.max_retry_wait_seconds
        while True:
            try:
                res = self._send(method, url, params, headers, body)
            except _Transient as e:
                if method == "POST" and not e.connect:
                    # the request may have been received and acted upon: sending it again could do it twice
                    raise ApiError(f"{e} (a POST is not sent again once it may have reached the API)") from None
                if attempt >= self.max_retries:
                    raise ApiError(f"{e}" + (f" (after {attempt + 1} attempts)" if attempt else "")) from None
                _sleep(min(2.0 ** attempt, 30.0))
                attempt += 1
                continue
            if res.status == 401 and self.conn.auth == "oauth2" and res.same_origin and not refreshed:
                # the token expired before its time, or was revoked: ask for a new one, once
                self._forget_token()
                refreshed = True
                continue
            # a POST again only on 429, where the API says it did not process it
            retriable = res.status in RETRY_STATUSES and (method != "POST" or res.status == 429)
            if retriable and attempt < self.max_retries:
                wait = _retry_after(res.headers)
                if wait is None:
                    wait = min(2.0 ** attempt, 30.0)
                if wait > cap:
                    raise ApiError(f"{res.display} answered HTTP {res.status} and asks to retry after {wait:.0f} s, "
                                   f"more than the {cap:g} s allowed (API__MAX_RETRY_WAIT_SECONDS)")
                logger.info("api %s: HTTP %s, retrying in %.1f s", res.display, res.status, wait)
                _sleep(wait)
                attempt += 1
                continue
            return res

    def http_error(self, res: HttpResult) -> ApiError:
        excerpt = _excerpt_line(res.text(self.redactor, 1000))
        reason = f" {res.reason}" if res.reason else ""
        return ApiError(f"{res.method} {res.display} answered HTTP {res.status}{reason}" + (f": {excerpt}" if excerpt else ""))


# ── the response → records ─────────────────────────────────────────────────────
_MISSING = object()


def _check_dot_path(path: str, what: str) -> None:
    if path and any(not seg.strip() for seg in path.split(".")):
        raise ApiError(f"invalid {what} '{path}': names separated by dots, e.g. data.items")


def dig(doc: Any, path: str) -> Any:
    """The value at a dot path (`data.items`, `results.0.rows`), or _MISSING."""
    cur = doc
    for seg in [s for s in (path or "").split(".") if s != ""]:
        if isinstance(cur, dict):
            if seg not in cur:
                return _MISSING
            cur = cur[seg]
        elif isinstance(cur, list) and seg.isdigit():
            i = int(seg)
            if i >= len(cur):
                return _MISSING
            cur = cur[i]
        else:
            return _MISSING
    return cur


def _keys_hint(doc: Any) -> str:
    if isinstance(doc, dict):
        keys = list(doc)[:12]
        return "it has: " + (", ".join(map(str, keys)) or "nothing") + ("…" if len(doc) > 12 else "")
    if isinstance(doc, list):
        return f"it is a list of {len(doc)} elements"
    return f"it is a {type(doc).__name__}"


def _flatten_into(obj: dict, prefix: str, out: dict, explode: str, stash: dict) -> None:
    for k, v in obj.items():
        name = f"{prefix}.{k}" if prefix else str(k)
        if isinstance(v, dict):
            if v:
                _flatten_into(v, name, out, explode, stash)
                continue
            v = None
        elif isinstance(v, list):
            if explode and name == explode:
                stash["list"] = v
                continue
            v = json.dumps(v, ensure_ascii=False)
        if name in out:
            raise ApiError(f"two fields of the response make the same column '{name}'")
        out[name] = v


def flatten(record: dict, explode: str = "") -> list[dict]:
    """One record → one row (or one row per element of the exploded list)."""
    flat: dict = {}
    stash: dict = {}
    _flatten_into(record, "", flat, explode, stash)
    elements = stash.get("list")
    if not elements:
        return [flat]
    rows = []
    for el in elements:
        row = dict(flat)
        if isinstance(el, dict):
            _flatten_into(el, explode, row, "", {})
        else:
            row[explode] = json.dumps(el, ensure_ascii=False) if isinstance(el, list) else el
        rows.append(row)
    return rows


def _records_of(data: Any, keyed: bool) -> list[dict]:
    if data is None:
        return []
    if keyed:
        if not isinstance(data, dict):
            raise ApiError(f"«keyed» records need an object (its keys become rows) and the records are a {type(data).__name__}")
        clash = next((k for k, v in data.items() if isinstance(v, dict) and KEY_COLUMN in v), None)
        if clash is not None:
            raise ApiError(f"the record of key '{_excerpt_line(str(clash), 60)}' has a field named '{KEY_COLUMN}', the "
                           "column of the keys: the records are not keyed (untick «keyed»), or use a JMESPath to rename it")
        return [{KEY_COLUMN: str(k), **v} if isinstance(v, dict) else {KEY_COLUMN: str(k), "value": v} for k, v in data.items()]
    if isinstance(data, list):
        return [x if isinstance(x, dict) else {"value": x} for x in data]
    if isinstance(data, dict):
        return [data]
    return [{"value": data}]


def parse_page(res: HttpResult, rs: ResponseSpec, first: bool, redactor: Redactor) -> tuple[Any, list[dict]]:
    """(the whole JSON document or None, the records of this page)."""
    if rs.format == "csv":
        if not res.body.strip():
            return None, []
        try:
            df = pl.read_csv(io.BytesIO(res.body), separator=rs.csv_separator, infer_schema_length=10_000)
        except Exception as e:
            reason = _excerpt_line(redactor.text(str(e)), 200)
            raise ApiError(f"the response is not a readable CSV ({reason}): {_excerpt_line(res.text(redactor, 300))}") from None
        return None, df.to_dicts()
    if rs.format == "ndjson":
        out = []
        for i, line in enumerate(res.body.splitlines(), 1):
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except ValueError as e:
                raise ApiError(f"line {i} of the response is not valid JSON: {_excerpt_line(redactor.text(line.decode('utf-8', 'replace')), 200)}") from e
            out.append(item if isinstance(item, dict) else {"value": item})
        return None, out
    try:
        doc = json.loads(res.body) if res.body.strip() else None
    except ValueError as e:
        ct = res.content_type.split(";")[0] or "no content type"
        raise ApiError(f"the response is not valid JSON ({ct}): {_excerpt_line(res.text(redactor, 300))}") from e
    data = doc
    if rs.jmespath.strip():
        import jmespath

        try:
            data = jmespath.search(rs.jmespath, data)
        except jmespath.exceptions.JMESPathError as e:
            raise ApiError(f"the JMESPath expression failed: {e}") from e
    if rs.records_path.strip():
        found = dig(data, rs.records_path.strip())
        if found is _MISSING:
            if first:
                raise ApiError(f"the records path '{rs.records_path.strip()}' is not in the response ({_keys_hint(data)})")
            found = None
        data = found
    return doc, _records_of(data, rs.keyed)


# ── records → typed columns ────────────────────────────────────────────────────
_INT64 = (-(2**63), 2**63 - 1)


def _kind(v: Any) -> str:
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, int):
        return "int"
    if isinstance(v, float):
        return "float"
    return "str"


def _as_text(v: Any) -> str | None:
    if v is None:
        return None
    if isinstance(v, bool):
        return "true" if v else "false"
    return v if isinstance(v, str) else str(v)


def records_to_frame(rows: list[dict], conflicts: set[str]) -> pl.DataFrame:
    """Rows of dicts → a DataFrame with inferred types; a mix of types in a column
    makes it text (its name goes in `conflicts`)."""
    names: dict[str, None] = {}
    for r in rows:
        for k in r:
            names.setdefault(k, None)
    series = []
    for name in names:
        values = [r.get(name) for r in rows]
        kinds = {_kind(v) for v in values if v is not None}
        if not kinds or kinds == {"str"}:
            series.append(pl.Series(name, [_as_text(v) for v in values], dtype=pl.String))
        elif kinds == {"bool"}:
            series.append(pl.Series(name, values, dtype=pl.Boolean))
        elif kinds == {"int"} and all(v is None or _INT64[0] <= v <= _INT64[1] for v in values):
            series.append(pl.Series(name, values, dtype=pl.Int64))
        elif kinds <= {"int", "float"} and kinds != {"int"}:
            try:
                series.append(pl.Series(name, [None if v is None else float(v) for v in values], dtype=pl.Float64))
            except OverflowError:
                conflicts.add(name)
                series.append(pl.Series(name, [_as_text(v) for v in values], dtype=pl.String))
        else:
            if kinds != {"int"}:  # an integer too big for 64 bits is text without being a conflict
                conflicts.add(name)
            series.append(pl.Series(name, [_as_text(v) for v in values], dtype=pl.String))
    return pl.DataFrame(series)


def _merge_dtype(a: pl.DataType, b: pl.DataType, name: str, conflicts: set[str]) -> pl.DataType:
    if a == b:
        return a
    if a.is_numeric() and b.is_numeric():
        return pl.Float64
    conflicts.add(name)
    return pl.String


def align(frames: list[pl.DataFrame], conflicts: set[str]) -> list[pl.DataFrame]:
    """The frames with the same type for each column: integer + decimal = decimal,
    any other mix = text. A column made only of nulls in a frame takes the type of
    the others."""
    final: dict[str, pl.DataType | None] = {}
    for f in frames:
        for c in f.columns:
            final.setdefault(c, None)
            if f.height == 0 or f[c].null_count() == f.height:
                continue
            d = f.schema[c]
            final[c] = d if final[c] is None else _merge_dtype(final[c], d, c, conflicts)
    out = []
    for f in frames:
        casts = [pl.col(c).cast(final[c] or pl.String) for c in f.columns if f.schema[c] != (final[c] or pl.String)]
        out.append(f.with_columns(casts) if casts else f)
    return out


def _order(df: pl.DataFrame) -> pl.DataFrame:
    first = [c for c in df.columns if c == KEY_COLUMN]
    last = [c for c in df.columns if c.startswith(PARAM_PREFIX)]
    middle = [c for c in df.columns if c not in first and c not in last]
    return df.select(first + middle + last)


# ── validation and resolution of the call ──────────────────────────────────────
def _body_strings(node: Any) -> list[str]:
    if isinstance(node, str):
        return [node]
    if isinstance(node, list):
        return [s for x in node for s in _body_strings(x)]
    if isinstance(node, dict):
        return [s for k, v in node.items() for s in (k, *_body_strings(v))]
    return []


def _render_body(node: Any, ctx: ph.Context) -> Any:
    if isinstance(node, str):
        single = ph.single_placeholder(node)
        if single is not None and single.fmt in ("epoch", "epoch_ms"):
            return int(ph.render(node, ctx))  # a timestamp the API wants as a number
        return ph.render(node, ctx)
    if isinstance(node, list):
        return [_render_body(x, ctx) for x in node]
    if isinstance(node, dict):
        return {ph.render(k, ctx): _render_body(v, ctx) for k, v in node.items()}
    return node


def _combinations(source: ApiSourceSpec) -> list[dict[str, str]]:
    if not source.variables:
        return [{}]
    names = [v.name for v in source.variables]
    return [dict(zip(names, combo)) for combo in itertools.product(*(v.values for v in source.variables))]


def _ph(fn: Callable, *a, **kw):
    try:
        return fn(*a, **kw)
    except ph.PlaceholderError as e:
        raise ApiError(str(e)) from e


def validate_source(conn: ApiConnectionSpec, source: ApiSourceSpec, now: datetime | None = None,
                    last_refresh: str | None = None) -> dict[str, Any]:
    """Everything that can be said without calling the API. Returns the number of
    calls and the first request, resolved and masked."""
    check_base_url(conn.base_url)
    _ph(ph.zone, source.timezone)
    path = source.path.strip()
    if "://" in path or path.startswith("//"):
        raise ApiError("the path must be relative to the connection's base URL (e.g. /v1/items), not a whole address")
    if "?" in path or "#" in path:
        raise ApiError("put the query parameters in the Query section of the call, not in the path")
    names: list[str] = []
    for v in source.variables:
        _ph(ph.check_variable_name, v.name)
        if v.name in names:
            raise ApiError(f"the variable '{v.name}' is defined twice")
        names.append(v.name)
    has_initial = bool(source.last_refresh_initial.strip())
    if has_initial:
        _ph(ph.resolve_initial, source.last_refresh_initial, now or datetime.now(timezone.utc), ph.zone(source.timezone))
    templates = [path]
    for q in source.query:
        if not q.name.strip():
            raise ApiError("a query parameter has no name")
        templates += [q.name, q.value]
    for h in source.headers:
        if not _HEADER_NAME.match(h.name.strip() or "-"):
            raise ApiError(f"invalid header name '{h.name}'")
        if "\r" in h.value or "\n" in h.value:
            raise ApiError(f"the header '{h.name}' cannot hold a line break")
        templates.append(h.value)
    body_doc = None
    if source.body.strip():
        if source.method == "GET":
            raise ApiError("a GET call has no body: choose POST, or empty the body")
        try:
            body_doc = json.loads(source.body)
        except ValueError as e:
            raise ApiError(f"the body is not valid JSON ({e}). Placeholders go inside JSON strings: \"{{today}}\"") from e
        templates += _body_strings(body_doc)
    used: set[str] = set()
    for t in templates:
        used |= _ph(ph.check, t, set(names), has_initial)
    unused = [n for n in names if n not in used]
    if unused:
        raise ApiError(f"the variable '{unused[0]}' is not used in the path, the query, the headers or the body")
    rs = source.response
    if rs.jmespath.strip():
        import jmespath

        try:
            jmespath.compile(rs.jmespath)
        except jmespath.exceptions.JMESPathError as e:
            raise ApiError(f"invalid JMESPath expression: {e}") from e
    _check_dot_path(rs.records_path.strip(), "records path")
    _check_dot_path(rs.explode.strip(), "list to explode")
    pg = source.pagination
    if pg.type in ("page", "offset", "cursor") and not pg.param.strip():
        raise ApiError(f"the {pg.type} pagination needs the name of its query parameter")
    if pg.type == "offset" and not pg.limit_param.strip():
        raise ApiError("the offset pagination needs the name of the page-size parameter (e.g. limit)")
    if pg.type == "cursor" and not pg.cursor_path.strip():
        raise ApiError("the cursor pagination needs the JSON path of the next cursor in the response (e.g. meta.next)")
    if (pg.type == "cursor" or (pg.type == "next_link" and pg.next_path.strip())) and rs.format != "json":
        raise ApiError("a cursor or a next link read from the response needs a JSON response")
    _check_dot_path(pg.cursor_path.strip(), "cursor path")
    _check_dot_path(pg.next_path.strip(), "next link path")
    if source.mode == "append":
        keys = [k.strip() for k in source.keys if k.strip()]
        if not keys:
            raise ApiError("the append mode needs the key columns that identify a row (e.g. date, _param_symbol)")
        if len(set(keys)) != len(keys):
            raise ApiError("a key column is named twice")
    combos = _combinations(source)
    caps = _caps(source)
    if len(combos) > caps.max_calls:
        raise ApiError(f"the variables make {len(combos)} calls, over the limit of {caps.max_calls} per refresh "
                       "(the datasource's limit or API__MAX_CALLS): fewer values")
    ctx = _ph(ph.make_context, now or datetime.now(timezone.utc), source.timezone, last_refresh,
              source.last_refresh_initial, combos[0])
    req = _resolve(conn, source, ctx, body_doc)
    return {"ok": True, "calls": len(combos), "request": _display_request(conn, req, Redactor(conn))}


@dataclass
class _Request:
    method: str
    url: str
    params: list[tuple[str, str]]
    headers: dict[str, str]
    body: bytes | None
    body_text: str


def _resolve(conn: ApiConnectionSpec, source: ApiSourceSpec, ctx: ph.Context, body_doc: Any = None) -> _Request:
    url = _join(conn.base_url.strip(), _ph(ph.render, source.path.strip(), ctx, quote=_quote_path_value))
    params = [(_ph(ph.render, q.name, ctx), _ph(ph.render, q.value, ctx)) for q in source.query]
    headers = {}
    for h in source.headers:
        value = _ph(ph.render, h.value, ctx)
        if "\r" in value or "\n" in value:
            raise ApiError(f"the header '{h.name}' cannot hold a line break")
        headers[h.name.strip()] = value
    body, body_text = None, ""
    if source.body.strip():
        doc = body_doc if body_doc is not None else json.loads(source.body)
        body_text = json.dumps(_ph(_render_body, doc, ctx), ensure_ascii=False)
        body = body_text.encode()
    return _Request(source.method, url, params, headers, body, body_text)


def _display_request(conn: ApiConnectionSpec, req: _Request, redactor: Redactor) -> dict[str, Any]:
    shown = list(req.params)
    if conn.auth == "api_key" and conn.key_in == "query":
        shown.append((conn.key_name.strip(), "***"))
    headers = dict(conn.headers)
    headers.update(req.headers)
    if conn.auth == "api_key" and conn.key_in == "header":
        headers[conn.key_name.strip()] = "***"
    elif conn.auth in ("bearer", "basic", "oauth2"):
        headers["Authorization"] = "***"
    return {"method": req.method, "url": redactor.text(_with_query(req.url, shown)),
            "headers": {k: redactor.text(v) for k, v in headers.items()}, "body": redactor.text(req.body_text)}


# ── one call and its pagination ────────────────────────────────────────────────
def _page_params(params: list[tuple[str, str]], extra: list[tuple[str, str]]) -> list[tuple[str, str]]:
    names = {k for k, _ in extra}
    return [(k, v) for k, v in params if k not in names] + extra


def _next_of(source: ApiSourceSpec, res: HttpResult, doc: Any, n_records: int, state: dict) -> str | None:
    pg = source.pagination
    if pg.type == "none":
        return None
    if n_records == 0:
        return None
    if pg.type == "page":
        return str(state["page"] + 1)
    if pg.type == "offset":
        # only an EMPTY page ends it: an API that caps the page size returns short pages
        # that are not the last one (one request more, no rows lost)
        return str(state["offset"] + n_records)
    if pg.type == "cursor":
        nxt = dig(doc, pg.cursor_path.strip())
        if nxt is _MISSING or nxt in (None, "", [], {}):
            return None
        nxt = str(nxt)
        if nxt in state.get("seen", ()):
            raise ApiError(f"the cursor '{_excerpt_line(nxt, 60)}' leads back to a page already read: the pagination "
                           "would loop (check the cursor path)")
        return nxt
    # next link
    if pg.next_path.strip():
        nxt = dig(doc, pg.next_path.strip())
        if nxt is _MISSING or not nxt or not isinstance(nxt, str):
            return None
    else:
        import requests

        links = requests.utils.parse_header_links(res.headers.get("Link", "")) if res.headers.get("Link") else []
        nxt = next((ln.get("url") for ln in links if "next" in str(ln.get("rel", "")).split()), None)
        if not nxt:
            return None
    nxt = urljoin(res.url, nxt.strip())
    if nxt in state.get("seen", ()):
        raise ApiError("the next link leads back to a page already read: the pagination would loop "
                       "(check the next link's path)")
    return nxt


def _page_request(source: ApiSourceSpec, req: _Request, state: dict) -> tuple[str, list[tuple[str, str]]]:
    pg = source.pagination
    if pg.type == "page":
        return req.url, _page_params(req.params, [(pg.param.strip(), str(state["page"]))])
    if pg.type == "offset":
        return req.url, _page_params(req.params, [(pg.param.strip(), str(state["offset"])),
                                                  (pg.limit_param.strip(), str(pg.limit))])
    if pg.type == "cursor" and state.get("cursor"):
        return req.url, _page_params(req.params, [(pg.param.strip(), state["cursor"])])
    if pg.type == "next_link" and state.get("url"):
        return state["url"], []
    return req.url, req.params


def _start_state(source: ApiSourceSpec) -> dict:
    pg = source.pagination
    if pg.type == "page":
        return {"page": 1 if pg.start is None else pg.start}
    if pg.type == "offset":
        return {"offset": 0 if pg.start is None else pg.start}
    return {}


def _advance(source: ApiSourceSpec, state: dict, nxt: str) -> None:
    t = source.pagination.type
    if t == "page":
        state["page"] = int(nxt)
    elif t == "offset":
        state["offset"] = int(nxt)
    elif t == "cursor":
        state["cursor"] = nxt
        state.setdefault("seen", set()).add(nxt)
    elif t == "next_link":
        state["url"] = nxt


def _rows_of_page(source: ApiSourceSpec, records: list[dict], combo: dict[str, str]) -> list[dict]:
    explode = source.response.explode.strip()
    reserved = {PARAM_PREFIX + n for n in combo}
    rows = []
    for rec in records:
        for row in flatten(rec, explode):
            clash = reserved.intersection(row)
            if clash:
                raise ApiError(f"the response has a field '{sorted(clash)[0]}', the name of the column of a variable: rename the variable")
            for n, v in combo.items():
                row[PARAM_PREFIX + n] = v
            rows.append(row)
    return rows


# ── «Test connection» and «Try the call» ───────────────────────────────────────
def test_connection(conn: ApiConnectionSpec) -> dict[str, Any]:
    """A GET to the base URL (or to the test path) with the credentials."""
    client = ApiClient(conn, max_retries=0)
    try:
        url = _join(client.base, conn.test_path.strip()) if conn.test_path.strip() else client.base
        res = client.request("GET", url, [], {}, None)
        if res.status >= 400:
            raise client.http_error(res)
        return {"ok": True, "status": res.status, "url": res.display, "content_type": res.content_type,
                "excerpt": _excerpt_line(res.text(client.redactor, 500), 500)}
    finally:
        client.close()


def _json_safe(v: Any) -> Any:
    if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
        return None
    return v


def try_call(conn: ApiConnectionSpec, source: ApiSourceSpec, last_refresh: str | None = None,
             now: datetime | None = None) -> dict[str, Any]:
    """The FIRST page of the FIRST call, before saving: the request as sent
    (placeholders resolved, secrets masked), the status, an excerpt of the raw
    answer and the first rows of the table it makes. An answer that cannot be
    turned into rows is still shown, with what is wrong."""
    checked = validate_source(conn, source, now=now, last_refresh=last_refresh)
    now = now or datetime.now(timezone.utc)
    combo = _combinations(source)[0]
    ctx = _ph(ph.make_context, now, source.timezone, last_refresh, source.last_refresh_initial, combo)
    req = _resolve(conn, source, ctx)
    client = ApiClient(conn, max_retries=0)
    try:
        state = _start_state(source)
        url, params = _page_request(source, req, state)
        shown = _display_request(conn, _Request(req.method, url, params, req.headers, req.body, req.body_text), client.redactor)
        out: dict[str, Any] = {"ok": False, "calls": checked["calls"], "request": shown}
        res = client.request(req.method, url, params, req.headers, req.body)
        out.update(status=res.status, content_type=res.content_type, bytes=len(res.body),
                   excerpt=res.text(client.redactor), final_url=res.display)
        if res.status >= 400:
            out["error"] = str(client.http_error(res))
            return out
        state.setdefault("seen", set()).update({_with_query(url, params), res.url})
        try:
            doc, records = parse_page(res, source.response, True, client.redactor)
            rows = _rows_of_page(source, records, combo)
            frame = _order(records_to_frame(rows, set()))
            nxt = _next_of(source, res, doc, len(records), state)
        except ApiError as e:
            out["error"] = str(e)
            return out
        red = client.redactor.text
        # the rows are the API's answer: one that echoes a credential shows *** like the excerpt
        out.update(ok=True, records=len(rows), columns=[red(c) for c in frame.columns],
                   rows=[{red(k): red(v) if isinstance(v, str) else _json_safe(v) for k, v in r.items()}
                         for r in frame.head(TRY_ROWS).to_dicts()],
                   next=red(nxt) if nxt else None)
        return out
    finally:
        client.close()


# ── the refresh ────────────────────────────────────────────────────────────────
def _read_previous(storage, previous: dict | None, tmp: str) -> pl.DataFrame | None:
    if not previous or not previous.get("key"):
        return None
    local = os.path.join(tmp, "previous.parquet")
    try:
        storage.download_file(previous.get("bucket") or "", previous["key"], local)
    except Exception as e:  # noqa: BLE001 — named in the message
        raise ApiError(f"the previous snapshot could not be read for the append ({type(e).__name__}): refresh again, "
                       "or switch the datasource to snapshot once") from e
    return pl.read_parquet(local)


def _append(prev: pl.DataFrame | None, new: pl.DataFrame | None, keys: list[str],
            conflicts: set[str]) -> tuple[pl.DataFrame | None, int]:
    if new is not None:
        missing = [k for k in keys if k not in new.columns]
        if missing:
            raise ApiError(f"the key column '{missing[0]}' is not among the columns of the response "
                           f"({', '.join(new.columns[:15])}): fix the keys of the append")
        # the same key twice in one refresh: the last one received wins
        new = new.unique(subset=keys, keep="last", maintain_order=True)
    if prev is None:
        return new, 0
    missing = [k for k in keys if k not in prev.columns]
    if missing:
        raise ApiError(f"the previous snapshot has no column '{missing[0]}': fix the keys, or refresh once in "
                       "snapshot mode")
    if new is None:
        return prev, 0
    prev_a, new_a = align([prev, new], conflicts)
    kept = prev_a.join(new_a.select(keys).unique(), on=keys, how="anti", nulls_equal=True)
    return pl.concat([kept, new_a], how="diagonal"), prev_a.height - kept.height


def ingest_api_to_parquet(
    conn: ApiConnectionSpec,
    source: ApiSourceSpec,
    bucket: str,
    key: str,
    storage,
    progress: Callable[..., None] | None = None,
    last_refresh: str | None = None,
    previous: dict | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Every call (one per combination of the variables), every page, into one
    table; in append mode merged with the previous snapshot (`previous` =
    {bucket, key}). `last_refresh` = the start of the last successful refresh
    (ISO, UTC), None on the first. `progress`: «calls» at each call and page
    (call i of n, pages and rows so far), «saving» while the parquet goes up."""
    report = progress or (lambda *a, **k: None)
    now = now or datetime.now(timezone.utc)
    validate_source(conn, source, now=now, last_refresh=last_refresh)
    caps = _caps(source)
    combos = _combinations(source)
    client = ApiClient(conn, min_interval=caps.min_interval)
    frames: list[pl.DataFrame] = []
    conflicts: set[str] = set()
    pages = rows_fetched = 0
    try:
        for i, combo in enumerate(combos, 1):
            report("calls", call=i, calls=len(combos), pages=pages, rows=rows_fetched)
            ctx = _ph(ph.make_context, now, source.timezone, last_refresh, source.last_refresh_initial, combo)
            req = _resolve(conn, source, ctx)
            state = _start_state(source)
            page_no = 0
            while True:
                page_no += 1
                if page_no > caps.max_pages:
                    raise ApiError(f"the pagination of {client.display(req.url, req.params)} went on for more than "
                                   f"{caps.max_pages} pages (the datasource's limit or API__MAX_PAGES): check the "
                                   "pagination, or raise the limit")
                url, params = _page_request(source, req, state)
                res = client.request(req.method, url, params, req.headers, req.body)
                if res.status >= 400:
                    raise client.http_error(res)
                state.setdefault("seen", set()).update({_with_query(url, params), res.url})
                doc, records = parse_page(res, source.response, page_no == 1, client.redactor)
                rows = _rows_of_page(source, records, combo)
                pages += 1
                rows_fetched += len(rows)
                if rows_fetched > caps.max_rows:
                    raise ApiError(f"the calls returned more than {caps.max_rows} rows (the datasource's limit or "
                                   "API__MAX_ROWS): narrow the calls")
                if rows:
                    frames.append(records_to_frame(rows, conflicts))
                report("calls", call=i, calls=len(combos), pages=pages, rows=rows_fetched)
                nxt = _next_of(source, res, doc, len(records), state)
                if nxt is None:
                    break
                _advance(source, state, nxt)
    finally:
        client.close()

    new = _order(pl.concat(align(frames, conflicts), how="diagonal")) if frames else None
    replaced = 0
    with tempfile.TemporaryDirectory(prefix="api_in_") as tmp:
        if source.mode == "append":
            keys = [k.strip() for k in source.keys if k.strip()]
            prev = _read_previous(storage, previous, tmp)
            table, replaced = _append(prev, new, keys, conflicts)
            if table is None:
                raise ApiError("the calls returned no records and there is no previous snapshot: nothing to save")
            table = _order(table)
        else:
            if new is None:
                raise ApiError("the calls returned no records: nothing to save (the previous snapshot stays)")
            table = new
        report("saving", rows=table.height)
        out = os.path.join(tmp, "out.parquet")
        table.write_parquet(out, compression="zstd", row_group_size=get_settings().ingest.parquet_row_group_rows)
        storage.upload_file(out, bucket, key)
    if conflicts:
        logger.warning("api %s: columns made text by mixed types: %s", conn.base_url, sorted(conflicts))
    logger.info("api ingest done: %s calls, %s pages, %s rows fetched, %s rows written (%s)",
                len(combos), pages, rows_fetched, table.height, source.mode)
    return {
        "status": "success",
        "bucket": bucket,
        "output_key": key,
        "rows_written": table.height,
        "columns": [{"name": n, "dtype": str(t)} for n, t in table.schema.items()],
        "calls": len(combos),
        "pages": pages,
        "rows_fetched": rows_fetched,
        "rows_replaced": replaced,
        "type_changes": sorted(conflicts),
        "mode": source.mode,
    }
