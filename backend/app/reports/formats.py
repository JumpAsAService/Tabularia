"""Numbers and dates as a report shows them, in its five languages.

The server formats every value once, for the PDF, the pages on screen and the email;
Excel gets the raw value and the same pattern as a number format instead, so its cells
stay numbers. A pattern is spreadsheet-like and read by a small parser of our own:

  numbers  0  0.00  #,##0  #,##0.00  0%  0.0%  — text before or after the digits
           (`€ #,##0.00`, `#,##0 pcs`, "text in double quotes"), and up to three
           sections, positive;negative;zero (`#,##0;(#,##0)`: negatives in brackets)
  dates    yyyy yy MMMM MMM MM M dd d EEEE EEE HH H mm m ss s — `M` is the month, `m`
           the minute; 'text in single quotes' ('' is an apostrophe)

`,` and `.` in a pattern are the places of the grouping and decimal separators: the
characters written are the locale's (1.234,50 in Italian, 1 234,50 in French).
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from functools import lru_cache
from typing import Any

LOCALES = ("en", "it", "de", "es", "fr")


@dataclass(frozen=True)
class Locale:
    code: str
    decimal: str
    group: str
    # CLDR's minimum grouping digits: Spanish writes 1234 but 12.345
    min_grouping: int
    months: tuple[str, ...]
    months_short: tuple[str, ...]
    days: tuple[str, ...]  # Monday first
    days_short: tuple[str, ...]
    date: str  # the pattern of a date with no format of its own
    datetime: str
    # the texts the renderer writes itself; {page} {pages} {n} are filled in by the code
    texts: dict[str, str] = field(default_factory=dict)


_LOCALES = {
    "en": Locale(
        "en", ".", ",", 1,
        ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
         "November", "December"),
        ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"),
        ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"),
        ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"),
        "d MMM yyyy", "d MMM yyyy HH:mm",
        {"page": "Page {page} of {pages}", "total": "Total", "true": "Yes", "false": "No", "other": "Other",
         "parameters": "Parameters", "parameter": "Parameter", "value": "Value",
         "more": "… and {n} more: the whole detail is in the PDF", "empty": "No data"},
    ),
    "it": Locale(
        "it", ",", ".", 1,
        ("gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno", "luglio", "agosto", "settembre", "ottobre",
         "novembre", "dicembre"),
        ("gen", "feb", "mar", "apr", "mag", "giu", "lug", "ago", "set", "ott", "nov", "dic"),
        ("lunedì", "martedì", "mercoledì", "giovedì", "venerdì", "sabato", "domenica"),
        ("lun", "mar", "mer", "gio", "ven", "sab", "dom"),
        "dd/MM/yyyy", "dd/MM/yyyy HH:mm",
        {"page": "Pagina {page} di {pages}", "total": "Totale", "true": "Sì", "false": "No", "other": "Altro",
         "parameters": "Parametri", "parameter": "Parametro", "value": "Valore",
         "more": "… e altri {n}: il dettaglio completo è nel PDF", "empty": "Nessun dato"},
    ),
    "de": Locale(
        "de", ",", ".", 1,
        ("Januar", "Februar", "März", "April", "Mai", "Juni", "Juli", "August", "September", "Oktober",
         "November", "Dezember"),
        ("Jan.", "Feb.", "März", "Apr.", "Mai", "Juni", "Juli", "Aug.", "Sept.", "Okt.", "Nov.", "Dez."),
        ("Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"),
        ("Mo.", "Di.", "Mi.", "Do.", "Fr.", "Sa.", "So."),
        "dd.MM.yyyy", "dd.MM.yyyy HH:mm",
        {"page": "Seite {page} von {pages}", "total": "Summe", "true": "Ja", "false": "Nein", "other": "Sonstige",
         "parameters": "Parameter", "parameter": "Parameter", "value": "Wert",
         "more": "… und {n} weitere: das vollständige Detail steht im PDF", "empty": "Keine Daten"},
    ),
    "es": Locale(
        "es", ",", ".", 2,
        ("enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre", "octubre",
         "noviembre", "diciembre"),
        ("ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sept", "oct", "nov", "dic"),
        ("lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"),
        ("lun", "mar", "mié", "jue", "vie", "sáb", "dom"),
        "dd/MM/yyyy", "dd/MM/yyyy HH:mm",
        {"page": "Página {page} de {pages}", "total": "Total", "true": "Sí", "false": "No", "other": "Otros",
         "parameters": "Parámetros", "parameter": "Parámetro", "value": "Valor",
         "more": "… y {n} más: el detalle completo está en el PDF", "empty": "Sin datos"},
    ),
    "fr": Locale(
        "fr", ",", " ", 1,
        ("janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août", "septembre", "octobre",
         "novembre", "décembre"),
        ("janv.", "févr.", "mars", "avr.", "mai", "juin", "juil.", "août", "sept.", "oct.", "nov.", "déc."),
        ("lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"),
        ("lun.", "mar.", "mer.", "jeu.", "ven.", "sam.", "dim."),
        "dd/MM/yyyy", "dd/MM/yyyy HH:mm",
        {"page": "Page {page} sur {pages}", "total": "Total", "true": "Oui", "false": "Non", "other": "Autres",
         "parameters": "Paramètres", "parameter": "Paramètre", "value": "Valeur",
         "more": "… et {n} de plus : le détail complet est dans le PDF", "empty": "Aucune donnée"},
    ),
}


def locale(code: str | None) -> Locale:
    """The locale by its code; English for anything else."""
    return _LOCALES.get((code or "en").lower()[:2], _LOCALES["en"])


class FormatError(ValueError):
    """A pattern that cannot be read: the message names it."""


# ── numbers ──────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class _Section:
    prefix: str
    suffix: str
    min_int: int
    min_frac: int
    max_frac: int
    grouping: bool
    percent: bool
    digits: bool = True  # False: a section of text only (`#,##0;(#,##0);-` writes zero as -)


_NUMBER_CORE = re.compile(r"[0#,]*[0#](?:\.[0#]*)?|\.[0#]+")


def _literal_parts(section: str) -> list[tuple[bool, str]]:
    """The section as (is_literal, text) parts: "quoted text" and \\x are literal."""
    parts: list[tuple[bool, str]] = []
    i = 0
    while i < len(section):
        c = section[i]
        if c == '"':
            j = section.find('"', i + 1)
            if j < 0:
                raise FormatError(f"a quote is not closed in the format «{section}»")
            parts.append((True, section[i + 1:j]))
            i = j + 1
        elif c == "\\" and i + 1 < len(section):
            parts.append((True, section[i + 1]))
            i += 2
        else:
            j = i
            while j < len(section) and section[j] not in '"\\':
                j += 1
            parts.append((False, section[i:j]))
            i = j
    return parts


def _section(text: str) -> _Section:
    prefix, suffix, core = [], [], None
    percent = False
    for literal, part in _literal_parts(text):
        if literal:
            (suffix if core is not None else prefix).append(part)
            continue
        percent = percent or "%" in part
        if core is None:
            m = _NUMBER_CORE.search(part)
            if m:
                core = m.group(0)
                prefix.append(part[:m.start()])
                suffix.append(part[m.end():])
                continue
        (suffix if core is not None else prefix).append(part)
    if core is None:
        return _Section("".join(prefix), "", 0, 0, 0, False, percent, digits=False)
    whole, _, frac = core.partition(".")
    return _Section("".join(prefix), "".join(suffix), whole.count("0"), frac.count("0"), len(frac),
                    "," in whole, percent)


def is_number_pattern(pattern: str) -> bool:
    """True when the pattern formats numbers (it has 0 or # outside quotes)."""
    plain = "".join(p for literal, p in _safe_parts(pattern) if not literal)
    return "0" in plain or "#" in plain


def _safe_parts(pattern: str) -> list[tuple[bool, str]]:
    try:
        return _literal_parts(pattern)
    except FormatError:
        return [(False, pattern)]


@lru_cache(maxsize=512)
def number_pattern(pattern: str) -> tuple[_Section, ...]:
    """The pattern's sections (positive, negative, zero); FormatError says what is wrong."""
    sections = pattern.split(";")
    if len(sections) > 3:
        raise FormatError(f"the format «{pattern}» has more than three sections (positive;negative;zero)")
    parsed = tuple(_section(s) for s in sections)
    if not parsed[0].digits:
        raise FormatError(f"the format «{pattern}» has no digits: write 0 or # where the number goes")
    return parsed


def _digits(value: float, sec: _Section) -> tuple[str, str]:
    """The integer and decimal digits of |value|, rounded half away from zero."""
    try:
        d = Decimal(repr(abs(float(value))))
    except (InvalidOperation, ValueError, OverflowError):
        return "0", ""
    if sec.percent:
        d *= 100
    d = d.quantize(Decimal(1).scaleb(-sec.max_frac), rounding=ROUND_HALF_UP)
    text = format(d, "f")
    whole, _, frac = text.partition(".")
    frac = frac.rstrip("0")
    if len(frac) < sec.min_frac:
        frac = frac + "0" * (sec.min_frac - len(frac))
    if whole == "0" and sec.min_int == 0:
        whole = ""
    if len(whole) < sec.min_int:
        whole = "0" * (sec.min_int - len(whole)) + whole
    return whole, frac


def _group(whole: str, loc: Locale) -> str:
    if len(whole) < 3 + loc.min_grouping:
        return whole
    head = len(whole) % 3 or 3
    out = [whole[:head]]
    out.extend(whole[i:i + 3] for i in range(head, len(whole), 3))
    return loc.group.join(out)


def format_number(value: float | int, pattern: str, loc: Locale) -> str:
    sections = number_pattern(pattern)
    if isinstance(value, float) and not math.isfinite(value):
        return ""
    negative = value < 0
    sec = sections[0]
    if negative and len(sections) > 1:
        sec = sections[1]
    whole, frac = _digits(value, sec)
    if len(sections) > 2 and not whole.strip("0") and not frac.strip("0"):
        sec = sections[2]
        whole, frac = _digits(0, sec)
    if not sec.digits:
        return sec.prefix
    if sec.grouping:
        whole = _group(whole, loc)
    number = whole + (loc.decimal + frac if frac else "")
    zero = not any(c.isdigit() and c != "0" for c in whole + frac)
    sign = "-" if negative and len(sections) == 1 and not zero else ""
    return f"{sign}{sec.prefix}{number}{sec.suffix}"


# ── dates ────────────────────────────────────────────────────────────────────
_DATE_TOKEN = re.compile(r"''|'(?:[^']|'')*'|yyyy|yy|MMMM|MMM|MM|M|dd|d|EEEE|EEE|HH|H|mm|m|ss|s|[^'yMdEHms]+|[yMdEHms]")
_DATE_LETTERS = frozenset("yMdEHms")


@lru_cache(maxsize=512)
def date_tokens(pattern: str) -> tuple[str, ...]:
    tokens = tuple(_DATE_TOKEN.findall(pattern))
    for t in tokens:
        if t.startswith("'") and t != "''" and (len(t) < 2 or not t.endswith("'")):
            raise FormatError(f"a quote is not closed in the format «{pattern}»: text goes between 'quotes'")
    return tokens


def is_date_pattern(pattern: str) -> bool:
    try:
        tokens = date_tokens(pattern)
    except FormatError:
        return False
    return any(t[0] in _DATE_LETTERS for t in tokens)


def format_date(value: date, pattern: str, loc: Locale) -> str:
    out: list[str] = []
    has_time = isinstance(value, datetime)
    for t in date_tokens(pattern):
        if t == "''":
            out.append("'")
        elif t.startswith("'"):
            out.append(t[1:-1].replace("''", "'"))
        elif t == "yyyy":
            out.append(f"{value.year:04d}")
        elif t == "yy":
            out.append(f"{value.year % 100:02d}")
        elif t == "MMMM":
            out.append(loc.months[value.month - 1])
        elif t == "MMM":
            out.append(loc.months_short[value.month - 1])
        elif t == "MM":
            out.append(f"{value.month:02d}")
        elif t == "M":
            out.append(str(value.month))
        elif t == "dd":
            out.append(f"{value.day:02d}")
        elif t == "d":
            out.append(str(value.day))
        elif t == "EEEE":
            out.append(loc.days[value.weekday()])
        elif t == "EEE":
            out.append(loc.days_short[value.weekday()])
        elif t in ("HH", "H", "mm", "m", "ss", "s"):
            n = {"H": value.hour, "m": value.minute, "s": value.second}[t[0]] if has_time else 0  # type: ignore[union-attr]
            out.append(f"{n:02d}" if len(t) == 2 else str(n))
        else:
            out.append(t)
    return "".join(out)


# ── any value ────────────────────────────────────────────────────────────────
def display(value: Any, pattern: str | None, loc: Locale) -> str:
    """A value as the report writes it: with its pattern when it fits the value, the
    locale's way otherwise. NULL is empty."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return loc.texts["true" if value else "false"]
    if isinstance(value, (list, tuple)):
        return ", ".join(display(v, pattern, loc) for v in value)
    if isinstance(value, (int, float)):
        if pattern and is_number_pattern(pattern):
            try:
                return format_number(value, pattern, loc)
            except FormatError:
                pass
        if isinstance(value, float) and not math.isfinite(value):
            return ""
        if isinstance(value, int):
            # as a spreadsheet's General: a year, a month code, an id are never «2,026»
            return format_number(value, "0", loc)
        return format_number(value, "#,##0" if float(value).is_integer() else "#,##0.##", loc)
    if isinstance(value, date):
        if pattern and is_date_pattern(pattern) and not is_number_pattern(pattern):
            return format_date(value, pattern, loc)
        return format_date(value, loc.datetime if isinstance(value, datetime) else loc.date, loc)
    return str(value)


def check_pattern(pattern: str) -> None:
    """FormatError when a pattern can be read neither as a number's nor as a date's."""
    if is_number_pattern(pattern):
        number_pattern(pattern)
    elif not is_date_pattern(pattern):
        raise FormatError(f"«{pattern}» is not a format: use e.g. #,##0.00, 0.0%, dd/MM/yyyy or MMMM yyyy")
    else:
        date_tokens(pattern)


# ── Excel ────────────────────────────────────────────────────────────────────
def _xl_text(text: str) -> str:
    return '"' + text.replace('"', '""') + '"' if text else ""


def excel_number_format(pattern: str | None, whole: bool = False) -> str:
    """The pattern as an Excel number format: the text quoted, negatives in red when the
    pattern does not say how to write them. Excel uses its own user's separators. With no
    pattern a whole number (`whole`) has no separators, as `display` writes it."""
    if not pattern or not is_number_pattern(pattern):
        return "0;[Red]-0" if whole else '#,##0.##;[Red]-#,##0.##'
    out = []
    for sec in number_pattern(pattern):
        if not sec.digits:
            out.append(_xl_text(sec.prefix))
            continue
        core = ("#,##0" if sec.grouping else "0" if sec.min_int else "#")
        if sec.grouping and sec.min_int == 0:
            core = "#,##"
        if sec.max_frac:
            core += "." + "0" * sec.min_frac + "#" * (sec.max_frac - sec.min_frac)
        prefix, suffix = sec.prefix.replace("%", ""), sec.suffix.replace("%", "")
        out.append(f"{_xl_text(prefix)}{core}{'%' if sec.percent else ''}{_xl_text(suffix)}")
    if len(out) == 1:
        out.append("[Red]-" + out[0])
    return ";".join(out)


_EXCEL_DATE = {"yyyy": "yyyy", "yy": "yy", "MMMM": "mmmm", "MMM": "mmm", "MM": "mm", "M": "m", "dd": "dd",
               "d": "d", "EEEE": "dddd", "EEE": "ddd", "HH": "hh", "H": "h", "mm": "mm", "m": "m", "ss": "ss",
               "s": "s"}


_EXCEL_PLAIN = frozenset(" /:-.,()")  # what a date format of Excel writes as it is


def excel_date_format(pattern: str | None, loc: Locale, with_time: bool) -> str:
    """The pattern as an Excel date format (Excel reads `mm` after hours as minutes)."""
    if not pattern or not is_date_pattern(pattern):
        pattern = loc.datetime if with_time else loc.date
    out = []
    for t in date_tokens(pattern):
        if t in _EXCEL_DATE:
            out.append(_EXCEL_DATE[t])
            continue
        text = "'" if t == "''" else t[1:-1].replace("''", "'") if t.startswith("'") else t
        out.append(text if set(text) <= _EXCEL_PLAIN else _xl_text(text))
    return "".join(out)
