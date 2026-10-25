"""The dbt export matrix: EVERY operation of the editor (with its variants) on difficult data
(NULL, text with spaces, decimals, dates, booleans, column names with spaces, accents,
reserved words). A flow with one branch per case, run in Tabularia (production, ClickHouse
engine = the reference: it is the customers' one), exported to the targets, run with a REAL
dbt; every model compared with its result in Tabularia: COLUMNS IN ORDER,
the family of each column's type (integer, float, decimal, text, date, datetime, bool), and rows (as a
multiset, or in order where the order is the point). An empty text is not NULL, and a
text column is compared as text ('200' is not '200.0'). dbt runs in Europe/Rome (E2E_TZ):
the models must not depend on the session's zone.
Besides the main flow: chains after a compute or a sort, a text on several lines (every target) and after a pivot
(federated, also joined back on its index); a table with a
timestamptz, an unbounded numeric and empty texts; a file read as a seed with the texts dbt's
seed loaders change ('', 'null', blanks, \\N, CR LF, a leading ~).
The case tables (schema.sql) are in the sample Postgres; run_all.py creates them.
usage: python3 matrix.py [--engine clickhouse|duckdb|polars] [--only duckdb|native|clickhouse] [--case PREFIX] [--keep]"""
import json, math, os, re, subprocess, sys, time

from environment import CH_CONTAINER, CH_PORT, IMAGE, HERE, NETWORK
from common import (TZ_NAME, api, sign_in, outcomes, export, read_csv, ok, connection_passwords,
                    sample_postgres, clean_clickhouse)

CH_HOST, CH_PORT = CH_CONTAINER, CH_PORT
PG_CONN = {}   # the sample Postgres connection in Tabularia: host, database, user (to read dbt's tables)
PG_ID = None   # its id in Tabularia (the name of its password variable): set by main, or by lifecycle.py





