"""SharePoint source: Excel (and CSV) files of a document library → parquet.

For finance the real data lives in Excel on SharePoint. Here one defines a SITE (the
connection) and, for every datasource, a PATH — globs too — and a SHEET. A path that
matches several files stacks them into one table.

The contract on the file is deliberately strict: it must be MACHINE READABLE — the
header on the first row, one name per column, no duplicates. No "skip N rows" and no
heuristics: a file with a title above the table is fixed at the source. The reading
library alone would NOT refuse such files: it makes up `__UNNAMED__1` for an empty
cell and `id_1` for a duplicate, and dirty data would pass silently. That is why the
raw header is validated here.

Access through Microsoft Graph with the credentials of an APPLICATION registered on
Entra ID (client credentials). The right permission to ask IT for is `Sites.Selected`:
the app sees only the sites granted to it, not the whole of SharePoint. No Microsoft
library: `requests` and four calls are enough.

NOT tried against a real tenant yet (there is none in development): the tests run
against a fake Graph that keeps to its documented contract.
"""
from __future__ import annotations

import fnmatch
import logging
import os
import tempfile
import time
from dataclasses import dataclass
from typing import Any, Callable, Literal
from urllib.parse import quote, unquote, urlparse

import polars as pl
from pydantic import BaseModel, Field

from app.core.config import get_settings
from app.core.crypto import decrypt_secret
from app.ingest import file_templates as ft
from app.ingest.converters import IngestError
from app.ingest.models import CsvOptions

logger = logging.getLogger(__name__)

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
LOGIN_BASE = "https://login.microsoftonline.com"
EXCEL_EXTENSIONS = (".xlsx", ".xlsm", ".xlsb", ".xls")
CSV_EXTENSIONS = (".csv", ".tsv")
# which files a datasource takes, by its format: «excel» for the ones saved before CSV
# was read (a `Budget/*` of theirs must not start taking the CSV next to the workbooks)
EXTENSIONS = {"excel": EXCEL_EXTENSIONS, "csv": CSV_EXTENSIONS, "auto": EXCEL_EXTENSIONS + CSV_EXTENSIONS}
# provenance columns added to every row: finance needs to know which FILE a number
# comes from, and month or branch are often only in the file's name
FILE_COLUMN = "_file"
MODIFIED_COLUMN = "_file_modified_at"
# safety caps: a wrong glob (`**/*`) must not download a whole site
MAX_FILES = 500
MAX_FOLDERS = 2000
MAX_TOTAL_BYTES = 2 * 1024**3


class SharePointError(IngestError):
    """An error the user can read (it ends in the ingest's outcome)."""


class _NotFound(SharePointError):
    """A 404 from Graph: apart because for a FOLDER it is not an error (see children)."""


class SharePointConnectionSpec(BaseModel):
    tenant_id: str
    client_id: str
    client_secret_encrypted: str
    site_url: str  # es. https://contoso.sharepoint.com/sites/Finance
    # the document library; empty = the site's default one ("Documents")
    library: str = ""
    # Empty = the deployment's (SHAREPOINT__GRAPH_BASE / SHAREPOINT__LOGIN_BASE, for
    # sovereign clouds). The gateway never sets them: the user does not choose them.
    graph_base: str = ""
    login_base: str = ""

    def model_post_init(self, _ctx) -> None:
        cfg = get_settings().sharepoint
        self.graph_base = (self.graph_base or cfg.graph_base).rstrip("/")
        self.login_base = (self.login_base or cfg.login_base).rstrip("/")


class SharePointSourceSpec(ft.FileTemplates):
    # the path in the library, with globs: `Budget/2026/*.xlsx`, `**/closing_*.xlsx`;
    # with placeholders and the datasource's variables: `Chiusure/{month:yyyy-MM}.xlsx`
    path: str
    # auto = Excel and CSV, each by its extension; a datasource saved without it: Excel only
    format: Literal["auto", "excel", "csv"] = "excel"
    csv: CsvOptions = Field(default_factory=CsvOptions)
    sheet: str = ""  # Excel: empty = the first sheet


