"""Which characters the report's font can draw: the check that reports substitutions.

The PDF uses only the fonts shipped in fonts/ (IBM Plex Sans, SIL Open Font License, see
fonts/OFL.txt): no system font, no fallback, so a character the font lacks would print as
an empty box without anyone knowing. The characters of every text are checked against
the font's own table (its `cmap`, formats 4 and 12, read here without a dependency) and
the missing ones come back with the render as a warning.
"""
from __future__ import annotations

import struct
from functools import lru_cache
from pathlib import Path

FONTS = Path(__file__).with_name("fonts")
REGULAR = FONTS / "IBMPlexSans-Regular.ttf"


def _cmap(data: bytes) -> set[int]:
    num_tables = struct.unpack_from(">H", data, 4)[0]
    offset = None
    for i in range(num_tables):
        tag, _, off, _ = struct.unpack_from(">4sIII", data, 12 + 16 * i)
        if tag == b"cmap":
            offset = off
    if offset is None:
        return set()
    n = struct.unpack_from(">H", data, offset + 2)[0]
    best = None
    for i in range(n):
        platform, encoding, sub = struct.unpack_from(">HHI", data, offset + 4 + 8 * i)
        fmt = struct.unpack_from(">H", data, offset + sub)[0]
        if platform in (0, 3) and fmt in (4, 12) and (best is None or fmt > best[0]):
            best = (fmt, offset + sub)
    if best is None:
        return set()
    fmt, at = best
    codes: set[int] = set()
    if fmt == 12:
        groups = struct.unpack_from(">I", data, at + 12)[0]
        for g in range(groups):
            start, end, _ = struct.unpack_from(">III", data, at + 16 + 12 * g)
            codes.update(range(start, end + 1))
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
                codes.add(c)
    return codes


@lru_cache(maxsize=1)
def covered() -> frozenset[int]:
    return frozenset(_cmap(REGULAR.read_bytes()))


def missing(chars: set[str]) -> set[str]:
    """The characters the font cannot draw (spaces and controls do not count)."""
    have = covered()
    return {c for c in chars if ord(c) not in have and not c.isspace() and ord(c) >= 32}
