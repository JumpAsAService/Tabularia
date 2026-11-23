"""The saved Bayesian models: a model drawn in the editor as an object of a folder, with its
versions (docs/specs/bayes-model-store.md). Only the Go gateway reads and writes these tables
(gateway-go/internal/bayesmodels); the models are here so that the migrated database and the
models stay one and the same. Times are naive UTC, as in the other tables of the gateway.

- `BayesModel`: the object — name, description, folder, owner, the newest version (what the
  editor opens) and the template it was opened from (NULL: the free model). Who sees it:
  VIEW on its folder AND the use of the group `bayes`; whoever lacks either does not see it.
- `BayesModelVersion`: every save is a version, written once and never again. It holds the
  graph as saved (canonical, layout included), its semantic hash, the validator's findings
  (warnings only: a graph with an error is never saved) and the generated Stan program with
  its sha256, mapping table, the contract's two manifests and the generator's version: every
  version has a program, and the program stored is the one that runs. `family` is derived
  from the saved graph (hierarchical, series, glm, free).
"""
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import Column, Text, UniqueConstraint
from sqlmodel import Field, SQLModel


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class BayesModel(SQLModel, table=True):
    __tablename__ = "bayes_models"
    __table_args__ = (UniqueConstraint("project_id", "name", name="uq_bayes_model_project_name"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    name: str
    description: str = Field(default="", sa_column=Column(Text, nullable=False, server_default=""))
    project_id: int = Field(foreign_key="projects.id", index=True)
    owner_id: Optional[int] = Field(default=None, foreign_key="users.id")
    current_version: int = 1  # the newest version; a flow pins its own
    template_id: Optional[str] = Field(default=None, max_length=40)
    template_version: Optional[int] = None
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)


class BayesModelVersion(SQLModel, table=True):
    __tablename__ = "bayes_model_versions"
    __table_args__ = (UniqueConstraint("model_id", "version", name="uq_bayes_model_version"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    model_id: int = Field(foreign_key="bayes_models.id", index=True)
    version: int  # 1, 2, 3… per model
    graph: str = Field(sa_column=Column(Text, nullable=False))  # canonical JSON, layout included
    format_version: int
    semantic_hash: str = Field(max_length=64)  # sha256 of the canonical form without layout
    findings: str = Field(sa_column=Column(Text, nullable=False))  # JSON: the warnings at save
    program: str = Field(sa_column=Column(Text, nullable=False))  # the Stan program, never regenerated
    program_sha256: str = Field(max_length=64, index=True)  # the compile cache's key
    mapping: str = Field(sa_column=Column(Text, nullable=False))  # JSON
    data_manifest: str = Field(sa_column=Column(Text, nullable=False))  # JSON
    quantity_manifest: str = Field(sa_column=Column(Text, nullable=False))  # JSON
    codegen_version: int
    family: str = Field(max_length=12)  # hierarchical | series | glm | free
    catalog_sha256: str = Field(max_length=64)
    note: str = ""  # a stable English code the interface translates: "created", "edited", "layout", "restored from v3"
    created_at: datetime = Field(default_factory=_now)
    created_by: Optional[int] = Field(default=None, foreign_key="users.id")
