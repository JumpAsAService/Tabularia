"""What a turn cost, without price lists written by us.

`pydantic-ai` reports the cost by itself in `RunUsage.cost`, but looks it up under the
PROVIDER: ours is an OpenAI-compatible endpoint, so the lookup is «this model, at
OpenAI». It works for the models that exist there too (`gpt-oss-120b`), it fails for
all the others — and the cost comes `None` even when that model's price is perfectly
known. Seen live on 2026-09-19: every turn with `glm-5.2` saved without a cost, while
the same model looked up by NAME gets a price.

Here only one fallback is added: looking up by model name, without binding the
provider. The prices stay those of the library's price list, which updates with the
library; we write none.

**It stays an estimate**, and the user must be told: it is the model's public price
list, not the rate of the supplier you are really using. When nothing is found, the
cost is `None` — «I don't know», which is not «free».
"""
from __future__ import annotations

import logging
import re
from decimal import Decimal
from typing import Any, Optional

logger = logging.getLogger(__name__)

# the version/date tail suppliers stick to the name
# (`mistral-small-3.2-24b-instruct-2506` → `…-instruct`)
_DATE_TAIL = re.compile(r"-\d{4,}$")


def _by_name(usage: Any, model_ref: str) -> Optional[Decimal]:
    from genai_prices import calc_price

    try:
        return calc_price(usage, model_ref=model_ref).total_price
    except Exception:  # noqa: BLE001 — a model unknown to the price list: not an error
        return None


def cost_of(usage: Any, model_id: str) -> Optional[Decimal]:
    """The turn's cost in dollars, or `None` when it cannot be worked out.

    Order: what pydantic-ai reports; then the price list looked up by model name; then
    the same name without the date tail."""
    try:
        direct = getattr(usage, "cost", None)
        if direct is not None:
            return direct
        if not model_id:
            return None
        without_date = _DATE_TAIL.sub("", model_id)
        for ref in ([model_id] if without_date == model_id else [model_id, without_date]):
            price = _by_name(usage, ref)
            if price is not None:
                return price
        logger.info("assistant: no known price for model %r, cost not reported", model_id)
        return None
    except Exception:  # noqa: BLE001
        # the cost is an extra: the answer to the user has been given already and is not lost
        # because a price list is broken
        logger.exception("assistant: computing the cost failed for %r", model_id)
        return None
