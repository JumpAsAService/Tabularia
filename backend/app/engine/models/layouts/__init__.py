"""The report of a statistical model: a layout per model, as a definition of the report
engine (docs/specs/reports.md), drawn from the rows the model's runner wrote
(`results.parquet`; docs/specs/statistical-models.md, «The report»).

    definition(model, params, locale, sample, step)   the definition: one dataset, `result`
    with_labels(model, params, rows, locale)          the rows, each with the name of its number
    title(model, params, locale)                      the report's name when the request gives none

`common.py` holds the blocks every model has; each model has a file of its own
(`linear.py`, `logistic.py`, `kaplan_meier.py`, `cox.py`, `arima.py`, and `penalized.py`
for the blocks both regressions draw with a penalty, `boosting.py` for those XGBoost and CatBoost share), which registers itself in `LAYOUTS`,
so the models can be built in parallel without touching each other's files. A new model is
its file here, its words in `report_texts/<model>.json` and its import below.
"""
from __future__ import annotations

from typing import Any, Callable

from app.engine.models.layouts.common import MAX_REPORT_ROWS, columns, with_labels  # noqa: F401 — the route's

# model → (layout, name): layout(model, params, locale, sample, step) → definition;
# name(params, locale) → the model as the page's header says it
LAYOUTS: dict[str, tuple[Callable[..., dict], Callable[[dict[str, Any], str], str], Callable[[dict[str, Any], str], str]]] = {}


def register(model: str, layout: Callable[..., dict], name: Callable[[dict[str, Any], str], str],
             about: Callable[[dict[str, Any], str], str]) -> None:
    LAYOUTS[model] = (layout, name, about)


def _code(locale: str | None) -> str:
    from app.engine.models.layouts.common import code

    return code(locale)


def title(model: str, params: dict[str, Any] | None, locale: str | None) -> str:
    """«Linear regression — quantity ~ price + channel»."""
    _, name, about = LAYOUTS[model]
    p, lang = dict(params or {}), _code(locale)
    said = about(p, lang)
    return name(p, lang) + (" — " + said if said else "")


def definition(model: str, params: dict[str, Any] | None, locale: str | None = "en",
               sample: list[dict[str, Any]] | None = None, step: str | None = None) -> dict:
    """The report of the model `model` with the node's `params`, in a language: a definition
    of the report engine over one dataset, `result`, the model's rows with their labels.
    `sample`: the development samples of the chain the run read (`stats.layouts.samples_of`):
    the page says its rows are a sample's. `step`: the name the person gave the node."""
    step = " ".join(str(step).split())[:120] if step else None
    layout, _, _ = LAYOUTS[model]
    return layout(model, dict(params or {}), _code(locale), sample, step)


# the models' own files, each registering its layout
from app.engine.models.layouts import arima, catboost, cox, kaplan_meier, linear, logistic, xgboost  # noqa: E402, F401
