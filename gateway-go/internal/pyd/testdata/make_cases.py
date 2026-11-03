"""Writes cases.json: the requests of the differential test of internal/pyd. Bodies
are written as raw text (or base64 when they are not UTF-8), so Go and Python read
the same bytes."""
import base64
import gzip
import json
import sys

sys.path.insert(0, "/td")

import gen_models  # noqa: E402

cases: list = []


def add(kind: str, cid: str, **kw) -> None:
    c = {"id": f"{kind}:{cid}:{len(cases)}", "kind": kind}
    for k, v in kw.items():
        if k == "raw":
            c["body_b64"] = base64.b64encode(v).decode()
        else:
            c[k] = v
    cases.append(c)


JSON = "application/json"
LOGIN = "app.schemas.models.LoginRequest"

# ── raw JSON values (as written in a body) every scalar type meets ────────────
VALUES = [
    '"x"', '""', '"5"', '" 5 "', '"\\t5\\n"', '"5.0"', '"5.00"', '"5."', '".5"', '"5.5"', '"1_000"', '"1__0"',
    '"_1"', '"1_"', '"+_1"', '"-_1"', '"1_0.0"', '"1_0.0_0"', '" 1_0 "', '"1_e1"', '"1._5"', '"1e3"', '"1E3"',
    '"1e"', '"inf"', '"-Infinity"', '"INFINITY"', '"nan"', '"+nan"', '"infx"', '"0x10"', '"0b1"', '"+5"',
    '"-0"', '"00"', '"--1"', '"\\u0663"', '"\\u00a05\\u00a0"', '"\\u20281"', '"\\u001c1"', '"\\u30007"', '"\\uff11"',
    '"true"', '"True"', '"YES"', '"yes "', '"on"', '"off"', '"f"', '"t"', '"y"', '"n"', '"1"', '"0"', '"2"',
    '"1.0"', '"9223372036854775807"', '"9223372036854775808"', '"-9223372036854775809"', '"99999999999999999999"',
    '"1' + "0" * 30 + '"', '"' + "1" * 4300 + '"', '"' + "1" * 4301 + '"', '"' + "0" * 4301 + '"',
    '"1e400"', '"1e-400"', '"2.5e-324"',
    '5', '0', '1', '-1', '2', '-0', '5.0', '5.5', '0.0', '-0.0', '1.0', '0.1', '1e16', '1.5e16', '1e15',
    '123456789.123', '1e22', '1e-5', '1.5e-7', '0.0001', '1e300', '1e400', '-1e400', '9.2e18', '1e19',
    '-9.223372036854775808e18', '9.223372036854775807e18', '12345678901234567890', '-9223372036854775809',
    '9223372036854775807', '1' + '0' * 308, '1' + '0' * 309, '9' * 400, 'NaN', 'Infinity', '-Infinity',
    'true', 'false', 'null', '[]', '[1]', '{}', '{"a":1}', '"\\u00e9\\u20ac\\ud83d\\ude00"', '"\\ud800"',
    '"a\\u0000b"', '"\\u2028\\u2029"', '"<&>\'"', '"\\u007f"', '"\\"\\\\\\/\\b\\f\\n\\r\\t"', '"\\u001f"',
]

for v in VALUES:
    add("body", "scalars", model="syn.Scalars", body=f'{{"s":{v},"i":{v},"f":{v},"b":{v}}}', ct=JSON)
    add("body", "opt", model="syn.OptScalars", body=f'{{"s":{v},"i":{v},"f":{v},"b":{v},"a":{v}}}', ct=JSON)
    add("body", "strict", model="syn.StrictScalars", body=f'{{"si":{v},"sb":{v},"sf":{v},"ss":{v}}}', ct=JSON)
    add("body", "dt", model="syn.Dt", body=f'{{"when":{v}}}', ct=JSON)

# ── constraints, field by field ───────────────────────────────────────────────
STRS = ['""', '"a"', '"ab"', '"abc"', '"abcd"', '"\\u00e9\\u00e9"', '"\\ud83d\\ude00\\ud83d\\ude00\\ud83d\\ude00"',
        '"\\ud83d\\ude00\\ud83d\\ude00\\ud83d\\ude00\\ud83d\\ude00"', '"AB"', '"a1"', '"A1:B2"', '"A1:"', '"abc\\n"',
        '"abc\\ndef"', '"aaaa_-"', '"a b c d"', '"x\\u0000yz"', '5', 'null', '[]']
