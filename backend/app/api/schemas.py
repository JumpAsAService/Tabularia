from pydantic import Field
from typing import Any, Optional

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


class TaskResponse(BaseModel):
    task_id: str
    status: str
    message: str


class TaskStatusResponse(BaseModel):
    task_id: str
    status: str
    result: Optional[dict[str, Any]] = None
    error: Optional[str] = None