"""Paginating the lists: counts the total that matches the filter (over the WHOLE
dataset) and returns only the requested window. So the lists stay bounded even when
the data grows, but the search stays global."""
from __future__ import annotations

from sqlalchemy import func
from sqlmodel import Session, select


def paginate(session: Session, base_stmt, order_by, limit: int, offset: int):
    """`base_stmt` is a select with only the filters (WHERE), WITHOUT order/limit;
    `order_by` a column, or a tuple of them (the last one unique, or a page could repeat
    a row of the one before). Returns (items of the ordered window, total that matches
    the filter)."""
    total = session.exec(select(func.count()).select_from(base_stmt.subquery())).one()
    keys = order_by if isinstance(order_by, (tuple, list)) else (order_by,)
    items = session.exec(base_stmt.order_by(*keys).limit(limit).offset(offset)).all()
    return items, total
