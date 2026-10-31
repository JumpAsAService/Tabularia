"""The expression language of reports: a closed grammar of Tabularia's own.

Spreadsheet-like on purpose, a copy of nothing (docs/specs/reports.md, «Expressions»):

    expr    := or ;  or := and ("OR" and)* ; and := not ("AND" not)* ; not := "NOT" not | cmp
    cmp     := concat (("=" | "<>" | "<" | ">" | "<=" | ">=") concat)?
    concat  := add ("&" add)* ;  add := mul (("+" | "-") mul)* ;  mul := unary (("*" | "/") unary)*
    unary   := "-" unary | primary
    primary := number | "text" | TRUE | FALSE | NULL | [field] | {placeholder} | NAME "(" args ")" | "(" expr ")"

A recursive-descent parser builds a tree, `check` walks it once with what the editor
knows (the fields of the datasets, the parameters, where the expression sits) and names
the first mistake with its position, and `evaluate` walks it again for every row. Nothing
a person writes is ever executed: a name that is not in FUNCTIONS is an error, a field
is a key of the row, a placeholder goes through app/core/placeholders.py.

Semantics:
- NULL propagates through arithmetic and comparisons, aggregates skip it, a division by
  zero is NULL; `&` writes NULL as nothing, as a spreadsheet does; a condition that is
  NULL is false.
- An aggregate (SUM AVG MIN MAX COUNT COUNTDISTINCT FIRST LAST, and SUMIF(c, x) and
  COUNTIF(c), which are SUM(IF(c, x)) and COUNT(IF(c, 1))) works on the rows of the scope it
  sits in — the group, the table, the dataset of the item — or on what its last argument
  names: a whole dataset (`SUM([amount], "orders")`) or a group of the table around it
  (`SUM([amount], "region")`). A field outside a row (a group's header or footer) is the
  value of the scope's first row.
- `PERCENT(x)`: x over the same x one level up (a detail row over its group, a group
  over its parent); `PERCENT(x, "region")`: over x of that group. `RUNNING(SUM(x))`: from
  the first row of the group to this one. `PREVIOUS(x)`: x on the row above in the same
  group, or in a group's total on the group before it under the same parent; NULL on the
  first.
- `LOOKUP(key, key field, result[, "dataset"])`: the result on the rows of a whole dataset
  whose key field equals the key, as anywhere with a set of rows and no current row: an
  aggregate over them, a field from the first of them. Each is worked out once per key,
  never row by row; a missing key is NULL, and so is a NULL one.
- `PAGE()` and `PAGES()` are known only when the pages are laid out: they can only be
  written as text (`"Page " & PAGE()`), and the template fills them in.
"""
from __future__ import annotations

import difflib
import math
import re
from calendar import monthrange
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from typing import Any, Callable, Iterable

from app.core import placeholders as ph
from app.reports import formats

MAX_EXPRESSION_CHARS = 4000
MAX_DEPTH = 64
MAX_TREE_DEPTH = 200  # a chain of operators is a tree this deep: the walks below are recursive
MAX_TEXT_CHARS = 1_000_000  # the longest text REPLACE makes: nested, it multiplies a text at every level
PLACEHOLDERS = (*ph.DATE_BASES, "now")  # the placeholders an expression may write


class ExpressionError(ValueError):
    """A mistake in an expression: `pos` is the offset of the part that is wrong (0 is the
    first character), `name` the field or function it is about."""

    def __init__(self, message: str, pos: int = 0, length: int = 0, name: str | None = None):
        super().__init__(message)
        self.message = message
        self.pos = pos
        self.length = length
        self.name = name

    def shifted(self, offset: int) -> "ExpressionError":
        return ExpressionError(self.message, self.pos + offset, self.length, self.name)


# ── values the template completes ────────────────────────────────────────────
class PageNumber:
    """PAGE() or PAGES(): written by the template, which alone knows the pages."""

    __slots__ = ("kind",)

    def __init__(self, kind: str):
        self.kind = kind

    def __repr__(self) -> str:
        return f"{self.kind.upper()}()"


PAGE, PAGES = PageNumber("page"), PageNumber("pages")


class PageText(tuple):
    """Text with page numbers inside: parts that are str or PageNumber."""


# ── the tree ─────────────────────────────────────────────────────────────────
@dataclass(frozen=True, slots=True, eq=False)
class Node:
    pos: int
    end: int


@dataclass(frozen=True, slots=True, eq=False)
class Lit(Node):
    value: Any


@dataclass(frozen=True, slots=True, eq=False)
class Field(Node):
    name: str


@dataclass(frozen=True, slots=True, eq=False)
class Placeholder(Node):
    raw: str


@dataclass(frozen=True, slots=True, eq=False)
class Call(Node):
    name: str
    args: tuple[Node, ...]


@dataclass(frozen=True, slots=True, eq=False)
class Unary(Node):
    op: str
    arg: Node


@dataclass(frozen=True, slots=True, eq=False)
class Binary(Node):
    op: str
    left: Node
    right: Node


# ── tokens ───────────────────────────────────────────────────────────────────
@dataclass(frozen=True, slots=True)
class Token:
    kind: str  # num str field ph name op ( ) , end
    value: Any
    pos: int
    end: int


_NUMBER = re.compile(r"(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?")
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_OPERATORS = ("<=", ">=", "<>", "=", "<", ">", "&", "+", "-", "*", "/")
_HINTS = {"!=": "write <> for «not equal»", "==": "write = to compare", "&&": "write AND", "||": "write OR",
          "^": "use POWER(x, y)", "%": "divide by 100, or use a format such as 0%", "'": 'texts go in double quotes: "text"'}


def tokens(text: str) -> list[Token]:
    out: list[Token] = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c.isspace():
            i += 1
            continue
        if c.isdigit() or (c == "." and i + 1 < n and text[i + 1].isdigit()):
            m = _NUMBER.match(text, i)
            raw = m.group(0)  # type: ignore[union-attr]
            value: Any = float(raw) if any(x in raw for x in ".eE") else int(raw)
            out.append(Token("num", value, i, m.end()))  # type: ignore[union-attr]
            i = m.end()  # type: ignore[union-attr]
            continue
        if c == '"':
            j, buf = i + 1, []
            while True:
                if j >= n:
                    raise ExpressionError('a text is not closed: add " at its end', i, 1)
                if text[j] == '"':
                    if j + 1 < n and text[j + 1] == '"':
                        buf.append('"')
                        j += 2
                        continue
                    break
                buf.append(text[j])
                j += 1
            out.append(Token("str", "".join(buf), i, j + 1))
            i = j + 1
            continue
        if c == "[":
            j, buf = i + 1, []
            while True:
                if j >= n:
                    raise ExpressionError("a field is not closed: add ] at its end", i, 1)
                if text[j] == "]":
                    if j + 1 < n and text[j + 1] == "]":
                        buf.append("]")
                        j += 2
                        continue
                    break
                buf.append(text[j])
                j += 1
            name = "".join(buf)
            if not name.strip():
                raise ExpressionError("an empty field []: write the name of a column inside the brackets", i, 2)
            out.append(Token("field", name, i, j + 1))
            i = j + 1
            continue
        if c == "{":
            j = text.find("}", i + 1)
            if j < 0:
                raise ExpressionError("a placeholder is not closed: add } at its end, as in {today-7}", i, 1)
            out.append(Token("ph", text[i + 1:j], i, j + 1))
            i = j + 1
            continue
        m = _NAME.match(text, i)
        if m:
            out.append(Token("name", m.group(0), i, m.end()))
            i = m.end()
            continue
        for hint, why in _HINTS.items():
            if text.startswith(hint, i):
                raise ExpressionError(f"«{hint}» is not part of the language: {why}", i, len(hint))
        op = next((o for o in _OPERATORS if text.startswith(o, i)), None)
        if op:
            out.append(Token("op", op, i, i + len(op)))
            i += len(op)
            continue
        if c in "(),":
            out.append(Token(c, c, i, i + 1))
            i += 1
            continue
        raise ExpressionError(f"unexpected character «{c}»", i, 1)
    out.append(Token("end", None, n, n))
    return out


# ── the parser ───────────────────────────────────────────────────────────────
_KEYWORDS = {"AND", "OR", "NOT", "TRUE", "FALSE", "NULL"}
_COMPARE = {"=", "<>", "<", ">", "<=", ">="}


