"""Google Sheets source: one tab of a spreadsheet → parquet.

A connection is a Google Cloud SERVICE ACCOUNT (its JSON key, encrypted at rest); a
datasource is one spreadsheet, one tab and an optional A1 range. The service account
reads only the spreadsheets someone shared with its email: no domain-wide delegation,
no person's account.

Access with read-only scopes (`spreadsheets.readonly`, `drive.metadata.readonly`) and
a token asked with the key's signed JWT (RFC 7523): no Google library, `requests` and
`cryptography` are in the image already. Drive serves only to LIST the spreadsheets
shared with the account; without the Drive API the datasource takes a link.

The Google endpoints are constants, never taken from the connection nor from the key's
JSON (`token_uri` is ignored): whoever creates a connection must not be able to point
the backend, with a signed assertion in hand, at an address of their choice.

How the cells become a table:
- the FIRST row of the range is the header, with the same strict contract as the file
  sources (a name per column, no repeats, no title above the table): refused, never
  guessed at;
- values are read UNFORMATTED (numbers as numbers, whatever the locale's format; a
  percentage as its fraction) with dates as SERIAL NUMBERS, and the columns whose cells
  are formatted as dates, times or date-times in the sheet are turned back into them;
- the typed values then go through the CSV import's own inference (the same reader,
  the same rules for numbers, dates, booleans and empty cells), except that a column
  Sheets holds as TEXT in every cell stays text: a code like 00123 is text on purpose;
- empty cells are NULL, empty rows are dropped, formula errors (#N/A, #REF!…) are NULL
  and counted on the run.

Tested against a fake Google (tests/fake_google.py) that keeps to the documented
contract: there are no real credentials in development.
"""
from __future__ import annotations

import base64
import csv
import json
import logging
import math
import os
import re
import tempfile
import time
from collections import Counter
from datetime import datetime, timedelta
from typing import Any, Callable
from urllib.parse import quote

import polars as pl
from pydantic import BaseModel

from app.core.config import get_settings
from app.core.crypto import decrypt_secret
from app.ingest.converters import IngestError
from app.ingest.sharepoint_source import check_header

logger = logging.getLogger(__name__)

TOKEN_URL = "https://oauth2.googleapis.com/token"
SHEETS_BASE = "https://sheets.googleapis.com/v4"
DRIVE_BASE = "https://www.googleapis.com/drive/v3"
SCOPES = (
    "https://www.googleapis.com/auth/spreadsheets.readonly",
    "https://www.googleapis.com/auth/drive.metadata.readonly",
)
PREFIX = "Google Sheets: "
SPREADSHEET_MIME = "application/vnd.google-apps.spreadsheet"
# a read asks for about this many cells: Google advises answers of a few MB, and the
# quota counts requests (60 a minute per account), not cells
CELLS_PER_PAGE = 200_000
MAX_PAGE_ROWS = 20_000
# the cells whose number format says date / time / date-time, looked at below the header
FORMAT_SAMPLE_ROWS = 500
# the spreadsheets the picker lists (Drive pages of 100)
MAX_LISTED = 1000
# a 429 or a 5xx is retried this many times, waiting Retry-After or these seconds
RETRY_WAITS = (2.0, 5.0, 10.0, 20.0, 40.0)
# a spreadsheet id that cannot exist (ids are 44 characters): the connection test's probe
PROBE_ID = "tabularia-connection-test"
# Sheets' day zero: serial 1 is 1899-12-31, as in Excel without its 1900 leap-year bug
EPOCH = datetime(1899, 12, 30)
FORMULA_ERRORS = frozenset({"#N/A", "#REF!", "#VALUE!", "#DIV/0!", "#NUM!", "#NAME?", "#NULL!", "#ERROR!",
                            "#SPILL!", "#CALC!"})
_DATE_KINDS = {"DATE": "date", "DATE_TIME": "datetime", "TIME": "time"}
_ID = re.compile(r"^[A-Za-z0-9_-]{20,200}$")
_URL_ID = re.compile(r"/spreadsheets/(?:u/\d+/)?d/([A-Za-z0-9_-]+)")
_URL_GID = re.compile(r"[#?&]gid=(\d+)")
_A1 = re.compile(r"^([A-Za-z]{1,3})(\d+)?(?::([A-Za-z]{1,3})(\d+)?)?$")


