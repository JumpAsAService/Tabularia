"""What a CSV uploaded from the editor is, read from the file itself: the editor sends the
file alone, and Excel's «CSV» in Italian (or French, German, Spanish) is `;`-separated,
with commas for decimals and in Windows-1252 — read as `,` and UTF-8 it answered 500, or
became one column, «Citt�» and prices as text.

- encoding: UTF-8 when the whole file decodes as such, else Windows-1252 (which decodes
  any byte), written again as UTF-8 for the reader;
- separator: among `;` `,` tab `|`, the one found the same number of times (and at least
  once) on the first lines, outside quotes; `,` when none is;
- decimal comma: with a separator other than `,`, numbers written `16,125` and none
  written `16.5`; with `;` (Excel's separator in those languages) a number written
  `1.500` is a thousands dot too, never one and a half.
"""
from __future__ import annotations

import codecs
import csv
import io
import re
from dataclasses import dataclass

CANDIDATES = (";", ",", "\t", "|")
SAMPLE_LINES = 50
CHUNK = 1 << 20

_COMMA_NUMBER = re.compile(r"^-?\d+(?:\.\d{3})*,\d+$")
_DOT_NUMBER = re.compile(r"^-?\d+\.\d+$")
# `1.500`, `12.345.678`: a thousands dot where the comma is the decimal mark, a decimal
# dot with three decimals elsewhere; which one, the separator says
_THOUSANDS = re.compile(r"^-?\d{1,3}(?:\.\d{3})+$")


@dataclass(frozen=True)
class CsvShape:
    separator: str
    decimal_comma: bool
    encoding: str  # "utf8" or "cp1252"


def is_utf8(path: str) -> bool:
    decoder = codecs.getincrementaldecoder("utf-8")()
    try:
        with open(path, "rb") as f:
            while chunk := f.read(CHUNK):
                decoder.decode(chunk)
        decoder.decode(b"", final=True)
    except UnicodeDecodeError:
        return False
    return True


def to_utf8(src: str, dst: str, encoding: str = "cp1252") -> None:
    """Writes `src` again as UTF-8, in chunks (never the whole file in memory)."""
    with open(src, "r", encoding=encoding, errors="replace", newline="") as fin, \
            open(dst, "w", encoding="utf-8", newline="") as fout:
        while chunk := fin.read(CHUNK):
            fout.write(chunk)


def _sample(path: str, encoding: str) -> tuple[list[str], bool]:
    """The first lines, and whether the file goes on after them. Empty lines stay: one
    may sit inside a quoted cell (Alt+Enter in Excel)."""
    lines: list[str] = []
    with open(path, "r", encoding="utf-8-sig" if encoding == "utf8" else encoding, errors="replace", newline="") as f:
        for line in f:
            if len(lines) >= SAMPLE_LINES:
                return lines, True
            lines.append(line)
    return lines, False


def _fields(lines: list[str], sep: str, cut: bool = False) -> list[list[str]]:
    """The records of the sample, empty ones left out. `cut`: the sample stops mid-file,
    and its last record may be a quoted cell cut in two — it is dropped."""
    try:
        rows = list(csv.reader(io.StringIO("".join(lines)), delimiter=sep))
    except csv.Error:
        return []
    if cut and rows:
        rows = rows[:-1]
    return [r for r in rows if any(c.strip() for c in r)]


def sniff(path: str) -> CsvShape:
    encoding = "utf8" if is_utf8(path) else "cp1252"
    lines, cut = _sample(path, encoding)
    separator = ","
    best = 0
    for sep in CANDIDATES:
        rows = _fields(lines, sep, cut)
        widths = {len(r) for r in rows}
        if len(widths) == 1 and (n := widths.pop()) > 1 and n > best:
            separator, best = sep, n
    decimal_comma = False
    if separator != ",":
        tokens = [t.strip() for r in _fields(lines, separator, cut)[1:] for t in r]
        comma = sum(bool(_COMMA_NUMBER.match(t)) for t in tokens)
        if separator == ";":
            # Excel writes «;» only in the languages with a decimal comma: it is on unless
            # the sample shows dot decimals and nothing Italian. Off, «1.500» further down
            # the file would be read 1.5 without a word
            thousands = sum(bool(_THOUSANDS.match(t)) for t in tokens)
            dot = sum(bool(_DOT_NUMBER.match(t)) and not _THOUSANDS.match(t) for t in tokens)
            decimal_comma = not (dot > 0 and comma == 0 and thousands == 0)
        else:
            dot = sum(bool(_DOT_NUMBER.match(t)) for t in tokens)
            decimal_comma = comma > 0 and dot == 0
    return CsvShape(separator=separator, decimal_comma=decimal_comma, encoding=encoding)