class _Parser:
    def __init__(self, text: str):
        self.text = text
        self.toks = tokens(text)
        self.i = 0
        self.depth = 0

    @property
    def tok(self) -> Token:
        return self.toks[self.i]

    def take(self) -> Token:
        t = self.toks[self.i]
        self.i += 1
        return t

    def keyword(self, word: str) -> bool:
        t = self.tok
        return t.kind == "name" and t.value.upper() == word

    def parse(self) -> Node:
        if self.tok.kind == "end":
            raise ExpressionError("the expression is empty", 0, 0)
        node = self.or_()
        t = self.tok
        if t.kind != "end":
            what = "«)» without its «(»" if t.kind == ")" else f"«{self.text[t.pos:t.end]}»"
            raise ExpressionError(f"unexpected {what}: an operator (+ - * / & = AND OR) is missing before it?",
                                  t.pos, t.end - t.pos)
        return node

    def _nested(self, fn: Callable[[], Node]) -> Node:
        self.depth += 1
        if self.depth > MAX_DEPTH:
            raise ExpressionError("the expression is nested too deeply", self.tok.pos, 1)
        try:
            return fn()
        finally:
            self.depth -= 1

    def or_(self) -> Node:
        left = self.and_()
        while self.keyword("OR"):
            self.take()
            right = self.and_()
            left = Binary(left.pos, right.end, "OR", left, right)
        return left

    def and_(self) -> Node:
        left = self.not_()
        while self.keyword("AND"):
            self.take()
            right = self.not_()
            left = Binary(left.pos, right.end, "AND", left, right)
        return left

    def not_(self) -> Node:
        if self.keyword("NOT"):
            t = self.take()
            arg = self._nested(self.not_)
            return Unary(t.pos, arg.end, "NOT", arg)
        return self.cmp()

    def cmp(self) -> Node:
        left = self.concat()
        if self.tok.kind == "op" and self.tok.value in _COMPARE:
            op = self.take().value
            right = self.concat()
            if self.tok.kind == "op" and self.tok.value in _COMPARE:
                t = self.tok
                raise ExpressionError("one comparison at a time: join two with AND, as in [a] > 1 AND [a] < 9",
                                      t.pos, t.end - t.pos)
            return Binary(left.pos, right.end, op, left, right)
        return left

    def concat(self) -> Node:
        left = self.add()
        while self.tok.kind == "op" and self.tok.value == "&":
            self.take()
            right = self.add()
            left = Binary(left.pos, right.end, "&", left, right)
        return left

    def add(self) -> Node:
        left = self.mul()
        while self.tok.kind == "op" and self.tok.value in ("+", "-"):
            op = self.take().value
            right = self.mul()
            left = Binary(left.pos, right.end, op, left, right)
        return left

    def mul(self) -> Node:
        left = self.unary()
        while self.tok.kind == "op" and self.tok.value in ("*", "/"):
            op = self.take().value
            right = self.unary()
            left = Binary(left.pos, right.end, op, left, right)
        return left

    def unary(self) -> Node:
        if self.tok.kind == "op" and self.tok.value == "-":
            t = self.take()
            arg = self._nested(self.unary)
            if isinstance(arg, Lit) and isinstance(arg.value, (int, float)) and not isinstance(arg.value, bool):
                return Lit(t.pos, arg.end, -arg.value)
            return Unary(t.pos, arg.end, "-", arg)
        return self.primary()

    def primary(self) -> Node:
        t = self.tok
        if t.kind in ("num", "str"):
            self.take()
            return Lit(t.pos, t.end, t.value)
        if t.kind == "field":
            self.take()
            return Field(t.pos, t.end, t.value)
        if t.kind == "ph":
            self.take()
            return Placeholder(t.pos, t.end, t.value)
        if t.kind == "(":
            self.take()
            inner = self._nested(self.or_)
            if self.tok.kind != ")":
                raise ExpressionError("a «(» is not closed: add «)»", t.pos, 1)
            close = self.take()
            # the parentheses' span, for the messages
            return _respan(inner, t.pos, close.end)
        if t.kind == "name":
            word = t.value.upper()
            if word in ("TRUE", "FALSE", "NULL"):
                self.take()
                return Lit(t.pos, t.end, {"TRUE": True, "FALSE": False, "NULL": None}[word])
            if word in _KEYWORDS:
                raise ExpressionError(f"«{t.value}» needs something on its left", t.pos, t.end - t.pos)
            self.take()
            if self.tok.kind != "(":
                raise ExpressionError(
                    f"unknown name «{t.value}»: fields are written in brackets, [{t.value}], and functions "
                    f"with their parentheses, {word}(…)", t.pos, t.end - t.pos, t.value)
            self.take()
            args: list[Node] = []
            if self.tok.kind != ")":
                while True:
                    args.append(self._nested(self.or_))
                    if self.tok.kind == ",":
                        self.take()
                        continue
                    break
            if self.tok.kind != ")":
                raise ExpressionError(f"{word}( is not closed: add «)» after its arguments, separated by commas",
                                      t.pos, t.end - t.pos, word)
            close = self.take()
            return Call(t.pos, close.end, word, tuple(args))
        if t.kind == "end":
            raise ExpressionError("the expression ends too early: something is missing at the end", t.pos, 0)
        what = self.text[t.pos:t.end]
        raise ExpressionError(f"unexpected «{what}»", t.pos, t.end - t.pos)


def _respan(node: Node, pos: int, end: int) -> Node:
    import dataclasses

    return dataclasses.replace(node, pos=pos, end=end)


def parse(text: str) -> Node:
    if len(text) > MAX_EXPRESSION_CHARS:
        raise ExpressionError(f"the expression is longer than {MAX_EXPRESSION_CHARS} characters", 0, 0)
    node = _Parser(text).parse()
    _check_depth(node)
    return node


def _check_depth(root: Node) -> None:
    """A chain of `+` or `&` is as deep as it is long, and every walk of the tree recurses."""
    stack = [(root, 1)]
    while stack:
        node, depth = stack.pop()
        if depth > MAX_TREE_DEPTH:
            raise ExpressionError(f"the expression is more than {MAX_TREE_DEPTH} operations long: "
                                  "put a part of it in a calculated field", node.pos, max(node.end - node.pos, 0))
        children = node.args if isinstance(node, Call) else (node.arg,) if isinstance(node, Unary) \
            else (node.left, node.right) if isinstance(node, Binary) else ()
        stack.extend((c, depth + 1) for c in children)


# ── types and the functions' table ───────────────────────────────────────────
NUM, TEXT, DATE, DATETIME, BOOL, ANY, NULL, PAGETYPE, LIST = (
    "number", "text", "date", "datetime", "boolean", "any", "null", "page", "list")
_DATES = (DATE, DATETIME)


def field_type(dtype: str | None) -> str:
    """The language's type of a column, from its type name (the engine's or the editor's)."""
    d = (dtype or "").lower()
    if d.startswith(("int", "uint", "float", "double", "decimal", "number", "numeric", "real", "bigint")):
        return NUM
    if d.startswith(("str", "text", "utf8", "varchar", "char", "categorical", "enum")):
        return TEXT
    if d.startswith(("datetime", "timestamp")):
        return DATETIME
    if d == "date":
        return DATE
    if d.startswith("bool"):
        return BOOL
    return ANY


AGGREGATES = frozenset({"SUM", "AVG", "MIN", "MAX", "COUNT", "COUNTDISTINCT", "FIRST", "LAST", "SUMIF", "COUNTIF"})
# name → (min args, max args or None for any)
ARITY: dict[str, tuple[int, int | None]] = {
    **{a: (1, 2) for a in AGGREGATES}, "COUNT": (0, 2), "SUMIF": (2, 3), "COUNTIF": (1, 2),
    "PERCENT": (1, 2), "RUNNING": (1, 1), "ROWNUMBER": (0, 0), "PREVIOUS": (1, 1), "LOOKUP": (3, 4),
    "IF": (2, 3), "IFS": (2, None), "ISNULL": (1, 1), "COALESCE": (1, None),
    "ROUND": (1, 2), "ABS": (1, 1), "FLOOR": (1, 1), "CEILING": (1, 1), "POWER": (2, 2),
    "UPPER": (1, 1), "LOWER": (1, 1), "LEFT": (2, 2), "RIGHT": (2, 2), "MID": (3, 3), "LEN": (1, 1),
    "TRIM": (1, 1), "REPLACE": (3, 3), "CONTAINS": (2, 2), "STARTSWITH": (2, 2), "TEXT": (1, 1),
    "CONCAT": (1, None),
    "DATE": (3, 3), "YEAR": (1, 1), "MONTH": (1, 1), "DAY": (1, 1), "WEEKDAY": (1, 1),
    "DATEADD": (3, 3), "DATEDIFF": (3, 3), "TODAY": (0, 0), "NOW": (0, 0),
    "FORMAT": (2, 2), "PAGE": (0, 0), "PAGES": (0, 0), "PARAM": (1, 1), "REPORT": (0, 0),
}
FUNCTIONS = frozenset(ARITY)
_DATE_UNITS = ("d", "w", "m", "y")
_USAGE = {
    "IF": 'IF(condition, value if true, value if false)', "IFS": "IFS(condition1, value1, condition2, value2, …, else)",
    "ROUND": "ROUND(number, decimals)", "LEFT": "LEFT(text, count)", "RIGHT": "RIGHT(text, count)",
    "MID": "MID(text, start, count)", "REPLACE": "REPLACE(text, old, new)", "DATE": "DATE(year, month, day)",
    "DATEADD": 'DATEADD("d", 7, date)', "DATEDIFF": 'DATEDIFF("d", start, end)', "FORMAT": 'FORMAT(value, "#,##0.00")',
    "PARAM": 'PARAM("name")', "POWER": "POWER(number, exponent)", "PERCENT": "PERCENT(SUM([amount]))",
    "RUNNING": "RUNNING(SUM([amount]))", "CONTAINS": 'CONTAINS(text, "part")', "STARTSWITH": 'STARTSWITH(text, "start")',
    "PREVIOUS": "PREVIOUS([amount])", "LOOKUP": 'LOOKUP(key, key field, result, "dataset")',
    "SUMIF": "SUMIF(condition, value)", "COUNTIF": "COUNTIF(condition)",
}


