"""A report's parameters: their values, and their place in a dataset's SQL.
docs/specs/reports.md, «The definition» (parameters, datasets); the report itself in
services/reports.py.

Values arrive as text — a query string, a form, a flow group's cell — and are converted
to the parameter's type and checked against its choices before anything runs. A value
or a default written as one placeholder of Tabularia's language (`{today-30}`) is
resolved by the engine, in the report's time zone.

In a dataset's SQL a parameter is written `{name}`, and the server puts in its place a
literal of the dialect — a string quoted and escaped, a number checked as a number, a
date as a typed literal, a list as `(a, b, c)` for `IN {regions}` — never the text as it
was typed. The SQL is read by a scanner that knows where strings, quoted names and
comments start and end, each database's own quotes included (PostgreSQL's and
ClickHouse's dollar quotes, Oracle's q'[…]', BigQuery's triple quotes):

- outside them, `{name}` is the parameter's literal (NULL when an optional one has no
  value), a placeholder its value as a typed literal (`{today-30}` → `DATE
  '2026-09-28'`), `{{` and `}}` a brace;
- inside a string or a quoted name a parameter is refused («write {region} without
  quotes: Tabularia quotes it»: text pasted between quotes is how injections start), a
  placeholder becomes its text (`'{today-30}'` → `'2026-09-28'`), `{{today}}` the text
  `{today}`; any other brace stays as it is (JSON in a string);
- in a comment nothing changes.

Where a string ends can depend on the server: MySQL without NO_BACKSLASH_ESCAPES and
PostgreSQL with standard_conforming_strings off read a backslash as an escape, and
PostgreSQL nests block comments where the others do not. The scanner reads the SQL each
way and refuses when the readings place a brace differently: a value never goes where
the database might read it as part of a string.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from itertools import zip_longest
from typing import Any, Iterable, Mapping, Optional

from fastapi import HTTPException
from sqlglot import exp

from app.core.engine_client import CHECK, get_engine_client

# the names of the placeholder language (backend/app/core/placeholders.py): a parameter
# cannot take one, nor `format` (the API's query string names the file's format with it)
BUILTINS = frozenset({"today", "yesterday", "start_of_month", "start_of_week", "start_of_year", "now",
                      "last_refresh"})
RESERVED = BUILTINS | {"run_id", "flow", "date", "datetime", "format"}
NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
# the inside of a brace: a name, an optional offset, an optional format (the engine's grammar)
_BODY = re.compile(r"^(?P<name>[A-Za-z_][A-Za-z0-9_]*)(?P<offset>[+-]\d{1,5}(?:min|d|w|m|y|h|s)?)?(?::(?P<fmt>.+))?$",
                   re.S)
TYPES = ("text", "number", "date", "boolean")
LANGS = ("en", "it", "de", "es", "fr")  # a report's locale: the names of months and days ({today:MMMM})
MAX_TEXT = 1000  # characters of a text value
MAX_VALUES = 1000  # values of a multi parameter
# the value that says «no value» out loud, in a query string, a body or a Pushmail cell: an
# optional parameter then has none whatever its default (an empty value takes the default)
NO_VALUE = "{none}"
_NUMBER = re.compile(r"^[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d{1,3})?$")
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_ISO_DATETIME = re.compile(r"^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}(?::\d{2})?)(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?$")
_TRUE, _FALSE = ("true", "1", "yes"), ("false", "0", "no")


def bad_value(name: str, message: str) -> HTTPException:
    """A value that does not fit: 400 with the parameter's name, for the API's callers."""
    return HTTPException(status_code=400, detail={"message": f"Parameter «{name}»: {message}", "parameter": name})


def _refuse(message: str) -> HTTPException:
    return HTTPException(status_code=422, detail=message)


# ── values ────────────────────────────────────────────────────────────────────
def placeholder_of(text: Any) -> Optional[str]:
    """The inside of the placeholder when `text` is one placeholder of the language
    (`{today-30}` → `today-30`), else None: a value written that way is resolved."""
    if not isinstance(text, str):
        return None
    t = text.strip()
    if len(t) < 3 or t[0] != "{" or t[-1] != "}" or t.startswith("{{") or "{" in t[1:-1]:
        return None
    m = _BODY.match(t[1:-1].strip())
    return t[1:-1].strip() if m and m["name"] in BUILTINS else None


def convert(param: Mapping[str, Any], text: str) -> Any:
    """One value of the parameter, from its text: str, int or float, date, bool."""
    name, kind = param["name"], param["type"]
    if not isinstance(text, str):
        text = "true" if text is True else "false" if text is False else str(text)
    if "\x00" in text:
        raise bad_value(name, "a value cannot hold a NUL character")
    if kind == "text":
        if len(text) > MAX_TEXT:
            raise bad_value(name, f"a value is at most {MAX_TEXT} characters")
        return text
    t = text.strip()
    if kind == "number":
        if not _NUMBER.match(t):
            raise bad_value(name, f"'{t[:40]}' is not a number (decimals after a dot: 1234.5)")
        d = Decimal(t)
        if abs(d) >= Decimal(10) ** 15:
            raise bad_value(name, f"'{t[:40]}' is too large a number")
        return int(d) if d == d.to_integral_value() and not re.search(r"[.eE]", t) else float(d)
    if kind == "date":
        try:
            if not _ISO_DATE.match(t):
                raise ValueError
            return date.fromisoformat(t)
        except ValueError:
            raise bad_value(name, f"'{t[:40]}' is not a date: write it as yyyy-mm-dd") from None
    if kind == "boolean":
        if t.lower() in _TRUE:
            return True
        if t.lower() in _FALSE:
            return False
        raise bad_value(name, f"'{t[:40]}' is not true or false")
    raise bad_value(name, f"unknown type {kind}")


def to_json(value: Any) -> Any:
    """A converted value as the engine and the audit receive it: dates as yyyy-mm-dd."""
    if isinstance(value, list):
        return [to_json(v) for v in value]
    return value.isoformat() if isinstance(value, date) else value


def lang_of(definition: Mapping[str, Any]) -> str:
    """The language a report's placeholders write the names of months and days in: its own."""
    locale = definition.get("locale")
    return locale if locale in LANGS else "en"


