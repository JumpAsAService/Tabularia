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

from app.engine.base import DataSource


class OperationContext:
    def __init__(self, storage):
        self.storage = storage
        self._temp_paths: list[str] = []

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
        self.storage.download_file(source.bucket, source.key, local_path)
        return pl.scan_parquet(local_path)

    def cleanup(self) -> None:
        for path in self._temp_paths:
            try:
                os.remove(path)
            except OSError:
                pass
        self._temp_paths.clear()
