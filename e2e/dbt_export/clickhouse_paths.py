"""The ClickHouse paths of the dbt export against Tabularia's ClickHouse engine, and the seeds on
the targets that load them with their own loaders.

1. The same flow run by Tabularia with the external ClickHouse engine (production) and by dbt on
   ClickHouse must give the same data: columns in order, type families, rows. The paths:
   · the ClickHouse target over a Postgres table (read live through a federated `tab_src_*`
     database with the PostgreSQL table engine) and over a Postgres QUERY datasource (its SQL
     translated to ClickHouse by sqlglot: Tabularia ran it in Postgres);
   · a table of the ClickHouse the project runs on, read as it is: the ClickHouse target and
     the native target on that connection (dbt-clickhouse);
   · both in one flow (an Int32 key with a UInt64 one: refused by both).
   The data are what the engines see differently: NULL on the missing side of a join (a NOT NULL
   column of the right table), -0.0 and 0.0 as keys, NaN, a Float32, dates out of ClickHouse's
   Date range, DateTime with a zone of its own, Decimal, UInt64 at its maximum, text in byte
   order, a filter value as the editor's calendar writes it, a float or a decimal made text,
   Postgres' integer division, ::numeric and ::int in a query.
2. A file read as a seed: dbt-postgres (native target) and dbt-clickhouse (ClickHouse target,
   native ClickHouse) load it with their own loaders, and the model must still give what
   Tabularia read from the file: codes with zeros, 'NULL', 'null', '', 'True', quotes, a comma and
   a line break in a text, a BOM, dates out of ClickHouse's Date range, instants (one with an
   offset), many digits, reserved, accented and capitalised names; an Excel sheet (its
   date-times to the millisecond); a file of exactly SEED_MAX_ROWS rows, and one more, which the
   plan must disable.

Cases marked «motore» were differences where the ENGINE was wrong and dbt gave the right value
(the engine's cast, backend/app/engine/chdb_ops.py, fixed since). A query whose numbers the
export cannot write with Postgres' values (a type it cannot tell) must be refused, never changed.
The known differences between engines (docs/engines/engine-differences.md) and the text casts
the export writes as the in-process engines read them (docs/dbt-export.md) are not checked here;
nor a time of day (the ClickHouse engine reads a parquet time as a 1970-01-01 datetime).

Everything this oracle makes is named zzc…: its own Postgres schemas (zzc, zzc_dbt), its own
ClickHouse databases (zzc_dbt, the federated tab_src_<id>_zzc) and table (analytics.zzc_tipi),
its own NetworkPolicy (label e2e.tabularia.org/owner=zzc-hunt-ch) and dbt pod (E2E_DBT_POD) —
removed at the end (--keep keeps them). It never touches dbt_tabularia nor the policies of the
other oracles.
usage: python3 clickhouse_paths.py [--only pg|query|ch|mix|seed|excel|limit] [--case PREFIX] [--keep]"""
import io, json, os, re, sys, time, zipfile

from environment import CH_HOST, CH_PORT, KUBE, WORKDIR
from common import (api, sign_in, ok, outcomes, connection_passwords, sample_postgres, sample_clickhouse,
                    run_dbt_image, clickhouse_settings, remove_folder, reach_sources, kube_cleanup, clean_clickhouse)
import matrix as M

SEED_MAX_ROWS = 50_000   # backend/app/engine/dbt_export.py (drift.py checks the gateway's copy)

SCHEMA = "zzc_dbt"          # where dbt writes, on every target (never the other oracles' dbt_tabularia)
PG_SCHEMA = "zzc"           # the source tables in the sample Postgres
CH_TABLE = "analytics.zzc_tipi"
OWNER = "zzc-hunt-ch"       # the label of this oracle's NetworkPolicy
F, J = M.F, M.J

