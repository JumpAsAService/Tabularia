# check_sql and fill of services/report_params.py, on SQLs and values of every dialect:
#   docker run --rm -v "$PWD/gateway/app:/app/app:ro" -v "$PWD/gateway-go/internal/reportparams/testdata:/t" -w /app \
#       tabularia-gateway:latest python /t/golden.py
import json
from datetime import date
from fastapi import HTTPException
from app.services import report_params as rp

SQLS = [
    "SELECT * FROM t WHERE r = {r} AND n > {n} AND d >= {d} AND b = {b} AND m IN {m}",
    "SELECT '{today}', {today-7}, {now:yyyy}, {{x}}, '{{today}}' FROM t -- {nope}",
    "SELECT * FROM t WHERE x = '{r}'", "SELECT } FROM t", "SELECT { FROM t", "SELECT {nope}",
    "SELECT {r:fmt}", "SELECT 'a\\' , {r} -- '", "SELECT /* /* */ {r} */", "SELECT $$ {r} $$, {n}",
    "SELECT E'\\' {r}'", "SELECT q'[ {r} ]', {n}", "SELECT [{r}], {n}", "SELECT '''{r}''', {n}", "SELECT \"{r}\" # {r}",
    "SELECT `{r}`, {n}", "SELECT {today} , '{now}', {start_of_month+1m}", "SELECT {r}{n}", "SELECT {r} -{n}",
    "SELECT '{today:''x}'", "SELECT 'é' || {r}", "SELECT {{ {r} }}",
]
VALUES = [
    {"r": "Nord", "n": 5, "d": date(2026, 1, 2), "b": True, "m": ["a", "b"]},
    {"r": "it's \\ a\nb\t\x00é", "n": -2.5, "d": None, "b": False, "m": []},
    {"r": None, "n": 1e-7, "d": date(1999, 12, 31), "b": None, "m": [1, 2.0, -3]},
    {"r": "", "n": 123456789012345.0, "d": None, "b": True, "m": [True, None]},
]
PLACEHOLDERS = {"today": "2026-11-03", "today-7": "2026-10-27", "now:yyyy": "2026", "now": "2026-11-03T10:20",
                "start_of_month+1m": "1762160400"}
TYPES = {"r": "text", "n": "number", "d": "date", "b": "boolean", "m": "text"}
out, cases = [], []
for key in rp.DIALECTS:
    d = rp.dialect_of(key)
    for sql in SQLS:
        for vi, values in enumerate(VALUES):
            case = {"dialect": key, "sql": sql, "values": vi}
            try:
                used, ph = rp.check_sql(sql, d, TYPES)
                case["used"], case["placeholders"] = sorted(used), sorted(ph)
                filled = rp.fill(sql, d, {k: {"type": t} for k, t in TYPES.items()}, values,
                                 {b: PLACEHOLDERS.get(b, "x'y") for b in ph})
                case["filled"] = filled
            except HTTPException as e:
                case["refused"] = e.detail
            except KeyError as e:
                case["error"] = "KeyError"
            cases.append(case)
json.dump(cases, open("/t/golden.json", "w"), ensure_ascii=False, indent=0)
print(len(cases), "cases")
