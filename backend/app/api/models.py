from pydantic import BaseModel, Field, field_validator
from typing import Any, Literal, Optional



# ─────────────────────────────────────────────────────────────────────────────
# Request/Response Models
# ─────────────────────────────────────────────────────────────────────────────
class TransformOperation(BaseModel):
    type: str = Field(..., description="Tipo di operazione (es. filter, aggregate, join)")
    params: dict[str, Any] = Field(default_factory=dict, description="Parametri dell'operazione")


class TransformDataRequest(BaseModel):
    bucket: str = Field(..., description="Nome del bucket S3")
    input_key: str = Field(..., description="Chiave del file di input")
    output_key: str = Field(..., description="Chiave del file di output")
    operations: list[TransformOperation] = Field(..., description="Lista di operazioni da applicare")
    # destinazione opzionale (nodo Output): {"type": "database"|"s3",
    # "connection": …, "target": …}; la secret nella connection è
    # Fernet-cifrata, come per l'ingest
    destination: Optional[dict[str, Any]] = Field(
        default=None, description="Destinazione opzionale (database o S3)"
    )
    # copia BEST-EFFORT su S3 esterno, in aggiunta all'output: stessa forma di
    # `destination` ({"connection": …, "target": …}) ma un suo errore NON fa
    # fallire il task — output e datasource restano il risultato primario
    mirror: Optional[dict[str, Any]] = Field(
        default=None, description="Copia best-effort su S3 esterno (in aggiunta all'output)"
    )
    # invio dell'output come allegato email: stessa forma di `destination`
    # ({"connection": …, "target": …}) più `stop_on_failure`. La secret nella
    # connection è Fernet-cifrata, e i destinatari sono già stati validati dal
    # gateway contro i domini ammessi della connessione.
    email: Optional[dict[str, Any]] = Field(
        default=None, description="Invio dell'output come email con allegato"
    )
    engine: Optional[str] = Field(default=None, description="Engine da usare (es. polars); None = default")


class TaskResponse(BaseModel):
    task_id: str
    status: str
    message: str


class TaskStatusResponse(BaseModel):
    task_id: str
    status: str
    result: Optional[dict[str, Any]] = None
    error: Optional[str] = None
    error_detail: Optional[str] = None  # traceback completo su FAILURE (per il debug)
    message: Optional[str] = None


class PreviewRequest(BaseModel):
    bucket: str = Field(..., description="Nome del bucket S3")
    input_key: str = Field(..., description="Chiave del parquet di input")
    operations: list[TransformOperation] = Field(
        default_factory=list, description="Flow da applicare (IR dichiarativa)"
    )
    limit: int = Field(default=100, ge=1, le=1000, description="Righe massime nel campione")
    engine: Optional[str] = Field(default=None, description="Engine da usare (es. polars); None = default")
    no_cache: bool = Field(default=False, description="Non scrivere/leggere la step-cache (query esplorative del Viewer)")
    # Slot della preview: in ogni slot conta solo l'ULTIMA richiesta — una nuova
    # butta giu' la precedente (vedi api/preview_slots.py). Assente = come prima.
    slot: Optional[str] = Field(default=None, max_length=96, pattern=r"^[A-Za-z0-9:_-]+$",
                                description="Slot: una nuova preview sullo stesso slot annulla la precedente")
    # Le chiavi entrano nell'IDENTITA' della copia materializzata: variarle genera
    # copie distinte dello stesso dato. Senza un tetto, una richiesta ripetuta con
    # chiavi sempre diverse riempirebbe il disco del server condiviso.
    sort_keys: list[str] = Field(
        default_factory=list, max_length=8,
        description="Colonne di ORDER BY che la copia materializzata (ClickHouse) deve ereditare",
    )

    @field_validator("sort_keys")
    @classmethod
    def _clean_sort_keys(cls, v: list[str]) -> list[str]:
        out, seen = [], set()
        for k in v:
            k = (k or "").strip()
            if not k or k in seen or len(k) > 128:
                continue
            seen.add(k)
            out.append(k)
        return out


class ExportRequest(BaseModel):
    """Download diretto del risultato di una catena (anche parziale, fino a un
    nodo intermedio): csv in streaming, xlsx col tetto righe del formato."""
    bucket: str = Field(..., description="Nome del bucket S3")
    input_key: str = Field(..., description="Chiave del parquet di input")
    operations: list[TransformOperation] = Field(
        default_factory=list, description="Catena fino al nodo da esportare"
    )
    format: Literal["csv", "xlsx"] = Field(default="csv", description="Formato del file")
    limit: Optional[int] = Field(default=None, ge=1, description="Righe massime (opzionale)")
    filename: Optional[str] = Field(default=None, description="Nome file suggerito al browser")
    engine: Optional[str] = Field(default=None, description="Motore che calcola lo snapshot (dialetto compute); il file è poi scritto da Polars")