def _usage(name: str) -> str:
    return _USAGE.get(name, f"{name}(…)")


@dataclass
class Env:
    """What an expression may name where it sits.

    fields: the columns of the item's dataset (name → type), None when not known (any
    name passes); datasets: every dataset of the report with its columns (None = not
    known); params: the report's parameters (name → type)."""

    fields: dict[str, str] | None = None
    dataset: str | None = None
    datasets: dict[str, dict[str, str] | None] = field(default_factory=dict)
    params: dict[str, str] = field(default_factory=dict)
    page: bool = False  # PAGE() and PAGES() may be written (a text)
    header: bool = False  # the page header or footer: no fields, except in an aggregate of a named dataset
    aggregates: bool = True  # aggregates may be written (not in a group's `by`, a sort key…)
    want: str | None = None  # the type the place wants (boolean for a condition)
    rownumber: bool = True  # ROWNUMBER() has a value here (not before the rows are in their order)
    running: bool = True  # RUNNING has rows to run over (a table's: not a calculated field)
    # the table's groups, outermost first (None: one with no name); `reach` of them hold this
    # place, the others are said by `outside` ({} is the group's name)
    groups: tuple[str | None, ...] = ()
    reach: int = 0
    outside: str = ""
    # what the row before is here: "row" (a table's row), "group" (a group's total, a chart's
    # category); None: there is none, for the reason in `no_previous`, or the usual one
    previous: str | None = None
    no_previous: str = ""
    lookup: bool = True  # LOOKUP has its rows here (not the choices of a parameter)
    in_lookup: bool = False  # the key field or the result of a LOOKUP: on the rows of the key, and no others


def _err(node: Node, message: str, name: str | None = None) -> ExpressionError:
    return ExpressionError(message, node.pos, max(node.end - node.pos, 0), name)


def _plain(node: Node) -> str:
    return {Lit: "a value", Field: "a field", Placeholder: "a placeholder"}.get(type(node), "an expression")


def is_aggregate(node: Node) -> bool:
    """True when the expression holds an aggregate (its value depends on a set of rows)."""
    if isinstance(node, Call):
        if node.name in AGGREGATES or node.name in ("PERCENT", "RUNNING"):
            return True
        if node.name == "LOOKUP":  # its key field and its result are on rows of their own
            return any(is_aggregate(a) for a in node.args[:1])
        return any(is_aggregate(a) for a in node.args)
    if isinstance(node, Unary):
        return is_aggregate(node.arg)
    if isinstance(node, Binary):
        return is_aggregate(node.left) or is_aggregate(node.right)
    return False


def fields_of(node: Node) -> set[str]:
    out: set[str] = set()
    stack = [node]
    while stack:
        n = stack.pop()
        if isinstance(n, Field):
            out.add(n.name)
        elif isinstance(n, Call):
            stack.extend(n.args)
        elif isinstance(n, Unary):
            stack.append(n.arg)
        elif isinstance(n, Binary):
            stack.extend((n.left, n.right))
    return out


def calls_in(node: Node) -> Iterable[Call]:
    """Every function call of the expression."""
    stack = [node]
    while stack:
        n = stack.pop()
        if isinstance(n, Call):
            yield n
            stack.extend(n.args)
        elif isinstance(n, Unary):
            stack.append(n.arg)
        elif isinstance(n, Binary):
            stack.extend((n.left, n.right))


def calls(node: Node, name: str) -> bool:
    return any(c.name == name for c in calls_in(node))


_VALUE_ARGS = {"SUMIF": 2, "COUNTIF": 1}  # the arguments of an aggregate before its scope; the others, one


def scope_name(call: Call, datasets: Iterable[str] = (), groups: Iterable[str | None] = ()) -> str | None:
    """What an aggregate names as its last argument, a dataset or a group, if it names one."""
    if not call.args:
        return None
    last = call.args[-1]
    if not (isinstance(last, Lit) and isinstance(last.value, str)):
        return None
    if len(call.args) == _VALUE_ARGS.get(call.name, 1) + 1:
        return last.value
    if call.name == "COUNT" and len(call.args) == 1 and (last.value in set(datasets) or last.value in tuple(groups)):
        return last.value
    return None


def measure(call: Call, scoped: bool) -> tuple[str, Node | None]:
    """The plain aggregate a call is and the value it takes: SUMIF and COUNTIF are SUM and
    COUNT of an IF, the same numbers by construction. No value: COUNT() of the rows."""
    args = call.args
    if call.name == "SUMIF":
        return "SUM", Call(call.pos, call.end, "IF", (args[0], args[1]))
    if call.name == "COUNTIF":
        return "COUNT", Call(call.pos, call.end, "IF", (args[0], Lit(call.pos, call.end, 1)))
    values = args[:-1] if scoped else args
    return call.name, values[0] if values else None


def canon(node: Node) -> str:
    """An expression written back canonically: the same one in two places is computed once."""
    if isinstance(node, Lit):
        return f"L{type(node.value).__name__}:{node.value!r}"
    if isinstance(node, Field):
        return f"F{node.name!r}"
    if isinstance(node, Placeholder):
        return f"P{node.raw!r}"
    if isinstance(node, Unary):
        return f"U{node.op}({canon(node.arg)})"
    if isinstance(node, Binary):
        return f"B{node.op}({canon(node.left)},{canon(node.right)})"
    assert isinstance(node, Call)
    return f"C{node.name}(" + ",".join(canon(a) for a in node.args) + ")"


