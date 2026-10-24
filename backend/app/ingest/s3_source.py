"""S3: the files of a folder of a bucket read as a datasource («S3 folder»).

The SFTP datasource on object storage (AWS, MinIO, R2, Wasabi, GCS through its
S3 API): a bucket — the datasource's, or the connection's default —, a folder
(a key prefix) and a glob, every matching object read at each refresh and
stacked into one table. Every row says the object it comes from in `_file`, the
key below the folder (`vendite_01.csv`, `2026/q1/a.csv` with `**`), and its
`_file_modified_at`, the two last columns. The objects are left where they are:
the bucket is only listed and read. The same contract on the files as SFTP and
SharePoint (machine readable or refused, naming the file) and the same formats
(CSV, TSV, parquet, Excel, NDJSON).

THE GLOB is matched on the keys, segment by segment like a path: `*` and `?`
within a name, `**` for any depth. The listing starts at the folder plus the
glob's fixed leading folders (`in/2026/*.csv` lists `in/2026/`), and a folder
is a whole folder (`in` never takes `inbox/`). Names starting with a dot and
«folder» markers are left out.

NOTHING OUTSIDE THE BUCKET AND THE FOLDER: only the configured bucket is listed,
only keys the listing returned below the folder are read, and `..` is refused
in the folder, the glob and in what their placeholders resolve to.

The caps (S3_SOURCE__…) are checked on the listing's sizes BEFORE anything is
downloaded. Folder and glob may hold placeholders and variables, and the
refresh may append by key (see file_templates). Every message for the user
starts with «S3:», the prefix the gateway knows; none holds a credential.
"""
from __future__ import annotations

import fnmatch
import logging
import os
import posixpath
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Literal

import polars as pl
from pydantic import Field

from app.core.config import get_settings
from app.core.placeholders import has_placeholders
from app.engine.query_tag import was_interrupted
from app.ingest import file_readers
from app.ingest import file_templates as ft
from app.ingest.converters import IngestError
from app.ingest.models import CsvOptions
from app.ingest.s3_destination import S3ConnectionSpec
from app.ingest.sharepoint_source import FILE_COLUMN, MODIFIED_COLUMN
from app.ingest.sheets import PEEK_BYTES, sheets_of_first

logger = logging.getLogger(__name__)

PREFIX = "S3: "
_CHUNK = 1024 * 1024


class S3SourceError(IngestError):
    """A message for the user (it ends up in the run or in the response)."""

    def __init__(self, message: str):
        super().__init__(message if message.startswith(PREFIX) else PREFIX + message)


class S3SourceSpec(ft.FileTemplates):
    """Folder and pattern may hold placeholders and the datasource's variables."""
    bucket: str = Field(default="", max_length=255)  # empty = the connection's default bucket
    folder: str = Field(default="", max_length=1000)  # a key prefix, as a folder: `exports/2026`
    pattern: str = Field(min_length=1, max_length=500)
    format: Literal["auto", "csv", "parquet", "excel", "ndjson"] = "auto"
    csv: CsvOptions = Field(default_factory=CsvOptions)
    sheet: str = ""  # Excel: empty = the first sheet


@dataclass
class S3Object:
    key: str
    size: int
    modified: datetime | None
    rel: str = ""  # the key below the datasource's folder: what `_file` holds

    @property
    def path(self) -> str:
        return self.key

    @property
    def modified_at(self) -> str:
        return self.modified.isoformat() if self.modified else ""


def _no_dot_dot(path: str, what: str) -> None:
    if any(seg in ("..", ".") for seg in (path or "").replace("\\", "/").split("/")):
        raise S3SourceError(f"'..' is not allowed in the {what} ('{path}'): write the folder you mean")


def _clean_folder(folder: str) -> str:
    f = (folder or "").strip().replace("\\", "/").strip("/")
    _no_dot_dot(f, "folder")
    return f


def _hidden(name: str) -> bool:
    return name.startswith(".") or name.startswith("~$")