@dataclass
class RemoteFile:
    id: str
    path: str  # relative to the library's root
    size: int
    modified_at: str
    etag: str = ""


# ── Client Graph ────────────────────────────────────────────────────────────────
class GraphClient:
    """The four calls needed, with the token cached and the throttling respected (429/503
    + Retry-After: SharePoint really uses it)."""

    def __init__(self, conn: SharePointConnectionSpec, http: Callable[..., Any] | None = None, sleep=time.sleep):
        self.conn = conn
        if http is None:
            import requests

            http = requests.Session().request
        self._http = http
        self._sleep = sleep
        self._token = ""
        self._token_until = 0.0
        self._drive_id = ""

    # -- autenticazione --
    def _access_token(self) -> str:
        if self._token and time.time() < self._token_until - 60:
            return self._token
        r = self._http(
            "POST",
            f"{self.conn.login_base.rstrip('/')}/{self.conn.tenant_id}/oauth2/v2.0/token",
            data={
                "grant_type": "client_credentials",
                "client_id": self.conn.client_id,
                "client_secret": decrypt_secret(self.conn.client_secret_encrypted),
                "scope": self.conn.graph_base.split("/v1.0")[0].rstrip("/") + "/.default",
            },
            timeout=30,
        )
        if r.status_code != 200:
            raise SharePointError(
                "Microsoft refused access: check the app's tenant, client id and secret "
                f"({_graph_message(r)})"
            )
        body = r.json()
        self._token = body["access_token"]
        self._token_until = time.time() + float(body.get("expires_in", 3600))
        return self._token

    def _get(self, url: str, *, stream: bool = False):
        for attempt in range(4):
            r = self._http("GET", url, headers={"Authorization": f"Bearer {self._access_token()}"}, timeout=120, stream=stream)
            if r.status_code in (429, 503) and attempt < 3:
                # Retry-After may be an HTTP date, not only seconds
                try:
                    wait = float(r.headers.get("Retry-After", 2 ** attempt))
                except (TypeError, ValueError):
                    wait = float(2 ** attempt)
                if stream:
                    try:
                        r.close()
                    except Exception:
                        pass
                self._sleep(min(30.0, wait))
                continue
            return r
        return r

    def _json(self, url: str, what: str) -> dict:
        r = self._get(url)
        if r.status_code == 404:
            raise _NotFound(f"{what}: not found")
        if r.status_code in (401, 403):
            raise SharePointError(
                f"{what}: access denied. The app needs the Sites.Selected permission "
                f"AND access granted to this site ({_graph_message(r)})"
            )
        if r.status_code != 200:
            raise SharePointError(f"{what}: error {r.status_code} from Microsoft Graph ({_graph_message(r)})")
        return r.json()

    # -- site and library --
    def drive_id(self) -> str:
        if self._drive_id:
            return self._drive_id
        u = urlparse(self.conn.site_url.strip())
        if not u.hostname:
            raise SharePointError("Invalid site URL: expected https://<company>.sharepoint.com/sites/<name>")
        rel = u.path.rstrip("/")
        base = self.conn.graph_base.rstrip("/")
        site = self._json(f"{base}/sites/{u.hostname}:{quote(rel)}" if rel else f"{base}/sites/{u.hostname}", "SharePoint site")
        if not self.conn.library.strip():
            self._drive_id = self._json(f"{base}/sites/{site['id']}/drive", "Default document library")["id"]
            return self._drive_id
        wanted = self.conn.library.strip().casefold()
        drives = self._json(f"{base}/sites/{site['id']}/drives", "Document libraries of the site").get("value", [])
        for d in drives:
            if str(d.get("name", "")).casefold() == wanted:
                self._drive_id = d["id"]
                return self._drive_id
        names = ", ".join(sorted(str(d.get("name")) for d in drives)) or "none"
        raise SharePointError(f"Document library '{self.conn.library}' not found in the site. Available: {names}")

    def children(self, folder: str) -> list[dict]:
        base = self.conn.graph_base.rstrip("/")
        drive = self.drive_id()
        folder = folder.strip("/")
        url = f"{base}/drives/{drive}/root:/{quote(folder)}:/children" if folder else f"{base}/drives/{drive}/root/children"
        out: list[dict] = []
        while url:
            try:
                page = self._json(url, f"Folder '{folder or '/'}'")
            except _NotFound:
                # a folder that is not there is, for whoever writes a glob, "no file
                # matches" — not a different error to explain on its own
                return []
            out.extend(page.get("value", []))
            url = page.get("@odata.nextLink", "")
        return out

    def download(self, item_id: str, dest: str) -> None:
        r = self._get(f"{self.conn.graph_base.rstrip('/')}/drives/{self.drive_id()}/items/{item_id}/content", stream=True)
        if r.status_code != 200:
            try:
                r.close()
            except Exception:
                pass
            raise SharePointError(f"Download failed ({r.status_code}): {_graph_message(r)}")
        try:
            with open(dest, "wb") as f:
                for chunk in r.iter_content(1024 * 1024):
                    f.write(chunk)
        finally:
            try:
                r.close()  # the stream is closed even when the write fails
            except Exception:
                pass


