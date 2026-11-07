"""Data APIs: one or more datasources published as a read-only HTTP API, answered by pods
of their own on Kubernetes (docs/specs/data-apis.md).

Only the Go gateway reads and writes these tables; the models are here so that the migrated
database and the models stay one and the same. The times are naive UTC, as the rest.

- `DataApi`: the object of a folder — its name, its address (`slug`, unique in the
  installation), its state (draft, active, paused), the runtime of its pods (JSON). A
  deleted creator takes their data APIs with them.
- `DataApiVersion`: every save that changes what the API publishes is a version, as for
  the other objects; `content` is JSON (the resources and the runtime).
- `DataApiResource`: the current version's resources, one row each: a source given as a
  kind plus an id (only `datasource` for now), so that other sources fit in later. A
  datasource published by an API cannot be deleted until it is taken out (RESTRICT).
- `DataApiKey`: the keys the other systems call with; the key itself is never kept, only
  its sha-256 and its first 8 characters.
- `DataApiUsage`: the counts per key per minute the pods send.
"""
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import Column, Text, UniqueConstraint
from sqlmodel import Field, SQLModel


def _now() -> datetime:
    return datetime.now(timezone.utc)


class DataApi(SQLModel, table=True):
    __tablename__ = "data_apis"
    __table_args__ = (UniqueConstraint("project_id", "name", name="uq_data_api_project_name"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(max_length=200)
    # the address: /data/{slug}/…, lowercase letters, digits and dashes
    slug: str = Field(max_length=63, unique=True)
    description: str = ""
    project_id: int = Field(foreign_key="projects.id", index=True)
    # a deleted creator takes their data APIs with them
    creator_id: int = Field(foreign_key="users.id", index=True, ondelete="CASCADE")
    state: str = Field(default="draft", max_length=16)  # draft | active | paused
    runtime: str = Field(default="{}", sa_column=Column(Text, nullable=False))  # JSON
    # sha-256 of the pods' token, set at activation
    pod_token_hash: Optional[str] = Field(default=None, max_length=64)
    current_version: int = 1
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)


class DataApiVersion(SQLModel, table=True):
    __tablename__ = "data_api_versions"
    __table_args__ = (UniqueConstraint("data_api_id", "version", name="uq_data_api_version"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    data_api_id: int = Field(foreign_key="data_apis.id", index=True, ondelete="CASCADE")
    version: int
    content: str = Field(sa_column=Column(Text, nullable=False))
    note: str = ""  # a stable English code the interface translates: "created", "edited", "restored from v2"
    created_at: datetime = Field(default_factory=_now)
    created_by: Optional[int] = Field(default=None, foreign_key="users.id", ondelete="SET NULL")


class DataApiResource(SQLModel, table=True):
    __tablename__ = "data_api_resources"
    __table_args__ = (UniqueConstraint("data_api_id", "name", name="uq_data_api_resource_name"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    data_api_id: int = Field(foreign_key="data_apis.id", index=True, ondelete="CASCADE")
    position: int = 0
    name: str = Field(max_length=63)  # in the path: /data/{slug}/{name}
    source_kind: str = Field(default="datasource", max_length=16)
    datasource_id: Optional[int] = Field(default=None, foreign_key="datasources.id", index=True, ondelete="RESTRICT")
    columns: str = Field(default="[]", sa_column=Column(Text, nullable=False))  # JSON list of names
    filters: str = Field(default="[]", sa_column=Column(Text, nullable=False))  # JSON [{column, operators}]
    default_sort: str = Field(default="[]", sa_column=Column(Text, nullable=False))  # JSON ["-score", "id"]
    max_page: int = 1000


class DataApiKey(SQLModel, table=True):
    __tablename__ = "data_api_keys"

    id: Optional[int] = Field(default=None, primary_key=True)
    data_api_id: int = Field(foreign_key="data_apis.id", index=True, ondelete="CASCADE")
    name: str = Field(max_length=200)  # who uses it: «Salesforce production»
    prefix: str = Field(max_length=8)
    # sha-256 of the key (`tdk_` and 40 random characters): the key itself is never kept
    hash: str = Field(max_length=64, unique=True)
    expires_at: datetime
    rate_per_minute: int
    resources: str = Field(default="[]", sa_column=Column(Text, nullable=False))  # JSON names; [] = all
    cidrs: str = Field(default="[]", sa_column=Column(Text, nullable=False))  # JSON; [] = any address
    revoked_at: Optional[datetime] = None
    # a renewed key keeps working until then
    grace_until: Optional[datetime] = None
    created_by: Optional[int] = Field(default=None, foreign_key="users.id", ondelete="SET NULL")
    created_at: datetime = Field(default_factory=_now)
    last_used_at: Optional[datetime] = None


class DataApiUsage(SQLModel, table=True):
    __tablename__ = "data_api_usage"
    __table_args__ = (UniqueConstraint("key_id", "minute", name="uq_data_api_usage_key_minute"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    data_api_id: int = Field(foreign_key="data_apis.id", index=True, ondelete="CASCADE")
    key_id: int = Field(foreign_key="data_api_keys.id", ondelete="CASCADE")
    minute: datetime = Field(index=True)
    requests: int = 0
    rows: int = 0
    errors: int = 0
    throttled: int = 0