NUMS = ['0', '1', '5', '10', '11', '20', '-1', '0.5', '1.5', '1.0', '3600', '3600.5', '3601', '1000000000',
        '1000000001', '"5"', '"10.0"', '"abc"', 'true', 'false', 'null', 'NaN', 'Infinity', '-Infinity',
        '1e16', '1e17', '-2.5e-7', '-3e-7', '12345678901234567890', '"1e3"', '[5]']
LISTS = ['[]', '["a"]', '["a","b"]', '["a","b","c"]', '[1,"b","c"]', '["a",1]', '[1,2,3,4,5,6,7]', '"abc"', '{}',
         'null', '[null]', '[[]]', '[1]', '[1,2]', '["x"]', '[1,"x"]', '{"a":1}', '{"a":1,"b":2}',
         '{"a":"x","b":"y"}', '{"a":1,"b":2,"c":"x"}', '{"a":1,"b":"y"}', '{"a":"1"}', '{"a":true}', '[1.0, 2.5]']
for field in ["short", "one", "pat", "rng", "both", "opt_s", "opt_p"]:
    for v in STRS:
        add("body", f"constr-{field}", model="syn.Constrained", body=f'{{"{field}":{v}}}', ct=JSON)
for field in ["n", "big", "x", "g", "contra", "fl", "fb", "fneg", "opt_i"]:
    for v in NUMS:
        add("body", f"constr-{field}", model="syn.Constrained", body=f'{{"{field}":{v}}}', ct=JSON)
for field in ["lst", "lst1", "lst_min", "lst_both", "dct", "dct2", "opt_l"]:
    for v in LISTS:
        add("body", f"constr-{field}", model="syn.Constrained", body=f'{{"{field}":{v}}}', ct=JSON)
add("body", "constr-all", model="syn.Constrained", ct=JSON,
    body='{"opt_p":"Bad","dct":{"a":1,"b":2},"short":"a","n":0,"lst":[1,2,3],"x":-1,"one":"","pat":"A","g":10,'
         '"fl":0.5,"lst_min":[],"rng":"zz","both":"x","contra":5,"fb":0,"big":-1,"lst1":["x"],"opt_i":0}')

# ── nested models, lists of models, unions, literals, enums ───────────────────
NESTED = [
    '{}', '{"inner":{}}', '{"inner":5}', '{"inner":"x"}', '{"inner":[]}', '{"inner":null}', '{"inner":{"a":"x","b":1}}',
    '{"inner":{"a":1,"zz":2}}', '{"inners":[{"a":"x"},3,"s",null,{}]}', '{"inners":{}}', '{"opt":null}', '{"opt":[]}',
    '{"opt":{"b":2}}', '{"m":{"k":{"a":1},"j":{"a":"z"},"l":5}}', '{"m":[]}', '{"grid":[[null,{"a":1}],[{"a":"x"}]]}',
    '{"grid":[[null,null,null]]}', '{"grid":[[],[],[],[]]}', '{"grid":[5,[{"b":1},7]]}', '{"grid":[[null,{"a":"q"},{}]]}',
    '{"anyl":[1,"x",null,{}]}', '{"anyl":[1,2,3,4,5]}', '{"anyl":"x"}', '{"anyd":{"a":[1,{"b":null}]}}', '{"anyd":[]}',
    '{"bared":{"x":1}}', '{"bared":5}', '{"barel":[{},{"a":1},5,[]]}', '{"lit":"x"}', '{"lit":"info"}', '{"lit":null}',
    '{"lit":1}', '{"lit1":"x"}', '{"optlit":"c"}', '{"optlit":null}', '{"optlit":"a"}', '{"cap":"VIEW"}',
    '{"cap":"manage"}', '{"cap":1}', '{"cap":null}', '{"u":null}', '{"u":5.0}', '{"u":5.5}', '{"u":true}', '{"u":"x"}',
    '{"u":[1]}', '{"u":{}}', '{"u":"5"}', '{"u":12345678901234567890}', '{"u":1e400}',
    '{"rp":{"a":{},"b":[1,{}],"c":[null],"d":5.0,"e":true,"f":"5","g":1,"h":[1.5,"a",false],"i":null}}',
    '{"rp":{"a":[[1]]}}', '{"rp":[]}', '{"su":{"a":[],"b":1.0,"c":true}}', '{"su":{"a":1,"b":2,"c":3,"d":4}}',
    '{"su":{"a":{},"b":"x"}}', '{"dl":{"a":"file","b":"dir","c":null}}', '{"ints":[1,"2",3.0,"x",4.5,true,null]}',
    '{"optl":null}', '{"optl":[1,"a"]}', '{"inner":{"a":1},"inners":[{"a":1}],"lit":"danger","u":"q","cap":"run"}',
    '{"inner":{"a":"1"},"ints":["7"],"u":"7","rp":{"a":"1"},"optl":["2"]}',
]
for b in NESTED:
    add("body", "nested", model="syn.Nested", body=b, ct=JSON)