class GsheetsError(IngestError):
    """A message for the user (it ends up in the run or in the response). `code` says
    what Google refused, for whoever must tell the cases apart (the connection test):
    key | service_disabled | no_access | not_found | quota | other."""

    def __init__(self, message: str, code: str = "other", service: str = ""):
        super().__init__(message if message.startswith(PREFIX) else PREFIX + message)
        self.code = code
        self.service = service


class GsheetsConnectionSpec(BaseModel):
    # the service account's whole JSON key, Fernet-encrypted (the key shared with the
    # gateway): decrypted only here, to sign the token request
    key_encrypted: str
    # the account's email and project, for the messages (the gateway read them from the
    # key when it was saved); the key decides, these only explain
    client_email: str = ""
    project_id: str = ""


class GsheetsSourceSpec(BaseModel):
    spreadsheet_id: str
    # the tab's id (`gid` in its link): a renamed tab is still found by it
    sheet_id: int | None = None
    # the tab's title, when it was chosen: used when there is no id (or the id is gone)
    sheet: str = ""
    # A1 notation inside the tab (A1:F500, B3:H, A:F); empty = the whole tab
    range: str = ""


# ── the key ─────────────────────────────────────────────────────────────────────
def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def parse_key(text: str) -> dict:
    """The service account key as a dict, or a message saying which JSON it is not."""
    try:
        key = json.loads(text)
    except (TypeError, ValueError):
        raise GsheetsError("the stored key is not valid JSON: paste the service account's JSON key again") from None
    if not isinstance(key, dict) or key.get("type") != "service_account":
        raise GsheetsError("the stored key is not a service account key: paste the JSON key of a service account again")
    for field in ("client_email", "private_key"):
        if not str(key.get(field) or "").strip():
            raise GsheetsError(f"the service account key has no {field}: download a new JSON key")
    return key


def signed_assertion(key: dict, now: float) -> str:
    """The JWT the token endpoint takes for a service account (RS256, one hour)."""
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding, rsa

    try:
        private = serialization.load_pem_private_key(str(key["private_key"]).encode(), password=None)
    except (ValueError, TypeError):
        raise GsheetsError("the private key in the service account's JSON cannot be read: download a new JSON key") from None
    if not isinstance(private, rsa.RSAPrivateKey):
        raise GsheetsError("the service account's private key is not an RSA key: download a new JSON key")
    header = {"alg": "RS256", "typ": "JWT"}
    if key.get("private_key_id"):
        header["kid"] = str(key["private_key_id"])
    claims = {"iss": key["client_email"], "scope": " ".join(SCOPES), "aud": TOKEN_URL,
              "iat": int(now), "exp": int(now) + 3600}
    signing_input = ".".join(_b64(json.dumps(p, separators=(",", ":")).encode()) for p in (header, claims))
    signature = private.sign(signing_input.encode(), padding.PKCS1v15(), hashes.SHA256())
    return f"{signing_input}.{_b64(signature)}"


# ── what the user writes ─────────────────────────────────────────────────────────
def spreadsheet_ref(text: str) -> tuple[str, int | None]:
    """The spreadsheet's id, and the tab's id when a link names one (`#gid=…`), from
    a link copied from the browser or from the bare id."""
    t = (text or "").strip()
    if not t:
        raise GsheetsError("paste the spreadsheet's link (https://docs.google.com/spreadsheets/d/…) or its ID")
    m = _URL_ID.search(t)
    if m:
        g = _URL_GID.search(t)
        return m.group(1), int(g.group(1)) if g else None
    if "drive.google.com" in t or "/document/d/" in t or "/presentation/d/" in t:
        raise GsheetsError("this link is not a Google Sheets spreadsheet: open the spreadsheet and copy its link "
                           "(https://docs.google.com/spreadsheets/d/…). An Excel file in Drive must be saved as "
                           "Google Sheets first (File → Save as Google Sheets)")
    if _ID.match(t):
        return t, None
    raise GsheetsError("this is neither a spreadsheet's link nor its ID: copy the link from the browser's address bar "
                       "(https://docs.google.com/spreadsheets/d/…)")


