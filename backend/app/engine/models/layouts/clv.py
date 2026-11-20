"""The report of the node «Customer lifetime value» (docs/specs/customer-lifetime-models.md, `clv`):
Pareto/NBD, BG/NBD or MBG/NBD for the purchases (continuous time), Gamma–Gamma for the spend, the
discounted CLV. The blocks are `customer_lifetime.py`'s; this file says what is its own: the name,
what the page is about, and (in `customer_lifetime.build`) the comparison of the three purchase models."""
from __future__ import annotations

from typing import Any

from app.engine.models.layouts import common as c
from app.engine.models.layouts import customer_lifetime, register


def name(params: dict[str, Any], locale: str) -> str:
    return c.words("clv", "value", locale)["name"]


def about(params: dict[str, Any], locale: str) -> str:
    """«customer (frequency, recency, age) · spend: avg_spend»."""
    columns = ", ".join(str(params[k]) for k in ("frequency", "recency", "age") if params.get(k))
    said = f"{params.get('customer') or ''} ({columns})" if columns else str(params.get("customer") or "")
    return said + (f" · {c.words('clv', 'value', locale)['spend_of']} {params['spend']}" if params.get("spend") else "")


def layout(model: str, params: dict[str, Any], locale: str, sample: list[dict] | None, step: str | None) -> dict:
    return customer_lifetime.build(model, params, locale, sample, step, name=name(params, locale), about=about(params, locale))


register("clv", layout, name, about)