# ── the cases ────────────────────────────────────────────────────────────────
F = lambda col, op, val=None: {"type": "filter", "params": {"column": col, "operator": op, "value": val}}
J = lambda how, **kw: {"type": "join", "params": {"how": how, **kw}}
CASES = [
    # columns
    ("c01 select", [{"type": "select", "params": {"columns": ["città", "select", "Nome Strano", "id"]}}]),
    ("c02 reorder", [{"type": "reorder", "params": {"columns": ["città", "id"]}}]),
    ("c03 drop", [{"type": "drop", "params": {"columns": ["testo", "quando", "select"]}}]),
    ("c04 rename", [{"type": "rename", "params": {"mapping": {"Nome Strano": "nome_strano", "select": "sel", "città": "Città"}}}]),
    # cast
    ("c05 cast testo int", [{"type": "cast", "params": {"columns": {"testo": "int"}}}]),
    ("c06 cast testo float", [{"type": "cast", "params": {"columns": {"testo": "float"}}}]),
    ("c07 cast numero int", [{"type": "cast", "params": {"columns": {"numero": "int"}}}]),
    ("c08 cast importo int", [{"type": "cast", "params": {"columns": {"importo": "int"}}}]),
    ("c09 cast intero str", [{"type": "cast", "params": {"columns": {"intero": "str", "flag": "str"}}}]),
    ("c10 cast quando date", [{"type": "cast", "params": {"columns": {"quando": "date"}}}]),
    ("c11 cast data datetime", [{"type": "cast", "params": {"columns": {"data": "datetime"}}}]),
    ("c12 cast intero float", [{"type": "cast", "params": {"columns": {"intero": "float", "importo": "float"}}}]),
    # filters
    ("c13 filtro eq", [F("categoria", "eq", "A")]),
    ("c14 filtro ne", [F("categoria", "ne", "A")]),
    ("c15 filtro gt", [F("numero", "gt", 0)]),
    ("c16 filtro ge", [F("intero", "ge", 3)]),
    ("c17 filtro lt data", [F("data", "lt", "2024-03-01")]),
    ("c18 filtro le importo", [F("importo", "le", 3.33)]),
    ("c19 filtro in", [F("categoria", "in", ["A", "C"])]),
    ("c20 filtro not in", [F("categoria", "not_in", ["A", "C"])]),
    ("c21 filtro between", [F("intero", "between", [2, 10])]),
    ("c22 filtro contains", [F("città", "contains", "il")]),
    ("c23 filtro starts with", [F("città", "starts_with", "M")]),
    ("c24 filtro ends with", [F("città", "ends_with", "a")]),
    ("c25 filtro is null", [F("categoria", "is_null")]),
    ("c26 filtro not null", [F("numero", "is_not_null")]),
    ("c27 filtro eq bool", [F("flag", "eq", True)]),
    # rows
    ("c28 sort asc", [{"type": "sort", "params": {"by": "numero"}}], {"order": ["numero"]}),
    ("c29 sort desc", [{"type": "sort", "params": {"by": "numero", "descending": True}}], {"order": ["numero"]}),
    ("c30 sort multi", [{"type": "sort", "params": {"by": ["categoria", "intero"], "descending": True}}], {"order": ["categoria", "intero"]}),
    ("c31 sort limit", [{"type": "sort", "params": {"by": "id"}}, {"type": "limit", "params": {"n": 3}}], {"order": ["id"]}),
    ("c32 unique tutto", [{"type": "select", "params": {"columns": ["categoria", "città"]}}, {"type": "unique", "params": {}}]),
    ("c33 unique subset", [{"type": "unique", "params": {"subset": ["categoria"]}}], {"compare_on": ["categoria"]}),
    ("c34 fill null", [{"type": "fill_null", "params": {"columns": {"categoria": "Z", "intero": 0, "numero": 0.5}}}]),
    ("c35 drop nulls subset", [{"type": "drop_nulls", "params": {"subset": ["categoria"]}}]),
    ("c36 drop nulls tutto", [{"type": "drop_nulls", "params": {}}]),
    # aggregations
    ("c37 group by", [{"type": "group_by", "params": {"by": ["categoria"], "aggregations": [
        {"column": "intero", "func": "sum", "alias": "somma"}, {"column": "numero", "func": "mean", "alias": "media"},
        {"column": "data", "func": "min", "alias": "prima"}, {"column": "quando", "func": "max", "alias": "ultima"},
        {"column": "intero", "func": "count", "alias": "conta"}, {"column": "numero", "func": "median", "alias": "mediana"},
        {"column": "città", "func": "n_unique", "alias": "citta_diverse"}]}}]),
    ("c38 group by std var", [{"type": "group_by", "params": {"by": ["flag"], "aggregations": [
        {"column": "numero", "func": "std", "alias": "dev"}, {"column": "numero", "func": "var", "alias": "varianza"}, {"column": "importo", "func": "sum", "alias": "tot"}]}}]),
    # compute
    ("c39 compute nuova", [{"type": "compute", "params": {"columns": [{"name": "doppio", "expr": "numero * 2"}]}}]),
    ("c40 compute sostituisce", [{"type": "compute", "params": {"columns": [{"name": "intero", "expr": "intero * 10"}]}}]),
    ("c41 compute case coalesce", [{"type": "compute", "params": {"columns": [
        {"name": "segno", "expr": "CASE WHEN numero > 0 THEN 'pos' WHEN numero < 0 THEN 'neg' ELSE 'zero' END"},
        {"name": "cat", "expr": "COALESCE(categoria, 'nessuna')"}]}}]),
    # Postgres has ROUND(x, n) only for a numeric, and its EXTRACT gives a numeric: a known difference
    # of the native target (the values are Tabularia's), allowed for these columns only
    ("c42 compute round", [{"type": "compute", "params": {"columns": [{"name": "terzo", "expr": "ROUND(numero / 3, 2)"}]}}],
     {"allowed_types": {"terzo": {"native": "decimal"}}}),
    ("c43 compute anno", [{"type": "compute", "params": {"columns": [{"name": "anno", "expr": "EXTRACT(YEAR FROM data)"}]}}],
     {"allowed_types": {"anno": {"native": "decimal"}}}),
    ("c44 compute testo", [{"type": "compute", "params": {"columns": [{"name": "etichetta", "expr": "UPPER(categoria) || '-' || \"città\""}]}}]),
    # join / union
    ("c45 join inner", [J("inner", on="categoria")], {"right": []}),
    ("c46 join left", [J("left", on="categoria")], {"right": []}),
    ("c47 join right", [J("right", on="categoria")], {"right": []}),
    ("c48 join full", [J("full", on="categoria")], {"right": []}),
    ("c49 join semi", [J("semi", on="categoria")], {"right": []}),
    ("c50 join anti", [J("anti", on="categoria")], {"right": []}),
    ("c51 join left on", [J("left", left_on="id", right_on="id")], {"right": []}),
    ("c52 join full on", [J("full", left_on="id", right_on="id")], {"right": []}),
    ("c53 join cross", [{"type": "select", "params": {"columns": ["id", "categoria"]}}, J("cross")], {"right": [{"type": "select", "params": {"columns": ["extra"]}}]}),
    ("c54 union by name", [{"type": "select", "params": {"columns": ["id", "categoria", "numero"]}}, {"type": "union", "params": {}}], {"right": []}),
    ("c55 union strict", [{"type": "select", "params": {"columns": ["id", "categoria"]}}, {"type": "union", "params": {"strategy": "strict"}}], {"right": [{"type": "select", "params": {"columns": ["id", "categoria"]}}]}),
    # shape
    ("c56 unpivot", [{"type": "unpivot", "params": {"index": ["id"], "on": ["numero", "importo"], "variable_name": "misura", "value_name": "valore"}}]),
    # sql
    ("c57 sql", [{"type": "sql", "params": {"query": "SELECT categoria, COUNT(*) AS n FROM self GROUP BY categoria"}}]),
    ("c58 sql con with", [{"type": "sql", "params": {"query": "WITH x AS (SELECT * FROM input WHERE numero > 0) SELECT id, numero FROM x"}}]),
    # names with capitals born halfway through the flow and used afterwards (sqlglot lost them)
    ("c59 colonna maiuscola", [{"type": "compute", "params": {"columns": [{"name": "Margine", "expr": "numero * 2"}]}}, F("Margine", "gt", 0),
                               {"type": "sort", "params": {"by": "Margine"}}], {"order": ["Margine"]}),
    ("c60 alias maiuscolo", [{"type": "group_by", "params": {"by": ["categoria"], "aggregations": [{"column": "numero", "func": "sum", "alias": "Totale"}]}},
                             {"type": "fill_null", "params": {"columns": {"Totale": 0}}}, {"type": "sort", "params": {"by": "Totale", "descending": True}}], {"order": ["Totale"]}),
    # a column BORN on the right (not from the source): on ClickHouse it is not Nullable, and without
    # join_use_nulls = 1 the missing side of an outer join would be '' instead of NULL
    ("c61 join left colonna calcolata a destra", [J("left", on="categoria")],
     {"right": [{"type": "compute", "params": {"columns": [{"name": "etichetta", "expr": "'fissa'"}]}}]}),
]
FEDERATED_ONLY = [
    ("p01 pivot", [{"type": "pivot", "params": {"index": ["flag"], "on": ["categoria"], "values": "intero", "func": "sum"}}]),
    ("p02 pivot due colonne", [{"type": "pivot", "params": {"index": ["id"], "on": ["categoria", "flag"], "values": "numero", "func": "max"}}]),
    # after a pivot the columns are the data's: a join keeps them all, a compute replaces or adds one (#0)
    ("p03 pivot poi join", [{"type": "pivot", "params": {"index": ["id"], "on": ["categoria"], "values": "numero", "func": "max"}}, J("left", on="id")],
     {"right": [{"type": "select", "params": {"columns": ["id", "peso", "extra"]}}]}),
    ("p04 pivot poi compute", [{"type": "pivot", "params": {"index": ["flag"], "on": ["categoria"], "values": "intero", "func": "sum"}},
                               {"type": "compute", "params": {"columns": [{"name": "tot", "expr": 'COALESCE("A", 0) + COALESCE("B", 0)'}]}}]),
    # joined back on its index with left_on/right_on (what the editor writes once a key pair differs): the
    # right index is `id_right`, as in Tabularia — the model used to stop on a duplicate name (#0)
    ("p05 pivot poi join left on", [{"type": "pivot", "params": {"index": ["id"], "on": ["categoria"], "values": "numero", "func": "max"}},
                                    J("left", left_on="id", right_on="id")],
     {"right": [{"type": "select", "params": {"columns": ["id", "peso", "extra"]}}]}),
]
# chains whose types the export used to guess (#3 #4 #5 #8 #14 #18): every target
LINES = "a\n  b\r\n\tc\rd  e"
CHAINS = [
    ("k01 compute poi cast int", [{"type": "compute", "params": {"columns": [{"name": "x", "expr": "numero * 1"}, {"name": "y", "expr": "testo || ''"}]}},
                                  {"type": "cast", "params": {"columns": {"x": "int", "y": "int"}}}]),
    ("k02 compute poi unpivot", [{"type": "compute", "params": {"columns": [{"name": "doppio", "expr": "numero * 2"}]}},
                                 {"type": "unpivot", "params": {"index": ["id"], "on": ["numero", "doppio"], "variable_name": "misura", "value_name": "valore"}}]),
    ("k03 sort poi first last", [{"type": "sort", "params": {"by": "id", "descending": True}},
                                 {"type": "group_by", "params": {"by": ["categoria"], "aggregations": [
                                     {"column": "select", "func": "first", "alias": "primo"}, {"column": "select", "func": "last", "alias": "ultimo"}]}}]),
    ("k04 group by booleani", [{"type": "group_by", "params": {"by": ["categoria"], "aggregations": [
        {"column": "flag", "func": "sum", "alias": "veri"}, {"column": "flag", "func": "min", "alias": "minimo"},
        {"column": "flag", "func": "max", "alias": "massimo"}]}}]),
    ("k05 cast float testo", [{"type": "cast", "params": {"columns": {"numero": "str"}}}]),
    ("k06 cast booleano intero", [{"type": "cast", "params": {"columns": {"flag": "int"}}}]),
    # the mean, deviation, variance and median of an INTEGER column are floats in every engine of
    # Tabularia; Postgres' AVG, STDDEV and VAR_SAMP of an integer give a numeric (#4, #85)
    ("k07 medie di interi", [{"type": "group_by", "params": {"by": ["flag"], "aggregations": [
        {"column": "intero", "func": "mean", "alias": "media"}, {"column": "intero", "func": "std", "alias": "dev"},
        {"column": "intero", "func": "var", "alias": "varianza"}, {"column": "intero", "func": "median", "alias": "mediana"}]}}]),
    # a text on several lines (LF, CRLF, a lone CR, a tab, inner spaces) in a compute, a filter value, a
    # fill_null value, a sql node and a column name keeps its bytes: the model used to indent inside the
    # literal, and Jinja turned every CR into LF
    ("k08 testo su piu righe", [{"type": "compute", "params": {"columns": [{"name": "m", "expr": "'" + LINES.replace("'", "''") + "'"}]}},
                                F("m", "eq", LINES), {"type": "fill_null", "params": {"columns": {"categoria": LINES}}},
                                {"type": "sql", "params": {"query": "SELECT *, '" + LINES.replace("'", "''") + "' AS n FROM self"}},
                                {"type": "rename", "params": {"mapping": {"n": "n\n  2"}}}]),
]
# zzdbt.tipi: a timestamptz (#1, read in UTC whatever the session's zone), an unbounded numeric
# up to 1e20 (#25 on ClickHouse), and '', NULL and 'null' in one text column
TYPES = [
    ("t01 tipi", [], {"source": "tipi"}),
    ("t02 istante a data", [{"type": "cast", "params": {"columns": {"istante": "date"}}}], {"source": "tipi"}),
    ("t03 testo vuoto", [F("testo", "eq", "")], {"source": "tipi"}),
    ("t04 testo nullo", [F("testo", "is_null")], {"source": "tipi"}),
    ("t05 testo riempito", [{"type": "fill_null", "params": {"columns": {"testo": "Z"}}}], {"source": "tipi"}),
    ("t06 istante al microsecondo", [F("preciso", "eq", "2024-01-05 10:30:00.123456")], {"source": "tipi"}),
    ("t07 date prima del 1970", [{"type": "sort", "params": {"by": "nato"}}], {"source": "tipi", "order": ["nato"]}),
]
# a file (→ seed) with the texts dbt's seed loaders change: they must reach the models exactly (#36)
TEXTS = [{"id": 1, "t": ""}, {"id": 2, "t": None}, {"id": 3, "t": "null"}, {"id": 4, "t": "NULL"}, {"id": 5, "t": "  "},
         {"id": 6, "t": " a "}, {"id": 7, "t": "\\N"}, {"id": 8, "t": "a\r\nb"}, {"id": 9, "t": "~x"}, {"id": 10, "t": "x~0"},
         {"id": 11, "t": "\ttab"}, {"id": 12, "t": "ok"}]
