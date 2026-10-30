"""What a turn cost.

`pydantic-ai` reports the cost by itself in `RunUsage.cost`, but looks it up under the
PROVIDER: on an OpenAI-compatible endpoint the lookup is «this model, at OpenAI». It works
for the models that exist there too (`gpt-oss-120b`), it fails for all the others — and the
cost comes `None` even when that model's price is perfectly known. Seen live on 2026-09-19:
every turn with `glm-5.2` saved without a cost, while the same model looked up by NAME gets
a price. So a fallback looks up by model name, without binding the provider.

The prices stay those of the library's price list, which updates with the library; we write
none. (Keep it current: `genai-prices` 0.1.7 priced `claude-opus-5-5` as the older
`claude-opus-5` and did not know `claude-haiku-5-5`; tests/test_ai_providers.py checks today's.)

**It stays an estimate**, and the user must be told: it is the model's public price list,
not the rate of the supplier you are really using. When nothing is found, the cost is
`None` — «I don't know», which is not «free».

**The caps do not take «I don't know» for free**: `cost_for_caps` counts such a model at
`UNKNOWN_PRICE`, so a model with no known price still spends what a cap allows and no more.
Administration → AI models says which models are counted that way.
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
_MILLION = Decimal(1_000_000)


# What a model with no known price counts for, against the caps only, per million tokens in and
# out: Opus 5.5's, a current top-end model. A guess, and a cap counted on it is a cap all the same; on a cheaper model it
# stops the turn sooner than the real bill would, which can be read as a reason to raise it.
UNKNOWN_PRICE = (Decimal(4), Decimal(20))


def _by_name(usage: Any, model_ref: str) -> Optional[Decimal]:
    from genai_prices import calc_price

    try:
        return calc_price(usage, model_ref=model_ref).total_price
    except Exception:  # noqa: BLE001 — a model unknown to the price list: not an error
        return None


def _by_any_name(usage: Any, model: str) -> Optional[Decimal]:
    if not model:
        return None
    without_date = _DATE_TAIL.sub("", model)
    for ref in ([model] if without_date == model else [model, without_date]):
        price = _by_name(usage, ref)
        if price is not None:
            return price
    return None


def cost_of(usage: Any, model_id: str) -> Optional[Decimal]:
    """The turn's cost in dollars, or `None` when it cannot be worked out.

    `model_id` is the reference stored for a model (services/ai_models.py). Order: what
    pydantic-ai reports; then the price list looked up by model name; then the same name
    without the date tail."""
    from app.services.ai_models import model_name

    try:
        model = model_name(model_id)
        direct = getattr(usage, "cost", None)
        if direct is not None:
            return direct
        price = _by_any_name(usage, model)
        if price is None and model:
            logger.info("assistant: no known price for model %r, cost not reported", model)
        return price
    except Exception:  # noqa: BLE001
        # the cost is an extra: the answer to the user has been given already and is not lost
        # because a price list is broken
        logger.exception("assistant: computing the cost failed for %r", model_id)
        return None


def at_unknown_price(input_tokens: int, output_tokens: int) -> Decimal:
    return ((input_tokens or 0) * UNKNOWN_PRICE[0] + (output_tokens or 0) * UNKNOWN_PRICE[1]) / _MILLION


def cost_for_caps(usage: Any, model_id: str) -> Decimal:
    """What a turn counts for against the spending caps: never nothing."""
    cost = cost_of(usage, model_id)
    if cost is not None:
        return cost
    return at_unknown_price(getattr(usage, "input_tokens", 0), getattr(usage, "output_tokens", 0))


def is_priced(model_id: str) -> bool:
    """Whether a model has a known price: the panel says when it does not."""
    from genai_prices import Usage

    from app.services.ai_models import model_name

    return _by_any_name(Usage(input_tokens=0, output_tokens=0), model_name(model_id)) is not None
