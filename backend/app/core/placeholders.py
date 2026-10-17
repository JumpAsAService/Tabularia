"""One placeholder language for every input and output: a tiny parser of our own, no eval.

A datasource that reads «the last 30 days» or «yesterday's file», and an Output
that writes «today's file», cannot hold fixed dates: their templates hold
placeholders resolved at each refresh or run, in a time zone of their own (UTC
unless set). The same grammar and the same messages everywhere — the API call
(path, query, headers, body), the folder and glob of the SFTP, SharePoint and S3
datasources, the SFTP and S3 Outputs (folder, file name, object key), the email
Output (attachment name and subject).

    {today} {yesterday} {now} {start_of_month} {start_of_week} {start_of_year}
    {last_refresh}                    the start of the last successful refresh (inputs)
    {today-1}  {today-7d}  {today+1d} {today-2w} {today-1m} {today-1y}
    {now-2h}   {now-30min} {now+15s}  (hours, minutes, seconds: date-times only)
    {today:yyyyMMdd} {today:%Y%m%d} {now:epoch} {now:epoch_ms} {now:iso}
    {symbol}                          a list variable of the datasource (no offset, no format)
    {day} {day_end}                   a range variable: a date (or date-time) per value
    {run_id} {flow}                   Outputs: the run's number, the flow's name
    {date} {datetime}                 Outputs: {today}, and {now} as 2026-10-17_143005
    {{  }}                            a literal brace

A number without a unit on a date is days ({today-1}); `m` is months, clamped to
the end of the month (31 March - 1m = 28 February), `min` is minutes. Dates
render as YYYY-MM-DD, date-times as ISO 8601 with the offset of the time zone.
`{last_refresh}` on the first refresh takes the datasource's initial expression
(`today-365`, `2024-01-01`…); with none, using it is an error at save time.

RANGES. A variable `day = today-7 .. today step 1d` takes every value from the
start (included) to the end (EXCLUDED) by the step: the seven days before
today. `{day_end}` is the end of each value's window — the next value, or the
range's end for the last one — so an API asked `from={day}&to={day_end}` gets
consecutive half-open windows that never overlap. A range whose start AND end are
dates and that steps by days, weeks, months or years gives dates; any other gives
date-times in the time zone (a date bound is its local midnight, an ISO date-time
without an offset the zone's wall clock), values and window ends alike.

Everything here is a closed grammar checked with a regular expression and a
table of names: nothing a user types is ever evaluated.
"""
from __future__ import annotations

import itertools
import re
from calendar import monthrange
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import Callable, Literal, Union
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, Field, model_validator

DATE_BASES = ("today", "yesterday", "start_of_month", "start_of_week", "start_of_year")
DATETIME_BASES = ("now", "last_refresh")
BUILTINS = frozenset(DATE_BASES + DATETIME_BASES)
# Outputs only: the run's number and the flow's name (text), and the two names the
# SFTP Output had before this language — {date} is {today}, {datetime} is {now}
# written 2026-10-17_143005 (the same bytes as then)
OUTPUT_TEXT = ("run_id", "flow")
OUTPUT_ALIASES = {"date": "today", "datetime": "now"}
_ALIAS_DEFAULT_FORMAT = {"datetime": "%Y-%m-%d_%H%M%S"}
END_SUFFIX = "_end"

_BODY = re.compile(
    r"^(?P<name>[A-Za-z_][A-Za-z0-9_]*)"
    r"(?:(?P<sign>[+-])(?P<amount>\d{1,5})(?P<unit>min|d|w|m|y|h|s)?)?"
    r"(?::(?P<fmt>.+))?$",
    re.S,
)
_VARIABLE_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
# the strftime directives of the C library that make sense for a request (GNU flags `-` and `_` allowed)
# (no %n nor %t: a newline or a tab has no place in a file name, a key or a header)
_STRFTIME = re.compile(r"%[-_]?[aAbBcdDeFgGhHIjklmMpPrRsSTuUVwWxXyYzZ%]")
# readable formats, as people write them for an API (yyyy-MM-dd, ddmmyyyy, HH:mm:ss); text between
# single quotes is literal ('T'), and '' is an apostrophe (inside quotes too, as in Java's patterns).
# `mm` follows Excel's rule: minutes right after hours or right before seconds, the month otherwise —
# so ddmmyyyy is a date. Every other letter is refused: text goes in quotes.
_READABLE = re.compile(r"''|'(?:[^']|'')*'|yyyy|yy|MM|mm|dd|HH|hh|ss|[A-Za-z]|[^A-Za-z']+|'")
_READABLE_TOKENS = {"yyyy": "%Y", "yy": "%y", "MM": "%m", "dd": "%d", "HH": "%H", "ss": "%S"}
_QUOTED = re.compile(r"'(?:[^']|'')*'")
_STEP = re.compile(r"^\s*(\d{1,4})\s*(min|d|w|m|y|h)\s*$")


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
Value = Union[date, datetime, str]


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