class _Checker:
    def __init__(self, env: Env):
        self.env = env
        self.in_aggregate: str | None = None  # the dataset whose rows the aggregate reads ("" = the item's)
        self.in_special = False  # inside PERCENT or RUNNING

    def fields_now(self) -> dict[str, str] | None:
        if self.in_aggregate:
            return self.env.datasets.get(self.in_aggregate)
        return self.env.fields

    def check(self, node: Node) -> str:
        if isinstance(node, Lit):
            v = node.value
            if v is None:
                return NULL
            if isinstance(v, bool):
                return BOOL
            return NUM if isinstance(v, (int, float)) else TEXT
        if isinstance(node, Field):
            return self.field(node)
        if isinstance(node, Placeholder):
            name = node.raw.strip()
            if name in self.env.params:
                raise _err(node, f'{{{name}}} is a parameter: in an expression write PARAM("{name}")', name)
            try:
                p = ph.parse("{" + node.raw + "}")[0]
                if isinstance(p, ph.Placeholder) and p.name not in PLACEHOLDERS:
                    # a report has no datasource's variables, and no last refresh
                    known = ", ".join("{" + n + "}" for n in PLACEHOLDERS[:-1])
                    raise ph.PlaceholderError(f"unknown placeholder {p.text}: use {known} or {{{PLACEHOLDERS[-1]}}}")
                ph.check("{" + node.raw + "}")
            except ph.PlaceholderError as e:
                raise _err(node, str(e)) from None
            if p.fmt:
                return TEXT
            return DATETIME if p.name in ph.DATETIME_BASES or p.unit in ("h", "min", "s") else DATE
        if isinstance(node, Unary):
            t = self.check(node.arg)
            if node.op == "NOT":
                self.want(node.arg, t, BOOL, "NOT wants a condition (true or false)")
                return BOOL
            self.want(node.arg, t, NUM, "a minus sign goes before a number")
            return t if t in (NUM, NULL) else NUM
        if isinstance(node, Binary):
            return self.binary(node)
        assert isinstance(node, Call)
        return self.call(node)

    def want(self, node: Node, t: str, wanted: str, message: str) -> None:
        if t in (ANY, NULL, wanted) or (wanted == DATE and t == DATETIME):
            return
        raise _err(node, f"{message}: {_plain(node)} of type {t} is here")

    def field(self, node: Field) -> str:
        if self.env.header and not self.in_aggregate:
            raise _err(node, f"[{node.name}]: the page header and footer show no fields; an aggregate of a dataset "
                             f'is allowed, as SUM([{node.name}], "orders")', node.name)
        known = self.fields_now()
        if known is None:
            return ANY
        if node.name in known:
            return field_type(known[node.name])
        where = self.in_aggregate or self.env.dataset
        close = difflib.get_close_matches(node.name, list(known), n=1)
        hint = f": did you mean [{close[0]}]?" if close else ""
        place = f" of the dataset «{where}»" if where else ""
        raise _err(node, f"there is no field [{node.name}]{place}{hint}", node.name)

    def binary(self, node: Binary) -> str:
        lt, rt = self.check(node.left), self.check(node.right)
        op = node.op
        if op in ("AND", "OR"):
            self.want(node.left, lt, BOOL, f"{op} joins conditions (true or false)")
            self.want(node.right, rt, BOOL, f"{op} joins conditions (true or false)")
            return BOOL
        if op == "&":
            return PAGETYPE if PAGETYPE in (lt, rt) else TEXT
        if PAGETYPE in (lt, rt):
            raise _err(node, "PAGE() and PAGES() can only be joined to text with &, as \"Page \" & PAGE()")
        if op in _COMPARE:
            if LIST in (lt, rt):
                if op not in ("=", "<>"):
                    raise _err(node, "a parameter with many values can only be compared with = or <>")
                return BOOL
            fam = {DATETIME: DATE}
            a, b = fam.get(lt, lt), fam.get(rt, rt)
            if ANY not in (a, b) and NULL not in (a, b) and a != b:
                raise _err(node, f"compares {_article(lt)} with {_article(rt)}: they are of different types")
            return BOOL
        # arithmetic
        if lt in _DATES or rt in _DATES:
            if op == "+" and (lt in _DATES) != (rt in _DATES):
                other, ot = (node.right, rt) if lt in _DATES else (node.left, lt)
                self.want(other, ot, NUM, "a date moves by a number of days")
                return lt if lt in _DATES else rt
            if op == "-" and lt in _DATES:
                if rt in _DATES:
                    return NUM
                self.want(node.right, rt, NUM, "a date moves by a number of days")
                return lt
            raise _err(node, f"«{op}» does not apply to dates: use DATEADD and DATEDIFF")
        for side, t in ((node.left, lt), (node.right, rt)):
            if t == TEXT:
                raise _err(side, f"«{op}» works on numbers, and this is a text: join texts with &")
            self.want(side, t, NUM, f"«{op}» works on numbers")
        return NUM

    def call(self, node: Call) -> str:
        name, args = node.name, node.args
        if name not in FUNCTIONS:
            close = difflib.get_close_matches(name, sorted(FUNCTIONS), n=1)
            hint = f": did you mean {close[0]}?" if close else ""
            raise ExpressionError(f"unknown function {name}{hint}", node.pos, len(name), name)
        lo, hi = ARITY[name]
        if len(args) < lo or (hi is not None and len(args) > hi):
            want = f"{lo}" if lo == hi else f"{lo} to {hi}" if hi is not None else f"at least {lo}"
            raise ExpressionError(f"{name} takes {want} argument{'s' if hi != 1 else ''}, {len(args)} given: "
                                  f"{_usage(name)}", node.pos, node.end - node.pos, name)
        if name in AGGREGATES:
            return self.aggregate(node)
        if name == "PREVIOUS":
            return self.previous(node)
        if name == "LOOKUP":
            return self.lookup(node)
        if name in ("PERCENT", "RUNNING"):
            if not self.env.aggregates or self.in_aggregate is not None or self.in_special or self.env.in_lookup:
                raise ExpressionError(f"{name} cannot be written here", node.pos, len(name), name)
            inner = args[0]
            if name == "RUNNING" and not self.env.running:
                # with no rows to run over it is the total, on every row: a number that looks right
                raise ExpressionError("RUNNING goes over the rows a table shows: write it in a column of the table, "
                                      "not in a calculated field", node.pos, len(name), name)
            if name == "RUNNING" and not (isinstance(inner, Call) and inner.name in AGGREGATES
                                          and scope_name(inner, self.env.datasets, self.env.groups) is None):
                raise _err(inner, "RUNNING takes an aggregate of the group, as RUNNING(SUM([amount]))", name)
            if name == "PERCENT" and not is_aggregate(inner):
                raise _err(inner, "PERCENT takes an aggregate, as PERCENT(SUM([amount]))", name)
            if name == "PERCENT" and len(args) > 1:
                self.percent_of(args[1])
            self.in_special = True
            try:
                t = self.check(inner)
            finally:
                self.in_special = False
            self.want(inner, t, NUM, f"{name} works on numbers")
            return NUM
        if name == "ROWNUMBER":
            if self.env.header:
                raise ExpressionError("ROWNUMBER() has no row in the page header and footer", node.pos, len(name), name)
            if not self.env.rownumber:
                raise ExpressionError("ROWNUMBER() is the row's place among those the table shows: it is not known yet "
                                      "where they are chosen, sorted or grouped", node.pos, len(name), name)
            return NUM
        if name in ("PAGE", "PAGES"):
            if not self.env.page or self.in_aggregate is not None:
                raise ExpressionError(f"{name}() can be written only in a text, where the pages are known",
                                      node.pos, len(name), name)
            return PAGETYPE
        if name == "PARAM":
            arg = args[0]
            if not (isinstance(arg, Lit) and isinstance(arg.value, str)):
                raise _err(arg, 'PARAM takes the name of a parameter in quotes: PARAM("region")', name)
            if arg.value not in self.env.params:
                close = difflib.get_close_matches(arg.value, list(self.env.params), n=1)
                hint = f': did you mean "{close[0]}"?' if close else ""
                raise _err(arg, f"there is no parameter «{arg.value}»{hint}", arg.value)
            return self.env.params[arg.value]
        types = [self.check(a) for a in args]
        if PAGETYPE in types and name not in ("CONCAT",):
            i = types.index(PAGETYPE)
            raise _err(args[i], "PAGE() and PAGES() can only be joined to text with &, as \"Page \" & PAGE()")
        return self.scalar(node, types)

    def aggregate(self, node: Call) -> str:
        name, args = node.name, node.args
        if not self.env.aggregates:
            raise ExpressionError(f"{name}: an aggregate cannot be written here (the value of each row is wanted)",
                                  node.pos, len(name), name)
        if self.in_aggregate is not None:
            raise ExpressionError(f"{name} inside another aggregate: write one at a time", node.pos, len(name), name)
        written = scope_name(node, self.env.datasets, self.env.groups)
        if len(args) == _VALUE_ARGS.get(name, 1) + 1 and written is None:
            what = "a dataset or a group" if self.env.groups else "a dataset"
            example = {"SUMIF": "SUMIF([a] > 0, [x], ", "COUNTIF": "COUNTIF([a] > 0, "}.get(name, f"{name}([x], ")
            raise _err(args[-1], f'the last argument of {name} is the name of {what} in quotes: {example}"orders")', name)
        named = self.scope(args[-1], written) if written is not None else None
        if written is None and self.env.header:
            raise ExpressionError(f'{name} in the page header or footer names its dataset: {name}([x], "orders")',
                                  node.pos, len(name), name)
        value_args = args[:-1] if written is not None else args
        if name != "COUNT" and not value_args:
            raise ExpressionError(f"{name} takes the value to aggregate, as {name}([amount])", node.pos, len(name), name)
        self.in_aggregate = named or ""
        try:
            if name in _VALUE_ARGS:
                t = self.check(value_args[0])
                self.want(value_args[0], t, BOOL, f"{name}: the first argument is a condition — {_usage(name)}")
                if name == "SUMIF":
                    t = self.check(value_args[1])
                    self.want(value_args[1], t, NUM, "SUMIF adds numbers")
                return NUM
            t = self.check(value_args[0]) if value_args else NUM
        finally:
            self.in_aggregate = None
        if name in ("SUM", "AVG"):
            self.want(value_args[0], t, NUM, f"{name} adds numbers")
            return NUM
        if name in ("COUNT", "COUNTDISTINCT"):
            return NUM
        return t

    def scope(self, last: Node, written: str) -> str | None:
        """The dataset an aggregate's last argument names; None for a group that holds this
        place. Anything else is a mistake, said here."""
        env = self.env
        if env.in_lookup:
            raise _err(last, "an aggregate in LOOKUP works on the rows with the key: it names no dataset or group", written)
        in_dataset, in_group = written in env.datasets, written in env.groups
        if in_dataset and in_group:
            raise _err(last, f"«{written}» is both a dataset and a group of this table: rename the dataset, or give the "
                             "group a name of its own", written)
        if in_group:
            if self.in_special:
                raise _err(last, f'write the group after the aggregate: PERCENT(SUM([x]), "{written}")', written)
            self.group_here(last, written)
            return None
        if in_dataset:
            return written
        known = [*env.datasets, *(g for g in env.groups if g)]
        close = difflib.get_close_matches(written, known, n=1)
        hint = f': did you mean "{close[0]}"?' if close else ""
        if not env.groups:
            raise _err(last, f"there is no dataset «{written}»{hint}", written)
        unnamed = ". A group by an expression has no name until you give it one" if None in env.groups else ""
        raise _err(last, f"there is no dataset or group «{written}»{hint}{unnamed}", written)

    def group_here(self, last: Node, written: str) -> None:
        """The group holds this place, or the mistake is said."""
        env = self.env
        if env.groups.count(written) > 1:
            raise _err(last, f"two groups of this table are named «{written}»: give one of them a name of its own", written)
        if env.groups.index(written) >= env.reach:
            raise _err(last, env.outside.replace("{}", written) or f"the group «{written}» does not hold this place",
                       written)

    def percent_of(self, last: Node) -> None:
        """PERCENT's second argument: a group around this place, by name."""
        if not (isinstance(last, Lit) and isinstance(last.value, str)):
            raise _err(last, 'the second argument of PERCENT is the name of a group in quotes: '
                             'PERCENT(SUM([x]), "region")', "PERCENT")
        if last.value not in self.env.groups:
            close = difflib.get_close_matches(last.value, [g for g in self.env.groups if g], n=1)
            hint = f': did you mean "{close[0]}"?' if close else ""
            raise _err(last, f"PERCENT takes a group of the table, and «{last.value}» is none{hint}", last.value)
        self.group_here(last, last.value)

    def previous(self, node: Call) -> str:
        """PREVIOUS(x): x where the row above is, or in the group before."""
        env, inner = self.env, node.args[0]
        if env.previous is None:
            raise ExpressionError(env.no_previous or "PREVIOUS reads the row above among those a table shows: write it "
                                  "in a column of the table or in its total", node.pos, len("PREVIOUS"), "PREVIOUS")
        if self.in_aggregate is not None or self.in_special:
            raise ExpressionError("PREVIOUS inside an aggregate: an aggregate works on its rows in no order; write "
                                  "the aggregate inside it, as PREVIOUS(SUM([amount]))", node.pos, len("PREVIOUS"), "PREVIOUS")
        if env.previous == "row" and _plain_aggregate(inner):
            raise _err(inner, "on a row an aggregate is the same for every row of the group: write PREVIOUS([amount]) "
                              "here, and PREVIOUS(SUM([amount])) in the column's total", "PREVIOUS")
        return self.check(inner)

    def lookup(self, node: Call) -> str:
        """LOOKUP(key, key field, result[, dataset]): the key is here, the others on the
        rows of the dataset."""
        env, args = self.env, node.args
        if not env.lookup:
            raise ExpressionError("LOOKUP reads another row, and the choices of a parameter have only their own: "
                                  "join what you need in the dataset's SQL", node.pos, len("LOOKUP"), "LOOKUP")
        named = None
        if len(args) == 4:
            last = args[3]
            if not (isinstance(last, Lit) and isinstance(last.value, str)):
                raise _err(last, f"the last argument of LOOKUP is the name of a dataset in quotes: {_usage('LOOKUP')}", "LOOKUP")
            named = last.value
            if named not in env.datasets:
                close = difflib.get_close_matches(named, list(env.datasets), n=1)
                hint = f': did you mean "{close[0]}"?' if close else ""
                raise _err(last, f"there is no dataset «{named}»{hint}", named)
        target = named or self.in_aggregate or env.dataset
        if target is None or (not named and not self.in_aggregate and env.header):
            raise ExpressionError(f"LOOKUP here names its dataset: {_usage('LOOKUP')}", node.pos, len("LOOKUP"), "LOOKUP")
        key = self.check(args[0])
        if key == LIST:
            raise _err(args[0], "the key of LOOKUP is one value, and this is a parameter with many values", "LOOKUP")
        fields = env.datasets[target] if target in env.datasets else env.fields
        rows = replace(env, fields=fields, dataset=target, want=None, header=False, page=False, rownumber=False,
                       running=False, groups=(), reach=0, previous=None, in_lookup=True,
                       no_previous="PREVIOUS has no row above among the rows of a LOOKUP")
        field = _Checker(replace(rows, aggregates=False)).check(args[1])
        result = _Checker(replace(rows, aggregates=True)).check(args[2])
        family = {DATETIME: DATE}
        a, b = family.get(key, key), family.get(field, field)
        if ANY not in (a, b) and NULL not in (a, b) and a != b:
            raise _err(args[0], f"LOOKUP compares {_article(key)} with {_article(field)}: they are of different types, and "
                                "would never be equal. Make them one type, as TEXT([code])", "LOOKUP")
        if LIST in (field, result):
            raise _err(args[1 if field == LIST else 2], "LOOKUP works on one value, and this is a parameter with many values",
                       "LOOKUP")
        return result

    def scalar(self, node: Call, types: list[str]) -> str:
        name, args = node.name, node.args

        def need(i: int, wanted: str, what: str) -> None:
            self.want(args[i], types[i], wanted, f"{name}: {what} — {_usage(name)}")

        if name == "IF":
            need(0, BOOL, "the first argument is a condition")
            return _common(types[1:])
        if name == "IFS":
            pairs = len(args) // 2
            for i in range(pairs):
                need(2 * i, BOOL, f"argument {2 * i + 1} is a condition")
            return _common([types[2 * i + 1] for i in range(pairs)] + ([types[-1]] if len(args) % 2 else []))
        if name == "ISNULL":
            return BOOL
        if name == "COALESCE":
            return _common(types)
        if name in ("ROUND", "ABS", "FLOOR", "CEILING", "POWER"):
            for i in range(len(args)):
                need(i, NUM, "it works on numbers")
            return NUM
        if name in ("UPPER", "LOWER", "TRIM", "LEN"):
            need(0, TEXT, "it works on a text")
            return NUM if name == "LEN" else TEXT
        if name in ("LEFT", "RIGHT"):
            need(0, TEXT, "the first argument is a text")
            need(1, NUM, "the second is how many characters")
            return TEXT
        if name == "MID":
            need(0, TEXT, "the first argument is a text")
            need(1, NUM, "the second is where to start (1 is the first character)")
            need(2, NUM, "the third is how many characters")
            return TEXT
        if name == "REPLACE":
            for i in range(3):
                need(i, TEXT, "it works on texts")
            return TEXT
        if name in ("CONTAINS", "STARTSWITH"):
            need(0, TEXT, "it works on texts")
            need(1, TEXT, "it works on texts")
            return BOOL
        if name in ("TEXT", "CONCAT"):
            return PAGETYPE if PAGETYPE in types else TEXT
        if name == "DATE":
            for i in range(3):
                need(i, NUM, "year, month and day are numbers")
            return DATE
        if name in ("YEAR", "MONTH", "DAY", "WEEKDAY"):
            need(0, DATE, "it works on a date")
            return NUM
        if name in ("DATEADD", "DATEDIFF"):
            unit = args[0]
            if not (isinstance(unit, Lit) and unit.value in _DATE_UNITS):
                raise _err(unit, f'{name}: the first argument is the unit, "d" (days), "w" (weeks), "m" (months) or '
                                 f'"y" (years) — {_usage(name)}', name)
            if name == "DATEADD":
                need(1, NUM, "the second argument is how many")
                need(2, DATE, "the third is a date")
                return types[2] if types[2] in _DATES else DATE
            need(1, DATE, "the second argument is a date")
            need(2, DATE, "the third is a date")
            return NUM
        if name == "TODAY":
            return DATE
        if name == "NOW":
            return DATETIME
        if name == "FORMAT":
            pattern = args[1]
            if isinstance(pattern, Lit) and isinstance(pattern.value, str):
                try:
                    formats.check_pattern(pattern.value)
                except formats.FormatError as e:
                    raise _err(pattern, str(e), name) from None
            else:
                need(1, TEXT, "the second argument is a format")
            return TEXT
        if name == "REPORT":
            return TEXT
        raise AssertionError(name)  # pragma: no cover — every name of ARITY is handled above