SEEDS = [
    ("s01 seed", [], {"source": "testi"}),
    ("s02 seed nulli", [F("t", "is_null")], {"source": "testi"}),
    ("s03 seed riempito", [{"type": "fill_null", "params": {"columns": {"t": "Z"}}}], {"source": "testi"}),
    ("s04 seed vuoto", [F("t", "eq", "")], {"source": "testi"}),
    # a database source in the flow: the native target needs one
    ("s05 seed join", [J("left", on="id")], {"source": "testi", "right": [{"type": "select", "params": {"columns": ["id", "extra"]}}]}),
]

# ── comparison ───────────────────────────────────────────────────────────────
def family(dtype):
    """The family of a type, whoever wrote it (Polars, DuckDB, Postgres, ClickHouse): integer,
    float, decimal, text, date, datetime, time, bool. None: no type to compare (unknown, or an
    all-NULL column). Integers, floats and decimals are three families: an Int64 that comes back
    as a float, or a sum of integers as a DOUBLE, is a difference even where the values agree
    (#4, #85). The width of an integer is not compared (Int32, BIGINT, UInt64), except a 128-bit
    one (HUGEINT): the engines bring a sum of integers back to 64 bits, and so must the export. A
    Postgres `numeric` is a Float64 in Tabularia and the first step of every model casts it to a
    double, so a numeric column of a source is a float on both sides."""
    t = str(dtype or "").strip().lower()
    while True:
        m = re.match(r"^(nullable|lowcardinality)\((.*)\)$", t)
        if not m:
            break
        t = m.group(2)
    if not t or t == "null":
        return None
    if "bool" in t:
        return "bool"
    if "timestamp" in t or "datetime" in t:
        return "datetime"
    if t.startswith("date"):
        return "date"
    if t.startswith("time") or t.startswith("interval"):
        return "time"
    if re.search(r"decimal|numeric", t):
        return "decimal"
    if re.search(r"float|double|real", t):
        return "float"
    if re.search(r"char|string|text|utf8|categorical|enum", t):
        return "text"
    if re.search(r"hugeint|int128|int256", t):
        return "integer128"
    if re.search(r"int", t):     # Int8…Int64, UInt*, integer, bigint, smallint, tinyint, ubigint
        return "integer"
    return t