def _graph_message(r) -> str:
    try:
        body = r.json()
        err = body.get("error")
        if isinstance(err, dict):
            return str(err.get("message") or err.get("code") or r.status_code)
        return str(body.get("error_description") or err or r.status_code).split("\r\n")[0]
    except Exception:
        return str(r.status_code)


# ── Globs on the library ─────────────────────────────────────────────────────────
def _has_wildcard(s: str) -> bool:
    return any(c in s for c in "*?[")


def find_files(client: GraphClient, pattern: str, extensions: tuple[str, ...] = EXCEL_EXTENSIONS) -> list[RemoteFile]:
    """I file (Excel, or those of `extensions`) of the library that match `pattern`. Segment by segment:
    the parts without wildcards are crossed without listing, `*` lists a folder, `**`
    goes down to any depth. Case-insensitive, as SharePoint. Office's lock files
    (`~$…`) are left out, which a `*.xlsx` would take while someone has the file open."""
    parts = [p for p in pattern.replace("\\", "/").strip("/").split("/") if p]
    if not parts:
        raise SharePointError("Empty path: enter a file or a glob, e.g. Budget/2026/*.xlsx")
    listed = 0
    found: dict[str, RemoteFile] = {}

    def listing(folder: str) -> list[dict]:
        nonlocal listed
        listed += 1
        if listed > MAX_FOLDERS:
            raise SharePointError(f"The path goes through more than {MAX_FOLDERS} folders: narrow the glob")
        return client.children(folder)

    def walk(folder: str, rest: list[str]) -> None:
        if not rest:
            return
        seg, tail = rest[0], rest[1:]
        if seg == "**":
            walk(folder, tail)  # `**` matches zero folders too
            for it in listing(folder):
                if "folder" in it:
                    walk(f"{folder}/{it['name']}".strip("/"), rest)
            return
        if tail and not _has_wildcard(seg):
            walk(f"{folder}/{seg}".strip("/"), tail)  # a fixed part: no listing
            return
        for it in listing(folder):
            name = str(it.get("name", ""))
            if not fnmatch.fnmatchcase(name.casefold(), seg.casefold()):
                continue
            # the TRUE path (with SharePoint's case), not the one written in the glob:
            # `_file` must not change when someone writes the path otherwise
            # Graph returns it percent-encoded ("Budget%202026"): decoded, or children()
            # would encode it twice and the folder would give 404
            parent = str((it.get("parentReference") or {}).get("path") or "")
            real = unquote(parent.split("root:", 1)[1]).strip("/") if "root:" in parent else folder
            path = f"{real}/{name}".strip("/")
            if tail:
                if "folder" in it:
                    walk(path, tail)
            elif "file" in it and name.casefold().endswith(extensions) and not name.startswith("~$"):
                found[path] = RemoteFile(
                    id=it["id"], path=path, size=int(it.get("size") or 0),
                    modified_at=str(it.get("lastModifiedDateTime") or ""), etag=str(it.get("eTag") or ""),
                )
                if len(found) > MAX_FILES:
                    raise SharePointError(f"The path matches more than {MAX_FILES} files: narrow the glob")

    walk("", parts)
    return [found[k] for k in sorted(found, key=str.casefold)]


