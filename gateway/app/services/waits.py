"""How long to wait between two looks at something that is running: soon at first,
as most steps end within seconds, then less and less often, up to the interval the
installation sets. A one-second script was seen ending after nine to eleven seconds
when every look waited the full interval; doubling from the start still left it at
eight, a step that ends at 4.5 s being seen at 7.75."""
from __future__ import annotations

from collections.abc import Iterator

FIRST_S = 0.25
# within this, never more than a second between two looks (most steps end in it)
QUICK_S = 30.0


def waits(cap: float) -> Iterator[float]:
    """0.25, 0.5, then every second for the first thirty seconds, then `cap`; never
    more than `cap`."""
    delay, waited = FIRST_S, 0.0
    while True:
        pause = min(delay if waited >= QUICK_S else min(delay, 1.0), cap)
        yield pause
        waited += pause
        delay *= 2
