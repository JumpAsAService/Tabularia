"""
Interfaccia dell'engine di trasformazione e modelli di dominio.

L'idea è tenere separata la *rappresentazione logica del flow* (una lista di
Operation, la IR dichiarativa) dall'*engine di esecuzione* (Polars oggi,
eventualmente DuckDB/ClickHouse domani per i job pesanti). Route e task Celery
parlano solo con questa interfaccia, mai con Polars direttamente.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from pydantic import BaseModel, Field


# ─────────────────────────────────────────────────────────────────────────────
# Modelli di dominio (IR + risultati)
# ─────────────────────────────────────────────────────────────────────────────
class DataSource(BaseModel):
    """Riferimento a un parquet nello storage S3 (rclone)."""

    bucket: str
    key: str


class Operation(BaseModel):
    """Un singolo step del flow: un tipo + i suoi parametri."""

    type: str = Field(..., description="Tipo di operazione (filter, select, group_by, join, ...)")
    params: dict[str, Any] = Field(default_factory=dict)


class ColumnInfo(BaseModel):
    name: str
    dtype: str


class PreviewResult(BaseModel):
    """Risultato di una preview: campione di righe + schema."""

    columns: list[ColumnInfo]
    rows: list[dict[str, Any]]
    row_count: int = Field(..., description="Numero di righe nel campione restituito")
    truncated: bool = Field(..., description="True se il risultato reale ha più righe del limite")


class RunResult(BaseModel):
    """Risultato di un run completo: dove ha scritto e cosa."""

    destination: DataSource
    rows_written: int
    columns: list[ColumnInfo]


# ─────────────────────────────────────────────────────────────────────────────
# Interfaccia engine
# ─────────────────────────────────────────────────────────────────────────────
class Engine(ABC):
    """
    Contratto che ogni backend di esecuzione deve implementare.

    - `preview`: esegue il flow su un campione, in modo *sincrono e veloce*
      (serve per il feedback interattivo mentre l'utente costruisce il flow).
    - `run`: esegue il flow intero e materializza il parquet di output
      (tipicamente lanciato in modo asincrono da un worker Celery).
    - `export`: esegue il flow (anche parziale) e scrive il risultato in un
      file locale csv/xlsx, per il download diretto dall'editor.
    """

    @abstractmethod
    def preview(
        self,
        source: DataSource,
        operations: list[Operation] | list[dict[str, Any]],
        limit: int = 100,
    ) -> PreviewResult: ...

    @abstractmethod
    def run(
        self,
        source: DataSource,
        operations: list[Operation] | list[dict[str, Any]],
        destination: DataSource,
    ) -> RunResult: ...

    def export(
        self,
        source: DataSource,
        operations: list[Operation] | list[dict[str, Any]],
        fmt: str,
        out_path: str,
        limit: int | None = None,
    ) -> None:
        # non abstract: un engine può non supportare l'export diretto
        raise NotImplementedError(f"{type(self).__name__} non supporta l'export")