# ── Reading and validating ───────────────────────────────────────────────────────
def check_header(names: list, shown_as: str, where: str, error: type[IngestError] = SharePointError) -> None:
    """Refuses a header that is not machine readable: a column without a name, the
    same name twice (case-insensitive), a provenance column name. Shared by every
    file source (SharePoint, SFTP): `where` says which part of the file it is."""
    names = [None if v is None or str(v).strip() == "" else str(v).strip() for v in names]
    empty = [i + 1 for i, n in enumerate(names) if n is None]
    if empty:
        raise error(
            f"{shown_as}: incomplete header in {where} (columns without a name: "
            f"{', '.join(map(str, empty[:8]))}). The FIRST row must contain a name for every column: "
            "remove any titles or empty rows above the table from the file."
        )
    seen: set[str] = set()
    dup = sorted({n for n in names if n.casefold() in seen or seen.add(n.casefold())})  # type: ignore[func-returns-value]
    if dup:
        raise error(f"{shown_as}: repeated column names in {where}: {', '.join(dup[:8])}")
    reserved = [n for n in names if n in (FILE_COLUMN, MODIFIED_COLUMN)]
    if reserved:
        raise error(f"{shown_as}: '{reserved[0]}' is a name reserved for the source file's provenance")


def read_sheet(local_path: str, sheet: str | None, shown_as: str, error: type[IngestError] = SharePointError) -> pl.DataFrame:
    """Reads the sheet and REFUSES what is not machine readable. `sheet=None` = the
    first sheet of the workbook."""
    where = f"sheet '{sheet}'" if sheet else "the first sheet"
    which = {"sheet_name": sheet} if sheet else {"sheet_id": 1}
    try:
        header = pl.read_excel(local_path, **which, has_header=False, read_options={"n_rows": 1}, raise_if_empty=False)
    except ValueError as e:
        if "no matching sheet" in str(e):
            raise error(f"{shown_as}: sheet '{sheet}' does not exist") from e
        raise error(f"{shown_as}: unreadable Excel file ({e})") from e
    except Exception as e:
        raise error(f"{shown_as}: unreadable Excel file ({e})") from e
    if header.height == 0 or header.width == 0:
        raise error(f"{shown_as}: {where} is empty")
    check_header(list(header.row(0)), shown_as, where, error)
    df = pl.read_excel(local_path, **which, raise_if_empty=False)
    # the validated names are those TRIMMED of spaces: the columns must be named so,
    # or "Amount " in one file and "Amount" in another would make two columns
    return df.rename({c: c.strip() for c in df.columns if c != c.strip()})


def read_one(local_path: str, shown_as: str, source: SharePointSourceSpec) -> pl.DataFrame:
    """A workbook by its sheet, a CSV with the datasource's reading options: the same
    readers, and the same strict header, as the SFTP and S3 datasources."""
    if not shown_as.casefold().endswith(CSV_EXTENSIONS):
        return read_sheet(local_path, source.sheet.strip() or None, shown_as)
    from app.ingest import file_readers  # it imports this module: here, not at the top

    fmt = "tsv" if shown_as.casefold().endswith(".tsv") else "csv"
    return file_readers.read_file(local_path, shown_as, fmt, source.csv, "", SharePointError)


def _expansions(source: SharePointSourceSpec, now, last_refresh) -> list[ft.Expansion]:
    return ft.expand({"path": source.path}, source, SharePointError, now, last_refresh)


def validate_source(source: SharePointSourceSpec, now=None, last_refresh: str | None = None) -> dict[str, Any]:
    """The placeholders, the variables, the keys of the append, without Graph:
    how many expansions, the first and last resolved."""
    exps = _expansions(source, now, last_refresh)
    return {"ok": True, "expansions": len(exps), "first": exps[0].values, "last": exps[-1].values}


def gather_files(client: GraphClient, source: SharePointSourceSpec, now=None,
                 last_refresh: str | None = None) -> tuple[ft.Gathered, list[ft.Expansion]]:
    """The files of every expansion of the path, each once, and the values without one."""
    exps = _expansions(source, now, last_refresh)
    exts = EXTENSIONS[source.format]
    g = ft.gather(exps, lambda e: find_files(client, e.values["path"], exts), lambda f: f.path,
                  lambda e: e.values["path"], MAX_FILES, SharePointError)
    return g, exps