for b in ['{}', '{"inner":{"a":1}}', '{"name":"x"}', '{"inner":null,"name":null}', '{"inner":{"a":1},"name":"n"}',
          '{"name":5,"inner":{"a":[]}}']:
    add("body", "required-nested", model="syn.RequiredNested", body=b, ct=JSON)

# ── validators ────────────────────────────────────────────────────────────────
VALIDATED = [
    '{}', '{"pw":"bad"}', '{"pw":"worse"}', '{"pw":5}', '{"opt_pw":"bad"}', '{"opt_pw":null}', '{"folder":"a/../b"}',
    '{"folder":"..\\\\x"}', '{"folder":".."}', '{"folder":"a/..."}', '{"pattern":""}', '{"pattern":"../.."}',
    '{"pattern":"abcdef"}', '{"folder":"boom"}', '{"folder":"boom","pw":"bad"}', '{"templates":{"a":"12345"}}',
    '{"templates":{"abcd":"1"}}', '{"templates":{"a":"1","b":"2","c":"3"}}', '{"templates":{"a":"1","b":"2","c":5}}',
    '{"templates":{"a":"1"},"folder":"x/.."}', '{"pw":"bad","folder":"..","pattern":"..","opt_pw":"worse"}',
]
for b in VALIDATED:
    add("body", "validated", model="syn.Validated", body=b, ct=JSON)
    add("body", "child", model="syn.Child", body=b, ct=JSON)
add("body", "child", model="syn.Child", body='{"pw":"bad","extra_field":"x"}', ct=JSON)
add("body", "child", model="syn.Child", body='{"extra_field":1,"folder":"boom"}', ct=JSON)

FORBID = [
    '{}', '{"schema":"ok_1"}', '{"schema_":"ok"}', '{"schema":"A","schema_":"b","x":1,"n":"q","y":2}',
    '{"schema":"1bad"}', '{"schema":null}', '{"zzz":1}', '{"sub":{"a":{"a":1,"x":2}}}', '{"sub":{"a":{"a":"x"}},"q":1}',
    '{"schema_":"Bad!"}', '{"schema_":"ok","schema":"also"}',
]
for b in FORBID:
    add("body", "forbid", model="syn.Forbid", body=b, ct=JSON)

DATES = ["2024-01-01", "2024-01-01T10:20:30", "2024-01-01 10:20", "2024-01-01T10:20:30Z",
         "2024-01-01T10:20:30.123456789+02:00", "2024-1-1", "20240101", "2024-13-01", "2024-02-30",
         "2024-01-01T25:00", "2024-01-01T10:20:30+25:00", "1700000000", "1700000000.5", "1e10", "-1", "abc", "",
         "2024", "2024-01", "2024-01-01T", "2024-01-01x", "2024-01-01T10", "2024-01-01T10:2", "2024-01-01T10:20:61",
         "2024-01-01T10:20:30.", "2024-01-01T10:20:30Zx", "2024-01-01t10:20:30z", "2024-01-01_10:20",
         "99999999999999999999", "2024-01-01T10:20:30+0200", "2024-01-01T10:20:30+02", "2024-01-01T10:20:30-00:00",
         "0000-01-01", "0000-01-01T00:00:00", "2024-01-01T24:00:00", "2024-01-01T10:20:30.1234567", "  2024-01-01",
         "2024-01-01 ", "1700000000000", "17000000000000000", "-17000000000000000", "nan", "inf", "-1.5",
         "1_700_000_000", "+1700000000", "2024-02-29", "2023-02-29", "2024-04-31", "2024-01-00", "2024-00-01",
         "2000-02-29", "1900-02-29", "x024-01-01", "2024/01/01", "2024-0x-01", "2024-01-0x", "2024-01x01",
         "2024-01-01T10:20:30,5", "2024-01-01T10:20:30.000001Z", "2024-01-01T23:59:59.999999-12:30",
         "-11676096001", "-11676096000", "253402300799", "253402300800", "1.5", "1.", ".5", "-", "+",
         "1700000000.123456789", "20000000000", "20000000001", "-20000000001", "2024-01-01T10:20:30+23:59",
         "2024-01-01T10:20:30+24:00", "2024-01-01T10:20:30+02:60", "2024-01-01T10:20Z", "2024-01-01T10:20+01:00"]
