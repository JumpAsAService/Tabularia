"""Gira DENTRO l'immagine dbt: legge tutte le tabelle prodotte da dbt e le stampa in JSON.
uso: python dump.py duckdb <file.duckdb> | python dump.py postgres <host> <db> <utente> <schema> (password in PGPASSWORD)
   | python dump.py clickhouse <host> <porta> <database> (utente/password in CLICKHOUSE_USER/CLICKHOUSE_PASSWORD)"""
import json, os, sys
out = {}
if sys.argv[1] == "duckdb":
    import duckdb
    con = duckdb.connect(sys.argv[2], read_only=True)
    for (t,) in con.execute("SELECT table_name FROM information_schema.tables WHERE table_schema = 'main' AND table_type = 'BASE TABLE'").fetchall():
        cur = con.execute(f'SELECT * FROM main."{t}"')
        out[t] = {"cols": [d[0] for d in cur.description], "rows": cur.fetchall()}
elif sys.argv[1] == "clickhouse":
    import clickhouse_connect
    cl = clickhouse_connect.get_client(host=sys.argv[2], port=int(sys.argv[3]), username=os.environ["CLICKHOUSE_USER"], password=os.environ["CLICKHOUSE_PASSWORD"])
    for (t,) in cl.query("SELECT name FROM system.tables WHERE database = {d:String} AND engine NOT IN ('View', 'MaterializedView')", parameters={"d": sys.argv[4]}).result_rows:
        r = cl.query(f"SELECT * FROM `{sys.argv[4]}`.`{t}`")
        out[t] = {"cols": list(r.column_names), "rows": [list(x) for x in r.result_rows]}
else:
    import psycopg2
    con = psycopg2.connect(host=sys.argv[2], dbname=sys.argv[3], user=sys.argv[4], password=os.environ["PGPASSWORD"])
    cur = con.cursor()
    cur.execute("SELECT table_name FROM information_schema.tables WHERE table_schema = %s AND table_type = 'BASE TABLE'", (sys.argv[5],))
    for (t,) in cur.fetchall():
        c2 = con.cursor(); c2.execute(f'SELECT * FROM "{sys.argv[5]}"."{t}"')
        out[t] = {"cols": [d[0] for d in c2.description], "rows": c2.fetchall()}
print("===DUMP===" + json.dumps(out, default=str))
