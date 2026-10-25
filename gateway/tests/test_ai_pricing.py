"""A turn's cost comes from the library's price list, never from values of ours.

pydantic-ai looks up the price under the PROVIDER: with our OpenAI-compatible endpoint
the models that do not exist at OpenAI too stay without a cost, even when their price
is known. Live (2026-09-19) every turn with `glm-5.2` was saved without a cost.
"""
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.services.ai_pricing import cost_of


class _Usage(SimpleNamespace):
    """A usage with the shape the price list expects."""

    def __init__(self, cost=None):
        super().__init__(input_tokens=1_000_000, output_tokens=0, requests=1, details={}, cost=cost)


def test_what_pydantic_ai_reports_wins():
    # when the library has priced already, nothing else is looked for
    assert cost_of(_Usage(cost=Decimal("0.0001")), "qualunque-modello") == Decimal("0.0001")


def test_a_model_unknown_to_the_provider_is_priced_by_name():
    # the real case: `glm-5.2` does not exist at OpenAI, but the price list knows it
    cost = cost_of(_Usage(), "glm-5.2")
    assert cost is not None and cost > 0


def test_a_version_suffix_does_not_prevent_pricing():
    # the suppliers stick the date on: `…-instruct-2506`
    full = cost_of(_Usage(), "mistral-small-3.2-24b-instruct-2506")
    short = cost_of(_Usage(), "mistral-small-3.2-24b-instruct")
    assert full is not None and full == short


def test_an_unknown_model_gives_none_not_zero():
    # «I don't know» is not «free»: the UI shows a dash, not $0
    assert cost_of(_Usage(), "modello-che-non-esiste-da-nessuna-parte") is None


@pytest.mark.parametrize("empty", ["", None])
def test_without_a_model_there_is_no_price(empty):
    assert cost_of(_Usage(), empty) is None


def test_pricing_never_raises(monkeypatch):
    """A broken price list must not sweep away an answer already given to the user."""
    import app.services.ai_pricing as m

    monkeypatch.setattr(m, "_by_name", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert m.cost_of(_Usage(), "glm-5.2") is None
