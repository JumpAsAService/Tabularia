from pydantic import BaseModel, Field
from typing import Any, Literal, Optional



# ─────────────────────────────────────────────────────────────────────────────
# Request/Response Models
# ─────────────────────────────────────────────────────────────────────────────
class TestTaskRequest(BaseModel):
    message: str = Field(..., description="Messaggio da processare")
    delay: int = Field(default=5, description="Secondi di attesa simulata", ge=1, le=60)


class ProcessFileRequest(BaseModel):
    bucket: str = Field(..., description="Nome del bucket S3")
    input_key: str = Field(..., description="Chiave del file di input")
    output_key: str = Field(..., description="Chiave del file di output")


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