from datetime import datetime, timezone
from typing import Optional
from sqlmodel import SQLModel, Field
from sqlalchemy import Column, Text, UniqueConstraint


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Connection(SQLModel, table=True):
    """
    Una connessione a un database esterno (PostgreSQL/MySQL/MariaDB/ClickHouse/
    Trino). Vive in una cartella come flussi e datasource, ma con la capability
    ORTOGONALE `CONNECT`: chi la possiede può creare/usare/gestire le connessioni
    di quel ramo, senza mai vedere la password (cifrata a riposo, mai nelle API).
    """
    __tablename__ = "connections"
    __table_args__ = (UniqueConstraint("project_id", "name", name="uq_connection_project_name"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    name: str
    description: str = ""
    project_id: int = Field(foreign_key="projects.id", index=True)
    owner_id: Optional[int] = Field(default=None, foreign_key="users.id")

    db_type: str  # postgresql | mysql | mariadb | clickhouse | trino | s3 | smtp
    host: str
    port: Optional[int] = None  # None = porta di default del db_type
    username: str = ""
    password_encrypted: str = Field(default="", sa_column=Column(Text, nullable=False))
    database: str = ""
    db_schema: str = ""  # schema Postgres / schema Trino; vuoto = default

    # Opzioni specifiche del TIPO di connessione, in JSON. Le colonne qui sopra
    # coprono ciò che i database e S3 hanno in comune; SMTP invece porta tre
    # impostazioni senza una colonna naturale — mittente, modalità TLS e domini
    # ammessi per i destinatari — e riusare `database`/`db_schema` come fa S3 ne
    # avrebbe lasciata fuori una comunque. Meglio un posto dichiarato che un
    # terzo trucco. Vuoto ("{}") per tutti gli altri tipi.
    extra: str = Field(default="{}", sa_column=Column(Text, nullable=False))

    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)