for d in DATES:
    add("body", "dt-str", model="syn.Dt", body=json.dumps({"when": d}), ct=JSON)

# ── the body as FastAPI reads it ──────────────────────────────────────────────
BROKEN = [
    '', ' ', '\n', 'not json', '{', '{"email"', '{"email":', '{"email":"a",}', '{"email":"a" "password":"b"}',
    '{"email" "a"}', '"abc', '"a\\x"', '"\\u12"', '"\\u12x4"', '"\\u1234', '"\\ud800\\u12x4"', '"\\ud800\\udc00"',
    '"a\nb"', 'nul', 'tru', 'fals', '-', '-x', '01', '1.', '1.e5', '1e', '1e+', '[1 2]', '[1,]', '[1', '[1,,2]',
    '{"a":1}}', 'NaN', '-Infinity', 'Infinityx', '-Inf', '{1:2}', "{'a':1}", '  [ ]  x', '{"email":"a","email":5}',
    '{"email":"é€\U0001f600", }', '["éé", x]', 'é', '{"email":"a\u0001"}',
    '{"email":"a","password":"b"} x', '{"email":"a","password":"b"}\n', '\t{"email":"a","password":"b"}\r\n',
    '{"email":"a\\u00e9","password":"\\ud83d\\ude00"}', '{"email":"a\\ud800","password":5}',
    '{"email":"x","password":"\\udc00\\ud800"}', '{"email":["\\ud800"]}', '[]', '5', '"str"', 'true', 'null',
    '1' * 4300, '1' * 4301, '-' + '1' * 4300, '1' * 5000 + '.5', '{"email":' + '1' * 4300 + '}',
    '{"email":' + '[' * 50 + ']' * 50 + '}', '[' * 100 + ']' * 100, '{"email":1e400,"password":2}',
    '{"email":NaN}', '{"email":"a","password":"b","extra":NaN}', '{"\\ud800":1}', '{"email":{"\\ud800":1}}',
]
for b in BROKEN:
    add("body", "login", model=LOGIN, body=b, ct=JSON)
# json.loads gives up past 9993 nested containers (CPython's C recursion limit):
# the extra key is ignored, so the answer says only whether the body parsed
# duplicate keys (the last value, the first position), JSON whitespace, odd numbers
for b in ['{"email":5,"email":"a","password":"p"}', '{"email":"a","password":"p","email":[]}', '{"password":1,"email":2,"password":3}',
          ' {"email"\t:\r"a" ,\n"password" : "b"}', '{"email":"a",\u000b"password":"b"}', '\u000c{}', '{"email":-0,"password":1E5}',
          '{"email":1e-0,"password":0.0e0}', '{"email":-0.0,"password":1.5E+3}', '{"email":"\\u0000"}', '{"\u0001":1,"email":5}',
          '{"email":"a","password":"b"', '{"email":"a" , "password":"b" , }', '[1,2,{"email":5}]', '{"email":{"password":5}}',
          '{"email":"a","password":"b","c":{"d":[1,2,{"e":NaN}]}}', '{"email":"' + 'é' * 300 + '",}', '{"email":"\\ud83d\\ude00", "x": 1,}',
          '1.5', '-0.0', '1e400', '12345678901234567890', '"\\ud800"', '[NaN]', '{"email":true,"password":false}']:
    add("body", "login2", model=LOGIN, body=b, ct=JSON)
