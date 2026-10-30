"""Expressions of reports as SQL of DuckDB, with the interpreter's semantics to the letter.

The model works out a report's values row by row only where the rows go on the page; what
runs over every row of a dataset — a table's filter, its sort keys and group keys, a
chart's categories, a calculated field, the values an aggregate takes — is translated here
into an expression of DuckDB over the dataset's columns (app/reports/frames.py). The
translation is exact or it is not made: whatever this module cannot write with the same
result as app/reports/expressions.py raises Untranslatable, and the item it belongs to is
worked out by the interpreter, as before.

What «the same result» asks for:

- Types as Python has them. A number is an int or a float, and the difference shows (an
  int prints without separators, `1234`; a float as `1,234`): every expression carries its
  kind, and where Python's own depends on the row — `IF([amount] < 0, -[amount], 0)` is a
  float or the int 0 — the value is a DOUBLE with a boolean beside it (`isint`) that says,
  row by row, which one Python would have.
- NULL as the interpreter has it: it propagates through arithmetic and comparisons, `&`
  writes it as nothing, a condition that is NULL is false, a division by zero is NULL
  (DuckDB's is infinity), `ROUND` rounds the shortest decimal of the double half away from
  zero (DuckDB's rounds the binary value), `DATEADD` stops at the month's end, `DATEDIFF`
  counts whole days towards zero, `WEEKDAY` is ISO, texts compare by code point.
- What only Python writes the same — a value as the report shows it (`TEXT`, `FORMAT`, a
  number or a date joined with `&`), `UPPER` and `LOWER` (Python's full case mapping:
  «ß» is «SS») — is computed by Python once per distinct value and looked up
  (`Mapper`): a FORMAT of a date over a million rows formats a few hundred dates.
- A part with no field outside an aggregate is a constant — a parameter, a placeholder,
  `TODAY()`, an aggregate of the whole dataset in a filter — and is evaluated once by the
  interpreter itself, then written as a literal.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Callable, Mapping, Protocol

from app.reports import expressions as ex
from app.reports import formats

INT, FLOAT, NUM, TEXT, BOOL, DATE, DATETIME, NULL, LIST = (
    "int", "float", "num", "text", "bool", "date", "datetime", "null", "list")
NUMBERS = (INT, FLOAT, NUM)
DATES = (DATE, DATETIME)
SQL_TYPE = {INT: "BIGINT", FLOAT: "DOUBLE", NUM: "DOUBLE", TEXT: "VARCHAR", BOOL: "BOOLEAN", DATE: "DATE",
            DATETIME: "TIMESTAMP"}
_INT64 = (-2**63, 2**63 - 1)
# Python's whitespace (str.strip): DuckDB's trim takes spaces only
_SPACE = "".join(f"\\x{{{ord(c):x}}}" for c in map(chr, range(0x3001)) if c.isspace())
_TRIM = f"'^[{_SPACE}]+|[{_SPACE}]+$'"
# A value may be written several times in the SQL of the function around it (ROUND five, a CASE
# two), so nested functions multiply the text at every level: past this, the interpreter
MAX_SQL_CHARS = 200_000


class Untranslatable(Exception):
    """The expression cannot be written in SQL with the interpreter's result for certain."""


@dataclass(frozen=True)
class Col:
    """A column of a dataset in DuckDB: its quoted name, its kind, and for a column that
    holds Python ints and floats together the column that says which (NUM)."""

    sql: str
    kind: str
    flag: str | None = None


@dataclass(frozen=True)
class Sql:
    """A translated expression: DuckDB's text, the kind of its values, and for NUM a
    boolean of SQL true where the value is one of Python's ints."""

    text: str
    kind: str
    isint: str = "FALSE"
    values: tuple = ()  # LIST: a parameter of many values, kept as Python's values


def quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def string(text: str) -> str:
    if "\x00" in text:
        raise Untranslatable("a text with a NUL character")
    return "'" + text.replace("'", "''") + "'"


def _isint(s: Sql) -> str:
    return "TRUE" if s.kind == INT else s.isint if s.kind == NUM else "FALSE"


