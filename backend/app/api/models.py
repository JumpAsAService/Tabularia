from pydantic import BaseModel, Field, field_validator
from typing import Any, Literal, Optional



# ─────────────────────────────────────────────────────────────────────────────
# Request/Response Models
# ─────────────────────────────────────────────────────────────────────────────
class TransformOperation(BaseModel):
    type: str = Field(..., description="The operation's type (filter, aggregate, join…)")
    params: dict[str, Any] = Field(default_factory=dict, description="The operation's parameters")


class TransformDataRequest(BaseModel):
    bucket: str = Field(..., description="Nome del bucket S3")
    input_key: str = Field(..., description="Key of the input file")
    output_key: str = Field(..., description="Key of the output file")
    operations: list[TransformOperation] = Field(..., description="The operations to apply")
    # an optional destination (Output node): {"type": "database"|"s3",
    # "connection": …, "target": …}; the secret in the connection is Fernet-encrypted,
    # as for the ingest
    destination: Optional[dict[str, Any]] = Field(
        default=None, description="An optional destination (database or S3)"
    )
    # a BEST-EFFORT copy to an external S3, in addition to the output: the same shape as
    # `destination` ({"connection": …, "target": …}) but its error does NOT fail the
    # task — output and datasource stay the primary result
    mirror: Optional[dict[str, Any]] = Field(
        default=None, description="A best-effort copy to an external S3 (in addition to the output)"
    )
    # emailing the output as an attachment: the same shape as `destination`
    # ({"connection": …, "target": …}) plus `stop_on_failure`. The secret in the
    # connection is Fernet-encrypted, and the gateway has validated the recipients
    # against the connection's allowed domains already.
    email: Optional[dict[str, Any]] = Field(
        default=None, description="Sending the output as an email attachment"
    )
    engine: Optional[str] = Field(default=None, description="Engine da usare (es. polars); None = default")
    # the data contract of the datasource the output will be published to: evaluated on
    # the parquet just written, the report comes back with the task's result
    contract: Optional[dict[str, Any]] = Field(default=None, description="The data contract to evaluate on the output")


class TaskResponse(BaseModel):
    task_id: str
    status: str
    message: str


class TaskStatusResponse(BaseModel):
    task_id: str
    status: str
    result: Optional[dict[str, Any]] = None
    error: Optional[str] = None
    error_detail: Optional[str] = None  # the whole traceback on FAILURE (for debugging)
    message: Optional[str] = None
    # STARTED: how far an import is (phase, rows, file) — see jobs._Avanzamento
    progress: Optional[dict[str, Any]] = None


class PreviewRequest(BaseModel):
    bucket: str = Field(..., description="Nome del bucket S3")
    input_key: str = Field(..., description="Chiave del parquet di input")
    operations: list[TransformOperation] = Field(
        default_factory=list, description="Flow da applicare (IR dichiarativa)"
    )
    limit: int = Field(default=100, ge=1, le=1000, description="The most rows in the sample")
    engine: Optional[str] = Field(default=None, description="Engine da usare (es. polars); None = default")
    no_cache: bool = Field(default=False, description="Neither write nor read the step cache (the Viewer's exploratory queries)")
    # The preview's slot: in every slot only the LAST request counts — a new one knocks
    # down the previous (see api/preview_slots.py). Absent = as before.
    # On whose behalf it runs: "ai" = the assistant's query → the dedicated ClickHouse
    # user, when configured (engine/principal.py). Only a DOWNGRADE: declaring it gives
    # nothing more, so it needs no protection.
    principal: Optional[str] = Field(default=None, pattern=r"^ai$", description="The query's identity: 'ai' = the assistant")
    slot: Optional[str] = Field(default=None, max_length=96, pattern=r"^[A-Za-z0-9:_-]+$",
                                description="Slot: a new preview on the same slot cancels the previous one")
    # The keys are part of the materialized copy's IDENTITY: varying them makes distinct
    # copies of the same data. Without a cap, a request repeated with ever different
    # keys would fill the shared server's disk.
    sort_keys: list[str] = Field(
        default_factory=list, max_length=8,
        description="ORDER BY columns the materialized copy (ClickHouse) must inherit",
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
    """Downloading a chain's result directly (partial too, up to an intermediate node):
    csv streamed, xlsx with the format's row cap."""
    bucket: str = Field(..., description="Nome del bucket S3")
    input_key: str = Field(..., description="Chiave del parquet di input")
    operations: list[TransformOperation] = Field(
        default_factory=list, description="The chain up to the node to export"
    )
    format: Literal["csv", "xlsx"] = Field(default="csv", description="The file's format")
    limit: Optional[int] = Field(default=None, ge=1, description="The most rows (optional)")
    filename: Optional[str] = Field(default=None, description="The file name suggested to the browser")
    engine: Optional[str] = Field(default=None, description="The engine that computes the snapshot (compute dialect); Polars then writes the file")


class ExportStart(ExportRequest):
    """An export the requester follows and then downloads: the gateway names who
    asked, and only that name may see its state and take its file."""
    owner: str = Field(..., min_length=1, description="Who asked for the export, as the gateway names them")

class CacheChain(BaseModel):
    """One node of an editor's flow: the chain a preview of it would send."""
    node_id: str = Field(..., max_length=200)
    bucket: str
    input_key: str
    operations: list[TransformOperation] = Field(default_factory=list)


class CacheStatusRequest(BaseModel):
    """The nodes whose result the editor asks about, for its engine."""
    engine: Optional[str] = None
    chains: list[CacheChain] = Field(default_factory=list, max_length=2000)