async def resolve_placeholders(bodies: Iterable[str], timezone: str,
                               lang: str = "en") -> dict[str, tuple[Optional[str], Optional[str]]]:
    """{inside: (text, error)} of placeholders of the language, resolved NOW by the engine
    in `timezone`, the names of months and days in `lang` (twenty to a call, the engine's
    limit)."""
    wanted = sorted(set(bodies))
    out: dict[str, tuple[Optional[str], Optional[str]]] = {}
    for start in range(0, len(wanted), 20):
        chunk = wanted[start:start + 20]
        templates = {f"p{i}": "{" + body + "}" for i, body in enumerate(chunk)}
        try:
            resp = await get_engine_client().post("/placeholders/preview", timeout=CHECK, json={
                "kind": "input", "templates": templates, "timezone": timezone or "UTC", "lang": lang})
        except Exception as e:  # noqa: BLE001 — said in words
            raise HTTPException(status_code=502,
                                detail=f"The engine could not resolve the placeholders: {type(e).__name__}") from None
        if resp.status_code >= 400:
            raise HTTPException(status_code=502,
                                detail=f"The engine could not resolve the placeholders ({resp.status_code})")
        answer = resp.json()
        if answer.get("error"):
            raise _refuse(f"The report's time zone: {answer['error']}")
        fields = answer.get("fields") or {}
        for i, body in enumerate(chunk):
            got = fields.get(f"p{i}") or {}
            out[body] = (got.get("value"), got.get("error") or (None if got.get("value") is not None else "no value"))
    return out


def _texts(param: Mapping[str, Any], given: Optional[list]) -> Optional[list]:
    """The texts a parameter takes: the ones given, else its default; None = no value
    (none given and no default, or NO_VALUE given)."""
    name, multi = param["name"], bool(param.get("multi"))
    if given is not None:
        given = [g for g in given if not (isinstance(g, str) and not g.strip())]  # `?since=` is no value
    if given and len(given) == 1 and isinstance(given[0], str) and given[0].strip() == NO_VALUE:
        return None
    if given:
        if not multi and len(given) > 1:
            raise bad_value(name, "it takes one value")
        if len(given) > MAX_VALUES:
            raise bad_value(name, f"at most {MAX_VALUES} values")
        return list(given)
    default = param.get("default")
    if default is None or default == []:
        return None
    return list(default) if isinstance(default, list) else [default]


