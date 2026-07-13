"""
Contesto di esecuzione condiviso tra le operazioni di un flow.

Fornisce:
- accesso lazy alle sorgenti (`scan`) — usato anche dalle operazioni che
  hanno bisogno di leggere un secondo parquet (es. join);
- gestione dei file temporanei, ripuliti a fine run.
"""
from __future__ import annotations

import os
import tempfile

import polars as pl
from botocore.exceptions import ClientError

from app.engine.base import DataSource
from app.engine.exceptions import SourceNotFoundError

# codici S3/MinIO per "oggetto (o bucket) inesistente"
_NOT_FOUND_CODES = {"404", "NoSuchKey", "NoSuchBucket"}


class OperationContext:
    def __init__(self, storage):
        self.storage = storage
        self._temp_paths: list[str] = []
        # budget cumulativo di iterazioni foreach su TUTTA la catena di un run:
        # limita l'esplosione moltiplicativa dei foreach annidati (vedi op_foreach)
        self.foreach_iterations = 0

    def tempfile(self, suffix: str = ".parquet") -> str:
        fd, path = tempfile.mkstemp(suffix=suffix, prefix="dataprep_")
        os.close(fd)
        self._temp_paths.append(path)
        return path

    def scan(self, source: DataSource) -> pl.LazyFrame:
        """
        Scarica il parquet dallo storage e lo apre in modalità lazy.

        Il file temporaneo resta su disco finché non viene chiamato
        `cleanup()`, così lo scan lazy (e lo streaming) può leggerlo.
        """
        local_path = self.tempfile(".parquet")
        try:
            self.storage.download_file(source.bucket, source.key, local_path)
        except ClientError as e:
            code = str(e.response.get("Error", {}).get("Code", ""))
            if code in _NOT_FOUND_CODES:
                raise SourceNotFoundError(source.bucket, source.key) from e
            raise
        return pl.scan_parquet(local_path)

    def cleanup(self) -> None:
        for path in self._temp_paths:
            try:
                os.remove(path)
            except OSError:
                pass
        self._temp_paths.clear()
