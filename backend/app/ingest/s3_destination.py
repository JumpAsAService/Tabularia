"""S3 / object storage destinations: pours a run's output into an EXTERNAL bucket (AWS,
MinIO, R2, Wasabi…) — one file or a hive-style partitioned dataset (`column=value/…`),
ready for the partition pruning of Trino, Athena, DuckDB and Spark.

Data path: the internal parquet → the local disk (buffer) → a Polars sink, STREAMING
(format conversion and/or partitioning with `pl.PartitionBy`) → boto3 uploads file by
file. Memory O(batch), the disk is the buffer, as for the ingest and the database
destinations.

The credentials arrive encrypted (secret_key_encrypted, the Fernet key shared with the
gateway) and are decrypted only when the client is created.
"""
from __future__ import annotations

import logging
import os
import shutil
import tempfile
from datetime import datetime, timezone
from typing import Literal

import polars as pl
from pydantic import BaseModel, Field

from app.utils import transfer_config
from app.ingest.converters import IngestError

logger = logging.getLogger(__name__)

# beyond this many partitions comes the classic "thousand small files" problem (and
# very slow uploads): almost always the wrong column
MAX_PARTITIONS = 1000


class S3DestinationError(IngestError):
    """A clear error to show the user (bucket, key, credentials)."""


class S3ConnectionSpec(BaseModel):
    endpoint_url: str = ""  # empty = real AWS; set = MinIO/R2/Wasabi…
    access_key: str = ""
    # one of the two: `secret_key_encrypted` (Fernet, as it comes from the gateway) or
    # `secret_key` in clear (only for direct trials on the engine in development)
    secret_key: str = ""
    secret_key_encrypted: str = ""
    region: str = ""
    bucket: str = ""  # bucket di default della connessione

    def resolve_secret(self) -> str:
        if self.secret_key_encrypted:
            from app.core.crypto import decrypt_secret

            return decrypt_secret(self.secret_key_encrypted)
        return self.secret_key

    def client(self):
        import boto3
        from botocore.config import Config

        kwargs: dict = {}
        if self.endpoint_url.strip():
            kwargs["endpoint_url"] = self.endpoint_url.strip()
            # path style: required by MinIO/Rclone and harmless for the others
            kwargs["config"] = Config(
                signature_version="s3v4", s3={"addressing_style": "path"},
                # as in app.utils: boto3's chunked checksums break the PUTs on GCS
                request_checksum_calculation="when_required", response_checksum_validation="when_required",
            )
        if self.region.strip():
            kwargs["region_name"] = self.region.strip()
        return boto3.client(
            "s3",
            aws_access_key_id=self.access_key,
            aws_secret_access_key=self.resolve_secret(),
            **kwargs,
        )


class S3DestinationSpec(BaseModel):
    bucket: str = ""  # empty = the connection's default bucket
    # the file's key (no partitions) or the prefix (with partitions); a template with
    # the Outputs' placeholders (app.core.placeholders): exports/{today:yyyy/MM}/v_{date}.csv
    key: str
    format: Literal["parquet", "csv"] = "parquet"
    partition_by: list[str] = Field(default_factory=list)
    # the zone of the dates in the key (UTC unless set); run_id and flow set by the gateway
    timezone: str = Field(default="UTC", max_length=64)
    run_id: int | None = None
    flow: str = ""


def render_key(dest: S3DestinationSpec, now: datetime | None = None) -> str:
    """The object key (or prefix) with its placeholders resolved: values may add
    levels (`{today:yyyy/MM}`), never `..` or `.`; {flow} made safe. Without
    braces the key is used as written, as before."""
    if "{" not in (dest.key or "") and "}" not in (dest.key or ""):
        return _clean_key(dest.key)
    from app.core import placeholders as ph

    try:
        ctx = ph.output_context(now or datetime.now(timezone.utc), dest.timezone, dest.run_id, dest.flow)
        k = _clean_key(ph.render_output(dest.key.strip(), ctx, "path", "S3 key"))
    except ph.PlaceholderError as e:
        raise S3DestinationError(f"S3: {e}") from e
    # a key resolved from placeholders, checked here; a key written as it is goes as before
    if len(k.encode()) > MAX_KEY_BYTES:
        raise S3DestinationError(f"S3: the key is {len(k.encode())} bytes, over the {MAX_KEY_BYTES} an object key may have")
    return k


# the longest object key S3 accepts (in UTF-8 bytes)
MAX_KEY_BYTES = 1024


def _clean_key(key: str) -> str:
    k = (key or "").strip().strip("/")
    if not k:
        raise S3DestinationError("The destination S3 key/path is empty")
    if any(part in ("", ".", "..") for part in k.split("/")):
        raise S3DestinationError(f"Invalid S3 key: {key!r}")
    return k


def _local_files(root: str) -> list[tuple[str, str]]:
    """[(local path, relative path with '/' separators)] of every file under root."""
    out: list[tuple[str, str]] = []
    for dirpath, _dirs, files in os.walk(root):
        for f in files:
            local = os.path.join(dirpath, f)
            out.append((local, os.path.relpath(local, root).replace(os.sep, "/")))
    return sorted(out, key=lambda t: t[1])