def lit(v: Any) -> Sql:
    """A Python value as a typed literal of DuckDB."""
    if v is None:
        return Sql("NULL", NULL)
    if isinstance(v, bool):
        return Sql("TRUE" if v else "FALSE", BOOL)
    if isinstance(v, int):
        if not _INT64[0] <= v <= _INT64[1]:
            raise Untranslatable("an integer beyond 64 bits")
        return Sql(f"CAST({v} AS BIGINT)", INT)
    if isinstance(v, float):
        if not math.isfinite(v):
            raise Untranslatable("a number that is not finite")
        return Sql(f"CAST('{v!r}' AS DOUBLE)", FLOAT)
    if isinstance(v, str):
        return Sql(string(v), TEXT)
    if isinstance(v, datetime):
        if v.tzinfo is not None:
            raise Untranslatable("a date-time with its zone")
        return Sql(f"TIMESTAMP '{v.isoformat(sep=' ')}'", DATETIME)
    if isinstance(v, date):
        return Sql(f"DATE '{v.isoformat()}'", DATE)
    if isinstance(v, list):
        return Sql("", LIST, values=tuple(v))
    raise Untranslatable(f"a value of type {type(v).__name__}")


def typed_null(kind: str) -> str:
    return f"CAST(NULL AS {SQL_TYPE[kind]})" if kind in SQL_TYPE else "NULL"


class Mapper(Protocol):
    """Where a value only Python computes the same is looked up: `fn` applied to every
    distinct value of `arg` over the dataset, the SQL that reads the result for a row."""

    def lookup(self, key: str, fn: Callable[[Any], Any], arg: Sql, out: str) -> str: ...


