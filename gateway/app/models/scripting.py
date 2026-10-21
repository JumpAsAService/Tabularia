"""Scripting environments (Admin → Scripting environments) and the bucket the scripts
work in. See docs/specs/script-node.md.

- `ScriptStorage`, one row (id 1): the bucket the administrators created for the
  scripts and a key that works on that bucket only. The engine writes a Script
  node's input there and the Jobs read it with the same key; Tabularia's own
  storage never reaches a Job.
- `ScriptEnvironment`: a language, its packages and its variables, under a name.
- `ScriptEnvironmentVersion`: what an image is built from (language version,
  packages), built once by services/script_builds.py. Changing the packages makes a
  new version; the old images stay in the registry and can be made current again.
- `ScriptEnvironmentVariable`: what the scripts find in their environment. A secret
  one is encrypted and never read back.
- `ScriptEnvironmentGroup`: who may write Script nodes with the environment (with
  EDIT on the flow's folder). Administrators may use them all.

Times are naive UTC, as in the other tables of the gateway.
"""
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import Column, Text, UniqueConstraint
from sqlmodel import Field, SQLModel

LANGUAGES = ("python", "r")
# queued → building (the image) → checking (it starts, and what it holds) → ready | failed
VERSION_STATES = ("queued", "building", "checking", "ready", "failed")
IN_PROGRESS = ("queued", "building", "checking")


def now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class ScriptStorage(SQLModel, table=True):
    __tablename__ = "script_storage"

    id: int = Field(default=1, primary_key=True)
    endpoint: str = Field(default="", max_length=2000)  # empty = AWS
    region: str = Field(default="", max_length=64)
    bucket: str = Field(default="", max_length=255)
    access_key: str = Field(default="", max_length=255)
    secret_key_enc: Optional[str] = Field(default=None, sa_column=Column(Text))  # Fernet, never returned
    updated_at: Optional[datetime] = None
    updated_by: Optional[str] = Field(default=None, max_length=320)


class ScriptEnvironment(SQLModel, table=True):
    __tablename__ = "script_environments"

    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(max_length=120, sa_column_kwargs={"unique": True})
    description: str = Field(default="", sa_column=Column(Text, nullable=False, server_default=""))
    language: str = Field(max_length=16)  # python | r
    enabled: bool = True
    # the version the Jobs run; None until a first build succeeds
    current_version_id: Optional[int] = None
    # Kubernetes quantities, as the Jobs ask for them
    cpu_request: str = Field(default="500m", max_length=16)
    cpu_limit: str = Field(default="2", max_length=16)
    memory_request: str = Field(default="1Gi", max_length=16)
    memory_limit: str = Field(default="4Gi", max_length=16)
    max_timeout_seconds: int = 3600  # the longest timeout a node may ask for
    created_at: datetime = Field(default_factory=now_utc)
    created_by: Optional[str] = Field(default=None, max_length=320)
    updated_at: datetime = Field(default_factory=now_utc)
    updated_by: Optional[str] = Field(default=None, max_length=320)


class ScriptEnvironmentVersion(SQLModel, table=True):
    __tablename__ = "script_environment_versions"
    __table_args__ = (UniqueConstraint("environment_id", "number", name="uq_script_env_version_number"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    environment_id: int = Field(foreign_key="script_environments.id", index=True)
    number: int
    language_version: str = Field(max_length=16)
    base_image: str = Field(max_length=500)
    # one per line: Python requirements (`name==version`), R packages (`name` or `name@version`)
    packages: str = Field(default="", sa_column=Column(Text, nullable=False, server_default=""))
    status: str = Field(default="queued", max_length=16, index=True)
    job_name: Optional[str] = Field(default=None, max_length=63)  # the Job of the current stage
    image_ref: Optional[str] = Field(default=None, max_length=600)  # <registry>/…@sha256:…
    lock: Optional[str] = Field(default=None, sa_column=Column(Text))  # what the image holds, from the check
    # JSON {module: [public names]}: what the editor offers while typing, from the check
    completions: Optional[str] = Field(default=None, sa_column=Column(Text))
    build_log: Optional[str] = Field(default=None, sa_column=Column(Text))  # the end of it
    error: Optional[str] = Field(default=None, sa_column=Column(Text))
    created_at: datetime = Field(default_factory=now_utc)
    created_by: Optional[str] = Field(default=None, max_length=320)
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None


class ScriptEnvironmentVariable(SQLModel, table=True):
    __tablename__ = "script_environment_variables"
    __table_args__ = (UniqueConstraint("environment_id", "name", name="uq_script_env_variable_name"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    environment_id: int = Field(foreign_key="script_environments.id", index=True)
    name: str = Field(max_length=128)
    secret: bool = False
    value: str = Field(default="", sa_column=Column(Text, nullable=False, server_default=""))  # plain ones only
    value_enc: Optional[str] = Field(default=None, sa_column=Column(Text))  # secret ones (Fernet)
    # the marked exception: the value is one of Tabularia's own, named in `value` and
    # read from the orchestrator's environment when a Job starts
    from_tabularia: bool = False
    updated_at: datetime = Field(default_factory=now_utc)
    updated_by: Optional[str] = Field(default=None, max_length=320)


class ScriptEnvironmentGroup(SQLModel, table=True):
    __tablename__ = "script_environment_groups"

    environment_id: int = Field(foreign_key="script_environments.id", primary_key=True)
    group_id: int = Field(foreign_key="groups.id", primary_key=True)