def norm(v, fam=None):
    if v is None:
        return None
    if fam == "text":
        return str(v)   # a text as it is: '200' is not '200.0', and '' is not NULL
    if v == "":
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        x = float(v)
    else:
        s = str(v)
        if s.lower() in ("true", "false"):
            return s.lower()
        x = None
        if s == s.strip():
            try:
                x = float(s)
            except ValueError:
                x = None
        if x is None:
            # an instant as Polars' CSV and the drivers write it: 'T' or space, a fraction without its trailing
            # zeros, a UTC offset dropped — any other offset stays, so a shifted zone shows
            m = re.match(r"^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}:\d{2})(\.\d+)?([+-]\d{2}:?\d{2}|Z)?$", s)
            if m:
                fraction = (m.group(3) or "").rstrip("0").rstrip(".")
                zone = "" if m.group(4) in (None, "Z") or re.fullmatch(r"[+-]00:?00", m.group(4)) else m.group(4)
                return f"{m.group(1)} {m.group(2)}{fraction}{zone}"
            return s
    if math.isnan(x):
        return "nan"
    return int(x) if x == int(x) and abs(x) < 1e15 else round(x, 6)


def rows_of(cols, rows, fam=None):
    fam = fam or {}
    return [tuple(norm(v, fam.get(c)) for c, v in zip(cols, r)) for r in rows]


