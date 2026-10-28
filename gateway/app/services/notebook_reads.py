"""What the notebooks read (models/notebook_read.py): written by the pod's reads
(routes/notebook_data.py), read by the Lineage page (services/lineage.py) and by
OpenLineage (services/openlineage.py), where a session is a run and the datasources it
reads are its inputs.

A row per notebook, mode and datasource, touched once per session: the first read of a
datasource in a session moves its mode's row to that session, the reads after it change
nothing — a notebook re-runs its cells often, the database is written once. The app and
the editor of a notebook each have a row of their own: they may run at once.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable

from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.models import NotebookRead


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def record(session: Session, notebook_id: int, datasource_id: int, session_key: str, mode: str) -> bool:
    """Records that the session `session_key` (its Job's name) read the datasource; True
    when it is the session's first read of it. Commits."""
    now = _now()
    mine = (NotebookRead.notebook_id == notebook_id, NotebookRead.datasource_id == datasource_id, NotebookRead.mode == mode)
    moved = session.exec(
        update(NotebookRead).where(*mine, NotebookRead.last_session != session_key)
        .values(last_session=session_key, read_at=now)
    )
    if moved.rowcount:
        session.commit()
        return True
    if session.exec(select(NotebookRead.id).where(*mine)).first() is not None:
        session.rollback()
        return False  # read already in this session
    session.add(NotebookRead(notebook_id=notebook_id, datasource_id=datasource_id, last_session=session_key,
                             mode=mode, first_read_at=now, read_at=now))
    try:
        session.commit()
    except IntegrityError:  # the same read, at the same moment, from another call
        session.rollback()
        return False
    return True


def read_in_session(session: Session, notebook_id: int, session_key: str) -> list[int]:
    """The datasources the session read, in the order it first read them."""
    return list(session.exec(select(NotebookRead.datasource_id)
                             .where(NotebookRead.notebook_id == notebook_id, NotebookRead.last_session == session_key)
                             .order_by(NotebookRead.read_at, NotebookRead.id)).all())


def read_by(session: Session, notebook_ids: Iterable[int]) -> dict[int, list[int]]:
    """notebook id → the datasources it has ever read, in its app or its editor."""
    ids = list(notebook_ids)
    out: dict[int, dict[int, None]] = {}
    if not ids:
        return {}
    for nb, ds in session.exec(select(NotebookRead.notebook_id, NotebookRead.datasource_id)
                               .where(NotebookRead.notebook_id.in_(ids))  # type: ignore[attr-defined]
                               .order_by(NotebookRead.id)).all():
        out.setdefault(nb, {})[ds] = None
    return {nb: list(read) for nb, read in out.items()}


def readers_of(session: Session, datasource_ids: Iterable[int]) -> list[int]:
    """The notebooks that have read one of these datasources."""
    ids = list(datasource_ids)
    if not ids:
        return []
    return sorted(set(session.exec(select(NotebookRead.notebook_id)
                                   .where(NotebookRead.datasource_id.in_(ids))).all()))  # type: ignore[attr-defined]
