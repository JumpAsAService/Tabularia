"""Writes croniter.json: what croniter 6.2.4 (the gateway's image) answers for a corpus of
expressions, zones and starting instants. Run: docker run --rm -i tabularia-gateway:latest
python - < gen.py > croniter.json"""
import json, random
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from croniter import croniter

exprs = ["0 3 * * *", "*/15 * * * *", "0 * * * *", "30 2 * * *", "0 0 * * 0", "0 0 1 * *", "5 4 * * sun",
         "0 0 * * * *", "0 0 * * * * 2026", "0 0 * * * * 2030-2031", "@daily", "@hourly", "@weekly", "@monthly",
         "@yearly", "0 0 L * *", "0 0 * * 5#2", "0 0 * * l5", "0 0 ? * *", "0 0 1W * *", "0 0 15w * *",
         "0 0 * jan mon", "0 0 * * 7", "1-5/2 * * * *", "5-1 * * * *", "0 0 31 2 *", "0 9 * * mon-fri",
         "0 0 * * sun,sat", "0,30 * * * *", "0 0 * * 0-7", "0 0 */2 */3 *", "0 0 1-31/7 * *", "0 12 13 * 5",
         "0 0 1,15 * 1", "*/7 */5 * * *", "59 23 31 12 *", "0 0 29 2 *", "0 0 * * 1#5", "0 0 * * 6#1,6#3",
         "0 0 * * 2-4#2", "0 0 L 2 *", "15 1-3 * * *", "0 2 * * *", "30 1 * * *", "0 1 * * *", "*/20 1-3 * * *",
         "0 0 * nov-feb *", "0 0 * * fri-mon", "0 0 * apr-jan/3 *", "*/10 * * * * */20", "0 0 1 1 * 0",
         "0 0 0 * * *", "0 0 * 0 * *", "0 0 1-l * *", "0 0 w1 * *", "0 0 31w * *", "*/5 * * * *",
         "0 4 8-14 * 2", "0 0 * * 3l", "@midnight", "@annually", "0 0 * * mon#2", "* * * * *",
         "1 * * * * 30", "0 0 1 */2 *", "0 0 31 * *", "0 0 30 4,6 *", "10-50/20 3 * * *"]
invalid = ["@reboot", "0 0 * * 5L", "H * * * *", "0 0 * * 8", "60 * * * *", "0 24 * * *", "* * * *", "*/0 * * * *",
           "a * * * *", "0 0 0 * *", "* * * 13 *", "0 0 * * * 0 2100", "0 0 1W,2 * *", "0 0 * * 1#6", "0 0 * * 1,2#2",
           "0 0 ?/2 * *", "0 ? * * *", "-1 * * * *", "1--2 * * * *", "0 0 * * mon#", "", "0 0 32w * *", "x/5 * * * *",
           "0 0 l-5 * *", "*/x * * * *", "1-2-3 * * * *", "0 0 * * 0-7#2", "0 0 1 1 * 60", "0 0 * * l", "0 0 * jan-x *",
           "1,,2 * * * *", "0 0 * * * * 1969"]
zones = ["UTC", "Europe/Rome", "America/New_York", "Australia/Lord_Howe", "Asia/Kolkata"]
random.seed(7)
starts = [datetime(2026, 3, 28, 23, 0, tzinfo=timezone.utc), datetime(2026, 3, 29, 0, 30, tzinfo=timezone.utc),
          datetime(2026, 10, 24, 23, 59, 30, 123456, tzinfo=timezone.utc), datetime(2026, 10, 25, 0, 45, tzinfo=timezone.utc),
          datetime(2026, 3, 8, 6, 0, tzinfo=timezone.utc), datetime(2026, 11, 1, 5, 0, tzinfo=timezone.utc),
          datetime(2026, 4, 4, 14, 0, tzinfo=timezone.utc), datetime(2026, 12, 31, 23, 59, 59, 999999, tzinfo=timezone.utc),
          datetime(2028, 2, 28, 12, 0, tzinfo=timezone.utc)]
cases = []
for e in invalid + exprs:
    if not croniter.is_valid(e):
        cases.append({"expr": e, "valid": False})
        continue
    for tz in zones:
        for s in starts:
            it = croniter(e, s.astimezone(ZoneInfo(tz)))
            out = []
            try:
                for _ in range(6):
                    t = it.get_next(datetime)
                    out.append(t.isoformat())
            except Exception as ex:
                out.append("error:" + type(ex).__name__)
            cases.append({"expr": e, "valid": True, "zone": tz, "start": s.isoformat(), "next": out})
print(json.dumps(cases, indent=0))
