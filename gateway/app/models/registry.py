"""Container registries (Administration → Container registries): where the images of the
Container node come from. Administrators only define them; who writes flows picks an
image of an allowed repository.

- `ContainerRegistry`: the address, how to trust its certificate, the credentials
  (the password or token encrypted, never read back), and the repositories whose
  images may run (one per line: `team/forecast`, `team/*`, or `*`).
- `ContainerRegistryVariable`: what every container from that registry finds in its
  environment. A secret one is encrypted and never read back.
- `ContainerRegistryAccess`: who may see and use the registry in their flows, a group
  or a user, and which of its repositories (empty: all those the registry allows).
  Whoever has none cannot open a flow that uses the registry. Administrators may use
  them all.

Times are naive UTC, as in the other tables of the gateway.
"""
from datetime import datetime
from typing import Optional

from sqlalchemy import Column, Text, UniqueConstraint
from sqlmodel import Field, SQLModel

from app.models.scripting import now_utc

PROTOCOLS = ("https", "http")


class ContainerRegistry(SQLModel, table=True):
    __tablename__ = "container_registries"

    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(max_length=120, sa_column_kwargs={"unique": True})
    description: str = Field(default="", sa_column=Column(Text, nullable=False, server_default=""))
    host: str = Field(max_length=260)  # docker.io, ghcr.io, registry.acme.internal:5000
    protocol: str = Field(default="https", max_length=8)  # https | http (internal registries only)
    # how the certificate is trusted: public authorities, the company's (ca_cert), or not checked
    tls: str = Field(default="public", max_length=8)
    ca_cert: str = Field(default="", sa_column=Column(Text, nullable=False, server_default=""))  # PEM, public
    username: str = Field(default="", max_length=255)
    password_enc: Optional[str] = Field(default=None, sa_column=Column(Text))  # Fernet, never returned
    repositories: str = Field(default="", sa_column=Column(Text, nullable=False, server_default=""))
    enabled: bool = True
    # the last Test, kept so the list says whether the connection works
    last_test_at: Optional[datetime] = None
    last_test_ok: Optional[bool] = None
    last_test_message: str = Field(default="", sa_column=Column(Text, nullable=False, server_default=""))
    created_at: datetime = Field(default_factory=now_utc)
    created_by: Optional[str] = Field(default=None, max_length=320)
    updated_at: datetime = Field(default_factory=now_utc)
    updated_by: Optional[str] = Field(default=None, max_length=320)


class ContainerRegistryVariable(SQLModel, table=True):
    __tablename__ = "container_registry_variables"
    __table_args__ = (UniqueConstraint("registry_id", "name", name="uq_container_registry_variable_name"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    registry_id: int = Field(foreign_key="container_registries.id", index=True)
    name: str = Field(max_length=128)
    secret: bool = False
    value: str = Field(default="", sa_column=Column(Text, nullable=False, server_default=""))  # plain ones only
    value_enc: Optional[str] = Field(default=None, sa_column=Column(Text))  # secret ones (Fernet)
    updated_at: datetime = Field(default_factory=now_utc)
    updated_by: Optional[str] = Field(default=None, max_length=320)


class ContainerRegistryAccess(SQLModel, table=True):
    __tablename__ = "container_registry_access"

    id: Optional[int] = Field(default=None, primary_key=True)
    registry_id: int = Field(foreign_key="container_registries.id", index=True)
    # exactly one of the two
    group_id: Optional[int] = Field(default=None, foreign_key="groups.id", index=True)
    user_id: Optional[int] = Field(default=None, foreign_key="users.id", index=True)
    # one pattern per line, as the registry's own list; empty: all the registry allows
    repositories: str = Field(default="", sa_column=Column(Text, nullable=False, server_default=""))
    created_at: datetime = Field(default_factory=now_utc)
    created_by: Optional[str] = Field(default=None, max_length=320)