for b in ['{"dct2":{"\\ud800":"x"}}', '{"lst":["\\ud800"]}', '{"short":"\\ud800"}', '{"opt_s":"\\ud800\\ud800"}',
          '{"pat":"a\\ud800"}', '{"lst":["a","\\ud800","c"]}']:
    add("body", "constr-surrogate", model="syn.Constrained", body=b, ct=JSON)
for b in ['{"lit":"\\ud800"}', '{"cap":"\\ud800"}', '{"u":"\\ud800"}', '{"rp":{"a":"\\ud800"}}', '{"su":{"\\ud800":1}}',
          '{"anyd":{"\\ud800":"\\udc00"}}', '{"m":{"\\ud800":{"a":"x"}}}']:
    add("body", "nested-surrogate", model="syn.Nested", body=b, ct=JSON)
for b in ['{"schema":"\\ud800"}', '{"\\ud800":1}', '{"schema_":"\\ud800"}']:
    add("body", "forbid-surrogate", model="syn.Forbid", body=b, ct=JSON)

for depth in [9991, 9992, 9993, 10000, 20000]:
    add("body", f"depth{depth}", model="syn.OptScalars", body='{"zzz":' + '[' * depth + ']' * depth + '}', ct=JSON)
    add("body", f"depth-obj{depth}", model="syn.OptScalars", body='{"zzz":' * (depth + 1) + '1' + '}' * (depth + 1), ct=JSON)
# a deep input echoed in an error: jsonable_encoder recurses past Python's limit
add("body", "echo-depth", model=LOGIN, body='{"email":' + '[' * 2000 + ']' * 2000 + '}', ct=JSON)

RAW = [
    b'\xef\xbb\xbf{"email":"a","password":5}', b'\xef\xbb\xbf\xef\xbb\xbf{}', b'\xff', b'{"email":"\xff"}',
    b'{"email":"\xed\xa0\x80"}', b'{"email":"\xed\xa0\x80","password":"x"}', b'\xc3', b'{"email":"\xc3\xa9"}',
    '{"email":"é","password":5}'.encode("utf-16"), '{"email":"é","password":5}'.encode("utf-16-le"),
    '{"email":"é","password":5}'.encode("utf-16-be"), '{"email":5}'.encode("utf-32"),
    '{"email":5}'.encode("utf-32-le"), '{"email":5}'.encode("utf-32-be"), b'\x00{', b'{\x00', b'\xfe\xff\x00',
    b'\xff\xfe{\x00', '[]'.encode("utf-16-le"), b'\x00\x00\x00{', '{"email":"\U0001f600"}'.encode("utf-16-le"),
    b'\xff\xfe\x00\xd8', b'\xff\xfe\x00\x00\x00\x00\x11\x00',
]
for b in RAW:
    add("body", "raw", model=LOGIN, raw=b, ct=JSON)

CTS = [None, "", "text/plain", "application/json; charset=utf-8", "APPLICATION/JSON", "application/vnd.api+json",
       "application/json-patch+json", "application/x-www-form-urlencoded", "multipart/form-data; boundary=x", "json",
       "application/jsonx", "application /json", " application/json ", "application/json/x", "application/+json",
       "Application/Problem+JSON; q=1", "text/json", "application/json;", ";application/json"]
for ct in CTS:
    for b in ['{"email":5,"password":"x"}', 'email=a', '', 'null', '{bad']:
        add("body", "ct", model=LOGIN, body=b, ct=ct)
    add("body", "ct-raw", model=LOGIN, raw=b'\xff\xfe', ct=ct)
    add("body", "ct-raw", model=LOGIN, raw=b'a\xed\xa0\x80', ct=ct)
    add("body", "ct-esc", model=LOGIN, body='é \n "q" \\ \x01', ct=ct)
for b in ['', 'null', '5', '{}', '{"expected_fingerprint":5}', '[]', 'x']:
    add("obody", "optional", model="app.routes.connections.HostKeyAcceptance", body=b, ct=JSON)
    add("obody", "optional-nested", model="syn.RequiredNested", body=b, ct=JSON)
    add("obody", "optional-none", model=LOGIN, body=b, ct=None)