# ── the sources ───────────────────────────────────────────────────────────────
PG_SQL = r"""
DROP SCHEMA IF EXISTS zzc CASCADE;
CREATE SCHEMA zzc;
CREATE TABLE zzc.dati (id integer PRIMARY KEY, k integer, txt varchar, f double precision, r real, n numeric(12,4),
  d date, ts timestamp, tz timestamptz, b boolean, cat varchar, big bigint);
INSERT INTO zzc.dati VALUES
 (1, 1,    '12',         1.5,   0.1,  1.2345,  '1955-03-12', '2024-01-05 10:30:00.123456', '2024-03-31 00:30:00+00', true,  'a',  9007199254740993),
 (2, 2,    ' 7 ',        '-0',  1.5,  NULL,    '2150-06-01', '2024-01-05 10:30:00',        '2024-10-27 00:30:00+00', false, 'B',  -1),
 (3, NULL, 'abc',        0.0,   NULL, -0.0001, '2024-02-29', NULL,                         NULL,                     NULL,  'b',  NULL),
 (4, 4,    '',           'NaN', 7,    100,     NULL,         '1965-06-01 08:00:00',        '1965-06-01 00:00:00+00', true,  'A',  0),
 (5, 5,    NULL,         NULL,  -2.5, 2.5,     '1970-01-01', '2024-12-31 23:59:59.999999', '2024-12-31 23:30:00+00', false, NULL, 5),
 (6, 6,    'Città',      2.5,   2.5,  -2.5,    '2024-01-01', '2024-01-01 00:00:00',        '2024-06-30 22:00:00+00', true,  'é',  6),
 (7, 1,    'città',      -2.5,  0.5,  0.125,   '2024-01-02', '2024-01-02 12:00:00',        '2024-01-01 23:59:59+00', false, 'E',  7),
 (8, 2,    '2024-01-05', 1e20,  3.25, 99999999.9999, '2024-01-03', '2024-01-03 00:00:00.5', '2024-01-02 00:00:00+00', true, 'a', 8);
CREATE TABLE zzc.destra (rid integer NOT NULL, k integer, cat varchar, f double precision, peso double precision, label varchar NOT NULL);
INSERT INTO zzc.destra VALUES
 (10, 1, 'a', 0.0, 1.5, 'uno'), (20, 2, 'B', '-0', 2.5, 'due'), (30, NULL, NULL, NULL, 3.5, 'nullo'),
 (40, 9, 'z', 9.5, 9.5, 'nove'), (50, 1, 'A', 1.5, NULL, 'uno bis');
"""
CH_SQL = r"""
DROP TABLE IF EXISTS analytics.zzc_tipi;
CREATE TABLE analytics.zzc_tipi (
 id UInt64, k Nullable(Int32), cat LowCardinality(String), cat_n Nullable(String), en Enum8('rosso' = 1, 'verde' = 2),
 dec Decimal(18, 4), dec_n Nullable(Decimal(10, 2)), f Float64, f32 Float32, d Date, d32 Date32,
 dt DateTime('Europe/Rome'), dt64 DateTime64(3, 'America/New_York'), dtu DateTime64(6), b Bool, u8 UInt8
) ENGINE = MergeTree ORDER BY id;
INSERT INTO analytics.zzc_tipi VALUES
 (1, 1, 'a', 'x', 'rosso', 1.2345, 1.25, 1.5, 0.1, '2024-01-15', '1955-03-12', '2024-03-31 01:30:00', '2024-01-15 23:30:00.123', '2024-01-05 10:30:00.123456', true, 1),
 (2, NULL, 'B', NULL, 'verde', -0.0001, NULL, -0.0, 1.5, '2024-02-29', '2150-06-01', '2024-01-01 00:30:00', '2024-07-01 22:00:00.000', '2024-01-05 10:30:00', false, 0),
 (3, 3, '', '', 'rosso', 99999999999999.9999, -2.5, nan, 7, '1970-01-01', '1901-01-01', '2024-10-27 02:30:00', '2024-11-03 01:30:00.500', '1965-06-01 08:00:00', true, 255),
 (18446744073709551615, 2, 'b', 'città', 'verde', 2.5, 0.01, 0.0, -2.5, '2149-06-06', '2299-12-31', '2024-06-30 23:59:59', '2024-12-31 19:00:00.000', '2024-12-31 23:59:59.999999', false, 7);
"""

# ── the cases: a Postgres table on the ClickHouse target ────────────────────────
C = lambda cols: {"type": "cast", "params": {"columns": cols}}
G = lambda by, *aggs: {"type": "group_by", "params": {"by": by, "aggregations": [{"column": c, "func": fn, "alias": a} for c, fn, a in aggs]}}
S = lambda by, desc=False: {"type": "sort", "params": {"by": by, "descending": desc}}
SEL = lambda *cols: {"type": "select", "params": {"columns": list(cols)}}
CMP = lambda **e: {"type": "compute", "params": {"columns": [{"name": k, "expr": v} for k, v in e.items()]}}
UNP = lambda index, on, var="misura", val="valore": {"type": "unpivot", "params": {"index": index, "on": on, "variable_name": var, "value_name": val}}