def _check_format(ph: Placeholder, is_date: bool = False) -> None:
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
    if directives & {"z", "Z"} and is_date:
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


def has_placeholders(template: str) -> bool:
    """True when the template holds at least one placeholder (an invalid one too)."""
    try:
        return any(isinstance(p, Placeholder) for p in parse(template))
    except PlaceholderError:
        return True


def _range_of(name: str, ranges) -> str | None:
    """The range variable a name belongs to: `day` and `day_end` are both `day`'s."""
    if name in ranges:
        return name
    if name.endswith(END_SUFFIX) and name[: -len(END_SUFFIX)] in ranges:
        return name[: -len(END_SUFFIX)]
    return None


def _unknown(ph: Placeholder, output: bool) -> PlaceholderError:
    if output:
        known = ", ".join("{" + n + "}" for n in (*DATE_BASES, "now", *OUTPUT_ALIASES, *OUTPUT_TEXT))
        return PlaceholderError(f"unknown placeholder {ph.text}: use {known}")
    known = ", ".join("{" + n + "}" for n in (*DATE_BASES, *DATETIME_BASES))
    return PlaceholderError(f"unknown placeholder {ph.text}: use {known} or a variable of the datasource")


def _check_placeholder(ph: Placeholder, variables, has_initial: bool, ranges=frozenset(), output: bool = False) -> None:
    name = ph.name
    if output:
        if name in OUTPUT_TEXT:
            if ph.sign or ph.fmt:
                raise PlaceholderError(f"{{{name}}} takes no offset or format ({ph.text})")
            return
        if name == "last_refresh":
            raise PlaceholderError(
                "{last_refresh} has no meaning in an Output: it is the last refresh of a datasource. "
                "Use {today}, {now} or {yesterday}"
            )
        name = OUTPUT_ALIASES.get(name, name)
        if name not in BUILTINS:
            raise _unknown(ph, output=True)
    elif name not in BUILTINS:
        if _range_of(name, ranges):
            # a range value is a date or a date-time: offsets (a bare number is days)
            # and formats as on {today} or {now}
            _check_format(ph)
            return
        if name not in variables:
            raise _unknown(ph, output=False)
        if ph.sign or ph.fmt:
            raise PlaceholderError(f"the variable {{{ph.name}}} takes no offset or format ({ph.text})")
        return
    if ph.sign and name in DATE_BASES and ph.unit in ("h", "min", "s"):
        raise PlaceholderError(
            f"{ph.text}: hours, minutes and seconds apply to {{now}} and {{last_refresh}}, not to a date"
        )
    if ph.sign and name in DATETIME_BASES and not ph.unit:
        raise PlaceholderError(f"{ph.text}: say the unit of the offset (d, w, m, y, h, min, s), e.g. {{{ph.name}-2h}}")
    if name == "last_refresh" and not has_initial:
        raise PlaceholderError(
            "{last_refresh} has no value on the first refresh: set the initial value of last refresh "
            "(e.g. today-30) in the datasource"
        )
    _check_format(ph, is_date=name in DATE_BASES)


def check(template: str, variables: set[str] | frozenset[str] = frozenset(), has_initial: bool = False, *,
          ranges: set[str] | frozenset[str] = frozenset(), output: bool = False) -> set[str]:
    """Checks the template without resolving it; returns the variables it uses
    (a range variable is used by `{day}` and by `{day_end}`). `output`: the
    template of an Output ({run_id}, {flow}, {date}, {datetime}; no {last_refresh})."""
    used: set[str] = set()
    for part in parse(template):
        if isinstance(part, Placeholder):
            _check_placeholder(part, variables, has_initial, ranges, output)
            if output:
                continue
            owner = _range_of(part.name, ranges) if part.name not in BUILTINS else None
            if owner:
                used.add(owner)
            elif part.name not in BUILTINS:
                used.add(part.name)
    return used


