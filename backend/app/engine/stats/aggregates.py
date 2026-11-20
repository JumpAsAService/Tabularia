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


class PairMoments(NamedTuple):
    """Two columns of the same rows (A3), over the rows where both are numbers (a pair): the
    rows, the pairs (`n`), and of x, of y and of their difference x − y the mean, the sample
    variance, the smallest and the largest; the sample covariance of x and y. Means and
    variances as A2's, taken less a centre: `mean_cx` and `mean_cy` are the two means less
    their centres, `mean_cd` the difference's less `centre_x − centre_y` — a difference of
    means is taken between the centred ones. None where there is none."""

    rows: int
    n: int
    centre_x: float
    centre_y: float
    mean_cx: float | None
    mean_cy: float | None
    mean_cd: float | None
    var_x: float | None
    var_y: float | None
    var_d: float | None
    cov: float | None
    min_x: float | None
    max_x: float | None
    min_y: float | None
    max_y: float | None
    min_d: float | None
    max_d: float | None


class Deviations(NamedTuple):
    """A group's values as Levene's test reads them (A11): its rows, its values that are
    numbers (`n`), the centre of the group (its median or its mean, `center`), and the mean
    and the sample variance of the absolute deviations |value − center|; the smallest and
    the largest value, as they are (an infinite one is refused by name)."""

    level: Any
    rows: int
    n: int
    center: float | None
    mean: float | None
    var: float | None
    min: float | None
    max: float | None


class Successes(NamedTuple):
    """A group's rows (A8), its values (`n`: not NULL, not NaN) and its successes (`k`: the
    values equal to the success)."""

    level: Any
    rows: int
    n: int
    k: int


class Cell(NamedTuple):
    """A pair of values of two columns and its rows (A7); None where a column is NULL."""

    x: Any
    y: Any
    rows: int


class Cell3(NamedTuple):
    """A triple of values of three columns and its rows (A7 with strata); None where NULL."""

    x: Any
    y: Any
    z: Any
    rows: int


class Difference(NamedTuple):
    """A measure that is not a column: the difference x − y of two columns of the same row,
    where both are numbers (NULL elsewhere)."""

    x: str
    y: str


class Paired(NamedTuple):
    """The column `x` on the rows where `y` is a number too (the rows a paired test uses)."""

    x: str
    y: str


class RankGroup(NamedTuple):
    """A group's values ranked with the other groups' (A4): its values (`n`: not NULL, not NaN),
    the sum of their average ranks over every group compared together, the smallest and the
    largest value."""

    level: Any
    n: int
    rank_sum: float
    min: float | None
    max: float | None


class RankSums(NamedTuple):
    """A4: the groups' rank sums, and Σ(t³ − t) over the values held by t > 1 rows of the groups
    compared (the ties' correction)."""

    groups: list[RankGroup]
    tie_sum: float


class SignedRanks(NamedTuple):
    """A5: the differences d = x − y − mu (or x − mu) of the rows where x (and y) are numbers,
    ranked by |d|. `rows`: every row; `pairs`: those with a number in x (and y); `nonfinite`:
    pairs with an infinite value (the test refuses them); `zeros`: finite pairs with d = 0;
    `ranked`: the differences ranked (without the zeros, or with them by Pratt's rule);
    `w_plus`, `w_minus`: the sums of the ranks of the positive and of the negative ones;
    `tie_sum`: Σ(t³ − t) over the values of |d| above 0. The extremes of x, y and x − y (x
    alone without y: mu is not taken off) over the finite pairs, for the report."""

    rows: int
    pairs: int
    nonfinite: int
    zeros: int
    ranked: int
    w_plus: float
    w_minus: float
    tie_sum: float
    min_x: float | None
    max_x: float | None
    min_y: float | None
    max_y: float | None
    min_d: float | None
    max_d: float | None


class RankCorr(NamedTuple):
    """A6: the rows where x and y are both numbers (`n`), each ranked among them (average
    ranks) and centred by (n + 1)/2: Σ rx·ry, Σ rx², Σ ry²; the ties' Σ(t³ − t) of each column;
    the extremes of each (an infinite one is refused by name)."""

    n: int
    sxy: float
    sxx: float
    syy: float
    tie_sum_x: float
    tie_sum_y: float
    min_x: float | None
    max_x: float | None
    min_y: float | None
    max_y: float | None


class KsDistance(NamedTuple):
    """A9: the values of two groups (`n1`, `n2`), the largest distances between their empirical
    distribution functions as whole numbers — `d_plus` = max(F1 − F2)·n1·n2, `d_minus` =
    max(F2 − F1)·n1·n2 — the values held by more than one row (`tied`), the extremes; and
    where each distance is reached: the smallest value v where it is (`v_plus`, `v_minus`),
    and the values of each group at or below it there (`k1_plus`, `k2_plus`, …: F1(v) =
    k1/n1, F2(v) = k2/n2)."""

    n1: int
    n2: int
    d_plus: int
    d_minus: int
    tied: int
    min: float | None
    max: float | None
    v_plus: float | None = None
    k1_plus: int | None = None
    k2_plus: int | None = None
    v_minus: float | None = None
    k1_minus: int | None = None
    k2_minus: int | None = None


class CentralMoments(NamedTuple):
    """A10: the rows, the values (`n`, an infinite one counted), the finite ones, the mean less
    the centre and the central moments m₂, m₃, m₄ of the finite values (each the mean of
    (v − mean)^k), the extremes."""

    rows: int
    n: int
    finite: int
    centre: float
    mean_centred: float | None
    m2: float | None
    m3: float | None
    m4: float | None
    min: float | None
    max: float | None