PG_CASES = [
    ("zzc p01 tipi", []),
    # a NOT NULL column of the right table on the missing side: NULL (join_use_nulls)
    ("zzc p02 join left", [J("left", on="k")], {"right": []}),
    ("zzc p03 join full", [J("full", on="k")], {"right": []}),
    ("zzc p04 join anti", [J("anti", on="k")], {"right": []}),
    # -0.0 and 0.0 are one value for the engine (it groups and joins a float by its value)
    ("zzc p05 join chiave float", [SEL("id", "f"), J("inner", left_on="f", right_on="f")], {"right": [SEL("f", "label")]}),
    ("zzc p06 group by float", [G(["f"], ("id", "count", "righe"))]),
    ("zzc p07 unique float", [SEL("f"), {"type": "unique", "params": {}}]),
    ("zzc p08 aggregati", [S("id"), G(["b"], ("txt", "n_unique", "testi"), ("txt", "count", "conta"), ("cat", "first", "primo"),
                                         ("cat", "last", "ultimo"), ("cat", "min", "minimo"), ("cat", "max", "massimo"),
                                         ("r", "mean", "media_r"), ("big", "sum", "somma"), ("n", "median", "mediana"),
                                         ("r", "std", "dev"), ("d", "max", "ultimo_giorno"), ("tz", "min", "primo_istante"))]),
    ("zzc p09 sort testo", [S("cat")], {"order": ["cat"]}),
    ("zzc p10 sort float desc", [S(["f", "id"], True)], {"order": ["f", "id"]}),
    # the editor's calendar writes 2024-01-05T10:30, without seconds
    ("zzc p11 filtro istante dal calendario", [F("ts", "ge", "2024-01-05T10:30")]),
    ("zzc p12 filtro istante utc", [F("tz", "lt", "2024-01-02T00:00")]),
    ("zzc p13 filtro data", [F("d", "lt", "1970-01-01")]),
    ("zzc p14 filtro testo ordine", [F("cat", "gt", "B")]),
    ("zzc p15 filtro contiene", [F("txt", "contains", "ttà")]),
    ("zzc p16 filtro float zero", [F("f", "eq", 0)]),
    ("zzc p17 cast float testo", [C({"f": "str", "n": "str", "big": "str"})]),
    ("zzc p18 cast real testo", [C({"r": "str"})]),
    ("zzc p19 cast date testo", [C({"ts": "str", "d": "str", "tz": "str", "b": "str"})]),
    ("zzc p20 cast tempi", [C({"ts": "date", "tz": "date"})]),
    ("zzc p21 cast numeri", [C({"f": "int", "n": "int", "r": "float", "b": "int"})]),
    # the engine's cast gives NULL here (accurateCastOrNull: 2^53 + 1 is not exactly a double), dbt the double
    ("zzc p21b motore intero grande a float", [C({"big": "float"})]),
    ("zzc p22 cast testo numeri", [C({"txt": "int"})]),
    ("zzc p23 cast numero booleano", [C({"f": "bool", "k": "bool"})]),
    ("zzc p24 unpivot testo e float", [UNP(["id"], ["txt", "f"])]),
    ("zzc p25 unpivot testo e real", [UNP(["id"], ["cat", "r"])]),
    ("zzc p26 unpivot numeri", [UNP(["id"], ["f", "r", "n"])]),
    ("zzc p27 unpivot date e istanti", [UNP(["id"], ["d", "ts"])]),
    ("zzc p28 union by name", [SEL("id", "k", "f", "cat"), {"type": "union", "params": {}}], {"right": [{"type": "rename", "params": {"mapping": {"rid": "id"}}}]}),
    ("zzc p29 union tipi diversi", [SEL("id", "k"), {"type": "union", "params": {}}], {"right": [SEL("rid", "k"), C({"k": "float"})]}),
    ("zzc p30 fill null", [{"type": "fill_null", "params": {"columns": {"f": 0, "txt": "", "cat": "Z", "r": 0.5, "b": True, "k": -1}}}]),
    # ClickHouse text in a compute and a sql node: the ClickHouse target writes it as the engine runs it
    ("zzc p31 compute clickhouse", [CMP(giorno="toDate(ts)", diviso="f / 0", intero="intDiv(id, 3)", tondo="round(r, 1)",
                                        lung="length(txt)", conc="concat(cat, txt)", anno="toYear(d)", mese="toStartOfMonth(ts)",
                                        fmt="formatDateTime(ts, '%Y-%m-%d %H')", segno="multiIf(f > 1, 'grande', f < 0, 'negativo', 'piccolo')",
                                        su="upper(txt)", ora="toHour(tz)", nulli="ifNull(k, -1)")]),
    ("zzc p35 compute testo di un real", [CMP(testo_r="toString(r)")]),
    # a number and a text by name: ClickHouse has no common type for them (Tabularia refuses)
    ("zzc p36 union numero e testo", [SEL("id", "k"), {"type": "union", "params": {}}], {"right": [SEL("rid", "k"), C({"k": "str"})]}),
    ("zzc p32 sql clickhouse", [{"type": "sql", "params": {"query": "SELECT cat, count() AS n, sum(f) AS s, max(ts) AS ultimo FROM self GROUP BY cat"}}]),
    ("zzc p33 group by data", [G(["d"], ("id", "count", "righe"), ("ts", "max", "ultimo"), ("n", "sum", "somma"))]),
    ("zzc p34 drop nulls e limit", [{"type": "drop_nulls", "params": {"subset": ["k", "cat"]}}, S("id"), {"type": "limit", "params": {"n": 3}}], {"order": ["id"]}),
]

# ── a table of the ClickHouse the project runs on: the ClickHouse target and the native one ──
CH_CASES = [
    ("zzc h01 tipi", []),
    ("zzc h02 cast istanti", [C({"dt": "date", "dt64": "date"})]),
    # a DateTime of ClickHouse is read with milliseconds: as text the engine writes 3 digits of fraction
    ("zzc h03 cast istanti al ms testo", [C({"dt": "str", "dt64": "str"})]),
    ("zzc h03b cast istanti al us testo", [C({"dtu": "str"})]),
    ("zzc h04 cast decimale testo", [C({"dec": "str"})]),
    ("zzc h04b cast decimale intero", [C({"dec_n": "int"})]),
    ("zzc h04d cast decimale largo intero", [C({"dec": "int"})]),
    ("zzc h04c cast float32 testo", [C({"f32": "str"})]),
    ("zzc h05 cast interi", [C({"id": "str", "u8": "str", "en": "str"})]),
    # the engine's cast goes through a 32-bit DateTime: 1955 is 1970, 2150 is 2106 (dbt gives the day)
    ("zzc h05b motore data a istante", [C({"d32": "datetime"})]),
    ("zzc h06 cast uint64", [C({"id": "int"})]),
    ("zzc h07 group by", [G(["en"], ("dec", "sum", "somma"), ("dec", "mean", "media"), ("dec_n", "sum", "somma_n"), ("dt", "max", "ultimo"),
                           ("cat_n", "n_unique", "diversi"), ("u8", "sum", "somma_u8"), ("d32", "min", "primo"), ("id", "count", "righe"))]),
    ("zzc h08 compute clickhouse", [CMP(giorno="toDate(dt)", ora="toHour(dt)", fmt="formatDateTime(dt64, '%F %T')", doppio="dec * 2",
                                        terzo="dec / 3", testo_dec="toString(dec)", testo_f32="toString(f32)", prima="d32 - 1",
                                        lung="length(cat_n)", anni="dateDiff('year', d32, d)")]),
    ("zzc h09 sort", [S(["dt", "id"], True)], {"order": ["dt", "id"]}),
    ("zzc h10 filtro istante", [F("dt", "ge", "2024-03-31T00:00")]),
    ("zzc h11 filtro decimale", [F("dec", "gt", 1)]),
    ("zzc h12 filtro uint64", [F("id", "eq", 18446744073709551615)]),
    ("zzc h13 join nullable", [J("left", on="k")], {"right": [SEL("k", "dec", "b")]}),
    ("zzc h14 unpivot decimali float", [UNP(["id"], ["dec", "f"])]),
    ("zzc h15 unpivot testo decimali", [UNP(["id"], ["cat", "dec", "f32"])]),
    ("zzc h16 group by float", [G(["f"], ("id", "count", "righe"))]),
]