@dataclass
class Context:
    now: datetime  # aware, in the time zone
    tz: ZoneInfo
    last_refresh: datetime | None = None  # aware, in the time zone
    # list variables (text) and range values (date / date-time); on an Output also
    # run_id and flow (text)
    variables: dict[str, Value] = field(default_factory=dict)
    output: bool = False


def _parse_instant(text: str, tz: ZoneInfo) -> datetime:
    s = text.strip()
    if s.endswith(("Z", "z")):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)  # stored instants are UTC
    return dt.astimezone(tz)


def make_context(now_utc: datetime, tz_name: str, last_refresh: str | None, initial: str,
                 variables: dict[str, Value]) -> Context:
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


def with_values(ctx: Context, values: dict[str, Value]) -> Context:
    """The same moment and zone with the values of one combination of the variables."""
    return Context(now=ctx.now, tz=ctx.tz, last_refresh=ctx.last_refresh, variables={**ctx.variables, **values},
                   output=ctx.output)


def _add_months(d: date, n: int) -> date:
    y, m = divmod(d.month - 1 + n, 12)
    y += d.year
    if not 1 <= y <= 9999:
        raise PlaceholderError("the date goes out of range")
    return d.replace(year=y, month=m + 1, day=min(d.day, monthrange(y, m + 1)[1]))


def _move(value: date | datetime, n: int, unit: str) -> date | datetime:
    """`value` moved by `n` units (d w m y h min s); a date moved by hours becomes
    its midnight's date-time first. Raises OverflowError / ValueError out of range."""
    if unit in ("m", "y"):
        months = n if unit == "m" else 12 * n
        if isinstance(value, datetime):
            return datetime.combine(_add_months(value.date(), months), value.timetz())
        return _add_months(value, months)
    if unit in ("d", "w"):
        # on a date-time: the same wall-clock time that many days away (zoneinfo
        # recomputes the offset across a change of summer time)
        return value + timedelta(days=n * (7 if unit == "w" else 1))
    delta = timedelta(hours=n) if unit == "h" else timedelta(minutes=n) if unit == "min" else timedelta(seconds=n)
    tz = value.tzinfo if isinstance(value, datetime) else None
    assert tz is not None, "a date is made a date-time before moving it by hours"
    return (value.astimezone(timezone.utc) + delta).astimezone(tz)


def _shift(value: date | datetime, ph: Placeholder, tz: ZoneInfo | None = None) -> date | datetime:
    if not ph.sign:
        return value
    n = ph.amount if ph.sign == "+" else -ph.amount
    unit = ph.unit or "d"
    if unit in ("h", "min", "s") and not isinstance(value, datetime):
        value = datetime.combine(value, time(0)).replace(tzinfo=tz or timezone.utc)
    try:
        return _move(value, n, unit)
    except (OverflowError, ValueError) as e:
        raise PlaceholderError(f"{ph.text}: the date goes out of range") from e


def _base(name: str, ctx: Context) -> date | datetime:
    today = ctx.now.date()
    if name == "today":
        return today
    if name == "yesterday":
        return today - timedelta(days=1)
    if name == "start_of_month":
        return today.replace(day=1)
    if name == "start_of_week":
        return today - timedelta(days=today.weekday())
    if name == "start_of_year":
        return today.replace(month=1, day=1)
    if name == "now":
        return ctx.now
    # last_refresh
    if ctx.last_refresh is None:
        raise PlaceholderError(
            "{last_refresh} has no value on the first refresh: set the initial value of last refresh "
            "(e.g. today-30) in the datasource"
        )
    return ctx.last_refresh


def evaluate(ph: Placeholder, ctx: Context) -> Value:
    """The value of a placeholder, before formatting."""
    name = ph.name
    if ctx.output:
        if name in OUTPUT_TEXT:
            value = ctx.variables.get(name)
            if value is None:
                if name == "run_id":
                    raise PlaceholderError("{run_id} is only known when the flow runs")
                raise PlaceholderError(f"unknown placeholder {ph.text}")
            return value
        name = OUTPUT_ALIASES.get(name, name)
        if name not in BUILTINS:
            raise _unknown(ph, output=True)
    if name not in BUILTINS:
        if name not in ctx.variables:
            raise PlaceholderError(f"unknown placeholder {ph.text}")
        value = ctx.variables[name]
        if isinstance(value, (date, datetime)):
            return _shift(value, ph, ctx.tz)
        return value
    return _shift(_base(name, ctx), ph, ctx.tz)


