"""What a hypothesis test may ask of the data, on Polars: the same aggregates as
`aggregates.py`, with the same answers, written as lazy expressions and collected by the
streaming engine (as `pivot` does) — the result is a row per group, whatever the input's
size."""
from __future__ import annotations

import polars as pl

from app.engine.stats.aggregates import CENTRE_ROWS, Level, Moments, moments_of, usable_centre
from app.engine.stats.catalog import Column

# the names of what a query returns (as `pivot`'s: names no column of a flow has)
_LEVEL, _ROWS, _N, _MEAN, _VAR, _MIN, _MAX = (
    f"__stat_{name}__" for name in ("lv", "rows", "n", "mean", "var", "min", "max")
)


class PolarsStatSource:
    """A step's input, a LazyFrame, as the tests read it."""

    def __init__(self, lf: pl.LazyFrame):
        self.lf = lf
        self._schema = lf.collect_schema()

    def columns(self) -> dict[str, Column]:
        return {name: Column(str(dtype), dtype.is_numeric()) for name, dtype in self._schema.items()}

    def levels(self, group: str, limit: int) -> list[Level]:
        """A1: the values of `group` with their rows, the NULL one too; `limit` at most."""
        df = (
            self.lf.group_by(pl.col(group).alias(_LEVEL))
            .agg(pl.len().alias(_ROWS))
            .head(limit)
            .collect(engine="streaming")
        )
        return [Level(value, int(rows)) for value, rows in df.iter_rows()]

    def moments(self, value: str, group: str | None = None) -> list[Moments]:
        """A2: rows, values, mean, sample variance, smallest and largest of `value` per
        group. A NULL or a NaN is no value; mean and variance are taken of the finite values
        less a centre, the mean of the first rows' (see aggregates.py)."""
        x = pl.col(value).cast(pl.Float64).fill_nan(None)
        finite = pl.when(x.is_finite()).then(x)  # an infinite value is counted, and kept out of the mean
        centre = usable_centre(self.lf.head(CENTRE_ROWS).select(finite.mean()).collect(engine="streaming").item())
        centred = finite - centre if centre else finite
        aggregates = [
            pl.len().alias(_ROWS), x.count().alias(_N), centred.mean().alias(_MEAN), centred.var().alias(_VAR),
            x.min().alias(_MIN), x.max().alias(_MAX),
        ]
        if group is None:
            df = self.lf.select(pl.lit(None).alias(_LEVEL), *aggregates).collect(engine="streaming")
        else:
            df = self.lf.group_by(pl.col(group).alias(_LEVEL)).agg(aggregates).collect(engine="streaming")
        return [moments_of(centre, *row) for row in df.iter_rows()]
