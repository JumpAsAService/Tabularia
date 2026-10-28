"""Which characters the report's font can draw, and how wide a text is in it.

The PDF uses only the fonts shipped in fonts/ (IBM Plex Sans, SIL Open Font License, see
fonts/OFL.txt): no system font, no fallback, so a character the font lacks would print as
an empty box without anyone knowing. The characters of every text are checked against
the font's own table (its `cmap`, formats 4 and 12, read here without a dependency) and
the missing ones come back with the render as a warning. The advance widths (`hmtx`) say
how wide a value prints, so a column too narrow for its numbers is said too.
"""
from __future__ import annotations

import struct
from functools import lru_cache
from pathlib import Path

FONTS = Path(__file__).with_name("fonts")
REGULAR = FONTS / "IBMPlexSans-Regular.ttf"
WEIGHTS = {400: REGULAR, 600: FONTS / "IBMPlexSans-SemiBold.ttf", 700: FONTS / "IBMPlexSans-Bold.ttf"}
PT_MM = 25.4 / 72


def _table(data: bytes, name: bytes) -> int | None:
    num_tables = struct.unpack_from(">H", data, 4)[0]
    for i in range(num_tables):
        tag, _, off, _ = struct.unpack_from(">4sIII", data, 12 + 16 * i)
        if tag == name:
            return off
    return None


def _cmap(data: bytes) -> dict[int, int]:
    """Character → glyph."""
    offset = _table(data, b"cmap")
    if offset is None:
        return {}
    n = struct.unpack_from(">H", data, offset + 2)[0]
    best = None
    for i in range(n):
        platform, encoding, sub = struct.unpack_from(">HHI", data, offset + 4 + 8 * i)
        fmt = struct.unpack_from(">H", data, offset + sub)[0]
        if platform in (0, 3) and fmt in (4, 12) and (best is None or fmt > best[0]):
            best = (fmt, offset + sub)
    if best is None:
        return {}
    fmt, at = best
    codes: dict[int, int] = {}
    if fmt == 12:
        groups = struct.unpack_from(">I", data, at + 12)[0]
        for g in range(groups):
            start, end, first = struct.unpack_from(">III", data, at + 16 + 12 * g)
            codes.update((c, first + c - start) for c in range(start, end + 1))
        return codes
    segs = struct.unpack_from(">H", data, at + 6)[0] // 2
    ends = struct.unpack_from(f">{segs}H", data, at + 14)
    starts = struct.unpack_from(f">{segs}H", data, at + 16 + 2 * segs)
    deltas = struct.unpack_from(f">{segs}h", data, at + 16 + 4 * segs)
    ranges_at = at + 16 + 6 * segs
    ranges = struct.unpack_from(f">{segs}H", data, ranges_at)
    for i in range(segs):
        for c in range(starts[i], ends[i] + 1):
            if c == 0xFFFF:
                continue
            if ranges[i] == 0:
                glyph = (c + deltas[i]) & 0xFFFF
            else:
                pos = ranges_at + 2 * i + ranges[i] + 2 * (c - starts[i])
                glyph = struct.unpack_from(">H", data, pos)[0]
                if glyph:
                    glyph = (glyph + deltas[i]) & 0xFFFF
            if glyph:
                codes[c] = glyph
    return codes


@lru_cache(maxsize=1)
def covered() -> frozenset[int]:
    return frozenset(_cmap(REGULAR.read_bytes()))


@lru_cache(maxsize=4)
def _widths(weight: int) -> dict[str, float]:
    """Every character's advance, in em."""
    data = WEIGHTS[weight].read_bytes()
    upem = struct.unpack_from(">H", data, _table(data, b"head") + 18)[0]
    metrics = struct.unpack_from(">H", data, _table(data, b"hhea") + 34)[0]
    hmtx = _table(data, b"hmtx")
    advances = [struct.unpack_from(">H", data, hmtx + 4 * i)[0] for i in range(metrics)]
    return {chr(c): advances[min(g, metrics - 1)] / upem for c, g in _cmap(data).items()}


def _weight(weight: int) -> int:
    return 600 if 500 <= weight < 700 else 700 if weight >= 700 else 400


@lru_cache(maxsize=4)
def widest(weight: int = 400) -> float:
    """The widest advance of the font, in em: a text shorter than a width over it fits."""
    return max(_widths(_weight(weight)).values())


def width_mm(text: str, size_pt: float, weight: int = 400) -> float:
    """How wide a line of text prints in the report's font (a character it lacks as 0.6 em)."""
    table = _widths(_weight(weight))
    return sum(table.get(c, 0.6) for c in text) * size_pt * PT_MM


def missing(chars: set[str]) -> set[str]:
    """The characters the font cannot draw (spaces and controls do not count)."""
    have = covered()
    return {c for c in chars if ord(c) not in have and not c.isspace() and ord(c) >= 32}