# the kinds of grouping column whose groups an aggregate can pick by a literal written from
# their labels (`typed_labels`), as each engine names their types (ClickHouse's without their
# Nullable(…) and LowCardinality(…) wrappers); a column of another type is read whole
_LITERAL_KINDS = {
    "duckdb": (("text", r"^VARCHAR\b"), ("integer", r"^U?(TINYINT|SMALLINT|INTEGER|BIGINT|HUGEINT)\b"),
               ("float", r"^(FLOAT|DOUBLE|REAL)$"), ("date", r"^DATE$"), ("boolean", r"^BOOLEAN$")),
    "clickhouse": (("text", r"^(String|Enum(8|16)?\(.*\))$"), ("integer", r"^U?Int(8|16|32|64|128|256)$"),
                   ("float", r"^Float(32|64)$"), ("date", r"^Date(32)?$"), ("boolean", r"^Bool$")),
    "bigquery": (("text", r"^STRING\b"), ("integer", r"^(INT64|INTEGER)$"), ("float", r"^(FLOAT64|FLOAT)$"),
                 ("date", r"^DATE$"), ("boolean", r"^(BOOL|BOOLEAN)$")),
}


def typed_labels(labels: list[str], kind: str | None) -> list | None:
    """The values a grouping column of `kind` holds under these labels (`catalog.label_of`),
    for an aggregate to keep to those groups by comparing the column with them; None when the
    column's kind cannot be compared so (a datetime, a decimal…) or a label is a value no
    literal writes the same on every engine (a NaN). A label that is no value of the kind (a
    text in a column of numbers) has no value: nothing is kept for it, and the caller says
    the group is not there."""
    if kind is None:
        return None
    out: list = []
    for label in labels:
        try:
            if kind == "text":
                v: Any = label
            elif kind == "integer":
                v = int(label) if re.fullmatch(r"-?\d+", label) else None
            elif kind == "float":
                v = float(label)
                if not math.isfinite(v):
                    return None
            elif kind == "date":
                v = _dt.date.fromisoformat(label)
            elif kind == "boolean":
                v = {"true": True, "false": False}.get(label)
            else:
                return None
        except ValueError:
            v = None
        if v is not None:
            out.append(v)
    return out


def typed_value(raw: Any, kind: str | None) -> Any:
    """A value written by a person (a success), as a column of `kind` holds it: a text, a whole
    or a float number, a date, a boolean. None when the column's kind cannot be compared with
    a value (a datetime, a decimal) or the value is none of its kind («yes» in a column of
    numbers)."""
    from app.engine.stats.catalog import label_of

    text = raw if isinstance(raw, str) else label_of(raw)
    try:
        if kind == "text":
            return text
        if kind == "integer":
            if isinstance(raw, bool):
                return None
            number = float(raw) if isinstance(raw, (int, float)) else float(text.strip())
            return int(number) if math.isfinite(number) and number == int(number) and abs(number) < 2**63 else None
        if kind == "float":
            if isinstance(raw, bool):
                return None
            number = float(raw) if isinstance(raw, (int, float)) else float(text.strip())
            return number if math.isfinite(number) else None
        if kind == "boolean":
            if isinstance(raw, bool):
                return raw
            return {"true": True, "false": False, "1": True, "0": False}.get(text.strip().lower())
        if kind == "date":
            return raw if isinstance(raw, _dt.date) and not isinstance(raw, _dt.datetime) else _dt.date.fromisoformat(text.strip())
    except (ValueError, OverflowError):
        return None
    return None


def is_value(v: Any) -> bool:
    """Whether a value of a column is one (not NULL, not NaN)."""
    return v is not None and not (isinstance(v, float) and math.isnan(v))


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


