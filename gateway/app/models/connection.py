from datetime import datetime, timezone
from typing import Optional
from sqlmodel import SQLModel, Field
from sqlalchemy import Column, Text, UniqueConstraint


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Connection(SQLModel, table=True):
    """
    A connection to an external database (PostgreSQL/MySQL/MariaDB/ClickHouse/Trino). It
    lives in a folder as flows and datasources do, but with the ORTHOGONAL `CONNECT`
    capability: whoever has it can create/use/manage that branch's connections, without
    ever seeing the password (encrypted at rest, never in the API).
    """
    __tablename__ = "connections"
    __table_args__ = (UniqueConstraint("project_id", "name", name="uq_connection_project_name"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    name: str
    description: str = ""
    project_id: int = Field(foreign_key="projects.id", index=True)
    owner_id: Optional[int] = Field(default=None, foreign_key="users.id")

    db_type: str  # postgresql | mysql | mariadb | clickhouse | trino | oracle | sqlserver | s3 | smtp | sharepoint | sftp | api | gsheets
    host: str
    port: Optional[int] = None  # None = the db_type's default port
    username: str = ""
    password_encrypted: str = Field(default="", sa_column=Column(Text, nullable=False))
    database: str = ""
    db_schema: str = ""  # Postgres schema / Trino schema; empty = default

    # Options specific to the connection's KIND, in JSON. The columns above cover what the
    # databases and S3 have in common; SMTP instead carries three settings without a natural
    # column — sender, TLS mode and the allowed domains for the recipients — and reusing
    # `database`/`db_schema` as S3 does would have left one out anyway. Better a declared
    # place than a third trick. Empty ("{}") for every other kind.
    extra: str = Field(default="{}", sa_column=Column(Text, nullable=False))

    # SFTP. The private key (PEM or OpenSSH) and its passphrase, encrypted TOGETHER
    # with Fernet as {"private_key", "passphrase"}: a secret like the password, never
    # in the API's answers. Which of the two authenticates is `extra.auth`.
    private_key_encrypted: str = Field(default="", sa_column=Column(Text, nullable=False, server_default=""))
    # The server's host key accepted at the first test (trust on first use): its type
    # (ssh-ed25519, ecdsa-sha2-nistp256, ssh-rsa…) and SHA256 fingerprint. Written ONLY
    # by a test the engine made (routes/connections.test_connection), with an audit
    # line; emptied when host or port change. Empty = never accepted: refused.
    host_key_type: str = Field(default="", sa_column_kwargs={"server_default": ""})
    host_key_fingerprint: str = Field(default="", sa_column_kwargs={"server_default": ""})

    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)