# ── every gateway model: the generic shapes, then every field the same value ──
GENERIC = ['{}', '[]', '"x"', '1', 'true', 'null', '{"zzz":1}', '[{}]']
FILL = ['{}', '[]', '"x"', '""', '1.5', '-1', '0', 'true', 'null', '[{}]', '{"a":{}}', '["a"]', '{"a":"b"}',
        '"' + 'x' * 260 + '"', '12345678901234567890']
models = gen_models.gateway_models()
for m in models:
    key = f"{m.__module__}.{m.__qualname__}"
    for b in GENERIC:
        add("body", "gw-generic", model=key, body=b, ct=JSON)
    for v in FILL:
        body = "{" + ",".join(f'{json.dumps(f.alias or n)}:{v}' for n, f in m.model_fields.items()) + "}"
        add("body", "gw-fill", model=key, body=body, ct=JSON)

GW = {
    "app.schemas.models.UserCreate": ['{"email":"a","password":""}', '{"email":"a","password":"admin"}',
                                      '{"email":"a","password":" Admin123 "}', '{"email":"a","password":"short"}',
                                      '{"email":"a","password":"ééééééééééé"}',
                                      '{"email":"a","password":"' + "é" * 37 + '"}',
                                      '{"email":"a","password":"' + "x" * 73 + '"}',
                                      '{"email":"a","password":"a-good-long-password"}',
                                      '{"email":"a","password":"CHANGE-ME-2026"}',
                                      '{"email":"a","password":"İadmin"}'],
    "app.schemas.models.UserUpdate": ['{"password":null}', '{"password":"x"}', '{"is_active":"yes","full_name":null}'],
    "app.schemas.models.BannerCreate": ['{"message":""}', '{"message":"' + "m" * 501 + '","level":"error"}',
                                        '{"message":"hi"}'],
    "app.schemas.models.PermissionCreate": ['{"capability":"view"}', '{"capability":"VIEW"}', '{"capability":"own"}',
                                            '{"capability":"run","user_id":"5","group_id":1.0}'],
    "app.schemas.models.FlowGroupCreate": [
        '{"name":"g","definition":{"steps":[[{"kind":"flow","id":1},null],[{"kind":"pushmail","id":2,"pushmail":'
        '{"to":["a@b"],"attachments":[{"maxRows":0},{"maxRows":"x"}],"reports":[{"format":"docx","params":'
        '{"a":{},"b":[1,{}]}}],"dashboards":[{"format":"png","params":{"x":null}}]}}]]}}',
        '{"name":"g","definition":{"steps":[' + ",".join(["[]"] * 51) + ']}}',
        '{"name":"g","definition":{"steps":[[' + ",".join(["null"] * 21) + ']]}}',
        '{"name":"' + "n" * 201 + '","definition":{"steps":[[{"kind":"sql"}]]}}',
        '{"name":"g","definition":{"steps":[[{"kind":"container","id":3,"container":{"cpu":1}}]]}}'],
    "app.schemas.models.ApiDatasourceCreate": [
        '{"name":"a","connection_id":1,"variables":[{"name":"v"}]}',
        '{"name":"a","connection_id":1,"variables":[{"name":"v","type":"range","start":"1","end":" ","step":"1d"}]}',
        '{"name":"a","connection_id":1,"variables":[{"name":"v","values":["x"]},{"name":""}]}',
        '{"name":"a","connection_id":1,"pagination":{"limit":0,"start":-1},"limits":{"min_interval_seconds":3600.5}}',
        '{"name":"a","connection_id":1,"response":{"csv_separator":";;"},"variables":[{"name":"\\ud800"}]}'],
    "app.schemas.models.PlaceholderPreviewQuery": [
        '{"templates":{"a":"' + "t" * 4001 + '"}}', '{"templates":{"' + "k" * 65 + '":"t"}}',
        '{"templates":{"a":"b"},"targets":{"a":"dir"}}', '{"templates":{}}', '{}'],
    "app.schemas.models.SftpDatasourceCreate": [
        '{"name":"s","connection_id":1,"pattern":"../x","folder":"a/.."}',
        '{"name":"s","connection_id":1,"pattern":"*.csv","csv":{"separator":""}}'],
    "app.schemas.models.GsheetsTab": ['{"spreadsheet_id":"short"}', '{"spreadsheet_id":"' + "a" * 44 + '","range":"A1:B"}',
                                      '{"spreadsheet_id":"' + "a" * 44 + '","sheet_id":-1}'],
    "app.services.dbt_export.DbtOptions": ['{"schema":"x y"}', '{"schema_":"ok","bogus":1}', '{"sources":{"a":{"name":"Bad"}}}',
                                           '{"materializations":{"a":"table","b":"x"}}', '{"target":"spark"}'],
    "app.routes.engine_settings.SettingsUpdate": ['{"values":{"a":[],"b":1.0,"c":"x","d":null,"e":true}}',
                                                  '{"values":{' + ",".join(f'"k{i}":1' for i in range(81)) + '}}'],
    "app.routes.workers.ScalingIn": ['{"min_replicas":"1","max_replicas":1.0}', '{"min_replicas":true,"max_replicas":1001}',
                                     '{"min_replicas":-1,"max_replicas":0}'],
    "app.routes.notebook_data.ReadIn": ['{"datasource":null}', '{"datasource":1.0}', '{"datasource":[1]}'],
    "app.routes.flows.PresenceBeat": ['{"instance":"abc"}', '{"instance":"abc!"}', '{"instance":"' + "a" * 65 + '"}'],
}
for key, bodies in GW.items():
    for b in bodies:
        add("body", "gw", model=key, body=b, ct=JSON)

