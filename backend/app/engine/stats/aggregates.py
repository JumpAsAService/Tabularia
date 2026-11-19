"""What a hypothesis test may ask of the data, on the three SQL engines: nothing else reaches
the rows. Every aggregate is a query the engine runs on the step's input (two for the
moments: their centre first), and returns at most a row per group: counts, means, variances,
never a row or a column of the data.

One builder for three dialects, as `app/engine/profile.py`: «duckdb», «clickhouse» (chDB
and the external server), «bigquery». Polars has its own, with the same methods and the
same answers (`polars_source.py`).

Rules of every query:
- a column is read as a double (`F`), and a NULL or a NaN is no value (`X`): left out of
  every aggregate, and counted apart (rows − values). An infinite value IS a value: it is
  counted, and shows as the smallest or the largest, where the test refuses it by name; means
  and variances are taken of the finite ones (DuckDB stops the query on a variance of
  infinities, the other engines answer NaN);
- the sample variance of fewer than two values is NULL (ClickHouse's own answer is NaN or
  inf), and on ClickHouse it is `varSampStable`: `varSamp` loses every digit on values far
  from zero (the variance of 1e9 + {5.1, 4.9, 6.2, …} comes out 0);
- means and variances are taken of the values LESS A CENTRE, a number near them asked of the
  engine first (`_centre`). Far from zero even the stable variance keeps half its digits
  (measured on 1,600 values near 1e9 with a spread of 1: 1e-8 off on every engine, and
  Polars 2e-8 from DuckDB and ClickHouse), and a mean near 1e9 is known to 1e-7, the size of
  the difference a test is about. Near zero nothing is lost, the engines agree to their last
  digits, and a difference of means is taken between the centred ones;
- no output takes the name of a column of the input: ClickHouse resolves an alias inside
  the same SELECT, and `count(x) AS n` next to `avg(n)` would be an aggregate of an
  aggregate. The outputs are read by position;
- the group's values come back typed, and become labels in Python (`catalog.label_of`).
"""
from __future__ import annotations

import datetime as _dt
import math
import re
from typing import Any, Callable, NamedTuple

from app.engine.profile import sql_histogram_bin
from app.engine.stats.catalog import Column


class Level(NamedTuple):
    """A value of the grouping column (None: the rows with no group) and its rows."""

    value: Any
    rows: int


class Moments(NamedTuple):
    """A group's rows, its values that are numbers (`n`), and their mean, sample variance,
    smallest and largest: None where there is none. `mean` = `centre` + `mean_centred`: a
    difference of two groups' means is taken between their `mean_centred` (the centre is
    the same for every group of an answer), where no digit is lost to the centre's size."""

    level: Any
    rows: int
    n: int
    mean: float | None
    var: float | None
    min: float | None
    max: float | None
    centre: float
    mean_centred: float | None


class Quartiles(NamedTuple):
    """A group's first quartile, median and third quartile (R's type 7: the engines' linear
    interpolation between the two nearest values); None where the group has no value."""

    level: Any
    q1: float | None
    median: float | None
    q3: float | None


class HistogramBin(NamedTuple):
    """The rows of a group whose value falls in an interval, 0 to bins − 1. An interval
    with no rows is not answered."""

    level: Any
    bin: int
    rows: int


# the rows the centre is the mean of: the chain's first ones, which an engine stops at
CENTRE_ROWS = 100_000


def comparable(levels: list | tuple | None) -> bool:
    """Whether an aggregate may keep to the rows of these groups by comparing the grouping
    column with them (`g = a OR g = b`): typed values A1 returned whose literal every engine
    reads as the column's own type — texts, booleans, whole numbers, finite floats, dates.
    For anything else (a datetime with its zone, a decimal, a NaN) the aggregate reads every
    group, as it did, and the caller keeps the ones it wants: the same answer, at a higher
    cost."""
    if not levels:
        return False
    for v in levels:
        if isinstance(v, float):
            if not math.isfinite(v):
                return False
        elif isinstance(v, _dt.datetime) or not isinstance(v, (str, bool, int, _dt.date)):
            return False
    return True


