"""Dynamic placeholders of the «API call» datasource: a tiny parser of our own, no eval.

A call that asks for «the last 30 days» or «since the previous refresh» cannot
hold fixed dates: its path, query, headers and body hold placeholders resolved
at each refresh, in the datasource's time zone.

    {today} {yesterday} {now} {start_of_month} {start_of_week} {start_of_year}
    {last_refresh}                    the start of the last successful refresh
    {today-1}  {today-7d}  {today+1d} {today-2w} {today-1m} {today-1y}
    {now-2h}   {now-30min} {now+15s}  (hours, minutes, seconds: {now} and {last_refresh} only)
    {today:%Y%m%d}  {now:epoch}  {now:epoch_ms}  {now:iso}
    {symbol}                          a variable of the datasource (no offset, no format)
    {{  }}                            a literal brace

A number without a unit on a date is days ({today-1}); `m` is months, clamped to
the end of the month (31 March - 1m = 28 February), `min` is minutes. Dates
render as YYYY-MM-DD, date-times as ISO 8601 with the offset of the time zone.
`{last_refresh}` on the first refresh takes the datasource's initial expression
(`today-365`, `2024-01-01`…); with none, using it is an error at save time.

Everything here is a closed grammar checked with a regular expression and a
table of names: nothing a user types is ever evaluated.
"""
from __future__ import annotations

import re
from calendar import monthrange
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import Callable, Union
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DATE_BASES = ("today", "yesterday", "start_of_month", "start_of_week", "start_of_year")
DATETIME_BASES = ("now", "last_refresh")
BUILTINS = frozenset(DATE_BASES + DATETIME_BASES)

_BODY = re.compile(
    r"^(?P<name>[A-Za-z_][A-Za-z0-9_]*)"
    r"(?:(?P<sign>[+-])(?P<amount>\d{1,5})(?P<unit>min|d|w|m|y|h|s)?)?"
    r"(?::(?P<fmt>.+))?$",
    re.S,
)
_VARIABLE_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
# the strftime directives of the C library that make sense for a request (GNU flags `-` and `_` allowed)
_STRFTIME = re.compile(r"%[-_]?[aAbBcdDeFgGhHIjklmMnpPrRsStTuUVwWxXyYzZ%]")
# readable formats, as people write them for an API (yyyy-MM-dd, ddmmyyyy, HH:mm:ss); text between
# single quotes is literal ('T'), and '' is an apostrophe (inside quotes too, as in Java's patterns).
# `mm` follows Excel's rule: minutes right after hours or right before seconds, the month otherwise —
# so ddmmyyyy is a date. Every other letter is refused: text goes in quotes.
_READABLE = re.compile(r"''|'(?:[^']|'')*'|yyyy|yy|MM|mm|dd|HH|hh|ss|[A-Za-z]|[^A-Za-z']+|'")
_READABLE_TOKENS = {"yyyy": "%Y", "yy": "%y", "MM": "%m", "dd": "%d", "HH": "%H", "ss": "%S"}
_QUOTED = re.compile(r"'(?:[^']|'')*'")


def _is_strftime(fmt: str) -> bool:
    """A format is strftime when it has a % OUTSIDE quoted text; else it is readable."""
    return "%" in _QUOTED.sub("", fmt)


def _readable_to_strftime(fmt: str, shown: str = "") -> str:
    """The strftime pattern of a readable format; PlaceholderError says what is wrong."""
    where = shown or f"'{fmt}'"
    tokens = _READABLE.findall(fmt)
    out: list[str] = []
    letters = [i for i, t in enumerate(tokens) if t[:1].isalpha()]
    for i, t in enumerate(tokens):
        if t == "''":
            out.append("'")
        elif t == "'":
            raise PlaceholderError(f"a quote is not closed in the format of {where}: text goes between 'quotes'")
        elif t.startswith("'"):
            out.append(t[1:-1].replace("''", "'").replace("%", "%%"))
        elif not t[:1].isalpha():
            out.append(t.replace("%", "%%"))
        elif t == "hh":
            raise PlaceholderError(f"{where}: hh (12-hour clock) is not supported: use HH (00-23)")
        elif t == "mm":
            k = letters.index(i)
            before = tokens[letters[k - 1]] if k > 0 else ""
            after = tokens[letters[k + 1]] if k + 1 < len(letters) else ""
            out.append("%M" if before == "HH" or after == "ss" else "%m")
        elif t in _READABLE_TOKENS:
            out.append(_READABLE_TOKENS[t])
        else:
            raise PlaceholderError(
                f"unknown letter '{t}' in the format of {where}: use yyyy yy MM dd HH mm ss, and put letters that are "
                f"text in quotes: '{t}' (or write a strftime pattern such as %Y-%m-%d, or epoch, epoch_ms, iso)"
            )
        if t[:1].isalpha() and i > 0 and tokens[i - 1] == t:
            raise PlaceholderError(f"{where}: {t} is written twice in a row ({t}{t}): write it once")
    if not letters:
        raise PlaceholderError(f"{where}: the format has no date or time part (yyyy, MM, dd, HH, mm, ss)")
    return "".join(out)


