"""Sorgente SharePoint: file Excel (and CSV) di una raccolta documenti → parquet.

Per il finance i dati veri vivono in Excel su SharePoint. Qui si definisce un
SITO (la connessione) e, per ogni datasource, un PERCORSO — anche con glob — e
un FOGLIO. Un percorso che corrisponde a più file li impila in una tabella sola.

Contratto sul file, deliberatamente stretto: deve essere MACHINE READABLE —
intestazione in prima riga, un nome per colonna, nessun doppione. Niente "salta
N righe" né euristiche: un file con un titolo sopra la tabella si sistema alla
fonte. La libreria di lettura, da sola, NON rifiuterebbe quei file: inventa
`__UNNAMED__1` per una cella vuota e `id_1` per un doppione, e il dato sporco
passerebbe in silenzio. Per questo l'intestazione grezza viene validata qui.

Accesso via Microsoft Graph con le credenziali di un'APPLICAZIONE registrata su
Entra ID (client credentials). Il permesso giusto da chiedere all'IT è
`Sites.Selected`: l'app vede solo i siti che le vengono concessi, non tutto
SharePoint. Nessuna libreria Microsoft: bastano `requests` e quattro chiamate.

NON ancora provato contro un tenant reale (in sviluppo non ce n'è uno): i test
girano contro un Graph finto che ne rispetta il contratto documentato.
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
# colonne di provenienza aggiunte a ogni riga: al finance serve sapere da QUALE
# file arriva un numero, e spesso mese o filiale stanno solo nel nome del file
FILE_COLUMN = "_file"
MODIFIED_COLUMN = "_file_modified_at"
# tetti di sicurezza: un glob sbagliato (`**/*`) non deve scaricare un sito intero
MAX_FILES = 500
MAX_FOLDERS = 2000
MAX_TOTAL_BYTES = 2 * 1024**3


class SharePointError(IngestError):
    """Errore leggibile dall'utente (finisce nell'esito dell'ingest)."""


class _NotFound(SharePointError):
    """404 da Graph: distinto perché per una CARTELLA non è un errore (vedi children)."""


class SharePointConnectionSpec(BaseModel):
    tenant_id: str
    client_id: str
    client_secret_encrypted: str
    site_url: str  # es. https://contoso.sharepoint.com/sites/Finance
    # raccolta documenti; vuoto = quella predefinita del sito ("Documenti")
    library: str = ""
    # Vuoti = quelli del deployment (SHAREPOINT__GRAPH_BASE / SHAREPOINT__LOGIN_BASE,
    # per i cloud sovrani). Il gateway non li valorizza mai: non li sceglie l'utente.
    graph_base: str = ""
    login_base: str = ""

    def model_post_init(self, _ctx) -> None:
        cfg = get_settings().sharepoint
        self.graph_base = (self.graph_base or cfg.graph_base).rstrip("/")
        self.login_base = (self.login_base or cfg.login_base).rstrip("/")


class SharePointSourceSpec(ft.FileTemplates):
    # percorso nella raccolta, con glob: `Budget/2026/*.xlsx`, `**/chiusura_*.xlsx`;
    # with placeholders and the datasource's variables: `Chiusure/{month:yyyy-MM}.xlsx`
    path: str
    # auto = Excel and CSV, each by its extension; a datasource saved without it: Excel only
    format: Literal["auto", "excel", "csv"] = "excel"
    csv: CsvOptions = Field(default_factory=CsvOptions)
    sheet: str = ""  # Excel: empty = the first sheet


@dataclass
class RemoteFile:
    id: str
    path: str  # relativo alla radice della raccolta
    size: int
    modified_at: str
    etag: str = ""


# ── Client Graph ────────────────────────────────────────────────────────────────
class GraphClient:
    """Le quattro chiamate che servono, con token in cache e rispetto del
    throttling (429/503 + Retry-After: SharePoint lo usa davvero)."""

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
                # Retry-After puo' essere una data HTTP, non solo secondi
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

    # -- sito e raccolta --
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
                # una cartella che non c'è, per chi scrive un glob, è "nessun file
                # corrisponde" — non un errore diverso da spiegare a parte
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
                r.close()  # lo stream va chiuso anche se la scrittura fallisce
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


# ── Glob sulla raccolta ─────────────────────────────────────────────────────────
def _has_wildcard(s: str) -> bool:
    return any(c in s for c in "*?[")