def compare(name, opts, tab, dbtres, target=None):
    """tab: (columns, rows) or (columns, rows, {column: Tabularia's dtype}); dbtres: {cols, rows, types?}.
    opts["allowed_types"]: {column: {target: family}}, a known type difference allowed on that target."""
    cols_t, rows_t = tab[0], tab[1]
    cols_d, rows_d = dbtres["cols"], dbtres["rows"]
    if cols_t != cols_d:
        return False, f"different columns:\n      tabularia {cols_t}\n      dbt       {cols_d}"
    fam = {}
    if len(tab) > 2 and dbtres.get("types"):
        fam = {c: family(tab[2].get(c)) for c in cols_t}
        allowed = opts.get("allowed_types", {})
        different = [f"{c}: tabularia {tab[2].get(c)} dbt {dbtres['types'].get(c)}" for c in cols_t
                     if fam[c] and family(dbtres["types"].get(c)) and fam[c] != family(dbtres["types"].get(c))
                     and allowed.get(c, {}).get(target) != family(dbtres["types"].get(c))]
        if different:
            return False, "different types:\n      " + "\n      ".join(different)
    rt, rd = rows_of(cols_t, rows_t, fam), rows_of(cols_d, rows_d, fam)
    if "compare_on" in opts:
        idx = [cols_t.index(c) for c in opts["compare_on"]]
        rt, rd = [tuple(r[i] for i in idx) for r in rt], [tuple(r[i] for i in idx) for r in rd]
    key = lambda r: tuple((x is None, str(x)) for x in r)
    if sorted(rt, key=key) != sorted(rd, key=key):
        only_t = [r for r in rt if r not in rd][:3]; only_d = [r for r in rd if r not in rt][:3]
        return False, f"different rows ({len(rt)} vs {len(rd)}):\n      only tabularia {only_t}\n      only dbt       {only_d}"
    if "order" in opts:
        idx = [cols_t.index(c) for c in opts["order"]]
        kt, kd = [tuple(r[i] for i in idx) for r in rt], [tuple(r[i] for i in idx) for r in rd]
        if kt != kd:
            return False, f"different order:\n      tabularia {kt}\n      dbt       {kd}"
    return True, ""


