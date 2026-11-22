"""The table of the series' types (docs/specs/foundation-forecast.md, «Self-test, reference values, witnesses»; lot F2):
data/ff_types.csv, 60 rows, three series of different spans in an order that is not the time's, written by formula
(no random numbers). From the repository's root, with any Python 3:

  python3 gateway-go/internal/modelgroups/groups/foundation_forecast/reference/make_reference_series.py

Columns: s_text and s_int64 (the series, as a text and as an Int64 above 2^31), t_int64 (an integer above 2^31),
t_int (an integer), t_date (a date), t_hour (an instant, one hour a step, across the change of the clock of
2026-03-29 in Europe/Rome), value. The comparison of the Python series with ts_series.R (compare_series.py) and the
self-test write each time column as its own Arrow type (Int64, Int32, date32, timestamp[us, UTC], timestamp[ns],
timestamp[ms, Europe/Rome]).

  Online    (3000000001) steps 0–24: the first value empty (before the span), the step 15 without a row and the step
            10 with an empty value (two gaps), the steps 22–24 empty (after the span: a forecast's future rows)
  Negozio   (3000000002) steps 3–22: no gap, the last step empty
  Telefono  (3000000003) steps 5–21: the step 12 without a row (one gap)
"""
import csv
import datetime as dt
import math
from pathlib import Path

HERE = Path(__file__).resolve().parent
SERIES = (("Online", 3000000001, range(0, 25), {15}), ("Negozio", 3000000002, range(3, 23), set()),
          ("Telefono", 3000000003, range(5, 22), {12}))
EMPTY = {"Online": {0, 10, 22, 23, 24}, "Negozio": {22}, "Telefono": set()}


def rows():
    out = []
    for s, (name, code, steps, absent) in enumerate(SERIES):
        for k in steps:
            if k in absent:
                continue
            value = "" if k in EMPTY[name] else f"{100 + 10 * math.sin(k + s) + 5 * s:.4f}"
            out.append({"s_text": name, "s_int64": str(code), "t_int64": str(5_000_000_000 + k), "t_int": str(k),
                        "t_date": (dt.date(2026, 3, 1) + dt.timedelta(days=k)).isoformat(),
                        "t_hour": (dt.datetime(2026, 3, 28, 12) + dt.timedelta(hours=k)).strftime("%Y-%m-%d %H:%M:%S"),
                        "value": value})
    # an order that is not the time's: the series interleaved, each from its last step
    out.sort(key=lambda r: (-int(r["t_int"]), r["s_text"]))
    return out


def main():
    table = rows()
    assert len(table) == 60, len(table)
    path = HERE / "data" / "ff_types.csv"
    path.parent.mkdir(exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(table[0]), lineterminator="\n")
        w.writeheader()
        w.writerows(table)
    print(f"written: {path.name}, {len(table)} rows")


if __name__ == "__main__":
    main()