class PlaceholderError(ValueError):
    """A message for the person who wrote the template (shown when saving)."""


@dataclass(frozen=True)
class Placeholder:
    raw: str
    name: str
    sign: str = ""
    amount: int = 0
    unit: str = ""
    fmt: str = ""

    @property
    def text(self) -> str:
        return "{" + self.raw + "}"


Part = Union[str, Placeholder]


def zone(name: str) -> ZoneInfo:
    """The time zone by its IANA name (Europe/Rome); UTC when empty."""
    name = (name or "UTC").strip() or "UTC"
    if ".." in name or name.startswith("/"):
        raise PlaceholderError(f"unknown time zone '{name}': use an IANA name such as Europe/Rome or UTC")
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError) as e:
        raise PlaceholderError(f"unknown time zone '{name}': use an IANA name such as Europe/Rome or UTC") from e


def check_variable_name(name: str) -> None:
    if not _VARIABLE_NAME.match(name or ""):
        raise PlaceholderError(
            f"invalid variable name '{name}': letters, digits and _, not starting with a digit"
        )
    if name in BUILTINS:
        raise PlaceholderError(f"'{name}' is a reserved placeholder name: choose another name for the variable")


def _check_format(ph: Placeholder) -> None:
    fmt = ph.fmt
    if not fmt or fmt in ("epoch", "epoch_ms", "iso"):
        return
    if not _is_strftime(fmt):  # no % outside quotes = a readable format (yyyy-MM-dd…)
        _readable_to_strftime(fmt, ph.text)
        return
    rest = _STRFTIME.sub("", fmt)
    if "%" in rest:
        raise PlaceholderError(f"bad format in {ph.text}: a '%' is not followed by a known directive (%Y, %m, %d, %H…)")
    directives = {d[-1] for d in _STRFTIME.findall(fmt)}
    if "s" in directives:
        # %s is the C library's, computed in the worker's own zone and not portable: never the right epoch
        raise PlaceholderError(f"{ph.text}: %s is not supported: use :epoch (or :epoch_ms) for seconds since 1970")
    if directives & {"z", "Z"} and ph.name in DATE_BASES:
        raise PlaceholderError(f"{ph.text}: %z and %Z are empty on a date: use them on {{now}} or {{last_refresh}}")


def _placeholder(raw: str) -> Placeholder:
    body = raw.strip()
    if not body:
        raise PlaceholderError("empty placeholder {}: write a name such as {today}, or {{}} for literal braces")
    m = _BODY.match(body)
    if not m:
        raise PlaceholderError(
            f"bad placeholder {{{raw}}}: expected a name, an optional offset (+1d, -2h, -1m) and an optional "
            "format after ':' — e.g. {today-7d:%Y%m%d}"
        )
    return Placeholder(raw=raw, name=m["name"], sign=m["sign"] or "", amount=int(m["amount"] or 0),
                       unit=m["unit"] or "", fmt=m["fmt"] or "")


def parse(template: str) -> list[Part]:
    """The template as text and placeholders. `{{` and `}}` are literal braces."""
    parts: list[Part] = []
    text: list[str] = []
    i, s = 0, template or ""
    while i < len(s):
        c = s[i]
        if c == "{":
            if s.startswith("{{", i):
                text.append("{")
                i += 2
                continue
            end = s.find("}", i + 1)
            if end < 0:
                raise PlaceholderError(f"a placeholder is not closed in '{s}': add '}}', or write {{{{ for a literal brace")
            inner = s[i + 1:end]
            if "{" in inner:
                raise PlaceholderError(f"a placeholder is not closed before another '{{' in '{s}'")
            if text:
                parts.append("".join(text))
                text = []
            parts.append(_placeholder(inner))
            i = end + 1
            continue
        if c == "}":
            if s.startswith("}}", i):
                text.append("}")
                i += 2
                continue
            raise PlaceholderError(f"a single '}}' in '{s}': write }}}} for a literal brace")
        text.append(c)
        i += 1
    if text:
        parts.append("".join(text))
    return parts


