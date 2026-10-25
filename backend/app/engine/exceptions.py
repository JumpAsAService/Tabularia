"""The engine's exceptions."""
from __future__ import annotations


class EngineError(Exception):
    """A generic engine error."""


class UnknownOperationError(EngineError):
    def __init__(self, op_type: str):
        self.op_type = op_type
        super().__init__(f"Unknown operation: '{op_type}'")


class SourceNotFoundError(EngineError):
    """An input parquet no longer exists on the storage (a removed blob or a stale key in a
    saved flow). To be turned into a 404, not a 500."""

    def __init__(self, bucket: str, key: str):
        self.bucket = bucket
        self.key = key
        super().__init__(
            f"Source not found: {bucket}/{key} does not exist (the data may "
            f"have been removed). Upload the file again or refresh the source."
        )


class OperationError(EngineError):
    """An error while applying one of the flow's operations."""

    def __init__(self, op_type: str, index: int, message: str):
        self.op_type = op_type
        self.index = index
        super().__init__(f"Error in operation #{index} '{op_type}': {message}")