def ingest_sharepoint_to_parquet(
    conn: SharePointConnectionSpec,
    source: SharePointSourceSpec,
    bucket: str,
    key: str,
    storage,
    client: GraphClient | None = None,
    progress: Callable[..., None] | None = None,
    last_refresh: str | None = None,
    previous: dict | None = None,
    now=None,
) -> dict[str, Any]:
    """`progress(phase, …)` for whoever watches the import: «files» at every file (file,
    files, rows read so far), «saving» while the parquet goes to the storage. The path is
    expanded once per value of the variables (see file_templates); in append mode
    the rows are merged into the previous snapshot (`previous` = {bucket, key})."""
    advances = progress or (lambda *a, **k: None)
    client = client or GraphClient(conn)
    g, exps = gather_files(client, source, now, last_refresh)
    files = g.files
    if not files:
        if len(exps) > 1:
            raise ft.nothing_found(SharePointError, g, f"'{source.path}'")
        kind = {"excel": "Excel file", "csv": "CSV file", "auto": "Excel or CSV file"}[source.format]
        raise SharePointError(f"No {kind} matches '{exps[0].values['path']}'")
    total = sum(f.size for f in files)
    if total > MAX_TOTAL_BYTES:
        raise SharePointError(f"The {len(files)} files total {total / 1024**3:.1f} GB: over the {MAX_TOTAL_BYTES // 1024**3} GB limit")

    frames: list[pl.DataFrame] = []
    columns_by_file: dict[str, list[str]] = {}
    read_values = 0
    with tempfile.TemporaryDirectory() as tmp:
        for i, f in enumerate(files):
            advances("files", file=i + 1, files=len(files), rows=read_values)
            local = os.path.join(tmp, f"{i}{os.path.splitext(f.path)[1]}")
            client.download(f.id, local)
            df = read_one(local, f.path, source)
            os.remove(local)
            columns_by_file[f.path] = df.columns
            read_values += df.height
            if df.height == 0:
                # a header only (an empty template of the month): the reader types it
                # all String, and the "relaxed" union would drag every numeric column of
                # the other files to text. Counted, not concatenated.
                continue
            df = ft.with_params(df, g.params.get(f.path) or {}, f.path, SharePointError)
            frames.append(df.with_columns(
                pl.lit(f.path).alias(FILE_COLUMN),
                pl.lit(f.modified_at).str.to_datetime(strict=False, time_zone="UTC").dt.replace_time_zone(None).alias(MODIFIED_COLUMN),
            ))

        # Different schemas across the files: a union BY NAME, null where missing. Not
        # an error — one month has an extra column — but it is said, because a typo in a
        # header makes two half-empty columns instead of one.
        if not frames:
            raise SharePointError("All the matching files are empty (header only)")
        try:
            table = pl.concat(frames, how="diagonal_relaxed")
        except Exception as e:
            raise SharePointError(f"The files have columns with the same name but incompatible types: {e}") from e
        # the provenance goes LAST: with a union by name, a column that appears only in
        # a later file would land after `_file`, among the data
        prov = [FILE_COLUMN, MODIFIED_COLUMN]
        table = ft.provenance_last(table, prov)
        every = set.intersection(*(set(c) for c in columns_by_file.values()))
        drift = sorted({c for cols in columns_by_file.values() for c in cols} - every)
        if drift:
            logger.warning("sharepoint %s: columns not in every file: %s", source.path, drift)
        table, replaced, repeated = ft.finish(table, source, storage, previous, tmp, SharePointError, prov)

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
        "files": [{"path": f.path, "modified_at": f.modified_at, "size": f.size} for f in files],
        "schema_drift": drift,
        "missing": ft.missing_report(g),
        "missing_count": len(g.missing),
        "expansions": g.expansions,
        "mode": source.mode,
        "rows_replaced": replaced,
        "duplicate_keys": repeated,
    }