# ── query and path parameters ─────────────────────────────────────────────────
QUERIES = {
    "list": ["", "limit=abc", "limit=0", "limit=201", "limit=5.0", "limit=5.5", "limit=", "limit=&limit=3",
             "limit=3&limit=x", "limit=+7", "limit=%207%20", "limit=1_0", "offset=-1&limit=0", "q=", "q=a+b%20c",
             "q=%ff", "q=%e2%82", "q=%zz", "q=caf%C3%A9", "limit=%", "limit", "q", "&&limit=2&&", "limit=1e2",
             "limit=99999999999999999999", "limit=%EF%BC%91", "offset=0x10"],
    "flags": ["confirm=yes", "confirm=nope", "confirm=", "confirm=1&with_report=off", "confirm=2", "tz_offset=-60",
              "tz_offset=--1", "confirm=TRUE&tz_offset=+120", "with_report"],
    "req": ["", "flow_id=1", "flow_id=x&language=it", "language=&table=", "flow_id=1&language=en&table=" + "t" * 501,
            "flow_id=1&language=en&table=ok"],
    "audit": ["since=2024-01-01", "since=2024-13-01&until=x", "since=1700000000", "until=2024-01-01T10:20:30Z",
              "since=2024-01-01T10:20:30%2B02:00", "since=2024-01-01T10:20:30+02:00", "actor_id=&since=",
              "until=2024-02-30T00:00:00", "since=0000-01-01"],
    "lits": ["sort=name", "sort=NAME", "sort=", "type=flow", "type=x", "direction=up&sort=x&type=y"],
    "pat": ["target=spark", "target=duckdb", "target=DUCKDB", "target=duckdbx", "reference=" + "r" * 201, "fmt=%00"],
    "num": ["ratio=nan", "ratio=inf", "ratio=-0", "ratio=1.5", "ratio=1_0", "ratio=.5", "minutes=4", "depth=9",
            "minutes=5.0", "ratio=1e-400", "ratio=0x1p-2"],
}
for route, qs in QUERIES.items():
    for q in qs:
        add("query", route, route=route, query=q)
for route, vals in {"int": ["5", "x", "5.0", " 7", "-0", "1_0", "99999999999999999999", "٣", "%"],
                    "str": ["x", "a b", "é"], "lit": ["run", "Run", "x"]}.items():
    for v in vals:
        add("path", route, route=route, path=v)
for path, q, b in [("x", "limit=0", '{"email":5}'), ("1", "limit=0", '{}'), ("x", "", '{"email":"a","password":"b"}'),
                   ("1", "limit=2", '{"email":"a","password":"b"}'), ("x", "limit=y", 'not json')]:
    add("combo", "combo", path=path, query=q, body=b)

with gzip.open("/td/cases.json.gz", "wt", encoding="utf-8") as fh:
    json.dump(cases, fh, indent=0, ensure_ascii=False)
    fh.write("\n")
print(f"{len(cases)} cases")
