"""The report of an XGBoost model (docs/specs/statistical-models.md, «Group `boosting` — Python»):
the blocks are `boosting.py`'s; here the name and the hyperparameters of this library."""
from __future__ import annotations

from functools import partial
from typing import Any

from app.engine.models.layouts import boosting, register
from app.engine.models.layouts import common as c

# the settings the page says, in the order of the node's form
HYPERPARAMETERS = ("learning_rate", "max_depth", "min_child_weight", "subsample", "colsample_bytree", "reg_lambda", "reg_alpha",
                   "gamma", "max_trees", "early_stopping", "early_stopping_rounds", "stop_metric", "class_weight", "validation",
                   "validation_share")

name = partial(boosting.name, "xgboost")
about = lambda params, locale: boosting.about(params, locale)  # noqa: E731


def layout(model: str, params: dict[str, Any], locale: str, sample: list[dict] | None, step: str | None) -> dict:
    return boosting.report(model, params, locale, sample, step, HYPERPARAMETERS)


c.READINGS["xgboost"] = boosting.readings  # the explanation: boosting.py chooses its lines
register("xgboost", layout, name, about)