def _column_index(letters: str) -> int:
    n = 0
    for ch in letters.upper():
        n = n * 26 + (ord(ch) - 64)
    return n


def column_letters(n: int) -> str:
    out = ""
    while n:
        n, r = divmod(n - 1, 26)
        out = chr(65 + r) + out
    return out


def parse_range(text: str) -> tuple[int, int, int, int | None] | None:
    """A1 inside the tab → (first column, first row, last column, last row or None);
    None for no range. Columns and rows count from 1."""
    t = (text or "").strip().replace(" ", "")
    if not t:
        return None
    if "!" in t:
        raise GsheetsError(f"the range '{text}' names a tab: choose the tab above and write only the cells (A1:F500)")
    m = _A1.match(t)
    if not m or not m.group(3):
        raise GsheetsError(f"'{text}' is not a range: write it as A1:F500, B3:H (to the last row) or A:F")
    c1, r1, c2, r2 = _column_index(m.group(1)), int(m.group(2) or 1), _column_index(m.group(3)), m.group(4)
    r2i = int(r2) if r2 else None
    if r1 < 1 or (r2i is not None and r2i < r1) or c2 < c1:
        raise GsheetsError(f"the range '{text}' ends before it starts: write the top-left cell first (A1:F500)")
    return c1, r1, c2, r2i


def quoted_tab(title: str) -> str:
    """A tab's title as A1 notation wants it: always quoted, inner quotes doubled."""
    return "'" + title.replace("'", "''") + "'"


