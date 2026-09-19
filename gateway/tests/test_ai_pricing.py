"""Il costo di un turno viene dal listino della libreria, mai da valori nostri.

pydantic-ai cerca il prezzo sotto il PROVIDER: col nostro endpoint compatibile
OpenAI i modelli che non esistono anche presso OpenAI restano senza costo,
anche quando il loro prezzo è noto. Dal vivo (2026-09-19) tutti i turni con
`glm-5.2` erano salvati senza costo.
"""
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.services.ai_pricing import cost_of


class _Usage(SimpleNamespace):
    """Un uso con la forma che il listino si aspetta."""

    def __init__(self, cost=None):
        super().__init__(input_tokens=1_000_000, output_tokens=0, requests=1, details={}, cost=cost)


def test_what_pydantic_ai_reports_wins():
    # se la libreria ha gia' prezzato, non si cerca altro
    assert cost_of(_Usage(cost=Decimal("0.0001")), "qualunque-modello") == Decimal("0.0001")


def test_a_model_unknown_to_the_provider_is_priced_by_name():
    # il caso reale: `glm-5.2` non esiste presso OpenAI, ma il listino lo conosce
    costo = cost_of(_Usage(), "glm-5.2")
    assert costo is not None and costo > 0


def test_a_version_suffix_does_not_prevent_pricing():
    # i fornitori appiccicano la data: `…-instruct-2506`
    pieno = cost_of(_Usage(), "mistral-small-3.2-24b-instruct-2506")
    corto = cost_of(_Usage(), "mistral-small-3.2-24b-instruct")
    assert pieno is not None and pieno == corto


def test_an_unknown_model_gives_none_not_zero():
    # «non lo so» non e' «gratis»: la UI mostra un trattino, non $0
    assert cost_of(_Usage(), "modello-che-non-esiste-da-nessuna-parte") is None


@pytest.mark.parametrize("vuoto", ["", None])
def test_without_a_model_there_is_no_price(vuoto):
    assert cost_of(_Usage(), vuoto) is None


def test_pricing_never_raises(monkeypatch):
    """Un listino rotto non deve travolgere una risposta gia' data all'utente."""
    import app.services.ai_pricing as m

    monkeypatch.setattr(m, "_per_nome", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert m.cost_of(_Usage(), "glm-5.2") is None
