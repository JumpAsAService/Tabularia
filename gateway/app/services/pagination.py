"""Paginazione delle liste: conta il totale che combacia col filtro (sul dataset
INTERO) e restituisce solo la finestra richiesta. Così le liste restano limitate
anche quando i dati crescono, ma la ricerca resta globale."""
from __future__ import annotations

from sqlalchemy import func
from sqlmodel import Session, select


def paginate(session: Session, base_stmt, order_by, limit: int, offset: int):
    """`base_stmt` è un select con i soli filtri (WHERE), SENZA order/limit.
    Torna (items della finestra ordinata, totale che combacia col filtro)."""
    total = session.exec(select(func.count()).select_from(base_stmt.subquery())).one()
    items = session.exec(base_stmt.order_by(order_by).limit(limit).offset(offset)).all()
    return items, total
