"""The waits between two looks at a running step: soon at first, every second for half a
minute, then up to the installation's interval (services/waits.py)."""
from itertools import islice

from app.services.waits import waits


def test_soon_then_every_second_then_up_to_the_interval():
    first = list(islice(waits(5.0), 40))
    assert first[:4] == [0.25, 0.5, 1.0, 1.0]
    assert max(first[:30]) == 1.0 and first[-1] == 5.0  # past thirty seconds, the interval
    assert sum(first[:32]) >= 30


def test_a_shorter_interval_wins_from_the_start():
    assert list(islice(waits(0.01), 3)) == [0.01, 0.01, 0.01]
