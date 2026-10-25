"""Paginating the lists: counts the total that matches the filter (over the WHOLE
dataset) and returns only the requested window. So the lists stay bounded even when
the data grows, but the search stays global."""
from __future__ import annotations

from sqlalchemy import func
from sqlmodel import Session, select


def paginate(session: Session, base_stmt, order_by, limit: int, offset: int):
    """`base_stmt` is a select with only the filters (WHERE), WITHOUT order/limit.
    Returns (items of the ordered window, total that matches the filter)."""
    total = session.exec(select(func.count()).select_from(base_stmt.subquery())).one()
    items = session.exec(base_stmt.order_by(order_by).limit(limit).offset(offset)).all()
    return items, total