# ── preparation in Tabularia ─────────────────────────────────────────────────
def slug(n):
    return re.sub(r"[^a-z0-9_]", "_", n.lower()).strip("_")


def build(flow_name, cases, folder, sources):
    """sources: {name: the source node's data}; a case reads «casi» unless its options say
    `source`, and the right side of a join/union reads «destra»."""
    used = {"destra"} | {(c[2] if len(c) > 2 else {}).get("source", "casi") for c in cases}
    nodes = [{"id": f"s_{k}", "type": "source", "data": v} for k, v in sources.items() if k in used]
    edges = []
    for i, case in enumerate(cases):
        name, ops, opts = case[0], case[1], (case[2] if len(case) > 2 else {})
        prev = f"s_{opts.get('source', 'casi')}"
        for k, op in enumerate(ops):
            nid = f"n{i}_{k}"
            nodes.append({"id": nid, "type": "operation", "data": {"opType": op["type"], "params": op["params"]}})
            edges.append({"source": prev, "target": nid, "targetHandle": "left"})
            if op["type"] in ("join", "union"):
                rprev = "s_destra"
                for h, rop in enumerate(opts.get("right", [])):
                    rid = f"d{i}_{h}"
                    nodes.append({"id": rid, "type": "operation", "data": {"opType": rop["type"], "params": rop["params"]}})
                    edges.append({"source": rprev, "target": rid, "targetHandle": "left"})
                    rprev = rid
                edges.append({"source": rprev, "target": nid, "targetHandle": "right"})
            prev = nid
        nodes.append({"id": f"o{i}", "type": "output", "data": {"destType": "datasource", "name": name, "projectId": folder, "overwrite": True}})
        edges.append({"source": prev, "target": f"o{i}", "targetHandle": "left"})
    st, f = api("POST", f"/projects/{folder}/flows", {"name": flow_name, "definition": json.dumps({"nodes": nodes, "edges": edges}), "engine": ENGINE})
    assert st in (200, 201), (st, f)
    return f


def wait_run(rid, seconds=900):
    end = time.time() + seconds
    while time.time() < end:
        st, r = api("GET", f"/runs/{rid}")
        if st == 200 and r["status"] in ("SUCCESS", "FAILURE"):
            return r
        time.sleep(3)
    return r


def tabularia_result(folder, name):
    """(columns, rows, {column: dtype}) of what Tabularia published: the CSV keeps '' apart from NULL."""
    d = next((x for x in api("GET", "/datasources")[1] if x["name"] == name and x["project_id"] == folder), None)
    if d is None:
        return None
    st, b = api("POST", "/tasks/export", {"bucket": d["bucket"], "input_key": d["key"], "operations": [], "format": "csv", "filename": "x.csv"}, raw=True)
    r = read_csv(b.decode())
    return r[0], r[1:], {c.get("name"): c.get("dtype") for c in d.get("columns") or []}


def dbt(folder, *arg, variables=None):
    cmd = ["docker", "run", "--rm", "--network", NETWORK, "--user", f"{os.getuid()}:{os.getgid()}", "-e", "HOME=/tmp",
           "-v", f"{folder}:/project", "-v", f"{HERE}/dump.py:/dump.py:ro", "-w", "/project", "-e", "DBT_PROFILES_DIR=/project",
           "-e", f"TZ={TZ_NAME}", "-e", f"PGTZ={TZ_NAME}"]
    for k, v in (variables or {}).items():
        cmd += ["-e", f"{k}={v}"]
    r = subprocess.run(cmd + [IMAGE, *arg], capture_output=True, text=True)
    return r.returncode, r.stdout + r.stderr