# the types that are numbers, as each engine names them (ClickHouse's without their
# Nullable(…) and LowCardinality(…) wrappers)
_NUMERIC = {
    "duckdb": re.compile(r"^(U?(TINYINT|SMALLINT|INTEGER|BIGINT|HUGEINT)|FLOAT|DOUBLE|REAL|DECIMAL)\b", re.IGNORECASE),
    "clickhouse": re.compile(r"^(U?Int(8|16|32|64|128|256)|Float(32|64)|Decimal)"),
    "bigquery": re.compile(r"^(INTEGER|INT64|FLOAT|FLOAT64|NUMERIC|BIGNUMERIC|DECIMAL|BIGDECIMAL)\b", re.IGNORECASE),
}
_CH_WRAPPER = re.compile(r"^(?:Nullable|LowCardinality)\((.*)\)$")

_DOUBLE = {"duckdb": "CAST({c} AS DOUBLE)", "clickhouse": "toFloat64({c})", "bigquery": "CAST({c} AS FLOAT64)"}
_IS_NAN = {"duckdb": "isnan({e})", "clickhouse": "isNaN({e})", "bigquery": "IS_NAN({e})"}
_IS_FINITE = {"duckdb": "isfinite({e})", "clickhouse": "isFinite({e})", "bigquery": "(NOT IS_NAN({e}) AND NOT IS_INF({e}))"}
# The exact quantile, linearly interpolated, of the values that are not NULL. BigQuery has
# no such aggregate: there it is a window function (`quartiles`).
_QUANTILE = {
    "duckdb": "quantile_cont({e}, {q})",
    "clickhouse": "quantileExactInclusive({q})({e})",
    "bigquery": "PERCENTILE_CONT({e}, {q}) OVER ({over})",
}
_VAR = {
    "duckdb": "var_samp({e})",
    "clickhouse": "if(count({e}) > 1, varSampStable({e}), NULL)",
    "bigquery": "VAR_SAMP({e})",
}
# A value less the centre. On ClickHouse the centre is written as a column, not a constant:
# its optimizer takes a constant out of an aggregate (`avg(x - c)` becomes `avg(x) - c`),
# which is the very sum of large numbers the centre is there to avoid (measured: the
# difference of two means near 1e9 off at the sixth digit, on 24.8 and on chDB).
_CENTRED = {"duckdb": "({x} - ({c}))", "clickhouse": "({x} - materialize({c}))", "bigquery": "({x} - ({c}))"}


def _is_numeric(dialect: str, dtype: str) -> bool:
    t = str(dtype).strip()
    if dialect == "clickhouse":
        while (m := _CH_WRAPPER.match(t)) is not None:
            t = m.group(1).strip()
    return _NUMERIC[dialect].match(t) is not None


def number(v: Any) -> float | None:
    """An aggregate as the tests use it: a float, whatever type the engine answered in."""
    return None if v is None else float(v)