def _check_placeholder(ph: Placeholder, variables: set[str] | frozenset[str], has_initial: bool) -> None:
    if ph.name not in BUILTINS:
        if ph.name not in variables:
            known = ", ".join("{" + n + "}" for n in (*DATE_BASES, *DATETIME_BASES))
            raise PlaceholderError(
                f"unknown placeholder {ph.text}: use {known} or a variable of the datasource"
            )
        if ph.sign or ph.fmt:
            raise PlaceholderError(f"the variable {{{ph.name}}} takes no offset or format ({ph.text})")
        return
    if ph.sign and ph.name in DATE_BASES and ph.unit in ("h", "min", "s"):
        raise PlaceholderError(
            f"{ph.text}: hours, minutes and seconds apply to {{now}} and {{last_refresh}}, not to a date"
        )
    if ph.sign and ph.name in DATETIME_BASES and not ph.unit:
        raise PlaceholderError(f"{ph.text}: say the unit of the offset (d, w, m, y, h, min, s), e.g. {{{ph.name}-2h}}")
    if ph.name == "last_refresh" and not has_initial:
        raise PlaceholderError(
            "{last_refresh} has no value on the first refresh: set the initial value of last refresh "
            "(e.g. today-30) in the datasource"
        )
    _check_format(ph)


def check(template: str, variables: set[str] | frozenset[str] = frozenset(), has_initial: bool = False) -> set[str]:
    """Checks the template without resolving it; returns the variables it uses."""
    used: set[str] = set()
    for part in parse(template):
        if isinstance(part, Placeholder):
            _check_placeholder(part, variables, has_initial)
            if part.name not in BUILTINS:
                used.add(part.name)
    return used


@dataclass
class Context:
    now: datetime  # aware, in the time zone
    tz: ZoneInfo
    last_refresh: datetime | None = None  # aware, in the time zone
    variables: dict[str, str] = field(default_factory=dict)


def _parse_instant(text: str, tz: ZoneInfo) -> datetime:
    s = text.strip()
    if s.endswith(("Z", "z")):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)  # stored instants are UTC
    return dt.astimezone(tz)


def make_context(now_utc: datetime, tz_name: str, last_refresh: str | None, initial: str,
                 variables: dict[str, str]) -> Context:
    """`last_refresh` = the start of the last successful refresh (ISO, UTC when
    naive), or None on the first one, which takes `initial`."""
    tz = zone(tz_name)
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)
    now = now_utc.astimezone(tz)
    lr: datetime | None = None
    if last_refresh:
        try:
            lr = _parse_instant(last_refresh, tz)
        except ValueError as e:
            raise PlaceholderError(f"invalid last refresh time '{last_refresh}'") from e
    elif (initial or "").strip():
        lr = resolve_initial(initial, now_utc, tz)
    return Context(now=now, tz=tz, last_refresh=lr, variables=dict(variables))


def _add_months(d: date, n: int) -> date:
    y, m = divmod(d.month - 1 + n, 12)
    y += d.year
    if not 1 <= y <= 9999:
        raise PlaceholderError("the date goes out of range")
    return d.replace(year=y, month=m + 1, day=min(d.day, monthrange(y, m + 1)[1]))


def _shift(value: date | datetime, ph: Placeholder) -> date | datetime:
    if not ph.sign:
        return value
    n = ph.amount if ph.sign == "+" else -ph.amount
    unit = ph.unit or "d"
    try:
        if unit in ("m", "y"):
            months = n if unit == "m" else 12 * n
            if isinstance(value, datetime):
                moved = _add_months(value.date(), months)
                return datetime.combine(moved, value.timetz())
            return _add_months(value, months)
        if unit in ("d", "w"):
            # on a date-time: the same wall-clock time that many days away (zoneinfo
            # recomputes the offset across a change of summer time)
            return value + timedelta(days=n * (7 if unit == "w" else 1))
        assert isinstance(value, datetime)
        delta = timedelta(hours=n) if unit == "h" else timedelta(minutes=n) if unit == "min" else timedelta(seconds=n)
        tz = value.tzinfo
        return (value.astimezone(timezone.utc) + delta).astimezone(tz)
    except (OverflowError, ValueError) as e:
        raise PlaceholderError(f"{ph.text}: the date goes out of range") from e