def read_dbt(folder, target, variables):
    if target == "duckdb":
        f = next(n for n in os.listdir(folder) if n.endswith(".duckdb"))
        rc, out = dbt(folder, "python", "/dump.py", "duckdb", f"/project/{f}", variables=variables)
    elif target == "clickhouse":
        rc, out = dbt(folder, "python", "/dump.py", "clickhouse", CH_HOST, CH_PORT, "dbt_tabularia", variables=variables)
    else:
        pw = variables[f"TABULARIA_DB_{PG_ID}_PASSWORD"]
        rc, out = dbt(folder, "python", "/dump.py", "postgres", PG_CONN["host"], PG_CONN["database"], PG_CONN["username"], "dbt_tabularia",
                      variables={**variables, "PGPASSWORD": pw})
    if "===DUMP===" not in out:
        raise RuntimeError(out[-1500:])
    return json.loads(out.split("===DUMP===", 1)[1])


ENGINE = sys.argv[sys.argv.index("--engine") + 1] if "--engine" in sys.argv else "clickhouse"   # the customers' engine is the reference




def prepare_clickhouse(dbt_folder, variables):
    """The step the data team does once: the federated databases towards the sources."""
    return dbt(dbt_folder, "dbt", "run-operation", "create_tabularia_sources", "--log-level-file", "none", "--no-use-colors", variables=variables)


def upload_texts(bucket):
    """The TEXTS file uploaded as from the editor (NDJSON keeps '' apart from NULL): its file node."""
    body = "".join(json.dumps(r) + "\n" for r in TEXTS).encode()
    st, up = api("POST", "/files", multipart={"file": ("testi.ndjson", body, "application/x-ndjson")})
    assert st in (200, 201) and up.get("parquet_key"), (st, up)
    return {"parquetKey": up["parquet_key"], "bucket": bucket, "filename": "testi.ndjson",
            "columns": [{"name": "id", "dtype": "Int64"}, {"name": "t", "dtype": "String"}]}


