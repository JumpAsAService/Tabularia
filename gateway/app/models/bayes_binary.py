"""The compiles of the saved Bayesian models' programs and the cache of their binaries
(docs/specs/bayes-stan-compile.md, «The schema»). Only the Go gateway reads and writes these
tables (gateway-go/internal/bayescompile); the models are here so that the migrated database
and the models stay one and the same. Times are naive UTC, as in the other tables of the gateway.

- `BayesBinary`: one row per cache key (the program's sha256, CmdStan's version, the flags, the
  architecture and the image's digest): the compile's state (queued, running, ready, failed,
  timeout; no row is `none`), who it counts against (one compile per person), the limits it ran
  under (a `timeout` is not compiled again under the same limits) and, once ready, the binary's
  sha256 from the compile Job's termination message, its size and its last use (least recently
  used eviction). The bytes live in Tabularia's storage under `bayes-binaries/<sha256>`.
- `BayesSettings`: one row (`id = 1`), the administrator's compile limits and cache limit; a
  NULL column is the recipe's default.
"""
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import BigInteger, Column, Text, UniqueConstraint
from sqlmodel import Field, SQLModel


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class BayesBinary(SQLModel, table=True):
    __tablename__ = "bayes_binaries"
    __table_args__ = (UniqueConstraint("cache_key", name="uq_bayes_binary_cache_key"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    cache_key: str = Field(max_length=64)  # sha256 of the key's seven lines
    program_sha256: str = Field(max_length=64, index=True)
    cmdstan: str = Field(max_length=16)  # 2.40.0
    image_digest: str = Field(max_length=71, index=True)  # sha256:…
    arch: str = Field(max_length=16)
    group_version: int  # the group's version whose image compiled it
    state: str = Field(max_length=8)  # queued | running | ready | failed | timeout
    reason: Optional[str] = Field(default=None, max_length=20)  # compile_failed, program_hash, crosscheck, deadline, memory, cluster, job, too_large
    detail: Optional[str] = Field(default=None, sa_column=Column(Text))  # the sentence, English; the page translates by reason
    requested_by: Optional[int] = Field(default=None, foreign_key="users.id")  # whose turn it counts in
    model_id: Optional[int] = None  # the request that queued it (a program may be held by several versions)
    version: Optional[int] = None
    attempt: int = Field(default=0, sa_column_kwargs={"server_default": "0"})
    job_name: Optional[str] = Field(default=None, max_length=63)
    deadline_seconds: Optional[int] = None  # the limits it ran under (a timeout is not retried under them)
    memory_bytes: Optional[int] = Field(default=None, sa_column=Column(BigInteger))
    binary_sha256: Optional[str] = Field(default=None, max_length=64)  # from the termination message only
    binary_bytes: Optional[int] = Field(default=None, sa_column=Column(BigInteger))
    info_sha256: Optional[str] = Field(default=None, max_length=64)
    warnings: Optional[str] = Field(default=None, sa_column=Column(Text))  # stanc's lines, JSON, at most 50
    seconds: Optional[float] = None  # the compile's time
    queued_at: Optional[datetime] = None
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    last_used_at: Optional[datetime] = None


class BayesSettings(SQLModel, table=True):
    __tablename__ = "bayes_settings"

    id: Optional[int] = Field(default=None, primary_key=True)  # always 1
    compile_cpu: Optional[str] = Field(default=None, max_length=8)
    compile_memory: Optional[str] = Field(default=None, max_length=16)
    compile_seconds: Optional[int] = None
    compile_tmp: Optional[str] = Field(default=None, max_length=16)
    compile_slots: Optional[int] = None  # «Compiles at once»
    cache_max_bytes: Optional[int] = Field(default=None, sa_column=Column(BigInteger))
    sampling_tmp: Optional[str] = Field(default=None, max_length=16)
    updated_by: Optional[str] = Field(default=None, max_length=320)
    updated_at: datetime = Field(default_factory=_now)
