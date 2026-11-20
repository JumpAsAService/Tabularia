"""XGBoost (lot X2 of docs/specs/statistical-models.md): fit_xgboost(task) with native categories,
early stopping on the validation part, TreeSHAP from the library, the model file xgboost.json;
predict_xgboost(task, card) from that file. Not built yet: both refuse with «not built yet».
"""
from __future__ import annotations

from typing import Any

from tab_protocol import not_built


def fit_xgboost(task: dict[str, Any]) -> dict[str, Any]:
    raise not_built("the XGBoost node")


def predict_xgboost(task: dict[str, Any], card: dict[str, Any]) -> dict[str, Any]:
    raise not_built("Predict for an XGBoost model")