# ── a Postgres table and a table of the engine's ClickHouse in one flow (ClickHouse target only) ──
MIX_CASES = [
    ("zzc m01 join int32 uint64", [SEL("id", "txt"), J("left", on="id")], {"right": [SEL("id", "cat", "dec")]}),
    ("zzc m02 union int32 uint64", [SEL("id", "k", "cat"), {"type": "union", "params": {}}], {"right": [SEL("id", "k", "cat")]}),
    ("zzc m03 join chiave nullable", [SEL("id", "k"), J("inner", on="k")], {"right": [SEL("k", "dt")]}),
]

# ── a Postgres QUERY datasource on the ClickHouse target: its SQL translated to ClickHouse by sqlglot.
# Tabularia ran it in Postgres; the project runs it in ClickHouse, on the federated tables
QUERIES = {
    "zzc q1 aritmetica": "SELECT id, id / 3 * 3 AS a, k / 2 * 2 AS b, (id + 0.0) / 4 AS c, id % 3 AS resto FROM zzc.dati",
    # x::numeric is a Decimal(10, 0) once translated: round(f::numeric, 2) loses the decimals (and 1e20 fails)
    "zzc q2 numeric": "SELECT id, round(f::numeric, 2) AS tondo, round(n, 1) AS n1 FROM zzc.dati WHERE id IN (1, 2, 3, 5, 6, 7)",
    # Postgres rounds a number cast to an integer (a numeric half away from zero, a double half to even),
    # ClickHouse truncates it: the export writes Postgres' rounding where it knows the type (a cast, a
    # real the query returns as it is)
    "zzc q7 cast int": ("SELECT id, n::numeric::int AS intero, (r * 10)::int AS r10, f::float8::int AS fi, r FROM zzc.dati "
                        "WHERE id IN (1, 2, 3, 5, 6, 7)"),
    "zzc q3 date": "SELECT id, date_trunc('month', ts) AS mese, d + 1 AS domani, ts::date AS giorno, to_char(ts, 'YYYY-MM') AS ym FROM zzc.dati",
    "zzc q4 testo": ("SELECT id, txt || '-' || cat AS etichetta, concat(txt, cat) AS conc, length(txt) AS lung, upper(txt) AS su, "
                     "substr(txt, 2, 3) AS pezzo, coalesce(cat, 'nessuna') AS cat2, txt ILIKE '%CITT%' AS simile FROM zzc.dati"),
    "zzc q5 aggregati": "SELECT cat, count(*) AS n, sum(k) AS somma, avg(k) AS media, max(ts) AS ultimo FROM zzc.dati GROUP BY cat",
}
QUERY_CASES = [(f"zzc r{name[5]} {name[7:]}", [], {"source": name}) for name in QUERIES]
# what the export refuses (query_not_translatable: the AI may then propose a translation), also after a model
# it translates: ClickHouse has no EXTRACT(DOW …), which only ClickHouse's own parser sees; a numeric or a
# double cast to an integer, whose type the query's output does not tell (Tabularia reads both as Float64),
# and a division of columns of an unknown type, which Postgres may do as integers
REFUSED = {
    "zzc q9 dow": "SELECT id, extract(dow from d) AS dow FROM zzc.dati",
    "zzc q8 tipo ignoto": "SELECT id, n::int AS intero, f::int AS fi FROM zzc.dati WHERE id IN (1, 2, 3, 5, 6, 7)",
    "zzc q6 divisione ignota": "SELECT k / nullif(big, 0) AS diviso FROM zzc.dati",
}

# ── a file read as a seed ───────────────────────────────────────────────────
# what the loaders read their own way: codes with zeros, the words a loader takes for NULL or a
# boolean, quotes, a line break and a comma inside a text, an empty quoted text, many digits, dates
# out of ClickHouse's Date range, an instant with an offset, a time; a BOM; names with a space, an
# accent, capitals, a reserved word
SEED_CSV = ("\ufeffcodice,Testo,Nome Città,select,MAIUSCOLO,numero,importo,grande,giorno,data_ambigua,istante,istante_tz,flag,nota\n"
            '00123,NULL,Città,1,ABC,1.5,1234567890.123456789,9007199254740993,2024-01-15,01/02/2024,2024-01-15 10:30:00,2024-01-15T10:30:00+02:00,true,"a ""q"" b"\n'
            'A12,null,"",2,abc,0.1,1e-7,-9223372036854775808,1955-03-12,13/02/2024,2024-02-29T23:59:59.123456,2024-07-01T00:30:00+02:00,false,"riga1\nriga2"\n'
            ",,,3,,,,,,,,,,\n"
            '007,True,"  spazi  ",4,É,-0.0,0.30000000000000004,0,2150-06-01,2024-02-30,1965-06-01 08:00:00,2024-12-31T23:30:00Z,false,"a,b"\n'
            "0000,N/A,ü,5,X,1e20,99999999999999.99,1,1970-01-01,2024-01-02,2024-01-01 00:00:00,2024-03-31T02:30:00+01:00,true,#commento\n"
            '-1,\\N,"tab\tx",6,Y,3,2,2,2024-12-31,1/2/24,2024-12-31 23:59:59.999999,2024-10-27T02:30:00+01:00,true,\'apice\'\n').encode()