def _given_texts(definition: Mapping[str, Any], given: Mapping[str, list]) -> tuple[dict, dict]:
    params = {p["name"]: p for p in definition.get("parameters") or []}
    for name in given:
        if name not in params:
            raise bad_value(name, "the report has no parameter by this name")
    return params, {name: _texts(p, given.get(name)) for name, p in params.items()}


async def values_for(definition: Mapping[str, Any], given: Mapping[str, list]) -> dict[str, Any]:
    """Every parameter's value: the ones given (texts; a list per name), the defaults for
    the others. 400 naming the parameter when a value does not fit: an unknown name, a
    required one without a value, a text that is not of its type, a value outside its
    fixed choices. The choices taken from a dataset are checked by the caller, which
    reads them (services/reports.py)."""
    params, texts = _given_texts(definition, given)
    bodies = {b for ts in texts.values() for t in ts or [] if (b := placeholder_of(t))}
    resolved = await resolve_placeholders(bodies, definition.get("timezone") or "UTC", lang_of(definition)) \
        if bodies else {}
    return _converted(params, texts, resolved)


def check_values(definition: Mapping[str, Any], given: Mapping[str, list]) -> None:
    """`values_for` without asking the engine anything: what a flow group's Pushmail cell
    is checked against when it is saved and before a scheduled run. A placeholder
    (`{start_of_month}`) is taken for what it will be; the engine resolves it when the
    report is rendered, and the render says then if it does not fit."""
    _converted(*_given_texts(definition, given), None)


def _converted(params: dict, texts: dict, resolved: Optional[dict]) -> dict[str, Any]:
    """The values converted to their types; `resolved` None leaves the placeholders out."""
    values: dict[str, Any] = {}
    for name, p in params.items():
        ts = texts[name]
        if ts is None:
            if p.get("required"):
                raise bad_value(name, "it needs a value")
            values[name] = [] if p.get("multi") else None
            continue
        converted, seen = [], set()
        for t in ts:
            body = placeholder_of(t)
            if body is not None:
                if resolved is None:
                    continue
                text, error = resolved[body]
                if error:
                    raise bad_value(name, f"{{{body}}}: {error}")
                t = text
            elif isinstance(t, str) and " " in t and placeholder_of(t.replace(" ", "+")) is not None:
                raise bad_value(name, f"'{t.strip()[:40]}' is not a placeholder: in an address a + is read as a "
                                      "space, write it %2B")
            value = convert(p, t)
            if _key(value) not in seen:  # a value given twice is one value
                seen.add(_key(value))
                converted.append(value)
        if not converted:  # only placeholders, not resolved here
            values[name] = [] if p.get("multi") else None
            continue
        fixed = (p.get("choices") or {}).get("values")
        if fixed is not None:
            allowed = {_key(convert(p, c)) for c in fixed}
            outside = [v for v in converted if _key(v) not in allowed]
            if outside:
                raise bad_value(name, f"'{to_json(outside[0])}' is not one of its choices")
        values[name] = converted if p.get("multi") else converted[0]
    return values


def _key(value: Any) -> Any:
    # 1 and 1.0 are the same choice; True and 1 are not
    return (type(value) is bool, float(value)) if isinstance(value, (int, float)) and not isinstance(value, bool) \
        else (type(value).__name__, value)


# ── the SQL ───────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Dialect:
    sqlglot: str  # how sqlglot reads and writes it
    hash_comments: bool = False  # `#` starts a comment
    dollar_quotes: bool = False  # PostgreSQL's $tag$…$tag$
    e_strings: bool = False  # PostgreSQL's E'…', where a backslash always escapes
    brackets: bool = False  # SQL Server's [quoted name]
    server: bool = False  # a database of someone else's, whose settings change how it reads a string
    q_quotes: bool = False  # Oracle's q'[…]', nq'{…}': an apostrophe inside does not close them
    triple_quotes: bool = False  # BigQuery's '''…''' and """…"""


