"""What a hypothesis test may ask of the data, on Polars: the same aggregates as
`aggregates.py`, with the same answers, written as lazy expressions and collected by the
streaming engine (as `pivot` does) — the result is a row per group, whatever the input's
size."""
from __future__ import annotations

import polars as pl

from app.engine.stats.aggregates import (
    CENTRE_ROWS,
    HistogramBin,
    Level,
    Moments,
    Quartiles,
    moments_of,
    quartiles_of,
    usable_centre,
)
from app.engine.stats.catalog import Column

# the names of what a query returns (as `pivot`'s: names no column of a flow has)
_LEVEL, _ROWS, _N, _MEAN, _VAR, _MIN, _MAX, _Q1, _MEDIAN, _Q3, _BIN = (
    f"__stat_{name}__" for name in ("lv", "rows", "n", "mean", "var", "min", "max", "q1", "median", "q3", "bin")
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

    def quartiles(self, value: str, group: str | None = None) -> list[Quartiles]:
        """A12: the first quartile, the median and the third quartile of `value` per group,
        linearly interpolated (R's type 7), of the values that are numbers."""
        x = pl.col(value).cast(pl.Float64).fill_nan(None)
        finite = pl.when(x.is_finite()).then(x)
        quantiles = [finite.quantile(q, "linear").alias(name) for q, name in ((0.25, _Q1), (0.5, _MEDIAN), (0.75, _Q3))]
        if group is None:
            df = self.lf.select(pl.lit(None).alias(_LEVEL), *quantiles).collect(engine="streaming")
        else:
            df = self.lf.group_by(pl.col(group).alias(_LEVEL)).agg(quantiles).collect(engine="streaming")
        return [quartiles_of(*row) for row in df.iter_rows()]

    def histogram(self, value: str, group: str | None, lo: float, hi: float, bins: int) -> list[HistogramBin]:
        """A13: the rows per group and interval, `bins` equal intervals between `lo` and
        `hi`, the last with its upper edge; the same arithmetic as the SQL engines'
        (`profile.sql_histogram_bin`), so that a value on an edge falls on the same side."""
        lo, hi, bins = float(lo), float(hi), int(bins)
        width = (hi - lo) / bins if hi > lo else 1.0
        x = pl.col(value).cast(pl.Float64).fill_nan(None)
        b = ((x - lo) / width).floor().cast(pl.Int64).clip(0, bins - 1).alias(_BIN)
        keys = [b] if group is None else [pl.col(group).alias(_LEVEL), b]
        df = (
            self.lf.filter(x.is_between(lo, hi))
            .group_by(keys)
            .agg(pl.len().alias(_ROWS))
            .collect(engine="streaming")
        )
        return [HistogramBin(row[0] if group is not None else None, int(row[-2]), int(row[-1])) for row in df.iter_rows()]