def _matches(parts: list[str], segs: list[str]) -> bool:
    """`parts` (the key's segments below the folder) against the glob's segments."""
    if not segs:
        return not parts
    head, tail = segs[0], segs[1:]
    if head == "**":
        return any(_matches(parts[i:], tail) for i in range(len(parts) + 1))
    return bool(parts) and fnmatch.fnmatchcase(parts[0], head) and _matches(parts[1:], tail)


def bucket_of(conn: S3ConnectionSpec, source: S3SourceSpec) -> str:
    bucket = (source.bucket or "").strip() or (conn.bucket or "").strip()
    if not bucket:
        raise S3SourceError("no bucket: set it on the datasource or as the connection's default")
    return bucket


def _client_error(e: Exception, bucket: str, what: str) -> S3SourceError:
    """botocore's error, said for a person (never with a credential: boto's
    messages name the request, not the keys)."""
    code = ""
    try:
        code = str(e.response.get("Error", {}).get("Code") or "")  # type: ignore[attr-defined]
    except AttributeError:
        pass
    if code in ("NoSuchBucket",):
        return S3SourceError(f"the bucket '{bucket}' does not exist (or is not visible with these credentials)")
    if code in ("AccessDenied", "AllAccessDisabled", "403"):
        return S3SourceError(f"access denied on {what}: the credentials need list and read rights on the bucket '{bucket}'")
    if code in ("InvalidAccessKeyId", "SignatureDoesNotMatch", "InvalidToken", "ExpiredToken"):
        return S3SourceError("the storage refused the credentials: check the access key and the secret of the connection")
    if code in ("NoSuchKey", "404"):
        return S3SourceError(f"{what} disappeared while being read")
    if code:
        return S3SourceError(f"{what}: {code}")
    return S3SourceError(f"{what}: {type(e).__name__}: {str(e)[:300]}")


def find_objects(client, bucket: str, folder: str, pattern: str, rel_to: str | None = None,
                 listed: list[int] | None = None) -> list[S3Object]:
    """The objects of `bucket` under `folder` whose key below it matches `pattern`.
    `listed`: the keys scanned so far by the refresh (shared by its expansions)."""
    cfg = get_settings().s3_source
    folder = _clean_folder(folder)
    _no_dot_dot(pattern, "file pattern")
    segs = [p for p in (pattern or "").replace("\\", "/").strip("/").split("/") if p]
    if not segs:
        raise S3SourceError("empty file pattern: enter a name or a glob, e.g. vendite_*.csv")
    fixed: list[str] = []
    for seg in segs[:-1]:
        if seg == "**" or any(c in seg for c in "*?["):
            break
        fixed.append(seg)
    base = folder + "/" if folder else ""
    listing = base + "".join(f + "/" for f in fixed)
    rel_base = _clean_folder(rel_to) if rel_to is not None else folder
    rel_prefix = rel_base + "/" if rel_base else ""
    found: list[S3Object] = []
    scanned = listed if listed is not None else [0]
    try:
        for page in client.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=listing):
            for obj in page.get("Contents", []) or []:
                scanned[0] += 1
                if scanned[0] > cfg.max_listed:
                    raise S3SourceError(f"the pattern goes through more than {cfg.max_listed} objects "
                                        "(S3_SOURCE__MAX_LISTED): narrow the folder or the pattern")
                key = str(obj.get("Key") or "")
                if not key.startswith(base) or key.endswith("/"):
                    continue
                parts = key[len(base):].split("/")
                if any(_hidden(p) or p in ("", ".", "..") for p in parts) or not _matches(parts, segs):
                    continue
                modified = obj.get("LastModified")
                if isinstance(modified, datetime) and modified.tzinfo is None:
                    modified = modified.replace(tzinfo=timezone.utc)
                rel = key[len(rel_prefix):] if key.startswith(rel_prefix) else key
                found.append(S3Object(key=key, size=int(obj.get("Size") or 0),
                                      modified=modified if isinstance(modified, datetime) else None, rel=rel))
                if len(found) > cfg.max_files:
                    raise S3SourceError(f"the pattern matches more than {cfg.max_files} files: narrow it")
    except S3SourceError:
        raise
    except Exception as e:  # noqa: BLE001 — botocore and the network, said for a person
        if was_interrupted(e):
            raise  # a time limit or a revoke stays what it is
        raise _client_error(e, bucket, f"listing s3://{bucket}/{listing}") from e
    return sorted(found, key=lambda o: o.key)


