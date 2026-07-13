"""Eccezioni dell'engine."""
from __future__ import annotations


class EngineError(Exception):
    """Errore generico dell'engine."""


class UnknownOperationError(EngineError):
    def __init__(self, op_type: str):
        self.op_type = op_type
        super().__init__(f"Operazione sconosciuta: '{op_type}'")


class SourceNotFoundError(EngineError):
    """Un parquet di input non esiste più nello storage (blob rimosso o chiave
    stantia in un flow salvato). Da tradurre in 404, non in 500."""

    def __init__(self, bucket: str, key: str):
        self.bucket = bucket
        self.key = key
        super().__init__(
            f"Sorgente non trovata: {bucket}/{key} non esiste (i dati potrebbero "
            f"essere stati rimossi). Ricarica il file o aggiorna la sorgente."
        )


class OperationError(EngineError):
    """Errore durante l'applicazione di una specifica operazione del flow."""

    def __init__(self, op_type: str, index: int, message: str):
        self.op_type = op_type
        self.index = index
        super().__init__(f"Errore nell'operazione #{index} '{op_type}': {message}")