def _plain_aggregate(node: Node) -> bool:
    """An aggregate that is one value for all the rows it sits in: not one that follows the
    row (PERCENT, RUNNING), nor one that is on rows of its own (LOOKUP's result)."""
    if isinstance(node, Call):
        if node.name in AGGREGATES:
            return True
        if node.name in ("PERCENT", "RUNNING"):
            return False
        return any(_plain_aggregate(a) for a in (node.args[:1] if node.name == "LOOKUP" else node.args))
    if isinstance(node, Unary):
        return _plain_aggregate(node.arg)
    if isinstance(node, Binary):
        return _plain_aggregate(node.left) or _plain_aggregate(node.right)
    return False


def _article(t: str) -> str:
    return {NUM: "a number", TEXT: "a text", DATE: "a date", DATETIME: "a date", BOOL: "a condition"}.get(t, t)


def _common(types: list[str]) -> str:
    known = {t for t in types if t not in (NULL, ANY)}
    if len(known) == 1:
        return known.pop()
    if known <= {DATE, DATETIME} and known:
        return DATETIME
    return ANY


def check(node: Node, env: Env) -> str:
    """The expression's type; ExpressionError at the first mistake, with its position."""
    t = _Checker(env).check(node)
    if env.want == BOOL and t not in (BOOL, ANY, NULL):
        raise _err(node, f"a condition is wanted here (true or false), as [amount] > 0: this is {_article(t)}")
    if t == PAGETYPE and not env.page:  # pragma: no cover — PAGE() is refused where it cannot go
        raise _err(node, "PAGE() and PAGES() can be written only in a text")
    return t