# ── the client ───────────────────────────────────────────────────────────────────
class GoogleClient:
    """The few calls needed, the token cached, a 429 or a 5xx retried (Retry-After)."""

    def __init__(self, conn: GsheetsConnectionSpec, http: Callable[..., Any] | None = None, sleep=time.sleep,
                 clock=time.time):
        self.conn = conn
        if http is None:
            import requests

            http = requests.Session().request
        self._http = http
        self._sleep = sleep
        self._clock = clock
        self._key: dict | None = None
        self._token = ""
        self._token_until = 0.0

    @property
    def key(self) -> dict:
        if self._key is None:
            self._key = parse_key(decrypt_secret(self.conn.key_encrypted))
        return self._key

    @property
    def email(self) -> str:
        return str(self.key.get("client_email") or self.conn.client_email)

    @property
    def project(self) -> str:
        return str(self.key.get("project_id") or self.conn.project_id)

    def _send(self, method: str, url: str, **kw):
        try:
            return self._http(method, url, timeout=(10, 120), **kw)
        except Exception as e:  # noqa: BLE001 — DNS, TLS, refused: a short message, the rest in the log
            logger.warning("google %s %s: %s", method, url.split("?")[0], e)
            raise GsheetsError(f"Google is not reachable from the worker ({type(e).__name__})") from None

    def _access_token(self) -> str:
        if self._token and self._clock() < self._token_until - 60:
            return self._token
        r = self._retrying(lambda: self._send("POST", TOKEN_URL, data={
            "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
            "assertion": signed_assertion(self.key, self._clock()),
        }))
        if r.status_code == 429 or r.status_code >= 500:
            raise self._error(r, "the access token")
        if r.status_code != 200:
            raise GsheetsError(self._token_message(r), "key")
        body = _json_of(r)
        self._token = str(body.get("access_token") or "")
        if not self._token:
            raise GsheetsError("Google answered the token request without a token")
        self._token_until = self._clock() + float(body.get("expires_in") or 3600)
        return self._token

    def _token_message(self, r) -> str:
        body = _json_of(r)
        error = str(body.get("error") or r.status_code)
        description = str(body.get("error_description") or "")
        low = description.lower()
        account = self.email
        if "signature" in low:
            return (f"Google refused the key of {account}: it was deleted or revoked. In the Google Cloud console, "
                    "IAM & Admin → Service accounts → this account → Keys, add a new JSON key and paste it into "
                    "the connection")
        if "not found" in low or error == "invalid_client":
            return (f"the service account {account} does not exist any more, or is disabled: check it in the Google "
                    "Cloud console (IAM & Admin → Service accounts), or paste the key of another account")
        if "timeframe" in low or "short-lived" in low:
            return "Google refused the token request because the worker's clock is wrong: set its time (NTP)"
        if "disabled" in low:
            return f"the service account {account} is disabled: enable it in the Google Cloud console"
        return f"Google refused the service account's key ({error}{': ' + description if description else ''})"

    def _retrying(self, send: Callable[[], Any]):
        """`send()` again on a 429 or a 5xx, waiting what Retry-After says (or a growing
        pause); the last answer when they do not end."""
        for wait in RETRY_WAITS:
            r = send()
            if r.status_code not in (429, 500, 502, 503, 504):
                return r
            self._sleep(min(60.0, _retry_after(r) or wait))
        return send()

    def get(self, url: str, params: dict | list | None = None, *, what: str) -> dict:
        """A GET with the token; the JSON, or a GsheetsError saying what to do."""
        def send():
            return self._send("GET", url, params=params, headers={"Authorization": f"Bearer {self._access_token()}"})

        r = self._retrying(send)
        if r.status_code == 401:
            self._token = ""  # revoked meanwhile, or expired early: one new token, once
            r = self._retrying(send)
        if r.status_code == 200:
            return _json_of(r)
        raise self._error(r, what)

    def _error(self, r, what: str) -> GsheetsError:
        """Google's refusal as what to do about it. `what`: what was being read, as a
        noun («the spreadsheet»)."""
        err = _json_of(r).get("error")
        err = err if isinstance(err, dict) else {"message": str(err or "")}
        message = str(err.get("message") or "")
        status = str(err.get("status") or "")
        details = [d for d in err.get("details") or [] if isinstance(d, dict)]
        reasons = {str(d.get("reason") or "") for d in details}
        service = next((str((d.get("metadata") or {}).get("service") or "") for d in details
                        if (d.get("metadata") or {}).get("service")), "")
        low = message.lower()
        code = r.status_code
        if code == 403 and (reasons & {"SERVICE_DISABLED", "ACCESS_NOT_CONFIGURED"}
                            or "has not been used in project" in low or "it is disabled" in low):
            drive = "drive" in (service or low)
            api, name = ("drive.googleapis.com", "Google Drive API") if drive else ("sheets.googleapis.com", "Google Sheets API")
            project = self.project
            return GsheetsError(
                f"enable the {name} in the Google Cloud project {project or 'of the service account'}: "
                f"https://console.cloud.google.com/apis/library/{api}" + (f"?project={project}" if project else "")
                + " (it takes a few minutes to apply)", "service_disabled", api)
        if code == 403:
            return GsheetsError(f"the service account cannot open {what}: share the spreadsheet with {self.email} "
                                "(Share → add the email → Viewer)", "no_access")
        if code == 404:
            return GsheetsError(f"{what} was not found: check the link, and that the spreadsheet is shared with "
                                f"{self.email}", "not_found")
        if code == 400 and "not supported for this document" in low:
            return GsheetsError(f"{what} is an Excel file stored in Drive, not a Google Sheets spreadsheet: open it "
                                "and choose File → Save as Google Sheets, then use the new spreadsheet", "other")
        if code == 400 and "unable to parse range" in low:
            return GsheetsError(f"the tab or the range does not exist in {what} ({message})", "not_found")
        if code == 429:
            return GsheetsError(f"Google's quota of read requests is used up (60 a minute for each service account) "
                                f"while reading {what}: try again in a minute, or refresh less often", "quota")
        if code == 401:
            return GsheetsError("Google did not accept the access token: test the connection again", "key")
        return GsheetsError(f"error {code} from Google while reading {what}"
                            + (f" ({status})" if status else "") + (f": {message}" if message else ""))

    # -- Drive --
    def spreadsheets(self) -> tuple[list[dict], bool]:
        """The spreadsheets shared with the account (name order), and whether there are
        more than MAX_LISTED."""
        out: list[dict] = []
        token = ""
        while True:
            params = {
                "q": f"mimeType='{SPREADSHEET_MIME}' and trashed=false",
                "fields": "nextPageToken,files(id,name,modifiedTime,owners(displayName,emailAddress))",
                "pageSize": 100, "supportsAllDrives": "true", "includeItemsFromAllDrives": "true",
                **({"pageToken": token} if token else {}),
            }
            page = self.get(f"{DRIVE_BASE}/files", params, what="the list of the spreadsheets (Google Drive)")
            for f in page.get("files") or []:
                owners = f.get("owners") or [{}]
                out.append({"id": str(f.get("id") or ""), "name": str(f.get("name") or ""),
                            "modified_at": str(f.get("modifiedTime") or ""),
                            "owner": str(owners[0].get("displayName") or owners[0].get("emailAddress") or "")})
            token = str(page.get("nextPageToken") or "")
            if not token or len(out) >= MAX_LISTED:
                break
        out.sort(key=lambda f: f["name"].casefold())
        return out[:MAX_LISTED], bool(token)

    # -- Sheets --
    def metadata(self, spreadsheet_id: str) -> dict:
        """The spreadsheet's title and its tabs, in order."""
        body = self.get(
            f"{SHEETS_BASE}/spreadsheets/{quote(spreadsheet_id, safe='')}",
            {"fields": "properties.title,sheets.properties(sheetId,title,index,sheetType,hidden,gridProperties(rowCount,columnCount))"},
            what="the spreadsheet",
        )
        tabs = []
        for s in body.get("sheets") or []:
            p = s.get("properties") or {}
            grid = p.get("gridProperties") or {}
            tabs.append({"sheet_id": int(p.get("sheetId") or 0), "title": str(p.get("title") or ""),
                         "index": int(p.get("index") or 0), "hidden": bool(p.get("hidden")),
                         "grid": (p.get("sheetType") or "GRID") == "GRID",
                         "rows": int(grid.get("rowCount") or 0), "columns": int(grid.get("columnCount") or 0)})
        tabs.sort(key=lambda t: t["index"])
        return {"id": spreadsheet_id, "title": str((body.get("properties") or {}).get("title") or ""), "tabs": tabs}

    def values(self, spreadsheet_id: str, a1: str) -> list[list]:
        body = self.get(
            f"{SHEETS_BASE}/spreadsheets/{quote(spreadsheet_id, safe='')}/values/{quote(a1, safe='')}",
            {"valueRenderOption": "UNFORMATTED_VALUE", "dateTimeRenderOption": "SERIAL_NUMBER", "majorDimension": "ROWS"},
            what="the spreadsheet",
        )
        return [row if isinstance(row, list) else [] for row in body.get("values") or []]

    def number_formats(self, spreadsheet_id: str, a1: str) -> list[list[str]]:
        """The number format's type of each cell of the range (DATE, TIME, DATE_TIME,
        NUMBER…; "" for none): the only way to tell a date from a number once the values
        come as serial numbers."""
        body = self.get(
            f"{SHEETS_BASE}/spreadsheets/{quote(spreadsheet_id, safe='')}",
            {"ranges": a1, "fields": "sheets(data(rowData(values(effectiveFormat(numberFormat(type))))))"},
            what="the spreadsheet",
        )
        rows: list[list[str]] = []
        for sheet in body.get("sheets") or []:
            for data in sheet.get("data") or []:
                for row in data.get("rowData") or []:
                    rows.append([str((((c or {}).get("effectiveFormat") or {}).get("numberFormat") or {}).get("type") or "")
                                 for c in row.get("values") or []])
        return rows


