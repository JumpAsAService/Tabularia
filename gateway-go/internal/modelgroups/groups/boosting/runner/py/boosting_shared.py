"""What the two boosting models share (lot X1 of docs/specs/statistical-models.md, «The boosting
group's lots», whose interface is fixed there): prepare(task) -> Prepared (the input read, rows
left out, kinds, categories with their levels, the positional frame, y, w, the validation mask,
the classes, the card's inputs, the row numbers); finish(task, prepared, fitted) -> {rows,
output, card, model_file}, every row of «The output» from a library wrapper's Fitted. Not built
yet: both refuse with «not built yet».
"""
from __future__ import annotations

from typing import Any

from tab_protocol import not_built


def prepare(task: dict[str, Any]) -> Any:
    raise not_built("the boosting group's preparation of the rows")


def finish(task: dict[str, Any], prepared: Any, fitted: Any) -> dict[str, Any]:
    raise not_built("the boosting group's results")