def compile_expression(text: str, env: Env) -> Node:
    """Parses and checks; ExpressionError otherwise."""
    node = parse(text)
    check(node, env)
    return node


# ── evaluation ───────────────────────────────────────────────────────────────
class EvalError(ValueError):
    """A value the expression cannot work on (a text added to a number…), found while
    rendering: the caller names the item."""


@dataclass
class Context:
    """Where an expression is evaluated.

    row: the current row (None in a group's header or footer, a total, a KPI);
    scopes: the sets of rows from the outermost (the table or dataset) to the innermost
    (the current group) — aggregates read the last; running/running_index: the rows a
    RUNNING total goes over and how far; datasets: every dataset by name;
    params: the parameters' values; memo: shared between the contexts of one render;
    dataset: the item's, the one a LOOKUP reads when it names none; groups: the names of the
    table's groups, scopes[i + 1] holding the rows of the i-th; previous: a group's total,
    the context of the total before it (a row's is in running/running_index).

    A set of rows is a list of dicts, or rows that stay in DuckDB (frames.SqlRows): those
    answer an aggregate, the first row's field and a running total themselves."""

    loc: formats.Locale
    now: datetime  # aware, in the report's time zone
    row: dict | None = None
    scopes: list[Any] = field(default_factory=list)
    running: Any = None
    running_index: int = -1
    rownumber: int | None = None
    datasets: Any = field(default_factory=dict)
    params: dict[str, Any] = field(default_factory=dict)
    report: str = ""
    memo: dict = field(default_factory=dict)
    ph_context: ph.Context | None = None
    dataset: str | None = None
    groups: tuple[str | None, ...] = ()
    previous: "Context | None" = None

    def at(self, **changes: Any) -> "Context":
        """The same context with some fields changed (the memo stays shared)."""
        c = Context(**{k: getattr(self, k) for k in self.__dataclass_fields__})
        for k, v in changes.items():
            setattr(c, k, v)
        return c


def _is_num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _as_datetime(v: date) -> datetime:
    return v if isinstance(v, datetime) else datetime(v.year, v.month, v.day)


def _cmp_pair(a: Any, b: Any) -> tuple[Any, Any]:
    if isinstance(a, date) and isinstance(b, date) and (isinstance(a, datetime) != isinstance(b, datetime)):
        return _as_datetime(a), _as_datetime(b)
    return a, b


def _compare(op: str, a: Any, b: Any) -> bool | None:
    if a is None or b is None:
        return None
    if isinstance(b, list) or isinstance(a, list):
        values, x = (b, a) if isinstance(b, list) else (a, b)
        found = any(compare_equal(x, v) for v in values)
        return found if op == "=" else (not found if op == "<>" else None)
    if _is_num(a) != _is_num(b) or isinstance(a, str) != isinstance(b, str) or isinstance(a, bool) != isinstance(b, bool) \
            or isinstance(a, date) != isinstance(b, date):
        raise EvalError(f"cannot compare {_kind(a)} with {_kind(b)}")
    a, b = _cmp_pair(a, b)
    if op == "=":
        return a == b
    if op == "<>":
        return a != b
    if op == "<":
        return a < b
    if op == ">":
        return a > b
    if op == "<=":
        return a <= b
    return a >= b


def compare_equal(a: Any, b: Any) -> bool:
    """`=` between two values of any kind: values of different kinds are different."""
    if _is_num(a) and _is_num(b):
        return a == b
    if isinstance(a, date) and isinstance(b, date):
        x, y = _cmp_pair(a, b)
        return x == y
    if isinstance(a, bool) and isinstance(b, bool) or isinstance(a, str) and isinstance(b, str):
        return a == b
    return False


def lookup_key(v: Any) -> tuple | None:
    """`=` of the language as a dict's key: numbers by value, a date as its midnight, values of
    different kinds different. None for what equals nothing (NULL)."""
    if v is None:
        return None
    if isinstance(v, bool):
        return ("b", v)
    if isinstance(v, (int, float)):
        return ("n", v) if v == v else None
    if isinstance(v, str):
        return ("t", v)
    if isinstance(v, date):
        return ("d", _as_datetime(v))
    return None


def _kind(v: Any) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "a condition"
    if _is_num(v):
        return "a number"
    if isinstance(v, str):
        return "a text"
    if isinstance(v, date):
        return "a date"
    return "a list" if isinstance(v, list) else type(v).__name__


def truthy(v: Any) -> bool:
    """A condition's outcome: NULL and false are false."""
    if v is None or v is False:
        return False
    if v is True:
        return True
    return bool(v)


def _num(v: Any, what: str) -> float | int:
    if _is_num(v):
        return v
    raise EvalError(f"{what} works on numbers, and {_kind(v)} is here")


def _text_of(v: Any, ctx: Context) -> Any:
    if isinstance(v, (PageNumber, PageText)):
        return v
    return formats.display(v, None, ctx.loc)


def _join(a: Any, b: Any) -> Any:
    parts: list[Any] = []
    for x in (a, b):
        if isinstance(x, PageText):
            parts.extend(x)
        else:
            parts.append(x)
    merged: list[Any] = []
    for p in parts:
        if isinstance(p, str) and merged and isinstance(merged[-1], str):
            merged[-1] += p
        elif p != "":
            merged.append(p)
    if any(isinstance(p, PageNumber) for p in merged):
        return PageText(merged)
    return "".join(merged)


def _add_months(d: date, n: int) -> date:
    y, m = divmod(d.month - 1 + n, 12)
    y += d.year
    if not 1 <= y <= 9999:
        raise EvalError("the date goes out of range")
    return d.replace(year=y, month=m + 1, day=min(d.day, monthrange(y, m + 1)[1]))


def _round(x: float, n: int) -> float | int:
    from decimal import ROUND_HALF_UP, Decimal

    if isinstance(x, float) and not math.isfinite(x):
        return x
    # a double has no digit past these: more decimals change nothing, and tens further than
    # its size are zero; the context holds every digit of the largest
    n = max(min(int(n), formats.MAX_PLACES), -formats.MAX_PLACES)
    d = Decimal(x) if isinstance(x, int) else Decimal(repr(x))
    q = d.quantize(Decimal(1).scaleb(-n), rounding=ROUND_HALF_UP, context=formats.PRECISE)
    return int(q) if n <= 0 else float(q)


# the bits of the largest value a report's number holds: a power past them is NULL at once
_MAX_POWER_BITS = 1024


def _power(a: float | int, b: float | int) -> Any:
    """a ** b; NULL when it is no real number or no number a report holds. An int to an int
    is Python's exact one, as long as a double holds it: the exponent is whatever a person
    (or a field) writes, and POWER(10, 99999999999) would keep the worker for good."""
    if isinstance(a, int) and isinstance(b, int) and b > 0 and (abs(a).bit_length() - 1) * b > _MAX_POWER_BITS:
        return None
    try:
        r = a ** b
    except (OverflowError, ZeroDivisionError):
        return None
    if isinstance(r, int) and r.bit_length() > _MAX_POWER_BITS:
        return None
    return None if isinstance(r, complex) else r