def _json_of(r) -> dict:
    try:
        body = r.json()
    except Exception:  # noqa: BLE001 — an HTML error page, an empty body
        return {}
    return body if isinstance(body, dict) else {}


def _retry_after(r) -> float | None:
    try:
        return max(0.0, float((r.headers or {}).get("Retry-After")))
    except (TypeError, ValueError):
        return None


# ── the tab, the cells, the table ────────────────────────────────────────────────
def find_tab(meta: dict, source: GsheetsSourceSpec) -> dict:
    tabs = meta["tabs"]
    if not tabs:
        raise GsheetsError(f"the spreadsheet '{meta['title']}' has no tabs")
    tab = None
    if source.sheet_id is not None:
        tab = next((t for t in tabs if t["sheet_id"] == source.sheet_id), None)
    if tab is None and source.sheet.strip():
        wanted = source.sheet.strip()
        tab = (next((t for t in tabs if t["title"] == wanted), None)
               or next((t for t in tabs if t["title"].casefold() == wanted.casefold()), None))
    if tab is None and source.sheet_id is None and not source.sheet.strip():
        tab = tabs[0]
    if tab is None:
        names = ", ".join(f"'{t['title']}'" for t in tabs[:20])
        raise GsheetsError(f"the tab '{source.sheet or source.sheet_id}' is not in the spreadsheet '{meta['title']}' "
                           f"any more (deleted, or renamed and recreated). Its tabs: {names}")
    if not tab["grid"]:
        raise GsheetsError(f"the tab '{tab['title']}' is a chart, not a grid of cells: choose another tab")
    return tab