SEED_CASES = [
    ("zzc s01 seed", [], {"source": "file"}),
    ("zzc s02 seed testo nullo", [F("Testo", "is_null")], {"source": "file"}),
    ("zzc s03 seed testo vuoto", [F("Nome Città", "eq", "")], {"source": "file"}),
    ("zzc s04 seed date testo", [C({"giorno": "str", "grande": "str"})], {"source": "file"}),
    ("zzc s05 seed sort", [S(["Nome Città", "select"])], {"source": "file", "order": ["Nome Città", "select"]}),
    ("zzc s06 seed group by", [G(["flag"], ("select", "count", "righe"), ("importo", "sum", "somma"), ("istante_tz", "max", "ultimo"))], {"source": "file"}),
    # a database table in the flow: the native target needs one (all sources on one connection)
    # an instant made text: the ClickHouse engine writes its microseconds ('…10:30:00.000000')
    ("zzc s08 seed istanti testo", [C({"istante": "str", "istante_tz": "str"})], {"source": "file"}),
    ("zzc s07 seed join", [J("left", left_on="select", right_on="k")], {"source": "file", "right": [SEL("k", "label")]}),
]


def xlsx(rows) -> bytes:
    """A one-sheet .xlsx written by hand (no library needed): text as inline strings, numbers,
    booleans, and datetime.date / datetime.datetime as serial numbers with a date format (the
    way Excel stores them). None is an empty cell."""
    import datetime as dt
    from xml.sax.saxutils import escape

    def col(i):
        name = ""
        while True:
            i, r = divmod(i, 26)
            name = chr(65 + r) + name
            if not i:
                return name
            i -= 1

    def cell(ref, v):
        if v is None:
            return ""
        if isinstance(v, bool):
            return f'<c r="{ref}" t="b"><v>{int(v)}</v></c>'
        if isinstance(v, (dt.date, dt.datetime)):
            base = dt.datetime(1899, 12, 30)
            moment = v if isinstance(v, dt.datetime) else dt.datetime(v.year, v.month, v.day)
            serial = (moment - base).total_seconds() / 86400
            return f'<c r="{ref}" s="{2 if isinstance(v, dt.datetime) else 1}"><v>{serial!r}</v></c>'
        if isinstance(v, (int, float)):
            return f'<c r="{ref}"><v>{v!r}</v></c>'
        return f'<c r="{ref}" t="inlineStr"><is><t xml:space="preserve">{escape(str(v))}</t></is></c>'

    sheet = "".join(f'<row r="{i + 1}">' + "".join(cell(f"{col(j)}{i + 1}", v) for j, v in enumerate(r)) + "</row>" for i, r in enumerate(rows))
    files = {
        "[Content_Types].xml": '<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/></Types>',
        "_rels/.rels": '<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>',
        "xl/workbook.xml": '<?xml version="1.0" encoding="UTF-8"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Foglio1" sheetId="1" r:id="rId1"/></sheets></workbook>',
        "xl/_rels/workbook.xml.rels": '<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
        '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/></Relationships>',
        "xl/styles.xml": '<?xml version="1.0" encoding="UTF-8"?><styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<fonts count="1"><font/></fonts><fills count="1"><fill/></fills><borders count="1"><border/></borders>'
        '<cellStyleXfs count="1"><xf/></cellStyleXfs><cellXfs count="3"><xf/><xf numFmtId="14" applyNumberFormat="1"/><xf numFmtId="22" applyNumberFormat="1"/></cellXfs></styleSheet>',
        "xl/worksheets/sheet1.xml": '<?xml version="1.0" encoding="UTF-8"?><worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f"<sheetData>{sheet}</sheetData></worksheet>",
    }
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for name, text in files.items():
            z.writestr(name, text)
    return out.getvalue()


def excel_rows():
    import datetime as dt
    return [["codice", "Descrizione", "importo", "pezzi", "consegna", "giorno", "attivo"],
            ["00123", "NULL", 1.5, 3, dt.datetime(2024, 1, 15, 10, 30), dt.date(1955, 3, 12), True],
            ["A12", "", 0.1, 4, dt.datetime(2024, 2, 29, 23, 59, 59), dt.date(2150, 6, 1), False],
            ["007", "  x  ", 1234567.891, None, None, None, None],
            [None, "a\nb", -2.5, 7, dt.datetime(1965, 6, 1, 8, 0), dt.date(2024, 12, 31), True]]


EXCEL_CASES = [
    ("zzc x01 excel", [], {"source": "file"}),
    # a date-time of an Excel sheet is read to the millisecond: as text the engine writes 3 digits of fraction
    ("zzc x02 excel istante testo", [C({"consegna": "str", "giorno": "str"})], {"source": "file"}),
    ("zzc x03 excel join", [J("left", left_on="pezzi", right_on="k")], {"source": "file", "right": [SEL("k", "label")]}),
]