# by engine (the catalog) and by connection type (a database)
DIALECTS: dict[str, Dialect] = {
    "polars": Dialect("postgres", dollar_quotes=True),
    "duckdb": Dialect("duckdb", dollar_quotes=True, e_strings=True),
    "chdb": Dialect("clickhouse", hash_comments=True, dollar_quotes=True),  # $heredoc$…$heredoc$
    "clickhouse": Dialect("clickhouse", hash_comments=True, dollar_quotes=True),
    "bigquery": Dialect("bigquery", hash_comments=True, triple_quotes=True),
    "postgresql": Dialect("postgres", dollar_quotes=True, e_strings=True, server=True),
    "mysql": Dialect("mysql", hash_comments=True, server=True),
    "mariadb": Dialect("mysql", hash_comments=True, server=True),
    "trino": Dialect("trino", server=True),
    "oracle": Dialect("oracle", server=True, q_quotes=True),
    "sqlserver": Dialect("tsql", brackets=True, server=True),
}


def dialect_of(key: Optional[str]) -> Dialect:
    return DIALECTS.get(key or "polars", DIALECTS["polars"])


@dataclass(frozen=True)
class Brace:
    start: int
    end: int  # after the last character
    quoted: bool  # inside a string or a quoted name
    kind: str  # "open" ({{), "close" (}}), "lone" (}), "unclosed" ({), "name" ({…}), "escaped" ({{…}} in a string)
    inner: str = ""


_DOLLAR = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)?\$")


def _ident(ch: str) -> bool:
    return ch.isalnum() or ch in "_$"


def _quoted_end(sql: str, i: int, close: str, backslash: bool) -> int:
    """The index after the quote that closes the one at `i` (the end of the SQL when none does)."""
    j, n = i + 1, len(sql)
    while j < n:
        ch = sql[j]
        if backslash and ch == "\\":
            j += 2
            continue
        if ch == close:
            if j + 1 < n and sql[j + 1] == close:  # doubled: the quote itself
                j += 2
                continue
            return j + 1
        j += 1
    return n


_Q_CLOSE = {"[": "]", "{": "}", "(": ")", "<": ">"}


def _q_quote(sql: str, i: int) -> Optional[tuple[int, int, int]]:
    """Oracle's q'X…X' (nq'X…X' too) starting at `i`: where its text starts, where its
    closing starts, and the index after it; None when there is none at `i`."""
    j = i + 1 if sql[i] in "nN" else i
    if (i > 0 and _ident(sql[i - 1])) or sql[j:j + 1] not in ("q", "Q") or sql[j + 1:j + 2] != "'" \
            or j + 2 >= len(sql) or sql[j + 2].isspace():
        return None
    close = _Q_CLOSE.get(sql[j + 2], sql[j + 2]) + "'"
    end = sql.find(close, j + 3)
    return (j + 3, end, end + 2) if end >= 0 else (j + 3, len(sql), len(sql))


def _triple_end(sql: str, i: int, backslash: bool) -> int:
    """The index where BigQuery's triple quote opened at `i` closes (the end of the SQL
    when none does)."""
    quote, j, n = sql[i] * 3, i + 3, len(sql)
    while j < n:
        if backslash and sql[j] == "\\":
            j += 2
            continue
        if sql.startswith(quote, j):
            return j
        j += 1
    return n


def _comment_end(sql: str, i: int, nested: bool) -> int:
    depth, j, n = 1, i + 2, len(sql)
    while j < n:
        if sql.startswith("*/", j):
            depth -= 1
            j += 2
            if depth == 0 or not nested:
                return j
        elif nested and sql.startswith("/*", j):
            depth += 1
            j += 2
        else:
            j += 1
    return n


def _in_quotes(sql: str, start: int, stop: int) -> list[Brace]:
    """The braces that mean something inside quoted text: `{name…}` and `{{name…}}`."""
    out, k = [], start
    while True:
        k = sql.find("{", k, stop)
        if k < 0:
            return out
        if sql.startswith("{{", k):
            end = sql.find("}}", k + 2, stop)
            if end >= 0 and _BODY.match(sql[k + 2:end].strip()):
                out.append(Brace(k, end + 2, True, "escaped", sql[k + 2:end]))
                k = end + 2
            else:
                k += 2
            continue
        end = sql.find("}", k + 1, stop)
        inner = sql[k + 1:end] if end >= 0 else ""
        if end >= 0 and "{" not in inner and _BODY.match(inner.strip()):
            out.append(Brace(k, end + 1, True, "name", inner))
            k = end + 1
        else:
            k += 1