# A count made a whole number of 64 bits, for products of counts that must not wrap (ClickHouse's
# counts are unsigned: a difference of two would)
_INT = {"duckdb": "CAST({e} AS BIGINT)", "clickhouse": "toInt64({e})", "bigquery": "CAST({e} AS INT64)"}
# the window that sums the rows up to the current value, and the one over every row
_RUNNING = "sum({e}) OVER (ORDER BY {by} ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)"
_RUNNING_PEERS = "sum({e}) OVER (ORDER BY {by} RANGE BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)"
# a whole number for each distinct value, in the values' order: the windows are partitioned by it,
# not by the value (BigQuery partitions by no FLOAT64)
_VALUE_ID = "dense_rank() OVER (ORDER BY {by})"


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

    def _measure(self, m: Any, finite: bool = False) -> str:
        """A measure as a double, NULL where it has no value: a column (`_value`), the
        difference of two (`Difference`), a column where another is a number too (`Paired`)."""
        if isinstance(m, Difference):
            return f"({self._value(m.x, finite)} - {self._value(m.y, finite)})"
        if isinstance(m, Paired):
            return f"CASE WHEN {self._value(m.y, finite)} IS NOT NULL THEN {self._value(m.x, finite)} END"
        return self._value(m, finite)

    def _centre(self, column: str) -> float:
        """A number near the column's values: the mean of those in the first `CENTRE_ROWS`
        rows (an aggregate of a prefix: the engine does not read the rest)."""
        rows = self._rows(
            f"SELECT avg({self._value(column, finite=True)}) AS {self._fresh('centre')} "
            f"FROM (SELECT {self._quote(column)} FROM {self._source} LIMIT {CENTRE_ROWS}) AS {self._fresh('head')}"
        )
        return usable_centre(rows[0][0] if rows and rows[0] else None)

    def _centred(self, expr: str, centre: float) -> str:
        return _CENTRED[self.dialect].format(x=expr, c=repr(centre)) if centre else expr

    def kind_of(self, column: str) -> str | None:
        """The kind of a column whose values a test compares with one written (`typed_value`)."""
        return self._kind(column)

    def _kind(self, column: str) -> str | None:
        """The kind of a grouping column, as `typed_labels` reads it, or None."""
        dtype = dict(self._schema).get(column, "").strip()
        if self.dialect == "clickhouse":
            while (m := _CH_WRAPPER.match(dtype)) is not None:
                dtype = m.group(1).strip()
        flags = 0 if self.dialect == "clickhouse" else re.IGNORECASE
        return next((kind for kind, pattern in _LITERAL_KINDS[self.dialect] if re.match(pattern, dtype, flags)), None)

    def _only(self, group: str, levels: list | None, nulls: bool = False) -> str:
        """A WHERE that keeps the rows of these groups (typed values; with `nulls`, the rows
        with no group too), or nothing when they cannot be told apart in a query."""
        if not comparable(levels):
            return ""
        g = self._quote(group)
        # an equality, not IN: ClickHouse's sets hold a float by its bits (−0.0 is not in (0.0))
        return " WHERE " + " OR ".join(([f"{g} IS NULL"] if nulls else []) + [f"{g} = {self._lit(v)}" for v in levels])

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

    def levels_of(self, group: str, labels: list[str]) -> list[Level] | None:
        """A1 for the groups named by these labels only, and the rows with no group: the
        engine compares the column with a literal for each, however many groups it holds.
        None when the column's type cannot be compared so (`typed_labels`): the caller then
        lists the groups (`levels`)."""
        typed = typed_labels(labels, self._kind(group))
        if typed is None:
            return None
        g = self._quote(group)
        only = " OR ".join([f"{g} IS NULL"] + [f"{g} = {self._lit(v)}" for v in typed])
        rows = self._rows(
            f"SELECT {g} AS {self._fresh('lv')}, count(*) AS {self._fresh('rows')} "
            f"FROM {self._source} WHERE {only} GROUP BY {g}"
        )
        return [Level(r[0], int(r[1])) for r in rows]

    def moments(self, value: str, group: str | None = None, levels: list | None = None) -> list[Moments]:
        """A2: rows, values, mean, sample variance, smallest and largest of `value`, for each
        value of `group` (the NULL one too), or over every row without one. `levels`: the
        groups to answer for (typed values of `levels` or `levels_of`), and the rows with no
        group; every group when they cannot be told apart in a query (`comparable`)."""
        x, finite = self._value(value), self._value(value, finite=True)
        centre = self._centre(value)
        centred = self._centred(finite, centre)
        aggregates = (
            f"count(*) AS {self._fresh('rows')}, count({x}) AS {self._fresh('n')}, "
            f"avg({centred}) AS {self._fresh('mean')}, {_VAR[self.dialect].format(e=centred)} AS {self._fresh('var')}, "
            f"min({x}) AS {self._fresh('min')}, max({x}) AS {self._fresh('max')}"
        )
        if group is None:
            rows = [[None, *r] for r in self._rows(f"SELECT {aggregates} FROM {self._source}")]
        else:
            g = self._quote(group)
            only = self._only(group, levels, nulls=True)
            rows = self._rows(f"SELECT {g} AS {self._fresh('lv')}, {aggregates} FROM {self._source}{only} GROUP BY {g}")
        return [moments_of(centre, *r) for r in rows]

    def pair_moments(self, x: str, y: str) -> PairMoments:
        """A3: two columns of the same rows, over the rows where both are numbers. Each is
        less its own centre, and the difference is that of the two centred values: one pass."""
        cx, cy = self._centre(x), self._centre(y)
        px, py = self._measure(Paired(x, y)), self._measure(Paired(y, x))
        both = f"{self._value(x, finite=True)} IS NOT NULL AND {self._value(y, finite=True)} IS NOT NULL"
        ex = self._centred(f"CASE WHEN {both} THEN {_DOUBLE[self.dialect].format(c=self._quote(x))} END", cx)
        ey = self._centred(f"CASE WHEN {both} THEN {_DOUBLE[self.dialect].format(c=self._quote(y))} END", cy)
        ed = f"({ex} - {ey})"
        d = f"({px} - {py})"
        var = _VAR[self.dialect]
        cov = {"duckdb": "covar_samp({a}, {b})", "clickhouse": "if(count({a}) > 1, covarSampStable({a}, {b}), NULL)",
               "bigquery": "COVAR_SAMP({a}, {b})"}[self.dialect].format(a=ex, b=ey)
        names = [self._fresh(n) for n in ("rows", "n", "mean_x", "mean_y", "mean_d", "var_x", "var_y", "var_d", "cov",
                                         "min_x", "max_x", "min_y", "max_y", "min_d", "max_d")]
        exprs = ["count(*)", f"count({px})", f"avg({ex})", f"avg({ey})", f"avg({ed})", var.format(e=ex), var.format(e=ey),
                 var.format(e=ed), cov, f"min({px})", f"max({px})", f"min({py})", f"max({py})", f"min({d})", f"max({d})"]
        rows = self._rows(f"SELECT {', '.join(f'{e} AS {n}' for e, n in zip(exprs, names))} FROM {self._source}")
        return pair_moments_of(cx, cy, *(rows[0] if rows else [0] * 15))

    def abs_dev_moments(self, value: str, group: str, center: str) -> list[Deviations]:
        """A11: for each group, the mean and the sample variance of |value − the group's
        centre| (`center`: «median» or «mean»), with the group's rows, values, centre, smallest
        and largest. The values are taken less the column's centre first, as A2's; the
        deviations are of the spread's size already, and are not centred again. The rows with
        no group are not answered (A1 counts them)."""
        x, finite = self._value(value), self._value(value, finite=True)
        c = self._centre(value)
        centred = self._centred(finite, c)
        g = self._quote(group)
        lv, ctr, key = self._fresh("lv"), self._fresh("ctr"), self._fresh("k")
        if center == "median":
            if self.dialect == "bigquery":
                # a window partitioned by a whole number for each group, not by the group (a
                # FLOAT64 column partitions nothing)
                cv, gid = self._fresh("cv"), self._fresh("gid")
                inner = (f"SELECT DISTINCT {key}, PERCENTILE_CONT({cv}, 0.5) OVER (PARTITION BY {gid}) AS {ctr} "
                         f"FROM (SELECT {g} AS {key}, {centred} AS {cv}, {_VALUE_ID.format(by=g)} AS {gid} "
                         f"FROM {self._source}) AS {self._fresh('gv')}")
            else:
                inner = f"SELECT {g} AS {key}, {_QUANTILE[self.dialect].format(e=centred, q=0.5, over='')} AS {ctr} FROM {self._source} GROUP BY {g}"
        else:
            inner = f"SELECT {g} AS {key}, avg({centred}) AS {ctr} FROM {self._source} GROUP BY {g}"
        t, m = self._fresh("d"), self._fresh("m")
        z = f"abs({centred} - {m}.{ctr})"
        var = _VAR[self.dialect].format(e=z)
        rows = self._rows(
            f"SELECT {t}.{g} AS {lv}, count(*) AS {self._fresh('rows')}, count({x}) AS {self._fresh('n')}, "
            f"max({m}.{ctr}) AS {self._fresh('center')}, avg({z}) AS {self._fresh('mean')}, {var} AS {self._fresh('var')}, "
            f"min({x}) AS {self._fresh('min')}, max({x}) AS {self._fresh('max')} "
            f"FROM (SELECT * FROM {self._source}) AS {t} JOIN ({inner}) AS {m} ON {t}.{g} = {m}.{key} GROUP BY {t}.{g}"
        )
        return [deviations_of(c, *r) for r in rows]

    def quartiles(self, value: Any, group: str | None = None, levels: list | None = None) -> list[Quartiles]:
        """A12: the first quartile, the median and the third quartile of `value`, for each
        value of `group` (the NULL one too), or over every row without one. Exact: the
        engine keeps the column's values to find them, which is why only a report asks —
        and why a test of two groups names them in `levels` (the typed values A1 returned):
        the values of the other groups are then never kept (`comparable` says when they can
        be told apart in the query; otherwise every group is answered, as without `levels`).
        Nothing is summed, so nothing is centred. `value`: a column, or a measure made of
        two (`Difference`, `Paired`)."""
        x = self._measure(value, finite=True)
        names = [self._fresh(name) for name in ("q1", "median", "q3")]
        g = None if group is None else self._quote(group)
        only = "" if g is None else self._only(group, levels)
        if self.dialect == "bigquery":
            # a window function: every row carries its group's quantiles, one is kept. The
            # window is partitioned by a whole number for each group (`_VALUE_ID`), not by the
            # group: GoogleSQL partitions by no FLOAT64
            if g is None:
                quantiles = ", ".join(f"{_QUANTILE['bigquery'].format(e=x, q=q, over='')} AS {name}"
                                      for q, name in zip((0.25, 0.5, 0.75), names))
                rows = self._rows(f"SELECT DISTINCT {quantiles} FROM {self._source}")
            else:
                lv, xv, gid = self._fresh("lv"), self._fresh("xv"), self._fresh("gid")
                quantiles = ", ".join(f"{_QUANTILE['bigquery'].format(e=xv, q=q, over=f'PARTITION BY {gid}')} AS {name}"
                                      for q, name in zip((0.25, 0.5, 0.75), names))
                rows = self._rows(f"SELECT DISTINCT {lv}, {quantiles} FROM (SELECT {g} AS {lv}, {x} AS {xv}, "
                                  f"{_VALUE_ID.format(by=g)} AS {gid} FROM {self._source}{only}) AS {self._fresh('gv')}")
            if g is None:
                rows = [[None, *r] for r in rows]
            return [quartiles_of(*r) for r in rows]
        quantiles = ", ".join(
            f"{_QUANTILE[self.dialect].format(e=x, q=q, over='')} AS {name}"
            for q, name in zip((0.25, 0.5, 0.75), names)
        )
        if g is None:
            rows = self._rows(f"SELECT {quantiles} FROM {self._source}")
        else:
            rows = self._rows(f"SELECT {g} AS {self._fresh('lv')}, {quantiles} FROM {self._source}{only} GROUP BY {g}")
        if g is None:
            rows = [[None, *r] for r in rows]
        return [quartiles_of(*r) for r in rows]

    def quartiles_many(self, measures: list) -> list[Quartiles]:
        """A12 of several measures over every row, in one pass: a Quartiles for each, its
        `level` the measure's place in the list (a paired test's x, y and difference)."""
        names = [[self._fresh(f"{q}_{i}") for q in ("q1", "median", "q3")] for i in range(len(measures))]
        over = ""
        quantiles = ", ".join(
            f"{_QUANTILE[self.dialect].format(e=self._measure(m, finite=True), q=q, over=over)} AS {name}"
            for m, three in zip(measures, names) for q, name in zip((0.25, 0.5, 0.75), three)
        )
        distinct = "DISTINCT " if self.dialect == "bigquery" else ""
        rows = self._rows(f"SELECT {distinct}{quantiles} FROM {self._source}")
        row = list(rows[0]) if rows else [None] * (3 * len(measures))
        return [quartiles_of(i, *row[3 * i:3 * i + 3]) for i in range(len(measures))]

    def successes(self, value: str, success: Any, group: str | None = None, levels: list | None = None) -> list[Successes]:
        """A8: for each value of `group` (the NULL one too), or over every row without one,
        the rows, the values of `value` (not NULL, and not NaN in a column of floats) and those
        equal to `success` (a value typed as the column's, `typed_value`). `levels`: the groups
        to answer for (typed values), and the rows with no group. Nothing is summed but ones."""
        q = self._quote(value)
        ok = f"{q} IS NOT NULL" + (f" AND NOT {_IS_NAN[self.dialect].format(e=q)}" if self._kind(value) == "float" else "")
        aggregates = (
            f"count(*) AS {self._fresh('rows')}, sum(CASE WHEN {ok} THEN 1 ELSE 0 END) AS {self._fresh('n')}, "
            f"sum(CASE WHEN {ok} AND {q} = {self._lit(success)} THEN 1 ELSE 0 END) AS {self._fresh('k')}"
        )
        if group is None:
            rows = [[None, *r] for r in self._rows(f"SELECT {aggregates} FROM {self._source}")]
        else:
            g = self._quote(group)
            only = self._only(group, levels, nulls=True)
            rows = self._rows(f"SELECT {g} AS {self._fresh('lv')}, {aggregates} FROM {self._source}{only} GROUP BY {g}")
        return [Successes(r[0], int(r[1] or 0), int(r[2] or 0), int(r[3] or 0)) for r in rows]

    def crosstab(self, x: str, y: str, limit: int) -> list[Cell]:
        """A7: the rows of each pair of values of `x` and `y`, NULL ones too; `limit` pairs at
        most, in no order (the caller asks for one more than a table may have)."""
        qx, qy = self._quote(x), self._quote(y)
        rows = self._rows(
            f"SELECT {qx} AS {self._fresh('lx')}, {qy} AS {self._fresh('ly')}, count(*) AS {self._fresh('rows')} "
            f"FROM {self._source} GROUP BY {qx}, {qy} LIMIT {int(limit)}"
        )
        return [Cell(r[0], r[1], int(r[2])) for r in rows]

    def crosstab3(self, x: str, y: str, z: str, limit: int) -> list[Cell3]:
        """A7 within strata: the rows of each triple of values of `x`, `y` and `z`, NULL ones
        too; `limit` triples at most, in no order."""
        qx, qy, qz = self._quote(x), self._quote(y), self._quote(z)
        rows = self._rows(
            f"SELECT {qx} AS {self._fresh('lx')}, {qy} AS {self._fresh('ly')}, {qz} AS {self._fresh('lz')}, "
            f"count(*) AS {self._fresh('rows')} FROM {self._source} GROUP BY {qx}, {qy}, {qz} LIMIT {int(limit)}"
        )
        return [Cell3(r[0], r[1], r[2], int(r[3])) for r in rows]

    # ── ranks (A4, A5, A6, A9): a value is a key, nothing is summed but whole numbers ──
    # A rank is a property of a value: the rows are counted by value first (a hash
    # aggregation), and the windows run over those counts — over the distinct values, never
    # over the rows. The value is the column as a double plus 0.0, so that −0.0 and 0.0 are one
    # value on every engine (ClickHouse groups a float by its bits). Average ranks are halves of
    # whole numbers: their sums are exact in doubles while they are below 2^53. Every alias is
    # a fresh name: ClickHouse resolves an alias inside the SELECT that names it.
    def _dbl(self, e: str) -> str:
        return _DOUBLE[self.dialect].format(c=e)

    def _key(self, column: str) -> str:
        """The column as a double, −0.0 made 0.0; NULL where it is NULL or NaN."""
        return f"({self._value(column)} + 0.0)"

    def rank_sums(self, value: str, group: str, levels: list | None = None) -> RankSums:
        """A4: the values of every group (or of the groups `levels` names, typed values A1
        returned, when they can be told apart in a query: `comparable`) ranked together, the
        average rank for ties; per group its values, its rank sum, its extremes; Σ(t³ − t) of
        the ties. The rows with no group, and the values that are NULL or NaN, are not ranked."""
        g = self._quote(group)
        lv, xv, c, vid, cum, t, first, rk = (self._fresh(n) for n in ("lv", "xv", "c", "vid", "cum", "t", "first", "rk"))
        only = self._only(group, levels)
        keep = f" AND ({only[len(' WHERE '):]})" if only else ""
        counted = (f"SELECT {g} AS {lv}, {self._key(value)} AS {xv}, count(*) AS {c} FROM {self._source} "
                   f"WHERE {g} IS NOT NULL AND {self._key(value)} IS NOT NULL{keep} GROUP BY {g}, {self._key(value)}")
        numbered = f"SELECT {lv}, {xv}, {c}, {_VALUE_ID.format(by=xv)} AS {vid} FROM ({counted}) AS {self._fresh('f')}"
        ranked = (f"SELECT {lv}, {xv}, {c}, {self._dbl(_RUNNING_PEERS.format(e=c, by=vid))} AS {cum}, "
                  f"{self._dbl(f'sum({c}) OVER (PARTITION BY {vid})')} AS {t}, "
                  f"row_number() OVER (PARTITION BY {vid} ORDER BY {lv}) AS {first} FROM ({numbered}) AS {self._fresh('v')}")
        rank = f"({cum} - ({t} - 1.0) / 2.0)"
        rows = self._rows(
            f"SELECT {lv} AS {self._fresh('level')}, sum({c}) AS {self._fresh('n')}, sum({self._dbl(c)} * {rank}) AS {rk}, "
            f"min({xv}) AS {self._fresh('min')}, max({xv}) AS {self._fresh('max')}, "
            f"sum(CASE WHEN {first} = 1 THEN {t} * {t} * {t} - {t} ELSE 0.0 END) AS {self._fresh('ties')} "
            f"FROM ({ranked}) AS {self._fresh('r')} GROUP BY {lv}"
        )
        return rank_sums_of(rows)

    def signed_ranks(self, x: str, y: str | None, mu: float, pratt: bool) -> SignedRanks:
        """A5: the differences d = x − y − mu (x − mu without `y`) of the rows where both are
        finite numbers, ranked by |d| with the average rank for ties: the zeros left out before
        ranking, or ranked and left out of the sums (`pratt`). Two queries: the ranks, and the
        counts (rows, pairs, infinite values, zeros) with the extremes."""
        fx = self._value(x, finite=True)
        fy = self._value(y, finite=True) if y is not None else None
        d = d0 = f"({fx} - {fy})" if fy is not None else f"({fx})"
        if mu:
            d = f"({d} - {self._dbl(repr(float(mu)))})"
        both = f"{fx} IS NOT NULL" + (f" AND {fy} IS NOT NULL" if fy is not None else "")
        dv, av, cp, cn, t, rk = (self._fresh(n) for n in ("dv", "av", "cp", "cn", "t", "rk"))
        diffs = f"SELECT {d} AS {dv} FROM {self._source} WHERE {both}"
        zero_out = "" if pratt else f" WHERE {dv} <> 0"
        counted = (f"SELECT abs({dv}) + 0.0 AS {av}, sum(CASE WHEN {dv} > 0 THEN 1 ELSE 0 END) AS {cp}, "
                   f"sum(CASE WHEN {dv} < 0 THEN 1 ELSE 0 END) AS {cn}, count(*) AS {t} "
                   f"FROM ({diffs}) AS {self._fresh('d')}{zero_out} GROUP BY abs({dv}) + 0.0")
        ranked = (f"SELECT {av}, {cp}, {cn}, {t}, {self._dbl(_RUNNING.format(e=t, by=av))} - ({self._dbl(t)} - 1.0) / 2.0 AS {rk} "
                  f"FROM ({counted}) AS {self._fresh('f')}")
        tf = self._dbl(t)
        ranks = self._rows(
            f"SELECT sum({t}) AS {self._fresh('n')}, sum({self._dbl(cp)} * {rk}) AS {self._fresh('wp')}, "
            f"sum({self._dbl(cn)} * {rk}) AS {self._fresh('wm')}, "
            f"sum(CASE WHEN {av} > 0 THEN {tf} * {tf} * {tf} - {tf} ELSE 0.0 END) AS {self._fresh('ties')} "
            f"FROM ({ranked}) AS {self._fresh('r')}"
        )
        ok_x = f"{self._value(x)} IS NOT NULL"
        ok = ok_x + (f" AND {self._value(y)} IS NOT NULL" if y is not None else "")
        dd = d0  # the extremes of x − y (of x), as the report's groups show them: without mu

        def over_pairs(fn: str, e: str) -> str:
            return f"{fn}(CASE WHEN {both} THEN {e} END)"

        extremes = [over_pairs("min", fx), over_pairs("max", fx), over_pairs("min", fy) if fy else "NULL",
                    over_pairs("max", fy) if fy else "NULL", over_pairs("min", dd), over_pairs("max", dd)]
        names = [self._fresh(n) for n in ("rows", "pairs", "inf", "zeros", "min_x", "max_x", "min_y", "max_y", "min_d", "max_d")]
        exprs = ["count(*)", f"sum(CASE WHEN {ok} THEN 1 ELSE 0 END)",
                 f"sum(CASE WHEN {ok} AND NOT ({both}) THEN 1 ELSE 0 END)",
                 f"sum(CASE WHEN {both} AND {d} = 0 THEN 1 ELSE 0 END)", *extremes]
        counts = self._rows(f"SELECT {', '.join(f'{e} AS {n}' for e, n in zip(exprs, names))} FROM {self._source}")
        return signed_ranks_of(counts[0] if counts else [0] * 10, ranks[0] if ranks else [0, 0, 0, 0])

    def rank_corr(self, x: str, y: str) -> RankCorr:
        """A6: the rows where x and y are both numbers, counted by pair of values; each column
        ranked among them (the average rank for ties) and centred by (n + 1)/2; Σ rx·ry, Σ rx²,
        Σ ry², the ties of each, the extremes. One query: the windows run over the distinct
        pairs."""
        kx, ky = self._key(x), self._key(y)
        xv, yv, c, rx, ry, tx, ty, fx, fy, ix, iy = (self._fresh(n) for n in ("xv", "yv", "c", "rx", "ry", "tx", "ty", "fx", "fy", "ix", "iy"))
        counted = (f"SELECT {kx} AS {xv}, {ky} AS {yv}, count(*) AS {c} FROM {self._source} "
                   f"WHERE {kx} IS NOT NULL AND {ky} IS NOT NULL GROUP BY {kx}, {ky}")
        numbered = (f"SELECT {xv}, {yv}, {c}, {_VALUE_ID.format(by=xv)} AS {ix}, {_VALUE_ID.format(by=yv)} AS {iy} "
                    f"FROM ({counted}) AS {self._fresh('p')}")
        n = self._dbl(f"sum({c}) OVER ()")

        def rank(v: str) -> str:
            t = self._dbl(f"sum({c}) OVER (PARTITION BY {v})")
            return f"{self._dbl(_RUNNING_PEERS.format(e=c, by=v))} - ({t} - 1.0) / 2.0 - ({n} + 1.0) / 2.0"

        ranked = (f"SELECT {xv}, {yv}, {self._dbl(c)} AS {c}, {rank(ix)} AS {rx}, {rank(iy)} AS {ry}, "
                  f"{self._dbl(f'sum({c}) OVER (PARTITION BY {ix})')} AS {tx}, {self._dbl(f'sum({c}) OVER (PARTITION BY {iy})')} AS {ty}, "
                  f"row_number() OVER (PARTITION BY {ix} ORDER BY {iy}) AS {fx}, row_number() OVER (PARTITION BY {iy} ORDER BY {ix}) AS {fy} "
                  f"FROM ({numbered}) AS {self._fresh('q')}")
        names = [self._fresh(n) for n in ("n", "sxy", "sxx", "syy", "ties_x", "ties_y", "min_x", "max_x", "min_y", "max_y")]
        exprs = [f"sum({c})", f"sum({c} * {rx} * {ry})", f"sum({c} * {rx} * {rx})", f"sum({c} * {ry} * {ry})",
                 f"sum(CASE WHEN {fx} = 1 THEN {tx} * {tx} * {tx} - {tx} ELSE 0.0 END)",
                 f"sum(CASE WHEN {fy} = 1 THEN {ty} * {ty} * {ty} - {ty} ELSE 0.0 END)",
                 f"min({xv})", f"max({xv})", f"min({yv})", f"max({yv})"]
        rows = self._rows(f"SELECT {', '.join(f'{e} AS {n}' for e, n in zip(exprs, names))} FROM ({ranked}) AS {self._fresh('r')}")
        return rank_corr_of(*(rows[0] if rows else [0] * 10))

    def count_rows(self) -> int:
        """The rows of the step's input."""
        rows = self._rows(f"SELECT count(*) AS {self._fresh('rows')} FROM {self._source}")
        return int(rows[0][0] or 0) if rows else 0

    def ks(self, value: str, group: str, levels: list) -> KsDistance:
        """A9: the values of the two groups `levels` (typed values A1 returned, which a literal
        names: `comparable`), counted by value; the running counts of each, in the values'
        order, give the two empirical distribution functions, and their largest distances are
        returned as whole numbers, max(k1·n2 − k2·n1) and max(k2·n1 − k1·n2): exact on every
        engine. The values held by more than one row are counted (the exact distribution
        assumes none)."""
        g = self._quote(group)
        a, b = (self._lit(v) for v in levels)
        k = self._key(value)
        xv, c1, c2, k1, k2, m1, m2 = (self._fresh(n) for n in ("xv", "c1", "c2", "k1", "k2", "m1", "m2"))
        counted = (f"SELECT {k} AS {xv}, sum(CASE WHEN {g} = {a} THEN 1 ELSE 0 END) AS {c1}, "
                   f"sum(CASE WHEN {g} = {b} THEN 1 ELSE 0 END) AS {c2} FROM {self._source} "
                   f"WHERE {k} IS NOT NULL AND ({g} = {a} OR {g} = {b}) GROUP BY {k}")
        i = _INT[self.dialect]
        running = (f"SELECT {xv}, {c1}, {c2}, {i.format(e=_RUNNING.format(e=c1, by=xv))} AS {k1}, "
                   f"{i.format(e=_RUNNING.format(e=c2, by=xv))} AS {k2}, {i.format(e=f'sum({c1}) OVER ()')} AS {m1}, "
                   f"{i.format(e=f'sum({c2}) OVER ()')} AS {m2} FROM ({counted}) AS {self._fresh('f')}")
        # each point's two distances, then the largest of each over every point beside them: the
        # point where a distance is reached is the smallest value whose distance is the largest
        dp, dm, dpx, dmx = (self._fresh(n) for n in ("dp", "dm", "dpx", "dmx"))
        both = (f"SELECT {xv}, {c1}, {c2}, {k1}, {k2}, {m1}, {m2}, {k1} * {m2} - {k2} * {m1} AS {dp}, "
                f"{k2} * {m1} - {k1} * {m2} AS {dm} FROM ({running}) AS {self._fresh('w')}")
        widest = (f"SELECT {xv}, {c1}, {c2}, {k1}, {k2}, {m1}, {m2}, {dp}, {dm}, max({dp}) OVER () AS {dpx}, "
                  f"max({dm}) OVER () AS {dmx} FROM ({both}) AS {self._fresh('w2')}")

        def at(top: str, e: str) -> str:
            return f"min(CASE WHEN {dp if top == dpx else dm} = {top} THEN {e} END)"

        names = [self._fresh(n) for n in ("n1", "n2", "dpm", "dmm", "tied", "min", "max", "vp", "k1p", "k2p", "vm", "k1m", "k2m")]
        exprs = [f"max({m1})", f"max({m2})", f"max({dp})", f"max({dm})",
                 f"sum(CASE WHEN {c1} + {c2} > 1 THEN 1 ELSE 0 END)", f"min({xv})", f"max({xv})",
                 at(dpx, xv), at(dpx, k1), at(dpx, k2), at(dmx, xv), at(dmx, k1), at(dmx, k2)]
        rows = self._rows(f"SELECT {', '.join(f'{e} AS {n}' for e, n in zip(exprs, names))} FROM ({widest}) AS {self._fresh('w3')}")
        return ks_of(*(rows[0] if rows else [0] * 7))

    def central_moments(self, value: str) -> CentralMoments:
        """A10: the mean and the central moments m₂, m₃, m₄ of the finite values, each the mean
        of (v − mean)^k, taken of the values less the column's centre (as A2's). Two passes in
        one query: the mean, then the powers of the deviations from it, each written as the
        same products on every engine (e·e, e·e·e, (e·e)·(e·e))."""
        x, finite = self._value(value), self._value(value, finite=True)
        centre = self._centre(value)
        centred = self._centred(finite, centre)
        xv, cv, d = self._fresh("xv"), self._fresh("cv"), self._fresh("d")
        s, m = self._fresh("s"), self._fresh("m")
        e = f"({s}.{cv} - {m}.{d})"
        names = [self._fresh(n) for n in ("rows", "n", "finite", "mean", "m2", "m3", "m4", "min", "max")]
        exprs = ["count(*)", f"count({s}.{xv})", f"count({s}.{cv})", f"max({m}.{d})", f"avg({e} * {e})",
                 f"avg({e} * {e} * {e})", f"avg(({e} * {e}) * ({e} * {e}))", f"min({s}.{xv})", f"max({s}.{xv})"]
        rows = self._rows(
            f"SELECT {', '.join(f'{ex} AS {n}' for ex, n in zip(exprs, names))} "
            f"FROM (SELECT {x} AS {xv}, {centred} AS {cv} FROM {self._source}) AS {s} "
            f"CROSS JOIN (SELECT avg({centred}) AS {d} FROM {self._source}) AS {m}"
        )
        return central_moments_of(centre, *(rows[0] if rows else [0] * 9))

    def histogram(self, value: Any, group: str | None, lo: float, hi: float, bins: int) -> list[HistogramBin]:
        """A13: the rows whose `value` falls in each of `bins` equal intervals between `lo`
        and `hi`, for each value of `group` (or over every row without one): the last
        interval holds its upper edge, a NULL, a NaN and what is outside are left out. The
        intervals are the column profile's (`profile.sql_histogram_bin`)."""
        q = self._quote(value) if isinstance(value, str) else self._measure(value)
        b, inside = sql_histogram_bin(self.dialect, q, float(lo), float(hi), int(bins))
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