def evaluate(ph: Placeholder, ctx: Context) -> date | datetime | str:
    """The value of a placeholder, before formatting."""
    if ph.name not in BUILTINS:
        if ph.name not in ctx.variables:
            raise PlaceholderError(f"unknown placeholder {ph.text}")
        return ctx.variables[ph.name]
    today = ctx.now.date()
    base: date | datetime
    if ph.name == "today":
        base = today
    elif ph.name == "yesterday":
        base = today - timedelta(days=1)
    elif ph.name == "start_of_month":
        base = today.replace(day=1)
    elif ph.name == "start_of_week":
        base = today - timedelta(days=today.weekday())
    elif ph.name == "start_of_year":
        base = today.replace(month=1, day=1)
    elif ph.name == "now":
        base = ctx.now
    else:  # last_refresh
        if ctx.last_refresh is None:
            raise PlaceholderError(
                "{last_refresh} has no value on the first refresh: set the initial value of last refresh "
                "(e.g. today-30) in the datasource"
            )
        base = ctx.last_refresh
    return _shift(base, ph)


def format_value(value: date | datetime | str, ph: Placeholder, tz: ZoneInfo) -> str:
    if isinstance(value, str):
        return value
    fmt = ph.fmt
    if fmt in ("epoch", "epoch_ms"):
        dt = value if isinstance(value, datetime) else datetime.combine(value, time(0), tzinfo=tz)
        seconds = dt.timestamp()
        return str(int(seconds * 1000)) if fmt == "epoch_ms" else str(int(seconds))
    if not fmt or fmt == "iso":
        return value.isoformat(timespec="seconds") if isinstance(value, datetime) else value.isoformat()
    _check_format(ph)
    if not _is_strftime(fmt):
        fmt = _readable_to_strftime(fmt, ph.text)
    try:
        return value.strftime(fmt)
    except ValueError as e:
        raise PlaceholderError(f"bad format in {ph.text}: {e}") from e


def render(template: str, ctx: Context, quote: Callable[[str], str] | None = None) -> str:
    """The template with every placeholder resolved; `quote` is applied to the
    VALUES only (e.g. URL-encoding of a path), never to the text around them."""
    out: list[str] = []
    for part in parse(template):
        if isinstance(part, str):
            out.append(part)
            continue
        value = format_value(evaluate(part, ctx), part, ctx.tz)
        out.append(quote(value) if quote else value)
    return "".join(out)


def single_placeholder(template: str) -> Placeholder | None:
    """The placeholder when the template is nothing but one placeholder."""
    try:
        parts = parse(template)
    except PlaceholderError:
        return None
    return parts[0] if len(parts) == 1 and isinstance(parts[0], Placeholder) else None


_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def resolve_initial(expr: str, now_utc: datetime, tz: ZoneInfo) -> datetime:
    """The initial value of {last_refresh}: an expression without braces
    (`today-365`, `now-2h`, `start_of_month`) or an ISO date / date-time. A date
    is its midnight in the time zone."""
    s = (expr or "").strip()
    if not s:
        raise PlaceholderError("the initial value of last refresh is empty")
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)
    value: date | datetime
    if _ISO_DATE.match(s):
        try:
            value = date.fromisoformat(s)
        except ValueError as e:
            raise PlaceholderError(f"invalid date '{s}' as the initial value of last refresh") from e
    elif re.match(r"^\d{4}-\d{2}-\d{2}[T ]", s):
        try:
            return _parse_instant(s, tz)
        except ValueError as e:
            raise PlaceholderError(f"invalid date-time '{s}' as the initial value of last refresh") from e
    else:
        if "{" in s or "}" in s:
            raise PlaceholderError(f"write the initial value of last refresh without braces: '{s.strip('{}')}'")
        ph = _placeholder(s)
        if ph.name not in BUILTINS or ph.name == "last_refresh" or ph.fmt:
            raise PlaceholderError(
                f"invalid initial value of last refresh '{s}': use e.g. today-30, now-2h, start_of_month or 2024-01-01"
            )
        _check_placeholder(ph, frozenset(), has_initial=True)
        value = evaluate(ph, Context(now=now_utc.astimezone(tz), tz=tz))
    if isinstance(value, datetime):
        return value
    return datetime.combine(value, time(0)).replace(tzinfo=tz)