def _expansions(source: S3SourceSpec, now, last_refresh) -> list[ft.Expansion]:
    _clean_folder(source.folder)
    _no_dot_dot(source.pattern, "file pattern")
    exps = ft.expand({"folder": source.folder, "pattern": source.pattern}, source, S3SourceError, now, last_refresh)
    for e in exps:  # a value cannot take the pattern out of its folder either
        _clean_folder(e.values["folder"])
        _no_dot_dot(e.values["pattern"], "file pattern")
    return exps


def validate_source(source: S3SourceSpec, now=None, last_refresh: str | None = None) -> dict[str, Any]:
    """Everything that can be said without the storage."""
    exps = _expansions(source, now, last_refresh)
    return {"ok": True, "expansions": len(exps), "first": exps[0].values, "last": exps[-1].values}


def _gather(client, bucket: str, source: S3SourceSpec, now, last_refresh) -> tuple[ft.Gathered, list[ft.Expansion]]:
    exps = _expansions(source, now, last_refresh)
    rel_to = ft.static_prefix(_clean_folder(source.folder)) if has_placeholders(source.folder) else None

    listed = [0]  # the cap on the keys scanned is for the whole refresh

    def find(e: ft.Expansion) -> list[S3Object]:
        return find_objects(client, bucket, e.values["folder"], e.values["pattern"], rel_to=rel_to, listed=listed)

    g = ft.gather(exps, find, lambda o: o.key, lambda e: posixpath.join(e.values["folder"], e.values["pattern"]),
                  get_settings().s3_source.max_files, S3SourceError)
    return g, exps


def check_files(conn: S3ConnectionSpec, source: S3SourceSpec, now=None, last_refresh: str | None = None) -> ft.Gathered:
    """«Check the files»: every object the expansions take, and the values without one."""
    bucket = bucket_of(conn, source)
    client = conn.client()
    g, _ = _gather(client, bucket, source, now, last_refresh)
    if source.format in ("auto", "excel"):  # the sheets of the first workbook, to choose from
        g.sheets = sheets_of_first(g.files, lambda o: o.key, lambda o: o.size,
                                   lambda o, local: _download(client, bucket, o, local, PEEK_BYTES), lambda o: o.rel or o.key)
    return g


def _download(client, bucket: str, obj: S3Object, local: str, cap: int) -> None:
    """GET of a key the listing returned, stopped if it grew over the cap."""
    try:
        limit = get_settings().s3_source.max_request_seconds
        deadline = time.monotonic() + limit
        res = client.get_object(Bucket=bucket, Key=obj.key)
        body = res["Body"]
        done = 0
        with open(local, "wb") as f:
            while True:
                chunk = body.read(_CHUNK)
                if not chunk:
                    break
                if time.monotonic() > deadline:
                    raise S3SourceError(f"{obj.key}: the download took more than {limit:g} s "
                                        "(S3_SOURCE__MAX_REQUEST_SECONDS)")
                done += len(chunk)
                if done > cap:
                    raise S3SourceError(f"{obj.key} grew over the limit of {cap / 1024**2:.0f} MB per file while being read")
                f.write(chunk)
    except S3SourceError:
        raise
    except Exception as e:  # noqa: BLE001
        if was_interrupted(e):
            raise
        raise _client_error(e, bucket, f"reading s3://{bucket}/{obj.key}") from e