def pair_moments_of(centre_x: float, centre_y: float, rows: Any, n: Any, mean_x: Any, mean_y: Any, mean_d: Any,
                    var_x: Any, var_y: Any, var_d: Any, cov: Any, *extremes: Any) -> PairMoments:
    """A row of A3 as every source answers it: counts as integers, the rest as floats, no
    variance or covariance of fewer than two pairs."""
    n = int(n or 0)

    def x(v: Any, at_least: int = 1) -> float | None:
        return number(v) if n >= at_least else None

    return PairMoments(int(rows or 0), n, centre_x, centre_y, x(mean_x), x(mean_y), x(mean_d),
                       x(var_x, 2), x(var_y, 2), x(var_d, 2), x(cov, 2), *(x(v) for v in extremes))


def deviations_of(centre: float, level: Any, rows: Any, n: Any, center: Any, mean: Any, var: Any, lo: Any, hi: Any) -> Deviations:
    """A row of A11 as every source answers it: the group's centre put back where it is."""
    n = int(n or 0)
    c = number(center) if n else None
    return Deviations(level, int(rows or 0), n, None if c is None else centre + c, number(mean) if n else None,
                      number(var) if n > 1 else None, number(lo) if n else None, number(hi) if n else None)


def quartiles_of(level: Any, q1: Any, median: Any, q3: Any) -> Quartiles:
    """A row of A12 as every source answers it: floats, and None for a quantile an engine
    could not give (no value: ClickHouse says NaN)."""
    def q(v: Any) -> float | None:
        v = number(v)
        return v if v is not None and math.isfinite(v) else None

    return Quartiles(level, q(q1), q(median), q(q3))