class Translator:
    """One expression over the columns `cols` of a dataset, as the interpreter evaluates it
    with a row (`ctx.row` set): `rownumber` is the SQL of ROWNUMBER() there (None: it is
    NULL), `constant` evaluates a part that does not change from row to row with the
    interpreter, `mapper` looks up what only Python computes."""

    def __init__(self, cols: Mapping[str, Col], loc: formats.Locale, constant: Callable[[ex.Node], Any],
                 mapper: Mapper | None, rownumber: str | None = None, single: bool = False,
                 datasets: frozenset[str] = frozenset()):
        self.cols = cols
        self.loc = loc
        self.datasets = datasets  # the report's datasets: an aggregate may name one
        self.constant = constant
        self.mapper = mapper
        self.rownumber = rownumber
        self.single = single  # inside PERCENT: an aggregate is over the one row

    def __call__(self, node: ex.Node) -> Sql:
        out = self.translate(node)
        if len(out.text) + len(out.isint) > MAX_SQL_CHARS:
            raise Untranslatable("an expression that takes too much SQL to write")
        return out

    def translate(self, node: ex.Node) -> Sql:
        if not self.per_row(node):
            try:
                value = self.constant(node)
            except ex.EvalError as e:
                raise Untranslatable(str(e)) from None
            return lit(value)
        if isinstance(node, ex.Field):
            col = self.cols.get(node.name)
            if col is None:
                return lit(None)
            return Sql(col.sql, col.kind, col.flag or ("TRUE" if col.kind == INT else "FALSE"))
        if isinstance(node, ex.Unary):
            return self.unary(node)
        if isinstance(node, ex.Binary):
            return self.binary(node)
        assert isinstance(node, ex.Call)
        return self.call(node)

    def per_row(self, node: ex.Node) -> bool:
        """True when the value can change from one row to the next."""
        if isinstance(node, ex.Field):
            return True
        if isinstance(node, ex.Call):
            if node.name in ex.AGGREGATES:  # over a scope: one value, or the one row's inside PERCENT
                return self.single and ex.scope_dataset(node, self.datasets) is None
            if node.name == "RUNNING":
                return False  # with a row and no running set it is the aggregate of the scope
            if node.name == "PERCENT":
                return True
            if node.name == "ROWNUMBER":
                return self.rownumber is not None
            return any(self.per_row(a) for a in node.args)
        if isinstance(node, ex.Unary):
            return self.per_row(node.arg)
        if isinstance(node, ex.Binary):
            return self.per_row(node.left) or self.per_row(node.right)
        return False

    # ── operators ──
    def unary(self, node: ex.Unary) -> Sql:
        a = self(node.arg)
        if node.op == "NOT":
            if a.kind not in (BOOL, NULL):
                raise Untranslatable("NOT of a value that is not a condition")
            return Sql(f"(NOT {a.text})", BOOL) if a.kind == BOOL else lit(None)
        if a.kind == NULL:
            return a
        if a.kind not in NUMBERS:
            raise Untranslatable("a minus sign before a value that is not a number")
        return Sql(f"(-{a.text})", a.kind, a.isint)

    def binary(self, node: ex.Binary) -> Sql:
        op = node.op
        a, b = self(node.left), self(node.right)
        if op in ("AND", "OR"):
            for x in (a, b):
                if x.kind not in (BOOL, NULL):
                    raise Untranslatable(f"{op} of a value that is not a condition")
            return Sql(f"({_bool(a)} {op} {_bool(b)})", BOOL)
        if op == "&":
            return Sql(f"({self.text(a)} || {self.text(b)})", TEXT)
        if op in ("=", "<>", "<", ">", "<=", ">="):
            return self.compare(op, a, b)
        if NULL in (a.kind, b.kind):
            if LIST in (a.kind, b.kind):
                raise Untranslatable("arithmetic with a parameter of many values")
            return lit(None)
        if a.kind in DATES or b.kind in DATES:
            return self.dates(op, a, b)
        if a.kind not in NUMBERS or b.kind not in NUMBERS:
            raise Untranslatable(f"«{op}» of values that are not numbers")
        if op == "/":
            return Sql(f"(CASE WHEN {b.text} = 0 THEN NULL ELSE CAST({a.text} AS DOUBLE) / {b.text} END)", FLOAT)
        if a.kind == INT and b.kind == INT:
            return Sql(f"({a.text} {op} {b.text})", INT)
        if NUM in (a.kind, b.kind):
            return Sql(f"(CAST({a.text} AS DOUBLE) {op} CAST({b.text} AS DOUBLE))", NUM,
                       f"({_isint(a)} AND {_isint(b)})")
        return Sql(f"(CAST({a.text} AS DOUBLE) {op} CAST({b.text} AS DOUBLE))", FLOAT)

    def dates(self, op: str, a: Sql, b: Sql) -> Sql:
        if op == "+" and (a.kind in DATES) != (b.kind in DATES):
            d, n = (a, b) if a.kind in DATES else (b, a)
            return self.move_days(d, n, negate=False)
        if op == "-" and a.kind in DATES and b.kind in NUMBERS:
            return self.move_days(a, b, negate=True)
        if op == "-" and a.kind in DATES and b.kind in DATES:
            if a.kind == DATE and b.kind == DATE:
                return Sql(f"CAST({a.text} - {b.text} AS BIGINT)", INT)
            # timedelta.total_seconds() / 86400: microseconds over 10**6, then over a day
            return Sql(f"((CAST(epoch_us(CAST({a.text} AS TIMESTAMP)) - epoch_us(CAST({b.text} AS TIMESTAMP)) "
                       f"AS DOUBLE) / 1000000.0) / 86400.0)", FLOAT)
        raise Untranslatable(f"«{op}» between a date and a value that does not fit")

    def move_days(self, d: Sql, n: Sql, negate: bool) -> Sql:
        """date ± timedelta(days=n): a date moves by the timedelta's days — n rounded down,
        and subtracted as such (date - timedelta takes -td.days); a date-time by an int only
        (a fraction would be microseconds)."""
        if n.kind == INT:
            days = n.text
        elif n.kind == FLOAT and d.kind == DATE:
            days = f"CAST(floor({n.text}) AS BIGINT)"
        else:
            raise Untranslatable("a date-time moved by a fraction of a day")
        if negate:
            days = f"(-{days})"
        if d.kind == DATE:
            return in_range(Sql(f"CAST({d.text} + to_days(CAST({days} AS INTEGER)) AS DATE)", DATE))
        return in_range(Sql(f"({d.text} + to_days(CAST({days} AS INTEGER)))", DATETIME))

    def compare(self, op: str, a: Sql, b: Sql) -> Sql:
        if LIST in (a.kind, b.kind):
            if a.kind == LIST and b.kind == LIST:  # pragma: no cover — both constants, folded
                raise Untranslatable("two parameters of many values")
            values, x = (b.values, a) if b.kind == LIST else (a.values, b)
            if op not in ("=", "<>"):
                return lit(None)
            if x.kind == NULL:
                return lit(None)
            members = [lit(v).text for v in values if _same_kind(x.kind, v)]
            found = f"({x.text} IN ({', '.join(members)}))" if members else \
                f"(CASE WHEN {x.text} IS NULL THEN NULL ELSE FALSE END)"
            return Sql(found if op == "=" else f"(NOT {found})", BOOL)
        if NULL in (a.kind, b.kind):
            return Sql(typed_null(BOOL), BOOL)
        fam = {INT: "n", FLOAT: "n", NUM: "n", DATE: "d", DATETIME: "d", TEXT: "t", BOOL: "b"}
        if fam.get(a.kind) != fam.get(b.kind):
            raise Untranslatable(f"comparing a {a.kind} with a {b.kind}")
        return Sql(f"({a.text} {op} {b.text})", BOOL)

    # ── texts ──
    def text(self, s: Sql) -> str:
        """A value as `&` joins it: the report's way of writing it, NULL as nothing."""
        if s.kind == TEXT:
            return f"coalesce({s.text}, '')"
        if s.kind == NULL:
            return "''"
        if s.kind == INT:  # formats.display: an int with no separators, as a spreadsheet's General
            return f"coalesce(CAST({s.text} AS VARCHAR), '')"
        if s.kind == BOOL:
            return (f"(CASE WHEN {s.text} THEN {string(self.loc.texts['true'])} WHEN NOT {s.text} THEN "
                    f"{string(self.loc.texts['false'])} ELSE '' END)")
        if s.kind in (FLOAT, NUM, DATE, DATETIME):
            return f"coalesce({self.shown(s, None)}, '')"
        raise Untranslatable(f"a {s.kind} joined to a text")

    def shown(self, s: Sql, pattern: str | None) -> str:
        """formats.display of every value: computed by Python, once per distinct value."""
        if self.mapper is None:
            raise Untranslatable("no place to compute what only Python writes")
        loc = self.loc
        return self.mapper.lookup(f"display:{loc.code}:{pattern!r}", lambda v: formats.display(v, pattern, loc),
                                  s, "VARCHAR")

    # ── functions ──
    def call(self, node: ex.Call) -> Sql:
        name = node.name
        if name in ex.AGGREGATES:
            return self.single_row_aggregate(node)
        if name == "PERCENT":
            return self.percent(node)
        if name == "ROWNUMBER":
            return Sql(f"CAST({self.rownumber} AS BIGINT)", INT)
        if name in ("PAGE", "PAGES"):
            raise Untranslatable("page numbers")
        if name == "IF":
            cond = self.condition(node.args[0])
            parts = [self(a) for a in node.args[1:]] + ([] if len(node.args) > 2 else [lit(None)])
            return self.case([(cond, parts[0])], parts[1])
        if name == "IFS":
            args = node.args
            pairs = [(self.condition(args[i]), self(args[i + 1])) for i in range(0, len(args) - 1, 2)]
            return self.case(pairs, self(args[-1]) if len(args) % 2 else lit(None))
        if name == "COALESCE":
            return self.coalesce([self(a) for a in node.args])
        args = [self(a) for a in node.args]
        if name == "ISNULL":
            return Sql(f"({args[0].text} IS NULL)", BOOL) if args[0].kind != NULL else lit(True)
        if name == "TEXT":
            return Sql(self.text(args[0]), TEXT)
        if name == "CONCAT":
            return Sql("(" + " || ".join(self.text(a) for a in args) + ")", TEXT)
        if name == "FORMAT":
            pattern = self.constant_of(node.args[1])
            pattern = pattern if isinstance(pattern, str) else None
            x = args[0]
            if x.kind == NULL:
                return lit("")
            if x.kind == TEXT and pattern is not None:  # a text prints as itself, whatever the pattern
                return Sql(f"coalesce({x.text}, '')", TEXT)
            if x.kind in (TEXT, INT, BOOL) and pattern is None:
                return Sql(self.text(x), TEXT)
            if x.kind not in (INT, FLOAT, NUM, DATE, DATETIME, BOOL):
                raise Untranslatable(f"FORMAT of a {x.kind}")
            return Sql(f"coalesce({self.shown(x, pattern)}, '')", TEXT)
        if name in ("DATEADD", "DATEDIFF"):
            return self.date_function(name, node, args)
        if any(a.kind == NULL for a in args):
            return lit(None)
        if name == "ROUND":
            return self.round(node, args)
        if name == "ABS":
            x = self.number(args[0])
            return Sql(f"abs({x.text})", x.kind, x.isint)
        if name in ("FLOOR", "CEILING"):
            x = self.number(args[0])
            if x.kind == INT:
                return x
            return Sql(f"CAST({'floor' if name == 'FLOOR' else 'ceil'}({x.text}) AS BIGINT)", INT)
        if name == "POWER":
            return self.power(node, args)
        if name in ("UPPER", "LOWER"):
            t = self.texts(args[0])
            if self.mapper is None:
                raise Untranslatable("no place to compute what only Python writes")
            fn = str.upper if name == "UPPER" else str.lower
            return Sql(self.mapper.lookup(name, fn, t, "VARCHAR"), TEXT)
        if name == "TRIM":
            return Sql(f"regexp_replace({self.texts(args[0]).text}, {_TRIM}, '', 'g')", TEXT)
        if name == "LEN":
            return Sql(f"CAST(length({self.texts(args[0]).text}) AS BIGINT)", INT)
        if name in ("LEFT", "RIGHT"):
            n = self.whole(args[1])
            return Sql(f"{name.lower()}({self.texts(args[0]).text}, {_at_least(n, 0)})", TEXT)
        if name == "MID":
            start, count = self.whole(args[1]), self.whole(args[2])
            return Sql(f"substring({self.texts(args[0]).text}, {_at_least(start, 1)}, {_at_least(count, 0)})", TEXT)
        if name == "REPLACE":
            t, old, new = (self.texts(a) for a in args)
            return Sql(f"(CASE WHEN {new.text} IS NULL THEN NULL WHEN {old.text} = '' THEN {t.text} "
                       f"ELSE replace({t.text}, {old.text}, {new.text}) END)", TEXT)
        if name in ("CONTAINS", "STARTSWITH"):
            t, part = self.texts(args[0]), self.texts(args[1])
            fn = "contains" if name == "CONTAINS" else "starts_with"
            return Sql(f"{fn}({t.text}, {part.text})", BOOL)
        if name == "DATE":
            y, m, d = (self.whole(a) for a in args)
            leap = f"({y} % 4 = 0 AND ({y} % 100 <> 0 OR {y} % 400 = 0))"
            last = (f"(CASE WHEN {m} = 2 THEN (CASE WHEN {leap} THEN 29 ELSE 28 END) "
                    f"WHEN {m} IN (4, 6, 9, 11) THEN 30 ELSE 31 END)")
            valid = f"({y} BETWEEN 1 AND 9999 AND {m} BETWEEN 1 AND 12 AND {d} >= 1 AND {d} <= {last})"
            return Sql(f"(CASE WHEN {valid} THEN make_date({y}, {m}, {d}) END)", DATE)
        if name in ("YEAR", "MONTH", "DAY", "WEEKDAY"):
            d = args[0]
            if d.kind not in DATES:
                raise Untranslatable(f"{name} of a {d.kind}")
            fn = {"YEAR": "year", "MONTH": "month", "DAY": "day", "WEEKDAY": "isodow"}[name]
            return Sql(f"CAST({fn}({d.text}) AS BIGINT)", INT)
        raise Untranslatable(f"the function {name}")

    def condition(self, node: ex.Node) -> str:
        c = self(node)
        if c.kind not in (BOOL, NULL):
            raise Untranslatable("a condition that is not true or false")
        return _bool(c)

    def case(self, pairs: list[tuple[str, Sql]], other: Sql) -> Sql:
        kind = _unify([v for _, v in pairs] + [other])
        if kind == NULL:
            return lit(None)
        value = "(CASE " + " ".join(f"WHEN {c} THEN {_as(v, kind)}" for c, v in pairs) + \
            f" ELSE {_as(other, kind)} END)"
        if kind != NUM:
            return Sql(value, kind)
        isint = "(CASE " + " ".join(f"WHEN {c} THEN {_isint(v)}" for c, v in pairs) + f" ELSE {_isint(other)} END)"
        return Sql(value, NUM, isint)

    def coalesce(self, parts: list[Sql]) -> Sql:
        kind = _unify(parts)
        parts = [p for p in parts if p.kind != NULL]
        if not parts:
            return lit(None)
        value = "coalesce(" + ", ".join(_as(p, kind) for p in parts) + ")"
        if kind != NUM:
            return Sql(value, kind)
        isint = "(CASE " + " ".join(f"WHEN {p.text} IS NOT NULL THEN {_isint(p)}" for p in parts) + " ELSE FALSE END)"
        return Sql(value, NUM, isint)

    def number(self, s: Sql) -> Sql:
        if s.kind not in NUMBERS:
            raise Untranslatable(f"a {s.kind} where a number is wanted")
        return s

    def whole(self, s: Sql) -> str:
        """int(x) of a number: towards zero."""
        s = self.number(s)
        return s.text if s.kind == INT else f"CAST(trunc({s.text}) AS BIGINT)"

    def texts(self, s: Sql) -> Sql:
        if s.kind != TEXT:
            raise Untranslatable(f"a {s.kind} where a text is wanted")
        return s

    def constant_of(self, node: ex.Node) -> Any:
        if self.per_row(node):
            raise Untranslatable("an argument that changes from row to row")
        try:
            return self.constant(node)
        except ex.EvalError as e:
            raise Untranslatable(str(e)) from None

    def round(self, node: ex.Call, args: list[Sql]) -> Sql:
        x = self.number(args[0])
        try:
            n = int(self.constant_of(node.args[1])) if len(args) > 1 else 0
        except (TypeError, ValueError, OverflowError):
            raise Untranslatable("ROUND to a number of decimals that is not a number") from None
        if n < 0:
            raise Untranslatable("ROUND to tens, hundreds…")
        if x.kind == INT:
            return x if n == 0 else Sql(f"CAST({x.text} AS DOUBLE)", FLOAT)
        # Decimal(repr(x)).quantize(…, ROUND_HALF_UP): DuckDB writes a double as its
        # shortest decimal too, and a decimal cast rounds half away from zero; a value
        # that rounds to zero keeps its sign (Decimal's -0.00)
        v = f"CAST({x.text} AS DOUBLE)"
        rounded = f"CAST(CAST({v} AS VARCHAR) AS DECIMAL(38, {n}))"
        if n == 0:
            return Sql(f"(CASE WHEN abs({v}) < 0.5 THEN CAST(0 AS BIGINT) ELSE CAST({rounded} AS BIGINT) END)", INT)
        zero = f"CAST(CASE WHEN signbit({v}) THEN '-0.0' ELSE '0.0' END AS DOUBLE)"
        return Sql(f"(CASE WHEN abs({v}) < CAST('0.5e-{n}' AS DOUBLE) THEN {zero} "
                   f"ELSE CAST(CAST({rounded} AS VARCHAR) AS DOUBLE) END)", FLOAT)

    def power(self, node: ex.Call, args: list[Sql]) -> Sql:
        a, b = self.number(args[0]), self.number(args[1])
        if a.kind == NUM or b.kind == NUM:
            raise Untranslatable("POWER of a value that is an int or a float row by row")
        if a.kind == INT and b.kind == INT:
            # int ** int is an int when the exponent is not negative: a product, which
            # DuckDB checks for overflow; with an exponent that changes row by row the type does
            e = self.constant_of(node.args[1])
            if not isinstance(e, int) or e > 16:
                raise Untranslatable("POWER of an int to an exponent that is not a small constant")
            if e > 0:
                return Sql("(" + " * ".join([a.text] * e) + ")", INT)
            if e == 0:  # NULL stays NULL: not the 1 of anything else to the zero
                return Sql(f"(CASE WHEN {a.text} IS NOT NULL THEN CAST(1 AS BIGINT) END)", INT)
        p = f"pow(CAST({a.text} AS DOUBLE), CAST({b.text} AS DOUBLE))"
        # overflow, 0 to a negative power, a negative number to a fraction: None in Python
        return Sql(f"(CASE WHEN isfinite({p}) THEN {p} END)", FLOAT)

    def date_function(self, name: str, node: ex.Call, args: list[Sql]) -> Sql:
        unit = self.constant_of(node.args[0])
        if unit not in ("d", "w", "m", "y"):
            raise Untranslatable("a unit of dates that is not d, w, m or y")
        if any(a.kind == NULL for a in args[1:]):
            return lit(None)
        if name == "DATEADD":
            n, d = self.whole(args[1]), args[2]
            if d.kind not in DATES:
                raise Untranslatable("DATEADD of a value that is not a date")
            step = {"d": f"to_days(CAST({n} AS INTEGER))", "w": f"to_days(CAST({n} * 7 AS INTEGER))",
                    "m": f"to_months(CAST({n} AS INTEGER))", "y": f"to_months(CAST({n} * 12 AS INTEGER))"}[unit]
            if d.kind == DATE:
                return in_range(Sql(f"CAST({d.text} + {step} AS DATE)", DATE))
            return in_range(Sql(f"({d.text} + {step})", DATETIME))
        a, b = args[1], args[2]
        if a.kind not in DATES or b.kind not in DATES:
            raise Untranslatable("DATEDIFF of a value that is not a date")
        if unit in ("d", "w"):
            if a.kind == DATE and b.kind == DATE:
                days = f"CAST({b.text} - {a.text} AS BIGINT)"
            else:  # timedelta / timedelta(days=1): microseconds over a day's, then towards zero
                micros = f"epoch_us(CAST({b.text} AS TIMESTAMP)) - epoch_us(CAST({a.text} AS TIMESTAMP))"
                days = f"CAST(trunc(CAST({micros} AS DOUBLE) / 86400000000.0) AS BIGINT)"
            if unit == "d":
                return Sql(days, INT)
            return Sql(f"CAST(trunc({days} / 7.0) AS BIGINT)", INT)
        months = f"((year({b.text}) - year({a.text})) * 12 + month({b.text}) - month({a.text}))"
        months = (f"(CASE WHEN {months} > 0 AND day({b.text}) < day({a.text}) THEN {months} - 1 "
                  f"WHEN {months} < 0 AND day({b.text}) > day({a.text}) THEN {months} + 1 ELSE {months} END)")
        if unit == "m":
            return Sql(f"CAST({months} AS BIGINT)", INT)
        return Sql(f"CAST(trunc({months} / 12.0) AS BIGINT)", INT)

    # ── aggregates where there is a row ──
    def percent(self, node: ex.Call) -> Sql:
        """PERCENT(x) with a row: x over the row alone, over x of the scope (a constant)."""
        inner = node.args[0]
        num = replace_(self, single=True)(inner)
        den = self.constant_of_scope(inner)
        if num.kind == NULL or den is None:
            return lit(None)
        if num.kind not in NUMBERS or not ex._is_num(den):
            raise Untranslatable("PERCENT of a value that is not a number")
        if den == 0:
            return lit(None)
        return Sql(f"(CAST({num.text} AS DOUBLE) / {lit(den).text})", FLOAT)

    def constant_of_scope(self, node: ex.Node) -> Any:
        try:
            return self.constant(node)
        except ex.EvalError as e:
            raise Untranslatable(str(e)) from None

    def single_row_aggregate(self, node: ex.Call) -> Sql:
        """An aggregate over a scope of one row (PERCENT's own value): `_aggregate` of the
        row's value alone."""
        if not self.single:  # pragma: no cover — an aggregate of a scope is a constant, folded
            raise Untranslatable("an aggregate where a row is")
        args = node.args
        if not args:
            return lit(1)
        x = replace_(self, single=False)(args[0])
        name = node.name
        if name in ("COUNT", "COUNTDISTINCT"):
            return Sql(f"(CASE WHEN {x.text} IS NULL THEN 0 ELSE 1 END)", INT) if x.kind != NULL else lit(0)
        # sum() starts from the int 0: 0 + -0.0 is 0.0
        if name == "AVG":
            if x.kind == NULL:
                return lit(None)
            return Sql(f"(CAST({self.number(x).text} AS DOUBLE) + CAST(0.0 AS DOUBLE))", FLOAT)
        if name == "SUM":
            if x.kind == NULL:
                return x
            x = self.number(x)
            return x if x.kind == INT else Sql(f"({x.text} + CAST(0.0 AS DOUBLE))", x.kind, x.isint)
        return x


