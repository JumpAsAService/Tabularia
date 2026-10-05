"""Data contracts: ciò che chi produce una datasource promette a chi la consuma.

Un contratto per datasource. Niente chiavi esterne verso `datasources`: le righe
si tolgono a mano quando la datasource sparisce (`services.contracts.forget`),
perché nei test SQLite non le fa rispettare e un vincolo dimenticato in una delle
strade che eliminano una datasource romperebbe solo in produzione.
"""
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import Column, Text, UniqueConstraint
from sqlmodel import Field, SQLModel


def _now() -> datetime:
    return datetime.now(timezone.utc)


class DataContract(SQLModel, table=True):
    __tablename__ = "data_contracts"
    __table_args__ = (UniqueConstraint("datasource_id", name="uq_contract_datasource"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    datasource_id: int = Field(index=True)
    version: int = 1
    document: str = Field(sa_column=Column(Text, nullable=False))  # JSON {description, rules: [...]}
    enabled: bool = True

    # Lo stato sui dati che la datasource serve ADESSO: pending (mai valutato su
    # questi dati) | passed | warning | failed.
    status: str = "pending"
    checked_at: Optional[datetime] = None
    errors: int = 0
    warnings: int = 0
    # l'ultimo referto, per intero (JSON): è ciò che si mostra aprendo il contratto,
    # e la base su cui la freschezza viene ricalcolata col passare del tempo
    last_report: Optional[str] = Field(default=None, sa_column=Column("last_report", Text))
    # Un aggiornamento è stato RIFIUTATO da una regola bloccante: la datasource
    # serve ancora lo snapshot precedente. Resta valorizzato finché un
    # aggiornamento non riesce: i dati sono buoni ma non sono più quelli attuali.
    blocked_at: Optional[datetime] = None
    blocked_run_id: Optional[int] = None

    # Chi avvisare quando lo stato cambia: indirizzi separati da virgola e la
    # connessione SMTP con cui spedire (come per l'avviso di fallimento dei flussi).
    notify_emails: Optional[str] = None
    notify_connection_id: Optional[int] = None

    created_by: Optional[int] = None
    updated_by: Optional[int] = None
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)


class DataContractVersion(SQLModel, table=True):
    """Ogni salvataggio del contratto: chi, quando, e il documento com'era."""
    __tablename__ = "data_contract_versions"
    __table_args__ = (UniqueConstraint("datasource_id", "version", name="uq_contract_version"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    datasource_id: int = Field(index=True)
    version: int
    document: str = Field(sa_column=Column(Text, nullable=False))
    created_by: Optional[int] = None
    created_at: datetime = Field(default_factory=_now)


class DataContractResult(SQLModel, table=True):
    """Una valutazione del contratto: lo storico."""
    __tablename__ = "data_contract_results"

    id: Optional[int] = Field(default=None, primary_key=True)
    datasource_id: int = Field(index=True)
    contract_version: int
    # che cosa l'ha fatta partire: refresh | publish (dati nuovi in arrivo),
    # save | manual (sui dati correnti), freshness (il tempo che passa)
    trigger: str
    run_id: Optional[int] = None
    snapshot_key: str = ""
    outcome: str  # passed | warning | failed
    blocked: bool = False  # i dati nuovi NON sono stati pubblicati
    errors: int = 0
    warnings: int = 0
    rows: Optional[int] = None
    report: str = Field(sa_column=Column(Text, nullable=False))  # JSON
    # L'avviso da mandare per questa valutazione: None = nessuno (lo stato non è
    # cambiato, o nessuno ha chiesto di essere avvisato) | pending → sending →
    # sent | failed | skipped. Lo scrive chi registra il referto, lo consuma lo
    # scheduler (services.contract_notifier): niente email dentro la transazione
    # che pubblica i dati.
    notify: Optional[str] = None
    evaluated_at: datetime = Field(default_factory=_now, index=True)