def ingest_s3_to_parquet(
    conn: S3ConnectionSpec,
    source: S3SourceSpec,
    bucket: str,
    key: str,
    storage,
    progress: Callable[..., None] | None = None,
    last_refresh: str | None = None,
    previous: dict | None = None,
    now=None,
) -> dict[str, Any]:
    """Reads every matching object (of every expansion), stacks them and writes
    the datasource's parquet to `bucket/key` (the ENGINE's storage); in append
    mode merged with the previous snapshot. `progress`: «files» at each file,
    «saving» while the parquet goes up."""
    report = progress or (lambda *a, **k: None)
    cfg = get_settings().s3_source
    src_bucket = bucket_of(conn, source)
    client = conn.client()
    g, exps = _gather(client, src_bucket, source, now, last_refresh)
    files: list[S3Object] = g.files
    if not files:
        if len(exps) > 1:
            raise ft.nothing_found(S3SourceError, g, f"'{posixpath.join(source.folder, source.pattern)}'")
        only = exps[0].values
        where = "/".join(p for p in (src_bucket, _clean_folder(only["folder"])) if p)
        raise S3SourceError(f"No file matches '{only['pattern']}' in 's3://{where}'")
    # every size is checked BEFORE downloading anything
    formats = [file_readers.format_of(o.key, source.format, S3SourceError) for o in files]
    total = sum(o.size for o in files)
    if total > cfg.max_total_bytes:
        raise S3SourceError(
            f"the {len(files)} files total {total / 1024**2:.1f} MB, over the limit of "
            f"{cfg.max_total_bytes / 1024**2:.0f} MB per refresh (S3_SOURCE__MAX_TOTAL_BYTES): narrow the pattern"
        )
    big = [o for o in files if o.size > cfg.max_file_bytes]
    if big:
        o = big[0]
        raise S3SourceError(
            f"{o.key} is {o.size / 1024**2:.1f} MB, over the limit of {cfg.max_file_bytes / 1024**2:.0f} MB per file "
            "(S3_SOURCE__MAX_FILE_BYTES): narrow the pattern, or split the file at the source"
        )
    fmt_of = {o.key: f for o, f in zip(files, formats)}
    with tempfile.TemporaryDirectory(prefix="s3_in_") as tmp:
        table, drift = ft.stack_files(
            files,
            fetch=lambda o, local: _download(client, src_bucket, o, local, cfg.max_file_bytes),
            read=lambda local, o: file_readers.read_file(local, o.key, fmt_of[o.key], source.csv, source.sheet,
                                                         S3SourceError),
            meta=lambda o: (o.rel or o.key, o.modified.astimezone(timezone.utc).replace(tzinfo=None) if o.modified else None),
            params=g.params, key_of=lambda o: o.key, shown_as=lambda o: o.key, report=report, tmp=tmp,
            error=S3SourceError,
        )
        if drift:
            logger.warning("s3 %s/%s: columns not in every file: %s", source.folder, source.pattern, drift)
        table, replaced, repeated = ft.finish(table, source, storage, previous, tmp, S3SourceError, [FILE_COLUMN, MODIFIED_COLUMN])
        report("saving", rows=table.height)
        out = os.path.join(tmp, "out.parquet")
        table.write_parquet(out, compression="zstd", row_group_size=get_settings().ingest.parquet_row_group_rows)
        storage.upload_file(out, bucket, key)
    logger.info("s3 ingest done: s3://%s/%s/%s, %d files, %d rows", src_bucket, source.folder, source.pattern,
                len(files), table.height)
    return {
        "status": "success",
        "bucket": bucket,
        "output_key": key,
        "rows_written": table.height,
        "columns": [{"name": n, "dtype": str(t)} for n, t in table.schema.items()],
        "files": [{"path": o.rel or o.key, "modified_at": o.modified_at, "size": o.size} for o in files],
        "schema_drift": drift,
        "missing": ft.missing_report(g),
        "missing_count": len(g.missing),
        "expansions": g.expansions,
        "mode": source.mode,
        "rows_replaced": replaced,
        "duplicate_keys": repeated,
    }