def format_value(value: Value, ph: Placeholder, tz: ZoneInfo, output: bool = False) -> str:
    if isinstance(value, str):
        return value
    fmt = ph.fmt or (_ALIAS_DEFAULT_FORMAT.get(ph.name, "") if output else "")
    if fmt in ("epoch", "epoch_ms"):
        dt = value if isinstance(value, datetime) else datetime.combine(value, time(0), tzinfo=tz)
        seconds = dt.timestamp()
        return str(int(seconds * 1000)) if fmt == "epoch_ms" else str(int(seconds))
    if not fmt or fmt == "iso":
        return value.isoformat(timespec="seconds") if isinstance(value, datetime) else value.isoformat()
    _check_format(Placeholder(raw=ph.raw, name=ph.name, fmt=fmt))
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
        value = format_value(evaluate(part, ctx), part, ctx.tz, ctx.output)
        out.append(quote(value) if quote else value)
    return "".join(out)


def single_placeholder(template: str) -> Placeholder | None:
    """The placeholder when the template is nothing but one placeholder."""
    try:
        parts = parse(template)
    except PlaceholderError:
        return None
    return parts[0] if len(parts) == 1 and isinstance(parts[0], Placeholder) else None


def param_text(value: Value) -> str:
    """A value as the `_param_<name>` column holds it: text, ISO for dates."""
    if isinstance(value, datetime):
        return value.isoformat(timespec="seconds")
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_INITIAL_HELP = "use e.g. today-30, now-2h, start_of_month or 2024-01-01"


def _resolve_point(expr: str, ctx: Context, what: str, help_text: str, allow_last_refresh: bool,
                   naive_in_zone: bool = False) -> date | datetime:
    """A moment written without braces: `today-365`, `now-2h`, `start_of_month`,
    (`last_refresh`), an ISO date or date-time. A date stays a date. An ISO
    date-time without an offset is UTC, or (`naive_in_zone`) the zone's wall clock."""
    s = (expr or "").strip()
    if not s:
        raise PlaceholderError(f"the {what} is empty")
    if _ISO_DATE.match(s):
        try:
            return date.fromisoformat(s)
        except ValueError as e:
            raise PlaceholderError(f"invalid date '{s}' as the {what}") from e
    if re.match(r"^\d{4}-\d{2}-\d{2}[T ]", s):
        try:
            if naive_in_zone:
                dt = datetime.fromisoformat(s[:-1] + "+00:00" if s.endswith(("Z", "z")) else s)
                return dt.astimezone(ctx.tz) if dt.tzinfo else dt.replace(tzinfo=ctx.tz)
            return _parse_instant(s, ctx.tz)
        except ValueError as e:
            raise PlaceholderError(f"invalid date-time '{s}' as the {what}") from e
    if "{" in s or "}" in s:
        raise PlaceholderError(f"write the {what} without braces: '{s.strip('{}')}'")
    try:
        ph = _placeholder(s)
    except PlaceholderError:
        ph = None
    if ph is None or ph.name not in BUILTINS or ph.fmt or (ph.name == "last_refresh" and not allow_last_refresh):
        raise PlaceholderError(f"invalid {what} '{s}': {help_text}")
    _check_placeholder(ph, frozenset(), has_initial=True)
    return evaluate(ph, Context(now=ctx.now, tz=ctx.tz, last_refresh=ctx.last_refresh))  # type: ignore[return-value]


def resolve_initial(expr: str, now_utc: datetime, tz: ZoneInfo) -> datetime:
    """The initial value of {last_refresh}: an expression without braces
    (`today-365`, `now-2h`, `start_of_month`) or an ISO date / date-time. A date
    is its midnight in the time zone."""
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)
    if not (expr or "").strip():
        raise PlaceholderError("the initial value of last refresh is empty")
    value = _resolve_point(expr, Context(now=now_utc.astimezone(tz), tz=tz), "initial value of last refresh",
                           _INITIAL_HELP, allow_last_refresh=False)
    if isinstance(value, datetime):
        return value
    return datetime.combine(value, time(0)).replace(tzinfo=tz)


# ── outputs ───────────────────────────────────────────────────────────────────
def safe_flow_name(flow: str) -> str:
    """The flow's name as a piece of a file name (as the SFTP Output always wrote it)."""
    return re.sub(r"[^\w.-]+", "_", flow or "").strip("._") or "flow"