def replace_(t: Translator, **changes: Any) -> Translator:
    out = Translator(t.cols, t.loc, t.constant, t.mapper, t.rownumber, t.single, t.datasets)
    for k, v in changes.items():
        setattr(out, k, v)
    return out


def _at_least(n: str, low: int) -> str:
    """max(n, low), NULL staying NULL (DuckDB's greatest skips it)."""
    return f"(CASE WHEN {n} < {low} THEN {low} ELSE {n} END)"


def in_range(s: Sql) -> Sql:
    """A date Python cannot hold (before year 1, after 9999) stops the query: the item is
    then worked out by the interpreter, which says why."""
    low, high = ("DATE '0001-01-01'", "DATE '9999-12-31'") if s.kind == DATE else \
        ("TIMESTAMP '0001-01-01 00:00:00'", "TIMESTAMP '9999-12-31 23:59:59.999999'")
    return Sql(f"(CASE WHEN {s.text} BETWEEN {low} AND {high} OR {s.text} IS NULL THEN {s.text} "
               f"ELSE error('a date out of range') END)", s.kind)


def _bool(s: Sql) -> str:
    return s.text if s.kind == BOOL else typed_null(BOOL)


def _as(s: Sql, kind: str) -> str:
    if s.kind == NULL:
        return typed_null(kind)
    return f"CAST({s.text} AS DOUBLE)" if kind == NUM else s.text


def _unify(parts: list[Sql]) -> str:
    kinds = {p.kind for p in parts} - {NULL}
    if not kinds:
        return NULL
    if len(kinds) == 1:
        kind = kinds.pop()
        if kind == LIST:
            raise Untranslatable("a parameter of many values as a result")
        return kind
    if kinds <= set(NUMBERS):
        return NUM
    raise Untranslatable("results of different kinds")


def _same_kind(kind: str, v: Any) -> bool:
    """expressions.compare_equal: values of different kinds are different."""
    if kind in NUMBERS:
        return ex._is_num(v)
    if kind == TEXT:
        return isinstance(v, str)
    if kind == BOOL:
        return isinstance(v, bool)
    if kind in DATES:
        return isinstance(v, date)
    return False


def kind_of_value(v: Any) -> str:
    if v is None:
        return NULL
    if isinstance(v, bool):
        return BOOL
    if isinstance(v, int):
        return INT
    if isinstance(v, float):
        return FLOAT
    if isinstance(v, str):
        return TEXT
    if isinstance(v, datetime):
        return DATETIME
    if isinstance(v, date):
        return DATE
    return "other"