def rank_sums_of(rows: list) -> RankSums:
    """A4's rows as every source answers them: (level, n, rank sum, min, max, ties of the
    values this group counts first)."""
    groups = [RankGroup(r[0], int(r[1] or 0), number(r[2]) or 0.0, number(r[3]), number(r[4])) for r in rows]
    return RankSums(groups, math.fsum(number(r[5]) or 0.0 for r in rows))


def signed_ranks_of(counts: list, ranks: list) -> SignedRanks:
    """A5's two rows as every source answers them: the counts and extremes, then the ranks."""
    rows, pairs, nonfinite, zeros, *extremes = counts
    ranked, w_plus, w_minus, ties = ranks
    return SignedRanks(int(rows or 0), int(pairs or 0), int(nonfinite or 0), int(zeros or 0), int(ranked or 0),
                       number(w_plus) or 0.0, number(w_minus) or 0.0, number(ties) or 0.0, *(number(v) for v in extremes))


def rank_corr_of(n: Any, sxy: Any, sxx: Any, syy: Any, ties_x: Any, ties_y: Any, *extremes: Any) -> RankCorr:
    return RankCorr(int(n or 0), number(sxy) or 0.0, number(sxx) or 0.0, number(syy) or 0.0, number(ties_x) or 0.0,
                    number(ties_y) or 0.0, *(number(v) for v in extremes))


def ks_of(n1: Any, n2: Any, d_plus: Any, d_minus: Any, tied: Any, lo: Any, hi: Any, v_plus: Any = None, k1_plus: Any = None,
          k2_plus: Any = None, v_minus: Any = None, k1_minus: Any = None, k2_minus: Any = None) -> KsDistance:
    def whole(v: Any) -> int | None:
        return None if v is None else int(v)

    return KsDistance(int(n1 or 0), int(n2 or 0), int(d_plus or 0), int(d_minus or 0), int(tied or 0), number(lo), number(hi),
                      number(v_plus), whole(k1_plus), whole(k2_plus), number(v_minus), whole(k1_minus), whole(k2_minus))


def central_moments_of(centre: float, rows: Any, n: Any, finite: Any, mean: Any, m2: Any, m3: Any, m4: Any,
                       lo: Any, hi: Any) -> CentralMoments:
    finite = int(finite or 0)

    def x(v: Any) -> float | None:
        return number(v) if finite else None

    return CentralMoments(int(rows or 0), int(n or 0), finite, centre, x(mean), x(m2), x(m3), x(m4),
                          number(lo) if n else None, number(hi) if n else None)