def _bounds(tab: dict, source: GsheetsSourceSpec) -> tuple[int, int, int, int]:
    """The cells to read: first column, first row (the header), last column, last row."""
    rows, cols = tab["rows"], tab["columns"]
    if rows < 1 or cols < 1:
        raise GsheetsError(f"the tab '{tab['title']}' is empty")
    parsed = parse_range(source.range)
    if parsed is None:
        return 1, 1, cols, rows
    c1, r1, c2, r2 = parsed
    if c1 > cols or r1 > rows:
        raise GsheetsError(f"the range '{source.range}' starts outside the tab '{tab['title']}' "
                           f"({column_letters(cols)}{rows} is its last cell)")
    return c1, r1, min(c2, cols), min(r2 or rows, rows)


def _date_kinds(types: list[list[str]], width: int) -> dict[int, str]:
    """The columns whose cells are formatted as dates, times or date-times (most of the
    formatted cells of the sample): {column index: date | datetime | time}."""
    kinds: dict[int, str] = {}
    for j in range(width):
        seen = Counter(row[j] for row in types if j < len(row) and row[j])
        dated = sum(n for t, n in seen.items() if t in _DATE_KINDS)
        if dated and dated > sum(seen.values()) - dated:
            kinds[j] = ("datetime" if seen["DATE_TIME"] else "date" if seen["DATE"] else "time")
    return kinds


def _from_serial(value: float, kind: str) -> str | float:
    """A serial number (days since 1899-12-30, the time as the fraction) as the text the
    CSV reader turns into a date or a date-time. A time stays a number here: the whole
    column decides (see _times)."""
    if not math.isfinite(value) or kind == "time":
        return value
    try:
        if kind == "date":
            return (EPOCH + timedelta(days=math.floor(value))).date().isoformat()
        ms = round(value * 86_400_000)
        moment = EPOCH + timedelta(milliseconds=ms)
    except OverflowError:  # a number formatted as a date beyond year 9999: kept as it is
        return value
    return moment.isoformat(sep=" ", timespec="milliseconds" if ms % 1000 else "seconds")


def _times(df: pl.DataFrame, columns: list[str]) -> pl.DataFrame:
    """The columns formatted as times become times of day when every value is one (a
    fraction of a day); a value of 24 hours or more makes the column a duration, which
    stays a number (of days)."""
    for c in columns:
        col = df[c]
        if not col.dtype.is_numeric() or col.null_count() == len(col):
            continue
        if col.min() >= 0 and col.max() < 1:
            df = df.with_columns(((pl.col(c) * 86_400_000).round().cast(pl.Int64) * 1_000_000).cast(pl.Time).alias(c))
    return df