def _read(sql: str, d: Dialect, backslash: bool, nested: bool) -> list[Brace]:
    """The braces of the SQL that mean something, with where they are: one reading."""
    out: list[Brace] = []
    i, n = 0, len(sql)
    while i < n:
        c = sql[i]
        if sql.startswith("--", i) or (c == "#" and d.hash_comments):
            j = sql.find("\n", i)
            i = n if j < 0 else j
        elif sql.startswith("/*", i):
            i = _comment_end(sql, i, nested)
        elif d.q_quotes and c in "nNqQ" and (q := _q_quote(sql, i)) is not None:
            out.extend(_in_quotes(sql, q[0], q[1]))
            i = q[2]
        elif d.triple_quotes and c in "'\"" and sql.startswith(c * 3, i):
            j = _triple_end(sql, i, backslash)
            out.extend(_in_quotes(sql, i + 3, j))
            i = j + 3
        elif c in "'\"`" or (c == "[" and d.brackets):
            e_string = d.e_strings and c == "'" and i > 0 and sql[i - 1] in "eE" and (i < 2 or not _ident(sql[i - 2]))
            j = _quoted_end(sql, i, "]" if c == "[" else c, backslash or e_string)
            out.extend(_in_quotes(sql, i + 1, j))
            i = j
        elif c == "$" and d.dollar_quotes and (m := _DOLLAR.match(sql, i)) and not (i > 0 and _ident(sql[i - 1])):
            end = sql.find(m.group(0), m.end())
            out.extend(_in_quotes(sql, m.end(), n if end < 0 else end))
            i = n if end < 0 else end + len(m.group(0))
        elif sql.startswith("{{", i):
            out.append(Brace(i, i + 2, False, "open"))
            i += 2
        elif sql.startswith("}}", i):
            out.append(Brace(i, i + 2, False, "close"))
            i += 2
        elif c == "}":
            out.append(Brace(i, i + 1, False, "lone"))
            i += 1
        elif c == "{":
            end = sql.find("}", i + 1)
            if end < 0 or "{" in sql[i + 1:end]:
                out.append(Brace(i, i + 1, False, "unclosed"))
                i += 1
            else:
                out.append(Brace(i, end + 1, False, "name", sql[i + 1:end]))
                i = end + 1
        else:
            i += 1
    return out


def braces(sql: str, d: Dialect) -> list[Brace]:
    """The braces of the SQL, read every way its database might read it; 422 when the
    readings disagree on one."""
    if "{" not in sql and "}" not in sql:
        return []
    ways = [(b, nested) for b in ((False, True) if "\\" in sql else (False,))
            for nested in ((False, True) if "/*" in sql else (False,))]
    first = _read(sql, d, *ways[0])
    for way in ways[1:]:
        other = _read(sql, d, *way)
        if other != first:
            differ = next(a or b for a, b in zip_longest(first, other) if a != b)
            what = "a backslash" if way[0] else "a comment inside another"
            raise _refuse(f"The SQL is unclear near «{sql[differ.start:differ.end][:60]}»: {what} makes it uncertain "
                          "whether it is inside quoted text. Write the text before it without that")
    return first


def _parts(b: Brace) -> tuple[str, str, str]:
    m = _BODY.match(b.inner.strip())
    return (m["name"], m["offset"] or "", m["fmt"] or "") if m else ("", "", "")


def check_sql(sql: str, d: Dialect, parameters: Mapping[str, Any]) -> tuple[set[str], set[str]]:
    """The parameters and the placeholders (their insides) the SQL uses; 422 for a brace
    that is neither, a parameter inside quotes, an unclear reading."""
    used, placeholders = set(), set()
    for b in braces(sql, d):
        if b.kind == "lone":
            raise _refuse("A single } in the SQL: write }} for a literal brace")
        if b.kind == "unclosed":
            raise _refuse("A { in the SQL is not closed: add }, or write {{ for a literal brace")
        if b.kind != "name":
            continue
        name, offset, fmt = _parts(b)
        if b.quoted:
            if name in parameters:
                raise _refuse(f"Write {{{name}}} without quotes: Tabularia quotes it")
            if name in BUILTINS:
                placeholders.add(b.inner.strip())
            continue
        if name in parameters:
            if offset or fmt:
                raise _refuse(f"{{{b.inner}}}: a parameter takes no offset nor format")
            used.add(name)
        elif name in BUILTINS:
            placeholders.add(b.inner.strip())
        else:
            raise _refuse(f"{{{b.inner[:60]}}} is neither a parameter of the report nor a placeholder: "
                          "write {{ and }} for literal braces")
    return used, placeholders