def _delete_stale_objects(client, bucket: str, prefix: str, keep: set[str]) -> None:
    """Removes the objects under `prefix/` NOT written again in this run (`keep`).

    It makes the partitioned output IDEMPOTENT: Polars names the files
    deterministically, but the NUMBER of files per partition may change between runs
    (data volume, parallelism) and some partitions may vanish → without a cleanup the
    earlier runs' files stay and stale or double rows get counted. The new ones are
    uploaded first (overwriting), then the old ones left are removed (never an empty
    window)."""
    p = prefix.rstrip("/") + "/"
    stale: list[dict] = []
    try:
        paginator = client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket, Prefix=p):
            for obj in page.get("Contents", []):
                if obj["Key"] not in keep:
                    stale.append({"Key": obj["Key"]})
        # batches where there are, single DELETEs where not (GCS): see app.utils.delete_keys
        from app.utils import delete_keys

        _, errors = delete_keys(client, bucket, [o["Key"] for o in stale])
        if errors:
            raise RuntimeError(f"{len(errors)} objects not deleted, the first: {errors[0]}")
    except Exception as e:
        raise S3DestinationError(
            f"Cleanup of obsolete files under s3://{bucket}/{p} failed ({type(e).__name__}): "
            "the output may contain duplicate rows from previous runs. The S3 credentials "
            "also need list/delete permissions on the destination prefix."
        ) from e


def write_output_to_s3(
    conn: S3ConnectionSpec,
    dest: S3DestinationSpec,
    bucket: str,
    key: str,
    storage=None,
    now: datetime | None = None,
) -> dict:
    """Pours `bucket/key` (the internal parquet) into the external S3 destination.

    Without partitions: one object at `dest.key` (parquet copied as it is, csv converted
    streaming). With `partition_by`: a hive tree under the prefix `dest.key/` — the next
    runs overwrite the same paths, but the partitions gone from the data are NOT removed
    from the bucket.
    """
    if storage is None:
        from app.utils import get_storage_service

        storage = get_storage_service()

    target_bucket = (dest.bucket or "").strip() or (conn.bucket or "").strip()
    if not target_bucket:
        raise S3DestinationError(
            "No destination bucket: set it on the Output node or in the connection"
        )
    target_key = render_key(dest, now)

    fd, tmp = tempfile.mkstemp(suffix=".parquet", prefix="s3_dest_")
    os.close(fd)
    workdir = tempfile.mkdtemp(prefix="s3_dest_")
    try:
        storage.download_file(bucket, key, tmp)
        lf = pl.scan_parquet(tmp)
        rows = int(lf.select(pl.len()).collect(engine="streaming").item())

        n_partitions = 0
        if dest.partition_by:
            schema = lf.collect_schema()
            missing = [c for c in dest.partition_by if c not in schema]
            if missing:
                raise S3DestinationError(
                    f"Partition columns not in the output: {missing}"
                )
            n_partitions = int(
                lf.select(pl.struct(dest.partition_by).n_unique()).collect(engine="streaming").item()
            )
            if n_partitions > MAX_PARTITIONS:
                raise S3DestinationError(
                    f"Partitioning by {dest.partition_by} would produce {n_partitions} "
                    f"partitions (limit {MAX_PARTITIONS}): are these the right columns?"
                )
            # sink partizionato hive-style, in streaming
            sink_to = pl.PartitionBy(workdir, key=dest.partition_by, include_key=True)
            if dest.format == "parquet":
                lf.sink_parquet(sink_to, mkdir=True)
            else:
                lf.sink_csv(sink_to, mkdir=True)
            uploads = [(local, f"{target_key}/{rel}") for local, rel in _local_files(workdir)]
            if not uploads:  # an empty result: no partition, no file
                raise S3DestinationError("The output is empty: no partitions to write")
        elif dest.format == "parquet":
            uploads = [(tmp, target_key)]  # parquet already: no conversion
        else:
            single = os.path.join(workdir, "out.csv")
            lf.sink_csv(single)
            uploads = [(single, target_key)]

        client = conn.client()
        try:
            for local, k in uploads:
                # parts sized for storages that take at most 1,000 of them (see app.utils)
                client.upload_file(local, target_bucket, k, Config=transfer_config(os.path.getsize(local)))
        except Exception as e:  # credentials, a missing bucket, the network…
            raise S3DestinationError(
                f"Upload to s3://{target_bucket}/{target_key} failed: {type(e).__name__}: {e}"
            ) from e
        # a partitioned dataset: makes the prefix IDEMPOTENT by removing the earlier runs'
        # files (the single-file output overwrites the same key already)
        if dest.partition_by:
            _delete_stale_objects(client, target_bucket, target_key, {k for _, k in uploads})
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
        shutil.rmtree(workdir, ignore_errors=True)

    logger.info(
        "s3-dest done: s3://%s/%s (%d rows, %d files, %s)",
        target_bucket, target_key, rows, len(uploads), dest.format,
    )
    return {
        "rows_written": rows,
        "bucket": target_bucket,
        "key": target_key,
        "format": dest.format,
        "files": len(uploads),
        **({"partitions": n_partitions} if dest.partition_by else {}),
    }


def test_connection(conn: S3ConnectionSpec) -> None:
    """Tests the credentials on the upload's own path: if it does not raise, the connection
    is usable. With a default bucket it checks the access to it."""
    client = conn.client()
    try:
        if (conn.bucket or "").strip():
            client.list_objects_v2(Bucket=conn.bucket.strip(), MaxKeys=1)
        else:
            client.list_buckets()
    except Exception as e:
        raise S3DestinationError(f"S3 connection failed: {type(e).__name__}: {e}") from e