def upload(name, data, bucket, ctype="text/csv"):
    """A file uploaded as from the editor: its file node and the types Tabularia read."""
    st, up = api("POST", "/files", multipart={"file": (name, data, ctype)})
    assert st in (200, 201) and up.get("parquet_key"), (st, up)
    cols = up["dataset"]["columns"]
    # what the editor's file node holds (FlowEditor.vue): its rows too
    return {"parquetKey": up["parquet_key"], "bucket": bucket, "filename": name, "datasetId": up.get("dataset_id"),
            "rows": up["dataset"].get("rows"), "columns": cols}


def plan_targets(flow):
    plan = api("GET", f"/flows/{flow['id']}/export/dbt/plan")[1]
    return {t["id"]: (t.get("available"), t.get("reason") or "") for t in plan.get("targets", [])}


# ── where things run ────────────────────────────────────────────────────────
def export_to(flow, target, tag):
    """The complete project for `target`, with the schema SCHEMA: (folder, file names) or (None, why)."""
    st, zipped = api("POST", f"/flows/{flow['id']}/export/dbt", {"target": target, "schema": SCHEMA}, raw=True)
    if st != 200 or zipped[:2] != b"PK":
        return None, (st, zipped[:400] if isinstance(zipped, (bytes, str)) else zipped)
    folder = f"{WORKDIR}/zzc_{tag}_{target}"
    remove_folder(folder)
    os.makedirs(folder)
    with zipfile.ZipFile(io.BytesIO(zipped)) as z:
        z.extractall(folder)
        names = sorted(z.namelist())
    clickhouse_settings(f"{folder}/profiles.yml")
    return folder, names


def dbt(folder, *arguments, variables=None):
    """dbt (or dump.py) in the pod; on kind a MEMORY_LIMIT_EXCEEDED of the stack's small ClickHouse
    is waited out and run again (common.run_dbt_image)."""
    return run_dbt_image(folder, list(arguments), variables, retry_memory=True)


def dump(folder, target, variables, pg):
    """Every table dbt left in SCHEMA: {table: {cols, rows, types}}."""
    if target == "postgres":
        rc, out = dbt(folder, "python", "/dump.py", "postgres", pg["host"], pg["database"], pg["username"], SCHEMA,
                      variables={**variables, "PGPASSWORD": variables[f"TABULARIA_DB_{pg['id']}_PASSWORD"]})
    else:
        rc, out = dbt(folder, "python", "/dump.py", "clickhouse", CH_HOST, CH_PORT, SCHEMA, variables=variables)
    if "===DUMP===" not in out:
        raise RuntimeError(out[-1500:])
    return json.loads(out.split("===DUMP===", 1)[1])