def fill(sql: str, d: Dialect, parameters: Mapping[str, Any], values: Mapping[str, Any],
         placeholders: Mapping[str, str]) -> str:
    """The SQL the database reads: every parameter as a literal of the dialect, every
    placeholder resolved (`placeholders`: inside → text, see `resolve_placeholders`).
    Call `check_sql` first: it refuses what cannot be filled."""
    out, at = [], 0
    for b in braces(sql, d):
        out.append(sql[at:b.start])
        at = b.end
        name = _parts(b)[0]
        if b.kind == "open":
            out.append("{")
        elif b.kind == "close":
            out.append("}")
        elif b.kind == "escaped":
            out.append("{" + b.inner + "}")
        elif b.kind == "name" and b.quoted:
            if name in BUILTINS:
                text = placeholders[b.inner.strip()]
                if any(ch in text for ch in "'\"`\\]$\n"):
                    raise _refuse(f"{{{b.inner}}} gives «{text[:40]}», which cannot go inside quoted text")
                out.append(text)
            else:
                out.append(sql[b.start:b.end])  # text that only looks like a placeholder
        elif b.kind == "name" and name in parameters:
            out.append(literal(values.get(name), parameters[name]["type"], d, name))
        elif b.kind == "name" and name in BUILTINS:
            out.append(_placeholder_literal(placeholders[b.inner.strip()], d))
        else:
            out.append(sql[b.start:b.end])
    out.append(sql[at:])
    return "".join(out)


# ── literals ──────────────────────────────────────────────────────────────────
def _text(value: str, d: Dialect) -> str:
    if d.sqlglot == "tsql":
        return "N'" + value.replace("'", "''") + "'"  # a national string: no character lost to a code page
    if d.server and d.e_strings and "\\" in value:
        # E'…' reads a backslash as an escape whatever standard_conforming_strings says
        return "E'" + value.replace("\\", "\\\\").replace("'", "''") + "'"
    # MySQL and ClickHouse: the backslash doubled, the quote doubled (never \'), right with
    # and without NO_BACKSLASH_ESCAPES; BigQuery: \' and \\, it has no other way
    return exp.Literal.string(value).sql(dialect=d.sqlglot)


def _number(value: Any) -> str:
    text = str(value) if isinstance(value, int) else format(Decimal(repr(value)), "f")
    return f"({text})" if text.startswith("-") else text  # `a -{n}` must not become a comment


def _date(value: str, d: Dialect) -> str:
    if d.sqlglot == "clickhouse":
        return f"toDate('{value}')"
    if d.sqlglot == "tsql":
        return f"CAST('{value}' AS DATE)"
    return f"DATE '{value}'"


def _timestamp(value: str, d: Dialect) -> str:
    if d.sqlglot == "clickhouse":
        return f"toDateTime('{value}')"
    if d.sqlglot == "tsql":
        return f"CAST('{value.replace(' ', 'T')}' AS DATETIME2)"
    if d.sqlglot == "bigquery":
        return f"DATETIME '{value}'"
    return f"TIMESTAMP '{value}'"


def literal(value: Any, kind: str, d: Dialect, name: str = "") -> str:
    """A parameter's value as a literal of the dialect; a list as `(a, b, c)`."""
    if isinstance(value, list):
        if not value:
            raise _refuse(f"«{name}» has no value, and the SQL needs at least one: choose one")
        return "(" + ", ".join(literal(v, kind, d, name) for v in value) + ")"
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return ("1" if value else "0") if d.sqlglot in ("tsql", "oracle") else ("TRUE" if value else "FALSE")
    if isinstance(value, (int, float)):
        return _number(value)
    if isinstance(value, date):
        return _date(value.isoformat(), d)
    return _text(str(value), d)


def _placeholder_literal(text: str, d: Dialect) -> str:
    """A placeholder's value outside quotes: a date, a date-time in the report's zone
    (its wall clock), a whole number (an epoch), or else text."""
    if _ISO_DATE.match(text):
        return _date(text, d)
    m = _ISO_DATETIME.match(text)
    if m:
        clock = m.group(2) if len(m.group(2)) == 8 else m.group(2) + ":00"
        return _timestamp(f"{m.group(1)} {clock}", d)
    if re.fullmatch(r"-?\d{1,18}", text):
        return _number(int(text))
    return _text(text, d)