class SqlStatSource:
    """A step's input as the tests read it on a SQL engine.

    `rows(sql)` runs a query and returns its rows, values typed; `quote(name)` writes a
    column of the input as an identifier and `lit(value)` a value as a literal, both in the
    engine's own way; `source_sql` is the chain so far, a SELECT; `schema` its columns,
    (name, type) as the engine names the type."""

    def __init__(self, dialect: str, rows: Callable[[str], list], quote: Callable[[str], str],
                 lit: Callable[[Any], str], source_sql: str, schema: list[tuple[str, str]]):
        if dialect not in _NUMERIC:
            raise ValueError(f"unknown dialect: {dialect}")
        self.dialect = dialect
        self._rows = rows
        self._quote = quote
        self._lit = lit  # for the aggregates that compare a column with a value
        self._schema = [(str(name), str(dtype)) for name, dtype in schema]
        # the names an output may not take: the input's, as they are and as an engine that
        # allows only letters, digits and underscores would rewrite them; case apart
        taken = {name.lower() for name, _ in self._schema}
        self._taken = taken | {re.sub(r"[^a-z0-9_]", "_", name) for name in taken}
        self._source = f"({source_sql}) AS {self._fresh('t')}"

    def columns(self) -> dict[str, Column]:
        return {name: Column(dtype, _is_numeric(self.dialect, dtype)) for name, dtype in self._schema}

    # ── pieces of SQL ───────────────────────────────────────────────────────
    def _fresh(self, name: str) -> str:
        """An output's name that no column of the input has."""
        out = f"st_{name}"
        while out in self._taken:
            out += "_"
        return out

    def _value(self, column: str, finite: bool = False) -> str:
        """The column as a double, NULL where it is NULL or NaN (`finite`: or infinite)."""
        f = _DOUBLE[self.dialect].format(c=self._quote(column))
        keep = _IS_FINITE[self.dialect].format(e=f) if finite else f"NOT {_IS_NAN[self.dialect].format(e=f)}"
        return f"CASE WHEN {self._quote(column)} IS NOT NULL AND {keep} THEN {f} END"

    def _centre(self, column: str) -> float:
        """A number near the column's values: the mean of those in the first `CENTRE_ROWS`
        rows (an aggregate of a prefix: the engine does not read the rest)."""
        rows = self._rows(
            f"SELECT avg({self._value(column, finite=True)}) AS {self._fresh('centre')} "
            f"FROM (SELECT {self._quote(column)} FROM {self._source} LIMIT {CENTRE_ROWS}) AS {self._fresh('head')}"
        )
        return usable_centre(rows[0][0] if rows and rows[0] else None)

    # ── the aggregates ──────────────────────────────────────────────────────
    def levels(self, group: str, limit: int) -> list[Level]:
        """A1: the values of `group` with their rows, the NULL one too; `limit` of them at
        most, in no order (the caller asks for a few more than it accepts)."""
        g = self._quote(group)
        rows = self._rows(
            f"SELECT {g} AS {self._fresh('lv')}, count(*) AS {self._fresh('rows')} "
            f"FROM {self._source} GROUP BY {g} LIMIT {int(limit)}"
        )
        return [Level(r[0], int(r[1])) for r in rows]

    def moments(self, value: str, group: str | None = None) -> list[Moments]:
        """A2: rows, values, mean, sample variance, smallest and largest of `value`, for each
        value of `group` (the NULL one too), or over every row without one."""
        x, finite = self._value(value), self._value(value, finite=True)
        centre = self._centre(value)
        centred = _CENTRED[self.dialect].format(x=finite, c=repr(centre)) if centre else finite
        aggregates = (
            f"count(*) AS {self._fresh('rows')}, count({x}) AS {self._fresh('n')}, "
            f"avg({centred}) AS {self._fresh('mean')}, {_VAR[self.dialect].format(e=centred)} AS {self._fresh('var')}, "
            f"min({x}) AS {self._fresh('min')}, max({x}) AS {self._fresh('max')}"
        )
        if group is None:
            rows = [[None, *r] for r in self._rows(f"SELECT {aggregates} FROM {self._source}")]
        else:
            g = self._quote(group)
            rows = self._rows(f"SELECT {g} AS {self._fresh('lv')}, {aggregates} FROM {self._source} GROUP BY {g}")
        return [moments_of(centre, *r) for r in rows]

    def quartiles(self, value: str, group: str | None = None, levels: list | None = None) -> list[Quartiles]:
        """A12: the first quartile, the median and the third quartile of `value`, for each
        value of `group` (the NULL one too), or over every row without one. Exact: the
        engine keeps the column's values to find them, which is why only a report asks —
        and why a test of two groups names them in `levels` (the typed values A1 returned):
        the values of the other groups are then never kept (`comparable` says when they can
        be told apart in the query; otherwise every group is answered, as without `levels`).
        Nothing is summed, so nothing is centred."""
        x = self._value(value, finite=True)
        names = [self._fresh(name) for name in ("q1", "median", "q3")]
        g = None if group is None else self._quote(group)
        over = "" if g is None else f"PARTITION BY {g}"
        only = ""
        if g is not None and comparable(levels):
            # an equality, not IN: ClickHouse's sets hold a float by its bits (−0.0 is not in (0.0))
            only = " WHERE " + " OR ".join(f"{g} = {self._lit(v)}" for v in levels)
        quantiles = ", ".join(
            f"{_QUANTILE[self.dialect].format(e=x, q=q, over=over)} AS {name}"
            for q, name in zip((0.25, 0.5, 0.75), names)
        )
        if self.dialect == "bigquery":
            # a window function: every row carries its group's quantiles, one is kept
            lv = "" if g is None else f"{g} AS {self._fresh('lv')}, "
            rows = self._rows(f"SELECT DISTINCT {lv}{quantiles} FROM {self._source}{only}")
        elif g is None:
            rows = self._rows(f"SELECT {quantiles} FROM {self._source}")
        else:
            rows = self._rows(f"SELECT {g} AS {self._fresh('lv')}, {quantiles} FROM {self._source}{only} GROUP BY {g}")
        if g is None:
            rows = [[None, *r] for r in rows]
        return [quartiles_of(*r) for r in rows]

    def histogram(self, value: str, group: str | None, lo: float, hi: float, bins: int) -> list[HistogramBin]:
        """A13: the rows whose `value` falls in each of `bins` equal intervals between `lo`
        and `hi`, for each value of `group` (or over every row without one): the last
        interval holds its upper edge, a NULL, a NaN and what is outside are left out. The
        intervals are the column profile's (`profile.sql_histogram_bin`)."""
        b, inside = sql_histogram_bin(self.dialect, self._quote(value), float(lo), float(hi), int(bins))
        count = f"count(*) AS {self._fresh('rows')}"
        if group is None:
            rows = self._rows(f"SELECT {b} AS {self._fresh('bin')}, {count} FROM {self._source} WHERE {inside} GROUP BY {b}")
            rows = [[None, *r] for r in rows]
        else:
            g = self._quote(group)
            rows = self._rows(
                f"SELECT {g} AS {self._fresh('lv')}, {b} AS {self._fresh('bin')}, {count} "
                f"FROM {self._source} WHERE {inside} GROUP BY {g}, {b}"
            )
        return [HistogramBin(r[0], int(r[1]), int(r[2])) for r in rows]


def usable_centre(v: Any) -> float:
    """The centre an engine answered, or 0 (no centring) when it has none to give: no value
    in the first rows, or a mean that overflowed."""
    c = number(v)
    return c if c is not None and math.isfinite(c) else 0.0


def moments_of(centre: float, level: Any, rows: Any, n: Any, mean_centred: Any, var: Any, lo: Any, hi: Any) -> Moments:
    """A row of A2 as every source answers it: counts as integers, the rest as floats, no
    variance of fewer than two values, and the mean put back where it is."""
    n = int(n or 0)
    centred = number(mean_centred) if n else None
    return Moments(level, int(rows or 0), n, None if centred is None else centre + centred,
                   number(var) if n > 1 else None, number(lo) if n else None, number(hi) if n else None,
                   centre, centred)


def quartiles_of(level: Any, q1: Any, median: Any, q3: Any) -> Quartiles:
    """A row of A12 as every source answers it: floats, and None for a quantile an engine
    could not give (no value: ClickHouse says NaN)."""
    def q(v: Any) -> float | None:
        v = number(v)
        return v if v is not None and math.isfinite(v) else None

    return Quartiles(level, q(q1), q(median), q(q3))
