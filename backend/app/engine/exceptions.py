"""Eccezioni dell'engine."""
from __future__ import annotations


class EngineError(Exception):
    """Errore generico dell'engine."""


class UnknownOperationError(EngineError):
    def __init__(self, op_type: str):
        self.op_type = op_type
        super().__init__(f"Operazione sconosciuta: '{op_type}'")


class OperationError(EngineError):
    """Errore durante l'applicazione di una specifica operazione del flow."""

    def __init__(self, op_type: str, index: int, message: str):
        self.op_type = op_type
        self.index = index
        super().__init__(f"Errore nell'operazione #{index} '{op_type}': {message}")
