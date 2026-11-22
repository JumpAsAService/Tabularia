"""Forecast (Timestral) (docs/specs/foundation-forecast.md, «The node `timestral`»): fit_timestral(task).

Lot F0 gives the node its contract (the catalogue, the recipe, the route in run.py) and the vendored package
(runner/py/timestral/); the lots after it write the series (F2, common/py/tab_series.py), the runner (F3: ff_series.py,
ff_timestral.py, ff_measures.py, ff_reading.py) and the report (F4). Until then the node refuses, with a sentence, as
every node not built yet. It returns no card: nothing is registered or predicted.
"""
from tab_protocol import not_built


def fit_timestral(task):
    raise not_built("Forecast (Timestral)")