def _cell_text(value, kind: str | None) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        if kind:
            value = _from_serial(float(value), kind)
            if isinstance(value, str):
                return value
        if isinstance(value, float) and value.is_integer() and abs(value) < 2**53:
            return str(int(value))
        return repr(value)
    return str(value)


def read_tab(client: GoogleClient, source: GsheetsSourceSpec, workdir: str, max_rows: int | None = None,
             progress: Callable[..., None] | None = None) -> tuple[pl.DataFrame, dict]:
    """The tab (or its range) as a table, and what was read: spreadsheet, tab, range,
    rows, formula errors. `max_rows`: only the first data rows (the preview)."""
    advances = progress or (lambda *a, **k: None)
    meta = client.metadata(source.spreadsheet_id)
    tab = find_tab(meta, source)
    c1, r1, c2, r2 = _bounds(tab, source)
    if max_rows is not None:
        r2 = min(r2, r1 + max_rows)
    width_cols = c2 - c1 + 1
    per_page = max(100, min(MAX_PAGE_ROWS, CELLS_PER_PAGE // width_cols))
    where = f"the tab '{tab['title']}'" + (f", range {source.range.strip()}" if source.range.strip() else "")
    shown_as = f"'{meta['title']}'"

    def a1(first: int, last: int) -> str:
        return f"{quoted_tab(tab['title'])}!{column_letters(c1)}{first}:{column_letters(c2)}{last}"

    header: list | None = None
    width = 0
    kinds: dict[int, str] = {}
    texts: list[bool] = []  # per column: every non-empty cell is text in Sheets
    rows = 0
    errors = 0
    path = os.path.join(workdir, "sheet.csv")
    with open(path, "w", newline="", encoding="utf-8") as fh:
        out = csv.writer(fh)
        start = r1
        while start <= r2:
            end = min(r2, start + per_page - 1)
            page = client.values(source.spreadsheet_id, a1(start, end))
            for offset, row in enumerate(page):
                if header is None:
                    if offset != 0 or not any(v not in (None, "") for v in row):
                        raise GsheetsError(f"{shown_as}: the first row of {where} is empty. The header must be on the "
                                           "first row: remove the empty rows above the table, or start the range at it")
                    names = list(row)
                    while names and names[-1] in (None, ""):
                        names.pop()
                    check_header(names, shown_as, where, GsheetsError)
                    header = [str(n).strip() for n in names]
                    width = len(header)
                    texts = [True] * width
                    out.writerow(header)
                    if r2 > r1:
                        sample = a1(r1 + 1, min(r2, r1 + FORMAT_SAMPLE_ROWS))
                        kinds = _date_kinds(client.number_formats(source.spreadsheet_id, sample), width)
                    continue
                beyond = [i for i, v in enumerate(row[width:]) if v not in (None, "")]
                if beyond:
                    col = column_letters(c1 + width + beyond[0])
                    raise GsheetsError(f"{shown_as}: row {start + offset} of {where} has a value in column {col}, "
                                       "after the last named column. Give that column a name in the header, or "
                                       "narrow the range")
                cells: list[str] = []
                for j in range(width):
                    v = row[j] if j < len(row) else None
                    if isinstance(v, str) and v.strip() in FORMULA_ERRORS:
                        errors += 1
                        v = None
                    if v is None or v == "":
                        cells.append("")
                        continue
                    if not isinstance(v, str):
                        texts[j] = False
                    cells.append(_cell_text(v, kinds.get(j)))
                if any(cells):
                    out.writerow(cells)
                    rows += 1
            advances("reading", rows=rows)
            if header is None:
                raise GsheetsError(f"{shown_as}: {where} is empty")
            start = end + 1
    if header is None:
        raise GsheetsError(f"{shown_as}: {where} is empty")
    # the CSV import's inference, except for the columns Sheets holds as text in every cell
    text_columns = {header[j]: pl.String for j in range(width) if texts[j]}
    try:
        df = pl.read_csv(path, has_header=True, infer_schema_length=None, try_parse_dates=True,
                         schema_overrides=text_columns, encoding="utf8")
    except Exception as e:  # noqa: BLE001 — a cell the inference cannot take: said, not hidden
        raise GsheetsError(f"{shown_as}: the cells of {where} could not be read as a table ({e})") from e
    os.remove(path)
    df = _times(df, [header[j] for j, kind in kinds.items() if kind == "time"])
    info = {"spreadsheet": {"id": meta["id"], "title": meta["title"]},
            "sheet": {"id": tab["sheet_id"], "title": tab["title"]},
            "range": f"{column_letters(c1)}{r1}:{column_letters(c2)}{r2}",
            "rows": df.height, "formula_errors": errors}
    if errors:
        logger.info("gsheets %s %s: %d formula errors read as NULL", meta["id"], tab["title"], errors)
    return df, info


# ── what the routes and the worker call ──────────────────────────────────────────
def test_connection(client: GoogleClient) -> dict:
    """Everything that can be wrong with the connection: the key (a token), the Sheets
    API enabled in the account's project, and — when the Drive API is on — how many
    spreadsheets are shared with the account. A refusal is an error saying what to do."""
    client._access_token()
    # an id that does not exist: an API turned off answers 403 SERVICE_DISABLED before
    # looking for it; one turned on says the spreadsheet is not there (404, or 400/403)
    try:
        client.get(f"{SHEETS_BASE}/spreadsheets/{PROBE_ID}", {"fields": "spreadsheetId"}, what="the Sheets API check")
    except GsheetsError as e:
        if e.code in ("service_disabled", "key", "quota"):
            raise
    result: dict[str, Any] = {"ok": True, "client_email": client.email, "project_id": client.project}
    try:
        files, more = client.spreadsheets()
        result.update(drive=True, spreadsheets=len(files), more=more)
    except GsheetsError as e:
        if e.code != "service_disabled":
            raise
        # no Drive API: no list to choose from, the datasource takes the spreadsheet's link
        result.update(drive=False, spreadsheets=None, more=False)
    return result


def tabs(client: GoogleClient, spreadsheet: str) -> dict:
    """The tabs of a spreadsheet given by link or id, with the tab the link pointed at."""
    spreadsheet_id, gid = spreadsheet_ref(spreadsheet)
    meta = client.metadata(spreadsheet_id)
    return {**meta, "selected_sheet_id": gid if any(t["sheet_id"] == gid for t in meta["tabs"]) else None}


def preview(client: GoogleClient, source: GsheetsSourceSpec, limit: int = 20) -> dict:
    """The first rows as the import would read them: the columns with their types."""
    with tempfile.TemporaryDirectory() as tmp:
        df, info = read_tab(client, source, tmp, max_rows=limit)
    head = df.head(limit)
    return {
        **info,
        "columns": [{"name": n, "dtype": str(t)} for n, t in head.schema.items()],
        "rows": [[None if v is None else v if isinstance(v, (int, float, bool, str)) else str(v) for v in r]
                 for r in head.rows()],
    }


def ingest_gsheets_to_parquet(conn: GsheetsConnectionSpec, source: GsheetsSourceSpec, bucket: str, key: str, storage,
                              client: GoogleClient | None = None,
                              progress: Callable[..., None] | None = None) -> dict[str, Any]:
    """The tab read whole, written as the datasource's new parquet. `progress` hears
    «reading» with the rows so far, then «saving»."""
    advances = progress or (lambda *a, **k: None)
    client = client or GoogleClient(conn)
    with tempfile.TemporaryDirectory() as tmp:
        table, info = read_tab(client, source, tmp, progress=advances)
        advances("saving", rows=table.height)
        out = os.path.join(tmp, "out.parquet")
        table.write_parquet(out, compression="zstd", row_group_size=get_settings().ingest.parquet_row_group_rows)
        storage.upload_file(out, bucket, key)
    return {
        "status": "success",
        "bucket": bucket,
        "output_key": key,
        "rows_written": table.height,
        "columns": [{"name": n, "dtype": str(t)} for n, t in table.schema.items()],
        **info,
    }
