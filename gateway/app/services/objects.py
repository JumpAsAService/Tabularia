"""RBAC on the data plane: who may read which storage object.

The requests forwarded to the engine reference storage keys (input_key, foreach
drivers, export…). Before forwarding, EVERY "managed" key found in the payload must be
readable by the user under these rules:

- one's own uploads (the `uploads` register): a fresh upload lives in no project →
  only whoever uploaded it reads it;
- catalog datasources: VIEW on the datasource's project;
- runs' inputs/outputs: whoever launched the run, or VIEW on the flow's project (the
  shared history includes its outputs);
- keys referenced by a saved flow's `definition`: VIEW on the flow's project — sharing
  a flow means sharing the data it references.

Fail-closed: a managed key that falls under no rule → 403 (it holds for `cache/` too,
which is internal to the engine and must never come from the clients).
"""
from typing import Any, Iterable

from fastapi import HTTPException
from sqlmodel import Session, select
from sqlalchemy import or_

from app.models import Datasource, Flow, Run, Upload, User
from app.services import permissions as perm_service

# the managed storage's prefixes (everything else cannot be addressed by the clients)
MANAGED_PREFIXES = ("datasets/", "out/", "raw/", "cache/")

# fields that address a source for READING (top-level `input_key`, and `key` in the
# sub-sources of join/union/foreach); the output is not a read
_READ_KEY_FIELDS = ("key", "input_key")


def collect_read_refs(payload: Any) -> tuple[set[str], set[str]]:
    """Buckets and READ keys in a STRUCTURAL position (key/input_key and bucket fields),
    anywhere in the payload. Unlike collect_storage_keys, which collects any string with
    a managed prefix: here the POSITIONS count, so a key outside the managed prefixes in a
    source field is visible and refusable."""
    buckets: set[str] = set()
    keys: set[str] = set()

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                if isinstance(v, str):
                    if k in _READ_KEY_FIELDS:
                        keys.add(v)
                    elif k == "bucket":
                        buckets.add(v)
                walk(v)
        elif isinstance(node, (list, tuple)):
            for v in node:
                walk(v)

    walk(payload)
    return buckets, keys


def ensure_reads_pinned(session: Session, user: User, payload: Any, engine_bucket: str) -> None:
    """Every source of a non-superuser must be IN the engine's bucket and under a managed
    prefix. Without this, neither gateway nor engine bind bucket/key and the engine
    (credentials that read everything) would serve any (bucket, key)."""
    if perm_service.is_admin(session, user):
        return
    buckets, keys = collect_read_refs(payload)
    for b in buckets:
        if b and b != engine_bucket:
            raise HTTPException(status_code=403, detail=f"bucket not allowed: '{b}'")
    for k in keys:
        if k and not k.startswith(MANAGED_PREFIXES):
            raise HTTPException(status_code=403, detail=f"source outside the managed areas: '{k}'")


def collect_storage_keys(payload: Any) -> set[str]:
    """All the strings that look like managed keys, anywhere in the payload (the operations'
    nested parameters too, e.g. the foreach's driver)."""
    found: set[str] = set()

    def walk(node: Any) -> None:
        if isinstance(node, str):
            if node.startswith(MANAGED_PREFIXES):
                found.add(node)
        elif isinstance(node, dict):
            for value in node.values():
                walk(value)
        elif isinstance(node, (list, tuple)):
            for value in node:
                walk(value)

    walk(payload)
    return found


def _can_read_key(session: Session, user: User, key: str, readable: set[int]) -> bool:
    if session.exec(
        select(Upload.id).where(Upload.parquet_key == key, Upload.owner_id == user.id)
    ).first():
        return True

    if readable and session.exec(
        select(Datasource.id).where(Datasource.key == key, Datasource.project_id.in_(readable))
    ).first():
        return True

    for run in session.exec(
        select(Run).where(or_(Run.output_key == key, Run.input_key == key))
    ).all():
        if run.launched_by == user.id:
            return True
        if run.flow_id and readable:
            flow = session.get(Flow, run.flow_id)
            if flow and flow.project_id in readable:
                return True

    # LIKE on the readable flows' definitions: the keys are hexadecimal UUIDs, false
    # positives impossible in practice. It scales with the saved flows: if one day it became
    # a bottleneck → a key↔flow reference table.
    if readable and session.exec(
        select(Flow.id).where(Flow.project_id.in_(readable), Flow.definition.contains(key))  # type: ignore[attr-defined]
    ).first():
        return True

    return False


def ensure_can_run_keys(session: Session, user: User, keys: Iterable[str]) -> None:
    """403 at the first managed key the user cannot RUN on.

    `/tasks/transform-data` puts the workers to work and writes a parquet: it is a run,
    even without a saved flow behind it. Before, it asked only to be able to READ the
    sources, and a read-only guest (VIEW) ran what the official `/flows/{id}/runs` path
    denies them with `Capability.RUN` (audit 2026-09-30, found with the demo guest's
    credentials).

    The key→project resolution is the same as for reading — datasources, own uploads,
    runs, flows' definitions — only the set of projects changes: those with RUN, not
    those with VIEW."""
    keys = set(keys)
    if not keys or perm_service.is_admin(session, user):
        return
    runnable = perm_service.runnable_project_ids(session, user)
    for key in sorted(keys):
        if not _can_read_key(session, user, key, runnable):
            raise HTTPException(
                status_code=403,
                detail=f"Missing 'run' permission to run on '{key}': RUN on the folder is required",
            )


def ensure_can_read_keys(session: Session, user: User, keys: Iterable[str]) -> None:
    """403 at the first managed key the user cannot read."""
    keys = set(keys)
    if not keys or perm_service.is_admin(session, user):
        return
    readable = perm_service.readable_project_ids(session, user)
    for key in sorted(keys):
        if not _can_read_key(session, user, key, readable):
            raise HTTPException(status_code=403, detail=f"You do not have access to the object '{key}'")