def output_context(now_utc: datetime, tz_name: str, run_id: int | None, flow: str) -> Context:
    """The context of an Output's templates: the dates in the Output's zone (UTC
    unless set), the run's number and the flow's name."""
    ctx = make_context(now_utc, tz_name, None, "", {})
    ctx.output = True
    if run_id is not None:
        ctx.variables["run_id"] = str(run_id)
    ctx.variables["flow"] = flow or ""
    return ctx


OutputKind = Literal["file", "path", "text"]


def render_output(template: str, ctx: Context, kind: OutputKind, what: str) -> str:
    """An Output's template resolved. `file`: a file name, no value may put a
    folder in it; `path`: a folder or an object key, values may add levels but no
    segment may be `..` or `.`; `text`: a subject, as it is. In a file name or a
    path {flow} is made safe (as the SFTP Output always did)."""
    if kind == "text":
        text = render(template, ctx)
        if "\r" in text or "\n" in text:
            raise PlaceholderError(f"the {what} cannot hold a line break")
        return text
    named = with_values(ctx, {"flow": safe_flow_name(str(ctx.variables.get("flow") or ""))})
    if kind == "file":
        def no_folder(value: str) -> str:
            if "/" in value or "\\" in value:
                raise PlaceholderError(
                    f"a placeholder puts a '/' or '\\' in the {what} ('{value}'): a {what} cannot hold a folder — "
                    "choose a format without slashes, or put the folder in its own field"
                )
            return value

        name = render(template, named, quote=no_folder)
        _no_control(name, what)
        if len(name.encode()) > MAX_NAME_BYTES:
            raise PlaceholderError(f"the {what} is longer than {MAX_NAME_BYTES} bytes ({len(name.encode())}): shorten it")
        return name
    rendered = render(template, named)
    _no_control(rendered, what)
    segments = rendered.replace("\\", "/").split("/")
    if any(seg in ("..", ".") for seg in segments):
        raise PlaceholderError(f"'..' and '.' are not allowed in the {what} ('{rendered}'): write the folder you mean")
    long = next((seg for seg in segments if len(seg.encode()) > MAX_NAME_BYTES), None)
    if long is not None:
        raise PlaceholderError(f"a level of the {what} is longer than {MAX_NAME_BYTES} bytes: shorten it ('{long[:40]}…')")
    return rendered


# a file or folder name: the limit of every common file system
MAX_NAME_BYTES = 255


def _no_control(text: str, what: str) -> None:
    if any(ord(c) < 32 or ord(c) == 127 for c in text):
        raise PlaceholderError(f"the {what} cannot hold a control character (a line break, a tab…)")


def check_output(template: str) -> None:
    check(template, output=True)


# ── variables (lists and ranges) ──────────────────────────────────────────────
class Variable(BaseModel):
    """A variable of a datasource: a LIST (`symbol = AAPL, MSFT`, one value per
    expansion) or a RANGE (`day = today-7 .. today step 1d`)."""
    name: str = Field(max_length=64)
    type: Literal["list", "range"] = "list"
    values: list[str] = Field(default_factory=list, max_length=1000)
    start: str = Field(default="", max_length=100)
    end: str = Field(default="", max_length=100)
    step: str = Field(default="", max_length=20)

    @model_validator(mode="after")
    def _complete(self) -> "Variable":
        if self.type == "list" and not self.values:
            raise ValueError(f"the variable '{self.name}' has no values")
        if self.type == "range" and not (self.start.strip() and self.end.strip()):
            raise ValueError(f"the range '{self.name}' needs a start and an end")
        return self


def parse_step(step: str, name: str = "") -> tuple[int, str]:
    m = _STEP.match(step or "")
    if not m or int(m[1]) == 0:
        raise PlaceholderError(
            f"invalid step '{step}' of the range {{{name}}}: a number and a unit, e.g. 1d, 7d, 1w, 1m, 1y, 1h, 30min"
        )
    return int(m[1]), m[2]


def _instant(v: date | datetime, tz: ZoneInfo) -> datetime:
    """The moment as UTC: two aware date-times of the SAME zone compare by wall-clock
    time in Python (the second 02:30 of an autumn night would equal the first)."""
    dt = v if isinstance(v, datetime) else datetime.combine(v, time(0)).replace(tzinfo=tz)
    return dt.astimezone(timezone.utc)


