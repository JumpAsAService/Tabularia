"""The report of the node «BG/BB» (docs/specs/customer-lifetime-models.md, `bg_bb`): the purchases of
customers who can buy in discrete opportunities (a year's donation, a month's renewal), Gamma–Gamma for
the spend, the discounted CLV. The blocks are `customer_lifetime.py`'s; there is no comparison of
purchase models (BG/BB is the only one)."""
from __future__ import annotations

from typing import Any

from app.engine.models.layouts import common as c
from app.engine.models.layouts import customer_lifetime, register


def name(params: dict[str, Any], locale: str) -> str:
    return c.words("bg_bb", "value", locale)["name"]


def about(params: dict[str, Any], locale: str) -> str:
    """«donor (x, t_x, n) · spend: avg_gift»."""
    columns = ", ".join(str(params[k]) for k in ("frequency", "recency", "opportunities") if params.get(k))
    said = f"{params.get('customer') or ''} ({columns})" if columns else str(params.get("customer") or "")
    return said + (f" · {c.words('bg_bb', 'value', locale)['spend_of']} {params['spend']}" if params.get("spend") else "")


def layout(model: str, params: dict[str, Any], locale: str, sample: list[dict] | None, step: str | None) -> dict:
    return customer_lifetime.build(model, params, locale, sample, step, name=name(params, locale), about=about(params, locale))


register("bg_bb", layout, name, about)
