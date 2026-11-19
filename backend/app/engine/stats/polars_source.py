"""What a hypothesis test may ask of the data, on Polars: the same aggregates as
`aggregates.py`, with the same answers, written as lazy expressions and collected by the
streaming engine (as `pivot` does) — the result is a row per group, whatever the input's
size."""
from __future__ import annotations

import polars as pl

from typing import Any

from app.engine.stats.aggregates import (
    CENTRE_ROWS,
    CentralMoments,
    KsDistance,
    RankCorr,
    RankSums,
    SignedRanks,
    central_moments_of,
    ks_of,
    rank_corr_of,
    rank_sums_of,
    signed_ranks_of,
    Cell,
    Cell3,
    Successes,
    Deviations,
    Difference,
    HistogramBin,
    Level,
    Moments,
    PairMoments,
    Paired,
    Quartiles,
    comparable,
    deviations_of,
    moments_of,
    pair_moments_of,
    quartiles_of,
    typed_labels,
    usable_centre,
)
from app.engine.stats.catalog import Column

# the names of what a query returns (as `pivot`'s: names no column of a flow has)
_LEVEL, _ROWS, _N, _MEAN, _VAR, _MIN, _MAX, _Q1, _MEDIAN, _Q3, _BIN, _CTR = (
    f"__stat_{name}__" for name in ("lv", "rows", "n", "mean", "var", "min", "max", "q1", "median", "q3", "bin", "ctr")
)


def _number(column: str) -> pl.Expr:
    """The column as a double, NULL where it is NULL or NaN."""
    return pl.col(column).cast(pl.Float64).fill_nan(None)


def _finite(e: pl.Expr) -> pl.Expr:
    """The value, NULL where it is infinite too: kept out of means and variances."""
    return pl.when(e.is_finite()).then(e)


def _measure(m: Any) -> pl.Expr:
    """A measure as a double, NULL where it has no value (as `SqlStatSource._measure`)."""
    if isinstance(m, Difference):
        return _number(m.x) - _number(m.y)
    if isinstance(m, Paired):
        return pl.when(_number(m.y).is_not_null()).then(_number(m.x))
    return _number(m)


def _kind(dtype: pl.DataType) -> str | None:
    """The kind of a grouping column, as `aggregates.typed_labels` reads it."""
    if dtype in (pl.String, pl.Categorical) or isinstance(dtype, (pl.Categorical, pl.Enum)):
        return "text"
    if dtype.is_integer():
        return "integer"
    if dtype.is_float():
        return "float"
    if dtype == pl.Date:
        return "date"
    if dtype == pl.Boolean:
        return "boolean"
    return None