def clean_targets():
    sample_postgres(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
    clean_clickhouse(lambda n: n == SCHEMA)


def run_and_compare(flow, folder_id, cases, target, tag, variables, pg, warehouse):
    """Exports `flow` to `target`, runs dbt (federated databases, seeds, models) and compares every
    case with what Tabularia published. warehouse: 'clickhouse' or 'postgres' (where SCHEMA is)."""
    print(f"-- {tag} · target {target}", flush=True)
    clean_targets()
    dbt_folder, names = export_to(flow, target, tag)
    if not ok(f"{tag} · {target}: export", dbt_folder is not None, names):
        return
    if any(n.endswith("create_tabularia_sources.sql") for n in names):
        rc, out = dbt(dbt_folder, "dbt", "run-operation", "create_tabularia_sources", "--log-level-file", "none", "--no-use-colors", variables=variables)
        if not ok(f"{tag} · {target}: dbt run-operation create_tabularia_sources", rc == 0, out[-1200:]):
            return
    if any(n.startswith("seeds/") and n.endswith(".csv") for n in names):
        rc, out = dbt(dbt_folder, "dbt", "seed", "--no-use-colors", variables=variables)
        if not ok(f"{tag} · {target}: dbt seed", rc == 0, out[-1500:]):
            return
    rc, out = dbt(dbt_folder, "dbt", "run", "--no-use-colors", variables=variables)
    if rc != 0 and "ERROR creating sql" not in out:
        ok(f"{tag} · {target}: dbt run", False, out[-1500:])
        return
    failed = {re.sub(r"^.*\.", "", m).strip('`"') for m in re.findall(r"ERROR creating sql (?:table|view|incremental) model (\S+)", out)}
    tab = {c[0]: M.tabularia_result(folder_id, c[0]) for c in cases}
    expected = {M.slug(c[0]) for c in cases if tab[c[0]] is None}
    ok(f"{tag} · {target}: dbt run (only the models Tabularia refuses fail)", failed == expected,
       f"failed in dbt {sorted(failed)}, refused by Tabularia {sorted(expected)}\n"
       + "\n".join(l for l in out.splitlines() if "Error" in l or "Code:" in l)[:1500])
    try:
        produced = dump(dbt_folder, warehouse, variables, pg)
    except Exception as e:  # noqa: BLE001
        ok(f"{tag} · {target}: reading dbt's tables", False, e)
        return
    for c in cases:
        name, opts = c[0], (c[2] if len(c) > 2 else {})
        m = produced.get(M.slug(name)) if M.slug(name) not in failed else None
        if tab[name] is None:
            ok(f"{target} · {name}: refused by Tabularia AND by dbt (parity)", m is None, "Tabularia refuses it but dbt computes it")
            continue
        if m is None:
            ok(f"{target} · {name}", False, "dbt model absent or failed")
            continue
        equal, why = M.compare(name, opts, tab[name], m, target)
        ok(f"{target} · {name}", equal, why)


def make_flow(name, cases, folder_id, sources):
    f = M.build(name, cases, folder_id, sources)
    run = M.wait_run(api("POST", f"/flows/{f['id']}/run-now?mode=production")[1]["run_id"])
    if run["status"] != "SUCCESS":
        print("  (Tabularia's run has errors:", (run.get("error") or "")[:600], ")", flush=True)
    return f


def database_sources(folder_id, specs):
    """{name: datasource id} of database tables, imported and ready."""
    ids = {}
    for name, conn, ref in specs:
        st, d = api("POST", f"/projects/{folder_id}/datasources/database", {"name": name, "connection_id": conn, "source_type": "table", "source_ref": ref})
        assert st in (200, 201), (st, d)
        ids[name] = d["id"]
    for _ in range(90):
        ready = [x for x in api("GET", "/datasources")[1] if x["id"] in ids.values() and x.get("rows") and not x.get("refreshing")]
        if len(ready) == len(ids):
            break
        time.sleep(2)
    return ids


if __name__ == "__main__":
    only = sys.argv[sys.argv.index("--only") + 1] if "--only" in sys.argv else None
    prefix = sys.argv[sys.argv.index("--case") + 1] if "--case" in sys.argv else None
    pick = lambda items: [c for c in items if not prefix or c[0].startswith(prefix)]
    sign_in()
    # the sample connections (the first of their type): other oracles may add connections of their own
    conns = sorted(api("GET", "/connections")[1], key=lambda c: c["id"])
    pg = next(c for c in conns if c["db_type"] == "postgresql" and not c["name"].startswith("zz"))
    ch = next(c for c in conns if c["db_type"] == "clickhouse" and not c["name"].startswith("zz"))
    st, folder_obj = api("POST", "/projects", {"name": "zzc percorsi clickhouse"})
    folder_id = folder_obj["id"]
    try:
        r = sample_postgres(PG_SQL)
        assert r.returncode == 0, r.stderr[-400:]
        r = sample_clickhouse(CH_SQL)
        assert r.returncode == 0, r.stderr[-400:]
        variables = connection_passwords()
        why = reach_sources([(pg["host"], pg.get("port") or 5432)], owner=OWNER)
        ok("the engine's ClickHouse reaches the sample Postgres", why is None, why)
        ids = database_sources(folder_id, [("zzc dati", pg["id"], f"{PG_SCHEMA}.dati"), ("zzc destra", pg["id"], f"{PG_SCHEMA}.destra"),
                                           ("zzc tipi", ch["id"], CH_TABLE)])
        if only in (None, "pg") and pick(PG_CASES):
            print("\n== a Postgres table on the ClickHouse target", flush=True)
            f = make_flow("zzc pg", pick(PG_CASES), folder_id, {"casi": {"datasourceId": ids["zzc dati"]}, "destra": {"datasourceId": ids["zzc destra"]}})
            run_and_compare(f, folder_id, pick(PG_CASES), "clickhouse", "pg", variables, pg, "clickhouse")
        if only in (None, "ch") and pick(CH_CASES):
            print("\n== a table of the engine's ClickHouse, read as it is", flush=True)
            f = make_flow("zzc ch", pick(CH_CASES), folder_id, {"casi": {"datasourceId": ids["zzc tipi"]}, "destra": {"datasourceId": ids["zzc tipi"]}})
            for target in ("clickhouse", "native"):
                run_and_compare(f, folder_id, pick(CH_CASES), target, "ch", variables, pg, "clickhouse")
        if only in (None, "query") and pick(QUERY_CASES):
            print("\n== Postgres query datasources on the ClickHouse target", flush=True)
            qids = {}
            for name, sql in QUERIES.items():
                st, d = api("POST", f"/projects/{folder_id}/datasources/database", {"name": name, "connection_id": pg["id"], "source_type": "sql", "source_ref": sql})
                assert st in (200, 201), (st, d)
                qids[name] = d["id"]
            for _ in range(90):
                if all(x.get("rows") is not None and not x.get("refreshing") for x in api("GET", "/datasources")[1] if x["id"] in qids.values()):
                    break
                time.sleep(2)
            cases = pick(QUERY_CASES)
            f = make_flow("zzc query", cases, folder_id, {name: {"datasourceId": i} for name, i in qids.items()})
            run_and_compare(f, folder_id, cases, "clickhouse", "query", variables, pg, "clickhouse")
            for name, sql in REFUSED.items():
                st, d = api("POST", f"/projects/{folder_id}/datasources/database", {"name": name, "connection_id": pg["id"], "source_type": "sql", "source_ref": sql})
                assert st in (200, 201), (st, d)
                for _ in range(60):
                    if next(x for x in api("GET", "/datasources")[1] if x["id"] == d["id"]).get("rows") is not None:
                        break
                    time.sleep(2)
                cases = [("zzc r1 prima", [], {"source": "zzc q1 aritmetica"}), (f"zzc r{name[5]} rifiutata", [], {"source": name})]
                f = M.build(f"zzc query {name[4:]}", cases, folder_id, {"zzc q1 aritmetica": {"datasourceId": qids["zzc q1 aritmetica"]}, name: {"datasourceId": d["id"]}})
                st, body = api("POST", f"/flows/{f['id']}/export/dbt", {"target": "clickhouse", "schema": SCHEMA}, raw=True)
                ok(f"«{sql}» after a query it translates: the export refuses it (query_not_translatable)",
                   st == 422 and name in str(body) and "translate" in str(body), (st, body[:300]))
        if only in (None, "mix") and pick(MIX_CASES):
            print("\n== a Postgres table and a ClickHouse table in one flow", flush=True)
            f = make_flow("zzc mix", pick(MIX_CASES), folder_id, {"casi": {"datasourceId": ids["zzc dati"]}, "destra": {"datasourceId": ids["zzc tipi"]}})
            run_and_compare(f, folder_id, pick(MIX_CASES), "clickhouse", "mix", variables, pg, "clickhouse")
        if only in (None, "seed") and pick(SEED_CASES):
            print("\n== a file read as a seed", flush=True)
            bucket = next(x for x in api("GET", "/datasources")[1] if x["id"] == ids["zzc dati"])["bucket"]
            node = upload("zzc semi.csv", SEED_CSV, bucket)
            print("  Tabularia read the file as", [(c["name"], c["dtype"]) for c in node["columns"]], flush=True)
            f = make_flow("zzc seed", pick(SEED_CASES), folder_id, {"file": node, "destra": {"datasourceId": ids["zzc destra"]}})
            for target, warehouse in (("clickhouse", "clickhouse"), ("native", "postgres")):
                run_and_compare(f, folder_id, pick(SEED_CASES), target, "seed", variables, pg, warehouse)
            # the same file next to a table of the ClickHouse: the native target is dbt-clickhouse's seed loader
            cases = [(n.replace("zzc s", "zzc t", 1), ops, {**o, "right": [SEL("k", "cat")]} if "right" in o else o) for n, ops, o in pick(SEED_CASES)]
            f = make_flow("zzc seed ch", cases, folder_id, {"file": node, "destra": {"datasourceId": ids["zzc tipi"]}})
            run_and_compare(f, folder_id, cases, "native", "seedch", variables, pg, "clickhouse")
        if only in (None, "seed", "excel") and pick(EXCEL_CASES):
            print("\n== an Excel sheet read as a seed", flush=True)
            bucket = next(x for x in api("GET", "/datasources")[1] if x["id"] == ids["zzc dati"])["bucket"]
            node = upload("zzc foglio.xlsx", xlsx(excel_rows()), bucket, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
            print("  Tabularia read the sheet as", [(c["name"], c["dtype"]) for c in node["columns"]], flush=True)
            f = make_flow("zzc excel", pick(EXCEL_CASES), folder_id, {"file": node, "destra": {"datasourceId": ids["zzc destra"]}})
            for target, warehouse in (("clickhouse", "clickhouse"), ("native", "postgres")):
                run_and_compare(f, folder_id, pick(EXCEL_CASES), target, "excel", variables, pg, warehouse)
        if only in (None, "limit"):
            print("\n== a seed of SEED_MAX_ROWS rows, and one more", flush=True)
            bucket = next(x for x in api("GET", "/datasources")[1] if x["id"] == ids["zzc dati"])["bucket"]
            for n, fits in ((SEED_MAX_ROWS, True), (SEED_MAX_ROWS + 1, False)):
                data = ("id,testo,valore\n" + "".join(f"{i},t{i % 97},{i / 7}\n" for i in range(n))).encode()
                node = upload(f"zzc limite {n}.csv", data, bucket)
                cases = [(f"zzc l{n} seed", [], {"source": "file"}), (f"zzc l{n} tabella", [], {"source": "destra"})]
                f = make_flow(f"zzc limite {n}", cases, folder_id, {"file": node, "destra": {"datasourceId": ids["zzc destra"]}})
                targets = plan_targets(f)
                if not fits:
                    # the download refuses it (the engine counts the rows); the plan must say so before
                    st, body = api("POST", f"/flows/{f['id']}/export/dbt", {"target": "clickhouse", "schema": SCHEMA}, raw=True)
                    ok(f"{n} rows: the download refuses it (seed_too_large)", st == 422 and "50,000" in str(body), (st, body[:300]))
                    ok(f"{n} rows: the plan disables every target", all(not a for a, _ in targets.values()), targets)
                    continue
                ok(f"{n} rows: the plan allows the ClickHouse and native targets", targets.get("clickhouse", (0,))[0] and targets.get("native", (0,))[0], targets)
                for target, warehouse in (("clickhouse", "clickhouse"), ("native", "postgres")):
                    run_and_compare(f, folder_id, cases, target, f"limit{n}", variables, pg, warehouse)
    finally:
        if "--keep" not in sys.argv:
            for fl in api("GET", "/flows")[1]:
                if fl["name"].startswith("zzc"):
                    api("DELETE", f"/flows/{fl['id']}")
            for d in api("GET", "/datasources")[1]:
                if d["project_id"] == folder_id:
                    api("DELETE", f"/datasources/{d['id']}")
            api("DELETE", f"/projects/{folder_id}")
            clean_targets()
            sample_postgres(f"DROP SCHEMA IF EXISTS {PG_SCHEMA} CASCADE")
            sample_clickhouse(f"DROP TABLE IF EXISTS {CH_TABLE}")
            # the federated database of the schema zzc (dbt_export.federated_db_name: a plain lowercase schema keeps its name)
            clean_clickhouse(lambda n: n == f"tab_src_{pg['id']}_{PG_SCHEMA}")
            if KUBE:
                kube_cleanup(owner=OWNER)
    print(f"\n{sum(outcomes)}/{len(outcomes)} checks passed")
    sys.exit(0 if all(outcomes) else 1)