def expand_range(name: str, start: str, end: str, step: str, ctx: Context, max_values: int) -> list[tuple[date | datetime, date | datetime]]:
    """Every value of a range with the end of its window: [(value, value_end)].
    From the start (included) to the end (excluded) by the step, counted from the
    start (so 31 January by months is 28 February, then 31 March)."""
    n, unit = parse_step(step, name)
    help_text = "use e.g. today-7, now-2h, start_of_month, last_refresh or 2024-01-01"
    first = _resolve_point(start, ctx, f"start of the range {{{name}}}", help_text, allow_last_refresh=True,
                           naive_in_zone=True)
    last = _resolve_point(end, ctx, f"end of the range {{{name}}}", help_text, allow_last_refresh=True,
                          naive_in_zone=True)
    # dates only when both bounds are dates and the step is a day or more; otherwise
    # every value and every window end is a date-time in the zone (a date is its
    # local midnight), so `{x_end}` has the same type on every window
    if unit in ("h", "min") or isinstance(first, datetime) or isinstance(last, datetime):
        if not isinstance(first, datetime):
            first = datetime.combine(first, time(0)).replace(tzinfo=ctx.tz)
        if not isinstance(last, datetime):
            last = datetime.combine(last, time(0)).replace(tzinfo=ctx.tz)
    stop = _instant(last, ctx.tz)
    values: list[date | datetime] = []
    k = 0
    while True:
        try:
            v = _move(first, k * n, unit) if k else first
        except (OverflowError, ValueError) as e:
            raise PlaceholderError(f"the range {{{name}}} goes out of the calendar") from e
        if _instant(v, ctx.tz) >= stop:
            break
        values.append(v)
        if len(values) > max_values:
            raise PlaceholderError(
                f"the range {{{name}}} has more than {max_values} values (PLACEHOLDERS__MAX_VALUES): "
                "a shorter range or a longer step"
            )
        k += 1
    if not values:
        raise PlaceholderError(
            f"the range {{{name}}} is empty: its start ({param_text(first)}) is not before its end "
            f"({param_text(last)}) — the end is excluded"
        )
    out: list[tuple[date | datetime, date | datetime]] = []
    for i, v in enumerate(values):
        if i + 1 < len(values):
            nxt: date | datetime = values[i + 1]
        else:
            try:
                following = _move(v, n, unit)
            except (OverflowError, ValueError):
                following = None
            nxt = following if following is not None and _instant(following, ctx.tz) <= stop else last
        out.append((v, nxt))
    return out


def check_variables(variables: list[Variable]) -> None:
    """Names: valid, unique, and `<range>_end` is taken by the range."""
    names: list[str] = []
    for v in variables:
        check_variable_name(v.name)
        if v.name in names:
            raise PlaceholderError(f"the variable '{v.name}' is defined twice")
        names.append(v.name)
    for v in variables:
        if v.type == "range" and v.name + END_SUFFIX in names:
            raise PlaceholderError(
                f"'{v.name}{END_SUFFIX}' is the end of the range {{{v.name}}}: choose another name for the variable"
            )
        if v.type == "range":
            parse_step(v.step, v.name)


def variable_names(variables: list[Variable]) -> tuple[frozenset[str], frozenset[str]]:
    """(list variables, range variables)."""
    return (frozenset(v.name for v in variables if v.type != "range"),
            frozenset(v.name for v in variables if v.type == "range"))


def combinations(variables: list[Variable], ctx: Context, max_values: int,
                 max_total: int | None = None) -> list[dict[str, Value]]:
    """Every combination of the variables' values (lists and ranges), in order; a
    range gives `name` and `name_end`. At most `max_values` values per range and
    `max_total` (default `max_values`) combinations."""
    if not variables:
        return [{}]
    axes: list[list[dict[str, Value]]] = []
    for v in variables:
        if v.type == "range":
            axes.append([{v.name: a, v.name + END_SUFFIX: b}
                         for a, b in expand_range(v.name, v.start, v.end, v.step, ctx, max_values)])
        else:
            axes.append([{v.name: x} for x in v.values])
    total = 1
    for axis in axes:
        total *= len(axis)
    limit = max_total or max_values
    if total > limit:
        raise PlaceholderError(
            f"the variables make {total} combinations, more than {limit} (PLACEHOLDERS__MAX_VALUES): "
            "fewer values or a shorter range"
        )
    out: list[dict[str, Value]] = []
    for combo in itertools.product(*axes):
        merged: dict[str, Value] = {}
        for part in combo:
            merged.update(part)
        out.append(merged)
    return out


def params_of(variables: list[Variable], combo: dict[str, Value]) -> dict[str, str]:
    """The `_param_<name>` values of a combination: one per variable (not the ends)."""
    return {v.name: param_text(combo[v.name]) for v in variables if v.name in combo}