def _aggregate(name: str, values: list[Any]) -> Any:
    vals = [v for v in values if v is not None]
    if name == "COUNT":
        return len(vals)
    if name == "COUNTDISTINCT":
        return len({repr(v) if isinstance(v, (list, dict)) else v for v in vals})
    if not vals:
        return None
    if name == "SUM":
        return sum(_num(v, "SUM") for v in vals)
    if name == "AVG":
        return sum(_num(v, "AVG") for v in vals) / len(vals)
    if name in ("MIN", "MAX"):
        try:
            pairs = [_cmp_pair(v, vals[0])[0] for v in vals]
            return (min if name == "MIN" else max)(pairs)
        except TypeError:
            raise EvalError(f"{name} compares values of one type, and this column mixes them") from None
    if name == "FIRST":
        return vals[0]
    return vals[-1]


def _running(name: str, values: list[Any]) -> list[Any]:
    """The aggregate of values[:i+1] for every i, in one pass."""
    out: list[Any] = []
    acc: Any = None
    count = 0
    seen: set = set()
    first: Any = None
    for v in values:
        if v is not None:
            count += 1
            if name in ("SUM", "AVG"):
                acc = (acc or 0) + _num(v, name)
            elif name in ("MIN", "MAX"):
                acc = v if acc is None else (min if name == "MIN" else max)(acc, v)
            elif name == "COUNTDISTINCT":
                seen.add(v)
            elif name == "FIRST" and first is None:
                first = v
            elif name == "LAST":
                acc = v
        if name == "COUNT":
            out.append(count)
        elif name == "COUNTDISTINCT":
            out.append(len(seen))
        elif name == "AVG":
            out.append(acc / count if count else None)
        elif name == "FIRST":
            out.append(first)
        else:
            out.append(acc)
    return out


class Evaluator:
    """Walks a checked tree. One instance per render; contexts carry the rows."""

    def __init__(self) -> None:
        self._dispatch: dict[type, Callable[[Any, Context], Any]] = {
            Lit: lambda n, c: n.value, Field: self._field, Placeholder: self._placeholder,
            Unary: self._unary, Binary: self._binary, Call: self._call,
        }

    def __call__(self, node: Node, ctx: Context) -> Any:
        return self._dispatch[type(node)](node, ctx)

    # ── leaves ──
    def _field(self, node: Field, ctx: Context) -> Any:
        row = ctx.row
        if row is None:
            scope = ctx.scopes[-1] if ctx.scopes else None
            if scope is not None and not isinstance(scope, list):
                return scope.first_value(node.name)
            row = scope[0] if scope else None
            if row is None:
                return None
        return row.get(node.name)

    def _placeholder(self, node: Placeholder, ctx: Context) -> Any:
        p = ph.parse("{" + node.raw + "}")[0]
        pctx = ctx.ph_context or ph.Context(now=ctx.now, tz=ctx.now.tzinfo, lang=ctx.loc.code)  # type: ignore[arg-type]
        try:
            value = ph.evaluate(p, pctx)
            if p.fmt:
                return ph.format_value(value, p, pctx.tz, lang=pctx.lang)
        except ph.PlaceholderError as e:
            raise EvalError(str(e)) from None
        if isinstance(value, datetime):
            return value.replace(tzinfo=None)
        return value

    # ── operators ──
    def _unary(self, node: Unary, ctx: Context) -> Any:
        v = self(node.arg, ctx)
        if node.op == "NOT":
            return None if v is None else not truthy(v)
        if v is None:
            return None
        return -_num(v, "a minus sign")

    def _binary(self, node: Binary, ctx: Context) -> Any:
        op = node.op
        if op in ("AND", "OR"):
            a = self(node.left, ctx)
            if op == "AND" and a is False:
                return False
            if op == "OR" and a is True:
                return True
            b = self(node.right, ctx)
            if op == "AND":
                if b is False:
                    return False
                return None if a is None or b is None else truthy(a) and truthy(b)
            if b is True:
                return True
            return None if a is None or b is None else truthy(a) or truthy(b)
        a, b = self(node.left, ctx), self(node.right, ctx)
        if op == "&":
            return _join("" if a is None else _text_of(a, ctx), "" if b is None else _text_of(b, ctx))
        if op in _COMPARE:
            return _compare(op, a, b)
        if a is None or b is None:
            return None
        if isinstance(a, date) or isinstance(b, date):
            return self._date_arithmetic(op, a, b)
        x, y = _num(a, f"«{op}»"), _num(b, f"«{op}»")
        try:
            if op == "+":
                return x + y
            if op == "-":
                return x - y
            if op == "*":
                return x * y
            if y == 0:
                return None
            return x / y
        except OverflowError as e:  # an int too long for a float beside it
            raise EvalError(f"«{op}»: {e}") from None

    @staticmethod
    def _date_arithmetic(op: str, a: Any, b: Any) -> Any:
        try:
            if op == "+" and isinstance(a, date) and _is_num(b):
                return a + timedelta(days=b)
            if op == "+" and isinstance(b, date) and _is_num(a):
                return b + timedelta(days=a)
            if op == "-" and isinstance(a, date) and _is_num(b):
                return a - timedelta(days=b)
        except (OverflowError, ValueError):
            raise EvalError("the date goes out of range") from None
        if op == "-" and isinstance(a, date) and isinstance(b, date):
            x, y = _cmp_pair(a, b)
            delta = x - y
            return delta.days if not isinstance(x, datetime) else delta.total_seconds() / 86400
        raise EvalError(f"«{op}» does not apply to {_kind(a)} and {_kind(b)}")

    # ── functions ──
    def _values(self, node: Node, ctx: Context, rows: list[dict]) -> list[Any]:
        saved = ctx.row
        try:
            out = []
            for r in rows:
                ctx.row = r
                out.append(self(node, ctx))
            return out
        finally:
            ctx.row = saved

    def _aggregate(self, node: Call, ctx: Context) -> Any:
        named = scope_name(node, ctx.datasets, ctx.groups)
        whole = False
        if named is None:
            rows = ctx.scopes[-1] if ctx.scopes else ([ctx.row] if ctx.row is not None else [])
        elif named in ctx.groups:
            level = ctx.groups.index(named) + 1
            if level >= len(ctx.scopes):  # a total outside the group: the grand total, say
                return None
            rows = ctx.scopes[level]
        else:
            rows = ctx.datasets.get(named, [])
            whole = True
        if not isinstance(rows, list):  # rows that stay in DuckDB (app/reports/frames.py)
            return rows.aggregate(node, ctx)
        key = (id(node), id(rows))
        hit = ctx.memo.get(key)
        if hit is not None and hit[0] is rows and hit[1] is node:
            return hit[2]
        fn, arg = measure(node, named is not None)
        if arg is None:
            value = len(rows)
        else:
            value = _aggregate(fn, self._values(arg, ctx.at(dataset=named) if whole else ctx, rows))
        # the rows and the tree are kept alive with the key: neither id is reused while it is here
        ctx.memo[key] = (rows, node, value)
        return value

    def _percent(self, node: Call, ctx: Context) -> Any:
        inner = node.args[0]
        up = None
        if len(node.args) > 1:
            name = node.args[1].value  # type: ignore[attr-defined]
            level = ctx.groups.index(name) + 1 if name in ctx.groups else len(ctx.scopes)
            if level >= len(ctx.scopes):  # outside the group
                return None
            up = ctx.scopes[:level + 1]
        if ctx.row is not None:
            num = self(inner, ctx.at(scopes=[*ctx.scopes, [ctx.row]], row=None))
            den = self(inner, ctx.at(row=None) if up is None else ctx.at(row=None, scopes=up))
        else:
            num = self(inner, ctx)
            if up is not None:
                den = self(inner, ctx.at(scopes=up))
            else:
                den = self(inner, ctx.at(scopes=ctx.scopes[:-1])) if len(ctx.scopes) > 1 else num
        if num is None or den is None:
            return None
        n, d = _num(num, "PERCENT"), _num(den, "PERCENT")
        return None if d == 0 else n / d

    def _previous(self, node: Call, ctx: Context) -> Any:
        """The same expression one place back: on a row, the row above it among those of its
        group; in a total, the total before it, already built with its own rows."""
        inner = node.args[0]
        if ctx.row is None:
            return None if ctx.previous is None else self(inner, ctx.previous)
        i = ctx.running_index
        if i <= 0 or not isinstance(ctx.running, list):
            return None
        kept = ctx.row, ctx.rownumber
        ctx.row, ctx.running_index = ctx.running[i - 1], i - 1
        ctx.rownumber = None if ctx.rownumber is None else ctx.rownumber - 1
        try:
            return self(inner, ctx)
        finally:
            (ctx.row, ctx.rownumber), ctx.running_index = kept, i

    def _lookup(self, node: Call, ctx: Context) -> Any:
        args = node.args
        k = lookup_key(self(args[0], ctx))
        if k is None:
            return None
        name = args[3].value if len(args) > 3 else ctx.dataset  # type: ignore[attr-defined]
        rows = ctx.datasets.get(name) if name is not None else None
        if rows is None:
            raise EvalError(f"LOOKUP: the dataset «{name}» is not read here")
        memo = ("lookup", id(node), name)
        hit = ctx.memo.get(memo)
        if hit is None or hit[0] is not node or hit[1] is not rows:
            # the result is on the rows of the key: nothing of the place the LOOKUP sits in
            on = ctx.at(row=None, scopes=[], running=None, running_index=-1, rownumber=None, previous=None, groups=(),
                        dataset=name)
            hit = (node, rows, self._keyed(args[1], on, rows), on, {})
            ctx.memo[memo] = hit
        _, _, members, on, results = hit
        member = members.get(k)
        if member is None:
            return None
        if k not in results:
            on.scopes = [member]
            results[k] = self(args[2], on)
        return results[k]

    def _keyed(self, field_node: Node, ctx: Context, rows: Any) -> dict:
        """The rows of a dataset by the value of a LOOKUP's key field, in their order."""
        memo = ("keyed", canon(field_node), ctx.dataset)
        hit = ctx.memo.get(memo)
        if hit is not None and hit[0] is rows:
            return hit[1]
        if isinstance(rows, list):
            members: dict = {}
            at = ctx.at()
            for r in rows:
                at.row = r
                k = lookup_key(self(field_node, at))
                if k is not None:
                    members.setdefault(k, []).append(r)
        else:  # rows that stay in DuckDB
            members = rows.keyed(field_node, ctx)
        ctx.memo[memo] = (rows, members)
        return members

    def _call(self, node: Call, ctx: Context) -> Any:
        name, args = node.name, node.args
        if name in AGGREGATES:
            return self._aggregate(node, ctx)
        if name == "PERCENT":
            return self._percent(node, ctx)
        if name == "PREVIOUS":
            return self._previous(node, ctx)
        if name == "LOOKUP":
            return self._lookup(node, ctx)
        if name == "RUNNING":
            inner = args[0]
            assert isinstance(inner, Call)
            rows = ctx.running if ctx.running is not None else (ctx.scopes[-1] if ctx.scopes else [])
            idx = ctx.running_index
            if idx < 0 or not rows:
                return self._aggregate(inner, ctx)
            if not isinstance(rows, list):
                running = rows.running(inner, ctx)
                return running[min(idx, len(running) - 1)]
            key = ("running", id(inner), id(rows))
            hit = ctx.memo.get(key)
            if hit is None or hit[0] is not rows or hit[1] is not inner:
                fn, arg = measure(inner, False)
                values = self._values(arg, ctx, rows) if arg is not None else [1] * len(rows)
                hit = (rows, inner, _running(fn, values))
                ctx.memo[key] = hit
            return hit[2][min(idx, len(rows) - 1)]
        if name == "ROWNUMBER":
            return ctx.rownumber
        if name == "IF":
            if truthy(self(args[0], ctx)):
                return self(args[1], ctx)
            return self(args[2], ctx) if len(args) > 2 else None
        if name == "IFS":
            for i in range(0, len(args) - 1, 2):
                if truthy(self(args[i], ctx)):
                    return self(args[i + 1], ctx)
            return self(args[-1], ctx) if len(args) % 2 else None
        if name == "COALESCE":
            for a in args:
                v = self(a, ctx)
                if v is not None:
                    return v
            return None
        if name == "PAGE":
            return PAGE
        if name == "PAGES":
            return PAGES
        if name == "PARAM":
            return ctx.params.get(args[0].value)  # type: ignore[attr-defined]
        if name == "REPORT":
            return ctx.report
        if name == "TODAY":
            return ctx.now.date()
        if name == "NOW":
            return ctx.now.replace(tzinfo=None)
        values = [self(a, ctx) for a in args]
        try:
            return _scalar(name, values, ctx)
        except OverflowError:  # a date past year 9999, a whole number of an infinity
            raise EvalError(f"{name}: a value goes out of range") from None


