from datetime import datetime, timezone
from typing import Optional
from sqlmodel import SQLModel, Field
from sqlalchemy import Column, Text, UniqueConstraint


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Datasource(SQLModel, table=True):
    """
    Un dataset NOMINATO nel catalogo: un parquet nello storage con nome, cartella
    e permessi (ereditati dal progetto, come i flussi). Oggi nasce dalla
    pubblicazione dell'output di un run; domani anche da upload e connessioni DB
    (campo `kind`).
    """
    __tablename__ = "datasources"
    __table_args__ = (UniqueConstraint("project_id", "name", name="uq_datasource_project_name"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    name: str
    description: str = ""
    project_id: int = Field(foreign_key="projects.id", index=True)
    owner_id: Optional[int] = Field(default=None, foreign_key="users.id")

    bucket: str
    key: str  # parquet nello storage (datasets/…)
    rows: Optional[int] = None
    columns: str = Field(default="[]", sa_column=Column(Text, nullable=False))  # JSON [{name, dtype}]
    # chiavi di ORDER BY (JSON [nome colonna]) con cui il parquet è stato scritto
    # ordinato: abilita il pruning a valle (viewer/ClickHouse). Vuoto = nessun
    # ordine imposto, tutto come prima. Persiste tra i refresh.
    sort_keys: str = Field(default="[]", sa_column=Column(Text, nullable=False))
    # descrizioni dei CAMPI (JSON {nome colonna: testo}), curate a mano: il
    # significato di ogni colonna per chi la usa — e per la futura integrazione
    # AI (contesto semantico sul dataset). Separate da `columns`, che viene
    # rigenerato a ogni refresh/publish: così le descrizioni sopravvivono.
    column_descriptions: str = Field(default="{}", sa_column=Column(Text, nullable=False))

    kind: str = "flow"  # flow | database (| upload, futuro)
    flow_id: Optional[int] = Field(default=None, foreign_key="flows.id")  # provenienza

    # per kind="database": la definizione della sorgente e l'ultimo refresh.
    # Il parquet è uno SNAPSHOT: refresh = nuovo ingest che sostituisce il blob.
    connection_id: Optional[int] = Field(default=None, foreign_key="connections.id")
    source_type: Optional[str] = None  # table | sql
    source_ref: Optional[str] = Field(default=None, sa_column=Column(Text))
    refreshed_at: Optional[datetime] = None

    # refresh SCHEDULATO (solo kind="database"): lo scheduler del gateway riesegue
    # l'ingest quando `next_refresh_at` è scaduto, con l'autorità di
    # `refresh_scheduled_by` (RUN+CONNECT catturati alla creazione dello schedule).
    # schedule=None → disabilitato. Formato: "interval:<min>" oppure "daily:<HH>:<MM>".
    refresh_schedule: Optional[str] = None
    refresh_scheduled_by: Optional[int] = Field(default=None, foreign_key="users.id")
    next_refresh_at: Optional[datetime] = Field(default=None, index=True)

    # run che ha prodotto lo SNAPSHOT corrente: ordina gli aggiornamenti
    # concorrenti. Gli id dei run sono monotoni e assegnati al LANCIO, quindi
    # ordinano per istante di LETTURA della sorgente — non per istante di
    # scrittura, che premierebbe il run partito prima e finito dopo, cioè il dato
    # più stantio. Uno swap da un run più vecchio di questo viene rifiutato.
    # Intero semplice, senza foreign key: `runs.datasource_id` punta già qui e un
    # riferimento inverso creerebbe un ciclo nella creazione dello schema.
    # None = nessuna baseline (snapshot anteriore a questo campo) → si accetta.
    snapshot_run_id: Optional[int] = None

    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)