class PolarsStatSource:
    """A step's input, a LazyFrame, as the tests read it."""

    def __init__(self, lf: pl.LazyFrame):
        self.lf = lf
        self._schema = lf.collect_schema()

    def columns(self) -> dict[str, Column]:
        return {name: Column(str(dtype), dtype.is_numeric()) for name, dtype in self._schema.items()}

    def kind_of(self, column: str) -> str | None:
        """The kind of a column whose values a test compares with one written (`typed_value`)."""
        return _kind(self._schema[column])

    def successes(self, value: str, success: Any, group: str | None = None, levels: list | None = None) -> list[Successes]:
        """A8: per group (the NULL one too), or over every row, the rows, the values (not NULL,
        not NaN) and those equal to `success` (see SqlStatSource)."""
        col = pl.col(value)
        ok = col.is_not_null()
        if self._schema[value].is_float():
            ok = ok & ~col.is_nan()
        hit = ok & (col == pl.lit(success))
        aggregates = [pl.len().alias(_ROWS), ok.cast(pl.Int64).sum().alias(_N), hit.fill_null(False).cast(pl.Int64).sum().alias("__stat_k__")]
        if group is None:
            df = self.lf.select(pl.lit(None).alias(_LEVEL), *aggregates).collect(engine="streaming")
        else:
            lf = self.lf
            if comparable(levels):
                lf = lf.filter(pl.any_horizontal([pl.col(group).is_null()] + [pl.col(group) == pl.lit(v) for v in levels]))
            df = lf.group_by(pl.col(group).alias(_LEVEL)).agg(aggregates).collect(engine="streaming")
        return [Successes(lv, int(rows), int(n or 0), int(k or 0)) for lv, rows, n, k in df.iter_rows()]

    def crosstab(self, x: str, y: str, limit: int) -> list[Cell]:
        """A7: the rows of each pair of values of `x` and `y`, NULL ones too; `limit` at most."""
        df = (
            self.lf.group_by(pl.col(x).alias("__stat_lx__"), pl.col(y).alias("__stat_ly__"))
            .agg(pl.len().alias(_ROWS))
            .head(limit)
            .collect(engine="streaming")
        )
        return [Cell(a, b, int(rows)) for a, b, rows in df.iter_rows()]

    def crosstab3(self, x: str, y: str, z: str, limit: int) -> list[Cell3]:
        """A7 within strata: the rows of each triple of values; `limit` at most."""
        df = (
            self.lf.group_by(pl.col(x).alias("__stat_lx__"), pl.col(y).alias("__stat_ly__"), pl.col(z).alias("__stat_lz__"))
            .agg(pl.len().alias(_ROWS))
            .head(limit)
            .collect(engine="streaming")
        )
        return [Cell3(a, b, c, int(rows)) for a, b, c, rows in df.iter_rows()]

    def levels(self, group: str, limit: int) -> list[Level]:
        """A1: the values of `group` with their rows, the NULL one too; `limit` at most."""
        df = (
            self.lf.group_by(pl.col(group).alias(_LEVEL))
            .agg(pl.len().alias(_ROWS))
            .head(limit)
            .collect(engine="streaming")
        )
        return [Level(value, int(rows)) for value, rows in df.iter_rows()]

    def levels_of(self, group: str, labels: list[str]) -> list[Level] | None:
        """A1 for the groups named by these labels only, and the rows with no group; None
        when the column's type cannot be compared with a value written from a label."""
        typed = typed_labels(labels, _kind(self._schema[group]))
        if typed is None:
            return None
        keep = pl.any_horizontal([pl.col(group).is_null()] + [pl.col(group) == pl.lit(v) for v in typed])
        df = self.lf.filter(keep).group_by(pl.col(group).alias(_LEVEL)).agg(pl.len().alias(_ROWS)).collect(engine="streaming")
        return [Level(value, int(rows)) for value, rows in df.iter_rows()]

    def _centre(self, value: str) -> float:
        """A number near the column's values: the mean of the first rows' (see aggregates.py)."""
        return usable_centre(self.lf.head(CENTRE_ROWS).select(_finite(_number(value)).mean()).collect(engine="streaming").item())

    def moments(self, value: str, group: str | None = None, levels: list | None = None) -> list[Moments]:
        """A2: rows, values, mean, sample variance, smallest and largest of `value` per
        group. A NULL or a NaN is no value; mean and variance are taken of the finite values
        less a centre, the mean of the first rows' (see aggregates.py). `levels`: the groups
        to answer for, and the rows with no group (every group when they cannot be compared)."""
        x = _number(value)
        finite = _finite(x)  # an infinite value is counted, and kept out of the mean
        centre = self._centre(value)
        centred = finite - centre if centre else finite
        aggregates = [
            pl.len().alias(_ROWS), x.count().alias(_N), centred.mean().alias(_MEAN), centred.var().alias(_VAR),
            x.min().alias(_MIN), x.max().alias(_MAX),
        ]
        if group is None:
            df = self.lf.select(pl.lit(None).alias(_LEVEL), *aggregates).collect(engine="streaming")
        else:
            lf = self.lf
            if comparable(levels):
                lf = lf.filter(pl.any_horizontal([pl.col(group).is_null()] + [pl.col(group) == pl.lit(v) for v in levels]))
            df = lf.group_by(pl.col(group).alias(_LEVEL)).agg(aggregates).collect(engine="streaming")
        return [moments_of(centre, *row) for row in df.iter_rows()]

    def pair_moments(self, x: str, y: str) -> PairMoments:
        """A3: two columns of the same rows, over the rows where both are numbers."""
        cx, cy = self._centre(x), self._centre(y)
        px, py = _measure(Paired(x, y)), _measure(Paired(y, x))
        both = _finite(_number(x)).is_not_null() & _finite(_number(y)).is_not_null()
        ex = pl.when(both).then(_number(x) - cx if cx else _number(x))
        ey = pl.when(both).then(_number(y) - cy if cy else _number(y))
        ed, d = ex - ey, px - py
        exprs = [pl.len(), px.count(), ex.mean(), ey.mean(), ed.mean(), ex.var(), ey.var(), ed.var(), pl.cov(ex, ey, ddof=1),
                 px.min(), px.max(), py.min(), py.max(), d.min(), d.max()]
        row = self.lf.select([e.alias(f"__stat_a3_{i}__") for i, e in enumerate(exprs)]).collect(engine="streaming").row(0)
        return pair_moments_of(cx, cy, *row)

    def abs_dev_moments(self, value: str, group: str, center: str) -> list[Deviations]:
        """A11: per group, the mean and the sample variance of |value − the group's centre|
        (its median or its mean), as `SqlStatSource.abs_dev_moments`."""
        x = _number(value)
        c = self._centre(value)
        centred = _finite(x) - c if c else _finite(x)
        ctr = (centred.median() if center == "median" else centred.mean()).alias(_CTR)
        centres = self.lf.group_by(pl.col(group).alias(_LEVEL)).agg(ctr).filter(pl.col(_LEVEL).is_not_null())
        z = (centred - pl.col(_CTR)).abs()
        df = (
            self.lf.with_columns(pl.col(group).alias(_LEVEL))
            .join(centres, on=_LEVEL, how="inner")
            .group_by(_LEVEL)
            .agg(pl.len().alias(_ROWS), x.count().alias(_N), pl.col(_CTR).max(), z.mean().alias(_MEAN), z.var().alias(_VAR),
                 x.min().alias(_MIN), x.max().alias(_MAX))
            .collect(engine="streaming")
        )
        return [deviations_of(c, *row) for row in df.iter_rows()]

    def quartiles_many(self, measures: list) -> list[Quartiles]:
        """A12 of several measures over every row, in one pass (see SqlStatSource)."""
        exprs = [_finite(_measure(m)).quantile(q, "linear").alias(f"__stat_q{i}_{j}__")
                 for i, m in enumerate(measures) for j, q in enumerate((0.25, 0.5, 0.75))]
        row = self.lf.select(exprs).collect(engine="streaming").row(0)
        return [quartiles_of(i, *row[3 * i:3 * i + 3]) for i in range(len(measures))]

    def quartiles(self, value: Any, group: str | None = None, levels: list | None = None) -> list[Quartiles]:
        """A12: the first quartile, the median and the third quartile of `value` per group,
        linearly interpolated (R's type 7), of the values that are numbers. `levels`: the
        groups to answer for (the typed values A1 returned), so that the values of the
        others are never gathered; every group when they cannot be compared in the query
        (`aggregates.comparable`)."""
        finite = _finite(_measure(value))
        quantiles = [finite.quantile(q, "linear").alias(name) for q, name in ((0.25, _Q1), (0.5, _MEDIAN), (0.75, _Q3))]
        if group is None:
            df = self.lf.select(pl.lit(None).alias(_LEVEL), *quantiles).collect(engine="streaming")
        else:
            lf = self.lf
            if comparable(levels):
                lf = lf.filter(pl.any_horizontal([pl.col(group) == pl.lit(v) for v in levels]))
            df = lf.group_by(pl.col(group).alias(_LEVEL)).agg(quantiles).collect(engine="streaming")
        return [quartiles_of(*row) for row in df.iter_rows()]

    # ── ranks (A4, A5, A6, A9): as SqlStatSource's, the same arithmetic in the same order ──
    def rank_sums(self, value: str, group: str, levels: list | None = None) -> RankSums:
        """A4: the values of the groups ranked together (see SqlStatSource.rank_sums)."""
        key = (_number(value) + 0.0).alias("__stat_xv__")
        lf = self.lf.filter(pl.col(group).is_not_null() & _number(value).is_not_null())
        if comparable(levels):
            lf = lf.filter(pl.any_horizontal([pl.col(group) == pl.lit(v) for v in levels]))
        f = lf.group_by(pl.col(group).alias(_LEVEL), key).agg(pl.len().cast(pl.Float64).alias("__stat_c__"))
        v = (f.group_by("__stat_xv__").agg(pl.col("__stat_c__").sum().alias("__stat_t__")).sort("__stat_xv__")
             .with_columns((pl.col("__stat_t__").cum_sum() - (pl.col("__stat_t__") - 1.0) / 2.0).alias("__stat_rk__")))
        t = pl.col("__stat_t__")
        ties = v.select((t * t * t - t).sum()).collect(engine="streaming").item()
        df = (f.join(v, on="__stat_xv__").group_by(_LEVEL)
              .agg(pl.col("__stat_c__").sum().cast(pl.Int64).alias(_N), (pl.col("__stat_c__") * pl.col("__stat_rk__")).sum().alias("__stat_rs__"),
                   pl.col("__stat_xv__").min().alias(_MIN), pl.col("__stat_xv__").max().alias(_MAX))
              .collect(engine="streaming"))
        out = rank_sums_of([[*row, 0.0] for row in df.iter_rows()])
        return RankSums(out.groups, float(ties or 0.0))

    def signed_ranks(self, x: str, y: str | None, mu: float, pratt: bool) -> SignedRanks:
        """A5: the differences ranked by their absolute value (see SqlStatSource.signed_ranks)."""
        fx = _finite(_number(x))
        fy = _finite(_number(y)) if y is not None else None
        d = d0 = fx - fy if fy is not None else fx
        if mu:
            d = d - float(mu)
        both = fx.is_not_null() if fy is None else fx.is_not_null() & fy.is_not_null()
        ok = _number(x).is_not_null() if y is None else _number(x).is_not_null() & _number(y).is_not_null()
        dv = pl.col("__stat_dv__")
        diffs = self.lf.filter(both).select(d.alias("__stat_dv__"))
        if not pratt:
            diffs = diffs.filter(dv != 0)
        t = pl.col("__stat_t__")
        ranked = (diffs.group_by((dv.abs() + 0.0).alias("__stat_av__"))
                  .agg((dv > 0).sum().cast(pl.Float64).alias("__stat_cp__"), (dv < 0).sum().cast(pl.Float64).alias("__stat_cn__"),
                       pl.len().cast(pl.Float64).alias("__stat_t__"))
                  .sort("__stat_av__").with_columns((t.cum_sum() - (t - 1.0) / 2.0).alias("__stat_rk__")))
        rk = pl.col("__stat_rk__")
        ranks = ranked.select(t.sum().cast(pl.Int64).alias(_N), (pl.col("__stat_cp__") * rk).sum().alias("__stat_wp__"),
                              (pl.col("__stat_cn__") * rk).sum().alias("__stat_wm__"),
                              pl.when(pl.col("__stat_av__") > 0).then(t * t * t - t).otherwise(0.0).sum().alias("__stat_ties__")
                              ).collect(engine="streaming").row(0)

        def over_pairs(e: pl.Expr) -> pl.Expr:
            return pl.when(both).then(e)

        none = pl.lit(None, dtype=pl.Float64)
        exprs = [pl.len(), ok.sum(), (ok & ~both).sum(), (both & (d == 0)).sum(),
                 over_pairs(fx).min(), over_pairs(fx).max(), over_pairs(fy).min() if fy is not None else none,
                 over_pairs(fy).max() if fy is not None else none, over_pairs(d0).min(), over_pairs(d0).max()]
        counts = self.lf.select([e.alias(f"__stat_a5_{i}__") for i, e in enumerate(exprs)]).collect(engine="streaming").row(0)
        return signed_ranks_of(list(counts), list(ranks))

    def rank_corr(self, x: str, y: str) -> RankCorr:
        """A6: the pairs ranked column by column (see SqlStatSource.rank_corr)."""
        xv, yv, c = pl.col("__stat_xv__"), pl.col("__stat_yv__"), pl.col("__stat_c__")
        p = (self.lf.filter(_number(x).is_not_null() & _number(y).is_not_null())
             .group_by((_number(x) + 0.0).alias("__stat_xv__"), (_number(y) + 0.0).alias("__stat_yv__"))
             .agg(pl.len().cast(pl.Float64).alias("__stat_c__")))

        def ranks(v: str, rank: str, ties: str) -> pl.LazyFrame:
            t = pl.col("__stat_t__")
            return (p.group_by(v).agg(c.sum().alias("__stat_t__")).sort(v)
                    .with_columns((t.cum_sum() - (t - 1.0) / 2.0 - (t.sum() + 1.0) / 2.0).alias(rank), (t * t * t - t).alias(ties))
                    .drop("__stat_t__"))

        rx, ry = pl.col("__stat_rx__"), pl.col("__stat_ry__")
        tx = ranks("__stat_xv__", "__stat_rx__", "__stat_tx__")
        ty = ranks("__stat_yv__", "__stat_ry__", "__stat_ty__")
        sums = (p.join(tx, on="__stat_xv__").join(ty, on="__stat_yv__")
                .select(c.sum().cast(pl.Int64).alias(_N), (c * rx * ry).sum().alias("__stat_sxy__"), (c * rx * rx).sum().alias("__stat_sxx__"),
                        (c * ry * ry).sum().alias("__stat_syy__"),
                        xv.min().alias("__stat_x0__"), xv.max().alias("__stat_x1__"), yv.min().alias("__stat_y0__"),
                        yv.max().alias("__stat_y1__"))
                .collect(engine="streaming").row(0))
        tie_x = tx.select(pl.col("__stat_tx__").sum()).collect(engine="streaming").item()
        tie_y = ty.select(pl.col("__stat_ty__").sum()).collect(engine="streaming").item()
        n, sxy, sxx, syy, *extremes = sums
        return rank_corr_of(n, sxy, sxx, syy, tie_x, tie_y, *extremes)

    def count_rows(self) -> int:
        return int(self.lf.select(pl.len()).collect(engine="streaming").item())

    def ks(self, value: str, group: str, levels: list) -> KsDistance:
        """A9: the largest distances between two groups' empirical distribution functions, as
        whole numbers (see SqlStatSource.ks)."""
        a, b = levels
        g = pl.col(group)
        c1, c2 = pl.col("__stat_c1__"), pl.col("__stat_c2__")
        f = (self.lf.filter(_number(value).is_not_null() & ((g == pl.lit(a)) | (g == pl.lit(b))))
             .group_by((_number(value) + 0.0).alias("__stat_xv__"))
             .agg((g == pl.lit(a)).sum().cast(pl.Int64).alias("__stat_c1__"), (g == pl.lit(b)).sum().cast(pl.Int64).alias("__stat_c2__"))
             .sort("__stat_xv__")
             .with_columns(c1.cum_sum().alias("__stat_k1__"), c2.cum_sum().alias("__stat_k2__"), c1.sum().alias("__stat_m1__"),
                           c2.sum().alias("__stat_m2__")))
        k1, k2, m1, m2 = (pl.col(f"__stat_{n}__") for n in ("k1", "k2", "m1", "m2"))
        row = f.select(m1.max(), m2.max(), (k1 * m2 - k2 * m1).max().alias("__stat_dp__"), (k2 * m1 - k1 * m2).max().alias("__stat_dm__"),
                       ((c1 + c2) > 1).sum().alias("__stat_tied__"),
                       pl.col("__stat_xv__").min().alias("__stat_lo__"), pl.col("__stat_xv__").max().alias("__stat_hi__")
                       ).collect(engine="streaming").row(0)
        return ks_of(*row)

    def central_moments(self, value: str) -> CentralMoments:
        """A10: the central moments of the finite values, less the column's centre (see
        SqlStatSource.central_moments)."""
        x = _number(value)
        centre = self._centre(value)
        cv = _finite(x) - centre if centre else _finite(x)
        e = cv - cv.mean()
        row = self.lf.select(pl.len().alias("__stat_r__"), x.count().alias("__stat_n__"), cv.count().alias("__stat_f__"),
                             cv.mean().alias("__stat_d__"), (e * e).mean().alias("__stat_m2__"), (e * e * e).mean().alias("__stat_m3__"),
                             ((e * e) * (e * e)).mean().alias("__stat_m4__"), x.min().alias("__stat_lo__"), x.max().alias("__stat_hi__")
                             ).collect(engine="streaming").row(0)
        return central_moments_of(centre, *row)

    def histogram(self, value: Any, group: str | None, lo: float, hi: float, bins: int) -> list[HistogramBin]:
        """A13: the rows per group and interval, `bins` equal intervals between `lo` and
        `hi`, the last with its upper edge; the same arithmetic as the SQL engines'
        (`profile.sql_histogram_bin`), so that a value on an edge falls on the same side."""
        lo, hi, bins = float(lo), float(hi), int(bins)
        width = (hi - lo) / bins if hi > lo else 1.0
        x = _measure(value)
        b = ((x - lo) / width).floor().cast(pl.Int64).clip(0, bins - 1).alias(_BIN)
        keys = [b] if group is None else [pl.col(group).alias(_LEVEL), b]
        df = (
            self.lf.filter(x.is_between(lo, hi))
            .group_by(keys)
            .agg(pl.len().alias(_ROWS))
            .collect(engine="streaming")
        )
        return [HistogramBin(row[0] if group is not None else None, int(row[-2]), int(row[-1])) for row in df.iter_rows()]
