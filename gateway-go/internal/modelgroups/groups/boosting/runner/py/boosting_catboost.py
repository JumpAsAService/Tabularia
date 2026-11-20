"""CatBoost (lot X3 of docs/specs/statistical-models.md): fit_catboost(task) with native
categories, early stopping on the validation part, SHAP from the library, the model file
catboost.cbm; predict_catboost(task, card) from that file. Not built yet: both refuse with «not
built yet».
"""
from __future__ import annotations

from typing import Any

from tab_protocol import not_built


def fit_catboost(task: dict[str, Any]) -> dict[str, Any]:
    raise not_built("the CatBoost node")


def predict_catboost(task: dict[str, Any], card: dict[str, Any]) -> dict[str, Any]:
    raise not_built("Predict for a CatBoost model")
