"""Destinazioni S3 / object storage: riversa l'output di un run su un bucket
ESTERNO (AWS, MinIO, R2, Wasabi…) — file singolo oppure dataset partizionato
hive-style (`colonna=valore/…`), pronto per il partition pruning di Trino,
Athena, DuckDB e Spark.

Percorso dati: parquet interno → disco locale (buffer) → sink Polars in
STREAMING (conversione formato e/o partizionamento con `pl.PartitionBy`) →
upload boto3 file per file. Memoria O(batch), il disco fa da buffer, come per
ingest e destinazioni database.

Le credenziali arrivano cifrate (secret_key_encrypted, Fernet condiviso col
gateway) e vengono decifrate solo al momento di creare il client.
"""
from __future__ import annotations

import logging
import os
import shutil
import tempfile
from typing import Literal

import polars as pl
from pydantic import BaseModel, Field

from app.ingest.converters import IngestError

logger = logging.getLogger(__name__)

# oltre questo numero di partizioni si genera il classico problema dei "mille
# file piccoli" (e upload lentissimi): quasi sempre è una colonna sbagliata
MAX_PARTITIONS = 1000


class S3DestinationError(IngestError):
    """Errore parlante da mostrare all'utente (bucket, chiave, credenziali)."""


class S3ConnectionSpec(BaseModel):
    endpoint_url: str = ""  # vuoto = AWS vero; valorizzato = MinIO/R2/Wasabi…
    access_key: str = ""
    # una delle due: `secret_key_encrypted` (Fernet, come arriva dal gateway) o
    # `secret_key` in chiaro (solo per prove dirette sull'engine in sviluppo)
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
            # path-style: obbligatorio per MinIO/Rclone e innocuo per gli altri
            kwargs["config"] = Config(signature_version="s3v4", s3={"addressing_style": "path"})
        if self.region.strip():
            kwargs["region_name"] = self.region.strip()
        return boto3.client(
            "s3",
            aws_access_key_id=self.access_key,
            aws_secret_access_key=self.resolve_secret(),
            **kwargs,
        )


class S3DestinationSpec(BaseModel):
    bucket: str = ""  # vuoto = bucket di default della connessione
    key: str  # chiave del file (senza partizioni) o prefisso (con partizioni)
    format: Literal["parquet", "csv"] = "parquet"
    partition_by: list[str] = Field(default_factory=list)


def _clean_key(key: str) -> str:
    k = (key or "").strip().strip("/")
    if not k:
        raise S3DestinationError("La chiave/percorso S3 di destinazione è vuota")
    if any(part in ("", ".", "..") for part in k.split("/")):
        raise S3DestinationError(f"Chiave S3 non valida: {key!r}")
    return k


def _local_files(root: str) -> list[tuple[str, str]]:
    """[(path locale, path relativo con separatore '/')] di tutti i file sotto root."""
    out: list[tuple[str, str]] = []
    for dirpath, _dirs, files in os.walk(root):
        for f in files:
            local = os.path.join(dirpath, f)
            out.append((local, os.path.relpath(local, root).replace(os.sep, "/")))
    return sorted(out, key=lambda t: t[1])


def write_output_to_s3(
    conn: S3ConnectionSpec,
    dest: S3DestinationSpec,
    bucket: str,
    key: str,
    storage=None,
) -> dict:
    """Riversa `bucket/key` (parquet interno) sulla destinazione S3 esterna.

    Senza partizioni: un solo oggetto a `dest.key` (parquet copiato com'è,
    csv convertito in streaming). Con `partition_by`: albero hive sotto il
    prefisso `dest.key/` — le run successive sovrascrivono gli stessi path,
    ma le partizioni sparite dai dati NON vengono eliminate dal bucket.
    """
    if storage is None:
        from app.utils import get_storage_service

        storage = get_storage_service()

    target_bucket = (dest.bucket or "").strip() or (conn.bucket or "").strip()
    if not target_bucket:
        raise S3DestinationError(
            "Nessun bucket di destinazione: indicalo sul nodo Output o nella connessione"
        )
    target_key = _clean_key(dest.key)

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
                    f"colonne di partizione inesistenti nell'output: {missing}"
                )
            n_partitions = int(
                lf.select(pl.struct(dest.partition_by).n_unique()).collect(engine="streaming").item()
            )
            if n_partitions > MAX_PARTITIONS:
                raise S3DestinationError(
                    f"partizionare per {dest.partition_by} produrrebbe {n_partitions} "
                    f"partizioni (limite {MAX_PARTITIONS}): sono le colonne giuste?"
                )
            # sink partizionato hive-style, in streaming
            sink_to = pl.PartitionBy(workdir, key=dest.partition_by, include_key=True)
            if dest.format == "parquet":
                lf.sink_parquet(sink_to, mkdir=True)
            else:
                lf.sink_csv(sink_to, mkdir=True)
            uploads = [(local, f"{target_key}/{rel}") for local, rel in _local_files(workdir)]
            if not uploads:  # risultato vuoto: nessuna partizione, nessun file
                raise S3DestinationError("l'output è vuoto: nessuna partizione da scrivere")
        elif dest.format == "parquet":
            uploads = [(tmp, target_key)]  # già parquet: nessuna conversione
        else:
            single = os.path.join(workdir, "out.csv")
            lf.sink_csv(single)
            uploads = [(single, target_key)]

        client = conn.client()
        try:
            for local, k in uploads:
                client.upload_file(local, target_bucket, k)
        except Exception as e:  # credenziali, bucket inesistente, rete…
            raise S3DestinationError(
                f"upload su s3://{target_bucket}/{target_key} fallito: {type(e).__name__}: {e}"
            ) from e
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
        shutil.rmtree(workdir, ignore_errors=True)

    logger.info(
        "s3-dest completato: s3://%s/%s (%d righe, %d file, %s)",
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
    """Prova le credenziali sullo stesso percorso dell'upload: se non solleva,
    la connessione è utilizzabile. Col bucket di default ne verifica l'accesso."""
    client = conn.client()
    try:
        if (conn.bucket or "").strip():
            client.list_objects_v2(Bucket=conn.bucket.strip(), MaxKeys=1)
        else:
            client.list_buckets()
    except Exception as e:
        raise S3DestinationError(f"connessione S3 fallita: {type(e).__name__}: {e}") from e