def find_files(client: GraphClient, pattern: str, extensions: tuple[str, ...] = EXCEL_EXTENSIONS) -> list[RemoteFile]:
    """I file (Excel, or those of `extensions`) della raccolta che corrispondono a `pattern`. Segmento per
    segmento: i tratti senza jolly si attraversano senza elencare, `*` elenca una
    cartella, `**` scende a ogni profondità. Senza distinzione fra maiuscole e
    minuscole, come SharePoint. Esclusi i file di blocco di Office (`~$…`), che
    un `*.xlsx` prenderebbe mentre qualcuno ha il file aperto."""
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
            walk(folder, tail)  # `**` vale anche zero cartelle
            for it in listing(folder):
                if "folder" in it:
                    walk(f"{folder}/{it['name']}".strip("/"), rest)
            return
        if tail and not _has_wildcard(seg):
            walk(f"{folder}/{seg}".strip("/"), tail)  # tratto fisso: nessun elenco
            return
        for it in listing(folder):
            name = str(it.get("name", ""))
            if not fnmatch.fnmatchcase(name.casefold(), seg.casefold()):
                continue
            # il percorso VERO (con le maiuscole di SharePoint), non quello scritto nel
            # glob: `_file` non deve cambiare se qualcuno riscrive il percorso diversamente
            # Graph lo restituisce percent-encoded ("Budget%202026"): decodificato,
            # o children() lo codificherebbe due volte e la cartella darebbe 404
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


# ── Lettura e validazione ───────────────────────────────────────────────────────
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
    """Legge il foglio e RIFIUTA ciò che non è machine readable. `sheet=None` = the
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
    # i nomi validati sono quelli RIPULITI dagli spazi: le colonne devono chiamarsi
    # cosi', o "Importo " in un file e "Importo" in un altro farebbero due colonne
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
    """`progress(fase, …)` per chi guarda l'import: «files» a ogni file (file, files,
    rows letti finora), «saving» mentre il parquet va sullo storage. The path is
    expanded once per value of the variables (see file_templates); in append mode
    the rows are merged into the previous snapshot (`previous` = {bucket, key})."""
    avanza = progress or (lambda *a, **k: None)
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
    letti = 0
    with tempfile.TemporaryDirectory() as tmp:
        for i, f in enumerate(files):
            avanza("files", file=i + 1, files=len(files), rows=letti)
            local = os.path.join(tmp, f"{i}{os.path.splitext(f.path)[1]}")
            client.download(f.id, local)
            df = read_one(local, f.path, source)
            os.remove(local)
            columns_by_file[f.path] = df.columns
            letti += df.height
            if df.height == 0:
                # solo intestazione (un template vuoto del mese): il lettore la tipizza
                # tutta String, e nell'unione "rilassata" trascinerebbe a testo ogni
                # colonna numerica degli altri file. Contato, non concatenato.
                continue
            df = ft.with_params(df, g.params.get(f.path) or {}, f.path, SharePointError)
            frames.append(df.with_columns(
                pl.lit(f.path).alias(FILE_COLUMN),
                pl.lit(f.modified_at).str.to_datetime(strict=False, time_zone="UTC").dt.replace_time_zone(None).alias(MODIFIED_COLUMN),
            ))

        # Schemi diversi fra i file: unione PER NOME, null dove manca. Non è un
        # errore — un mese ha una colonna in più — ma va detto, perché un refuso
        # in un'intestazione produce due colonne mezze vuote invece di una.
        if not frames:
            raise SharePointError("All the matching files are empty (header only)")
        try:
            table = pl.concat(frames, how="diagonal_relaxed")
        except Exception as e:
            raise SharePointError(f"The files have columns with the same name but incompatible types: {e}") from e
        # la provenienza sta in FONDO: con l'unione per nome una colonna che compare
        # solo in un file successivo finirebbe dopo `_file`, in mezzo ai dati
        prov = [FILE_COLUMN, MODIFIED_COLUMN]
        table = ft.provenance_last(table, prov)
        every = set.intersection(*(set(c) for c in columns_by_file.values()))
        drift = sorted({c for cols in columns_by_file.values() for c in cols} - every)
        if drift:
            logger.warning("sharepoint %s: colonne non presenti in tutti i file: %s", source.path, drift)
        table, replaced, repeated = ft.finish(table, source, storage, previous, tmp, SharePointError, prov)

        avanza("saving", rows=table.height)
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
