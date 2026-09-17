"""Sorgente SharePoint: file Excel di una raccolta documenti → parquet.

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
from typing import Any, Callable
from urllib.parse import quote, urlparse

import polars as pl
from pydantic import BaseModel, Field

from app.core.config import get_settings
from app.core.crypto import decrypt_secret
from app.ingest.converters import IngestError

logger = logging.getLogger(__name__)

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
LOGIN_BASE = "https://login.microsoftonline.com"
EXCEL_EXTENSIONS = (".xlsx", ".xlsm", ".xlsb", ".xls")
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


class SharePointSourceSpec(BaseModel):
    # percorso nella raccolta, con glob: `Budget/2026/*.xlsx`, `**/chiusura_*.xlsx`
    path: str
    sheet: str = Field(min_length=1)


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
                "Accesso a Microsoft rifiutato: controlla tenant, client id e secret dell'app "
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
                self._sleep(min(30.0, float(r.headers.get("Retry-After", 2 ** attempt))))
                continue
            return r
        return r

    def _json(self, url: str, what: str) -> dict:
        r = self._get(url)
        if r.status_code == 404:
            raise _NotFound(f"{what}: non trovato")
        if r.status_code in (401, 403):
            raise SharePointError(
                f"{what}: accesso negato. L'app deve avere il permesso Sites.Selected "
                f"E l'accesso concesso a questo sito ({_graph_message(r)})"
            )
        if r.status_code != 200:
            raise SharePointError(f"{what}: errore {r.status_code} da Microsoft Graph ({_graph_message(r)})")
        return r.json()

    # -- sito e raccolta --
    def drive_id(self) -> str:
        if self._drive_id:
            return self._drive_id
        u = urlparse(self.conn.site_url.strip())
        if not u.hostname:
            raise SharePointError("URL del sito non valido: atteso https://<azienda>.sharepoint.com/sites/<nome>")
        rel = u.path.rstrip("/")
        base = self.conn.graph_base.rstrip("/")
        site = self._json(f"{base}/sites/{u.hostname}:{quote(rel)}" if rel else f"{base}/sites/{u.hostname}", "Sito SharePoint")
        if not self.conn.library.strip():
            self._drive_id = self._json(f"{base}/sites/{site['id']}/drive", "Raccolta documenti predefinita")["id"]
            return self._drive_id
        wanted = self.conn.library.strip().casefold()
        drives = self._json(f"{base}/sites/{site['id']}/drives", "Raccolte documenti del sito").get("value", [])
        for d in drives:
            if str(d.get("name", "")).casefold() == wanted:
                self._drive_id = d["id"]
                return self._drive_id
        names = ", ".join(sorted(str(d.get("name")) for d in drives)) or "nessuna"
        raise SharePointError(f"Raccolta '{self.conn.library}' non trovata nel sito. Disponibili: {names}")

    def children(self, folder: str) -> list[dict]:
        base = self.conn.graph_base.rstrip("/")
        drive = self.drive_id()
        folder = folder.strip("/")
        url = f"{base}/drives/{drive}/root:/{quote(folder)}:/children" if folder else f"{base}/drives/{drive}/root/children"
        out: list[dict] = []
        while url:
            try:
                page = self._json(url, f"Cartella '{folder or '/'}'")
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
            raise SharePointError(f"Download non riuscito ({r.status_code}): {_graph_message(r)}")
        with open(dest, "wb") as f:
            for chunk in r.iter_content(1024 * 1024):
                f.write(chunk)


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


def find_files(client: GraphClient, pattern: str) -> list[RemoteFile]:
    """I file Excel della raccolta che corrispondono a `pattern`. Segmento per
    segmento: i tratti senza jolly si attraversano senza elencare, `*` elenca una
    cartella, `**` scende a ogni profondità. Senza distinzione fra maiuscole e
    minuscole, come SharePoint. Esclusi i file di blocco di Office (`~$…`), che
    un `*.xlsx` prenderebbe mentre qualcuno ha il file aperto."""
    parts = [p for p in pattern.replace("\\", "/").strip("/").split("/") if p]
    if not parts:
        raise SharePointError("Percorso vuoto: indica un file o un glob, es. Budget/2026/*.xlsx")
    listed = 0
    found: dict[str, RemoteFile] = {}

    def listing(folder: str) -> list[dict]:
        nonlocal listed
        listed += 1
        if listed > MAX_FOLDERS:
            raise SharePointError(f"Il percorso attraversa più di {MAX_FOLDERS} cartelle: restringi il glob")
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
            parent = str((it.get("parentReference") or {}).get("path") or "")
            real = parent.split("root:", 1)[1].strip("/") if "root:" in parent else folder
            path = f"{real}/{name}".strip("/")
            if tail:
                if "folder" in it:
                    walk(path, tail)
            elif "file" in it and name.casefold().endswith(EXCEL_EXTENSIONS) and not name.startswith("~$"):
                found[path] = RemoteFile(
                    id=it["id"], path=path, size=int(it.get("size") or 0),
                    modified_at=str(it.get("lastModifiedDateTime") or ""), etag=str(it.get("eTag") or ""),
                )
                if len(found) > MAX_FILES:
                    raise SharePointError(f"Il percorso corrisponde a più di {MAX_FILES} file: restringi il glob")

    walk("", parts)
    return [found[k] for k in sorted(found, key=str.casefold)]


# ── Lettura e validazione ───────────────────────────────────────────────────────
def read_sheet(local_path: str, sheet: str, shown_as: str) -> pl.DataFrame:
    """Legge il foglio e RIFIUTA ciò che non è machine readable."""
    try:
        header = pl.read_excel(local_path, sheet_name=sheet, has_header=False, read_options={"n_rows": 1}, raise_if_empty=False)
    except ValueError as e:
        if "no matching sheet" in str(e):
            raise SharePointError(f"{shown_as}: il foglio '{sheet}' non esiste") from e
        raise SharePointError(f"{shown_as}: file Excel illeggibile ({e})") from e
    except Exception as e:
        raise SharePointError(f"{shown_as}: file Excel illeggibile ({e})") from e
    if header.height == 0 or header.width == 0:
        raise SharePointError(f"{shown_as}: il foglio '{sheet}' è vuoto")
    names = [None if v is None or str(v).strip() == "" else str(v).strip() for v in header.row(0)]
    empty = [i + 1 for i, n in enumerate(names) if n is None]
    if empty:
        raise SharePointError(
            f"{shown_as}: intestazione incompleta nel foglio '{sheet}' (colonne senza nome: "
            f"{', '.join(map(str, empty[:8]))}). La PRIMA riga deve contenere un nome per ogni colonna: "
            "titoli o righe vuote sopra la tabella vanno tolti dal file."
        )
    seen: set[str] = set()
    dup = sorted({n for n in names if n.casefold() in seen or seen.add(n.casefold())})  # type: ignore[func-returns-value]
    if dup:
        raise SharePointError(f"{shown_as}: nomi di colonna ripetuti nel foglio '{sheet}': {', '.join(dup[:8])}")
    reserved = [n for n in names if n in (FILE_COLUMN, MODIFIED_COLUMN)]
    if reserved:
        raise SharePointError(f"{shown_as}: '{reserved[0]}' è un nome riservato alla provenienza del file")
    return pl.read_excel(local_path, sheet_name=sheet, raise_if_empty=False)


def ingest_sharepoint_to_parquet(
    conn: SharePointConnectionSpec,
    source: SharePointSourceSpec,
    bucket: str,
    key: str,
    storage,
    client: GraphClient | None = None,
) -> dict[str, Any]:
    client = client or GraphClient(conn)
    files = find_files(client, source.path)
    if not files:
        raise SharePointError(f"Nessun file Excel corrisponde a '{source.path}'")
    total = sum(f.size for f in files)
    if total > MAX_TOTAL_BYTES:
        raise SharePointError(f"I {len(files)} file pesano {total / 1024**3:.1f} GB: oltre il tetto di {MAX_TOTAL_BYTES // 1024**3} GB")

    frames: list[pl.DataFrame] = []
    columns_by_file: dict[str, list[str]] = {}
    with tempfile.TemporaryDirectory() as tmp:
        for i, f in enumerate(files):
            local = os.path.join(tmp, f"{i}{os.path.splitext(f.path)[1]}")
            client.download(f.id, local)
            df = read_sheet(local, source.sheet, f.path)
            os.remove(local)
            columns_by_file[f.path] = df.columns
            frames.append(df.with_columns(
                pl.lit(f.path).alias(FILE_COLUMN),
                pl.lit(f.modified_at).str.to_datetime(strict=False, time_zone="UTC").dt.replace_time_zone(None).alias(MODIFIED_COLUMN),
            ))

        # Schemi diversi fra i file: unione PER NOME, null dove manca. Non è un
        # errore — un mese ha una colonna in più — ma va detto, perché un refuso
        # in un'intestazione produce due colonne mezze vuote invece di una.
        try:
            table = pl.concat(frames, how="diagonal_relaxed")
        except Exception as e:
            raise SharePointError(f"I file hanno colonne con lo stesso nome ma tipi inconciliabili: {e}") from e
        # la provenienza sta in FONDO: con l'unione per nome una colonna che compare
        # solo in un file successivo finirebbe dopo `_file`, in mezzo ai dati
        prov = [FILE_COLUMN, MODIFIED_COLUMN]
        table = table.select([c for c in table.columns if c not in prov] + prov)
        every = set.intersection(*(set(c) for c in columns_by_file.values()))
        drift = sorted({c for cols in columns_by_file.values() for c in cols} - every)
        if drift:
            logger.warning("sharepoint %s: colonne non presenti in tutti i file: %s", source.path, drift)

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
    }