def _scalar(name: str, v: list[Any], ctx: Context) -> Any:
    if name == "ISNULL":
        return v[0] is None
    if name == "TEXT":
        return "" if v[0] is None else _text_of(v[0], ctx)
    if name == "CONCAT":
        out: Any = ""
        for x in v:
            out = _join(out, "" if x is None else _text_of(x, ctx))
        return out
    if name == "FORMAT":
        if v[0] is None:
            return ""
        pattern = v[1] if isinstance(v[1], str) else None
        return formats.display(v[0], pattern, ctx.loc)
    if name in ("DATEADD", "DATEDIFF"):
        unit = v[0]
        if any(x is None for x in v[1:]):
            return None
        if name == "DATEADD":
            n, d = v[1], v[2]
            if not isinstance(d, date):
                raise EvalError(f"DATEADD moves a date, and {_kind(d)} is here")
            n = int(_num(n, "DATEADD"))
            if unit in ("d", "w"):
                return d + timedelta(days=n * (7 if unit == "w" else 1))
            return _add_months(d, n if unit == "m" else 12 * n)
        a, b = v[1], v[2]
        if not (isinstance(a, date) and isinstance(b, date)):
            raise EvalError("DATEDIFF counts between two dates")
        a, b = _cmp_pair(a, b)
        if unit in ("d", "w"):
            # whole days, towards zero: between date-times 10 hours back are no day (`.days` floors to -1)
            days = int((b - a) / timedelta(days=1))
            return days if unit == "d" else int(days / 7)
        months = (b.year - a.year) * 12 + b.month - a.month
        if months > 0 and b.day < a.day:
            months -= 1
        elif months < 0 and b.day > a.day:
            months += 1
        return months if unit == "m" else int(months / 12)
    if any(x is None for x in v):
        return None
    if name == "ROUND":
        return _round(_num(v[0], "ROUND"), int(_num(v[1], "ROUND")) if len(v) > 1 else 0)
    if name == "ABS":
        return abs(_num(v[0], "ABS"))
    if name == "FLOOR":
        return math.floor(_num(v[0], "FLOOR"))
    if name == "CEILING":
        return math.ceil(_num(v[0], "CEILING"))
    if name == "POWER":
        return _power(_num(v[0], "POWER"), _num(v[1], "POWER"))
    if name in ("UPPER", "LOWER", "TRIM", "LEN", "LEFT", "RIGHT", "MID", "REPLACE", "CONTAINS", "STARTSWITH"):
        t = v[0] if isinstance(v[0], str) else formats.display(v[0], None, ctx.loc)
        if name == "UPPER":
            return t.upper()
        if name == "LOWER":
            return t.lower()
        if name == "TRIM":
            return t.strip()
        if name == "LEN":
            return len(t)
        if name == "LEFT":
            return t[:max(int(_num(v[1], "LEFT")), 0)]
        if name == "RIGHT":
            n = max(int(_num(v[1], "RIGHT")), 0)
            return t[-n:] if n else ""
        if name == "MID":
            start, count = max(int(_num(v[1], "MID")), 1) - 1, max(int(_num(v[2], "MID")), 0)
            return t[start:start + count]
        if name == "REPLACE":
            old, new = str(v[1]), str(v[2])
            if not old:
                return t
            if len(t) + t.count(old) * (len(new) - len(old)) > MAX_TEXT_CHARS:
                raise EvalError(f"REPLACE makes a text longer than {MAX_TEXT_CHARS:,} characters")
            return t.replace(old, new)
        if name == "CONTAINS":
            return str(v[1]) in t
        return t.startswith(str(v[1]))
    if name == "DATE":
        try:
            return date(int(_num(v[0], "DATE")), int(_num(v[1], "DATE")), int(_num(v[2], "DATE")))
        except (ValueError, OverflowError):
            return None
    if name in ("YEAR", "MONTH", "DAY", "WEEKDAY"):
        d = v[0]
        if not isinstance(d, date):
            raise EvalError(f"{name} reads a date, and {_kind(d)} is here")
        return {"YEAR": d.year, "MONTH": d.month, "DAY": d.day, "WEEKDAY": d.isoweekday()}[name]
    raise AssertionError(name)  # pragma: no cover


def text_value(value: str) -> tuple[str, bool]:
    """A text item's value: an expression when it starts with «=», plain text otherwise."""
    if value.startswith("="):
        return value[1:], True
    return value, False