if __name__ == "__main__":
    only = sys.argv[sys.argv.index("--only") + 1] if "--only" in sys.argv else None
    prefix = sys.argv[sys.argv.index("--case") + 1] if "--case" in sys.argv else None
    sign_in()
    pg = next(c for c in api("GET", "/connections")[1] if c["db_type"] == "postgresql")
    PG_ID = pg["id"]
    PG_CONN.update(host=pg["host"], database=pg["database"], username=pg["username"])
    st, folder_obj = api("POST", "/projects", {"name": "zzdbt matrice"})
    folder = folder_obj["id"]
    created_ds = []
    try:
        ids = {}
        for name, ref in (("zzdbt casi", "zzdbt.casi"), ("zzdbt destra", "zzdbt.destra"), ("zzdbt tipi", "zzdbt.tipi")):
            st, d = api("POST", f"/projects/{folder}/datasources/database", {"name": name, "connection_id": pg["id"], "source_type": "table", "source_ref": ref})
            assert st in (200, 201), (st, d)
            ids[name] = d["id"]; created_ds.append(d["id"])
        for _ in range(90):
            ready = [x for x in api("GET", "/datasources")[1] if x["id"] in created_ds and x.get("rows") and not x.get("refreshing")]
            if len(ready) == len(created_ds):
                break
            time.sleep(2)
        sources = {"casi": {"datasourceId": ids["zzdbt casi"]}, "destra": {"datasourceId": ids["zzdbt destra"]},
                   "tipi": {"datasourceId": ids["zzdbt tipi"]}}
        pick = lambda items: [c for c in items if not prefix or c[0].startswith(prefix)]
        # (flow, cases, targets, required): a flow that is not required may be unavailable on a target
        # according to its export plan, and then it is skipped with the reason
        flows = [("zzdbt matrice", pick(CASES), ("duckdb", "native", "clickhouse"), True),
                 ("zzdbt pivot", pick(FEDERATED_ONLY), ("duckdb",), True),
                 ("zzdbt catene", pick(CHAINS), ("duckdb", "native", "clickhouse"), True),
                 ("zzdbt tipi", pick(TYPES), ("duckdb", "native", "clickhouse"), True),
                 ("zzdbt seed", pick(SEEDS), ("duckdb", "native", "clickhouse"), False)]
        flows = [f for f in flows if f[1]]
        if any(name == "zzdbt seed" for name, *_ in flows):
            bucket = next(x for x in api("GET", "/datasources")[1] if x["id"] == ids["zzdbt casi"])["bucket"]
            sources["testi"] = upload_texts(bucket)
        variables = connection_passwords()
        for flow_name, cs, targets, required in flows:
            print(f"\n== flow «{flow_name}»: {len(cs)} cases", flush=True)
            f = build(flow_name, cs, folder, sources)
            run = wait_run(api("POST", f"/flows/{f['id']}/run-now?mode=production")[1]["run_id"])
            if run["status"] != "SUCCESS":
                print("  (Tabularia's run has errors:", (run.get("error") or "")[:400], ")")
            tab = {c[0]: tabularia_result(folder, c[0]) for c in cs}
            plan = api("GET", f"/flows/{f['id']}/export/dbt/plan")[1]
            for t in targets:
                if only and t != only:
                    continue
                print(f"-- target {t}")
                available = next((x for x in plan.get("targets", []) if x["id"] == t), {})
                if not required and not available.get("available"):
                    print(f"  · {t}: not available according to the plan — {(available.get('reason') or '')[:160]}")
                    continue
                sample_postgres("DROP SCHEMA IF EXISTS dbt_tabularia CASCADE")
                dbt_folder, names = export(f, t, suffix="_matrice")
                if not ok(f"{flow_name} · {t}: export", dbt_folder is not None, names):
                    continue
                if t == "clickhouse":
                    clean_clickhouse()
                    rc, out = prepare_clickhouse(dbt_folder, variables)
                    if not ok(f"{flow_name} · {t}: dbt run-operation create_tabularia_sources", rc == 0, out[-1200:]):
                        continue
                if any(n.startswith("seeds/") and n.endswith(".csv") for n in names):
                    rc, out = dbt(dbt_folder, "dbt", "seed", "--no-use-colors", variables=variables)
                    if not ok(f"{flow_name} · {t}: dbt seed", rc == 0, out[-1200:]):
                        continue
                rc, out = dbt(dbt_folder, "dbt", "run", "--no-use-colors", variables=variables)
                # dbt-clickhouse writes the names between backticks: `db`.`model`
                failed = {re.sub(r"^.*\.", "", m).strip("`") for m in re.findall(r"ERROR creating sql table model (\S+)", out)}
                # a branch Tabularia itself refuses MUST fail in dbt too: it is parity, not a defect
                expected = {slug(c[0]) for c in cs if tab[c[0]] is None}
                ok(f"{flow_name} · {t}: dbt run (only the models Tabularia refuses fail)", failed == expected,
                   f"failed in dbt {sorted(failed)}, refused by Tabularia {sorted(expected)}\n" + "\n".join(l for l in out.splitlines() if "Error" in l)[:1200])
                try:
                    produced = read_dbt(dbt_folder, t, variables)
                except Exception as e:
                    ok(f"{flow_name} · {t}: reading dbt's tables", False, e); continue
                for c in cs:
                    name, opts = c[0], (c[2] if len(c) > 2 else {})
                    # a model that failed in the dbt run is absent, even if dbt-clickhouse left its table empty
                    m = produced.get(slug(name)) if slug(name) not in failed else None
                    if tab[name] is None:
                        ok(f"{t} · {name}: refused by Tabularia AND by dbt (parity)", m is None, "Tabularia refuses it but dbt computes it"); continue
                    if m is None:
                        ok(f"{t} · {name}", False, "dbt model absent or failed"); continue
                    equal, why = compare(name, opts, tab[name], m, t)
                    ok(f"{t} · {name}", equal, why)
    finally:
        if "--keep" not in sys.argv:
            for fl in api("GET", "/flows")[1]:
                if fl["name"].startswith("zzdbt"):
                    api("DELETE", f"/flows/{fl['id']}")
            for d in api("GET", "/datasources")[1]:
                if d["project_id"] == folder:
                    api("DELETE", f"/datasources/{d['id']}")
            api("DELETE", f"/projects/{folder}")
            sample_postgres("DROP SCHEMA IF EXISTS dbt_tabularia CASCADE")
            clean_clickhouse()
    print(f"\n{sum(outcomes)}/{len(outcomes)} checks passed")
    sys.exit(0 if all(outcomes) else 1)
