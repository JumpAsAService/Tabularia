package main

import (
	"fmt"
	"net/http"
	"time"
)

// routes/sql_scripts.py run_sql and export_sql: the SQL of the editor run or exported, on the
// catalog (read by sqlglot in the AI service, the rest in Go) or on a connection. Every way
// resolve refuses, the aliases put in the text, the engine's refusals in the script's words.
func init() {
	ids := map[string]int64{}
	register(Suite{
		Module: "sql_scripts_run",
		Seed: func(w *World) {
			ids = map[string]int64{}
			w.Token("writer", "tab_sqlrun_writer_write_000", "write", time.Hour)
			w.Token("writer", "tab_sqlrun_writer_read_0000", "read", time.Hour)
			folder := func(key, name string, parent *int64) int64 {
				ids[key] = w.Insert(`INSERT INTO projects (name, description, parent_id, created_at) VALUES ($1, '', $2, '2026-01-01')
					RETURNING id`, name, parent)
				return ids[key]
			}
			a := folder("a", "sr-a", nil)
			folder("a2", "Nord", &a)
			b := folder("b", "sr-b", nil)
			folder("b2", "Nord", &b)
			folder("hidden", "sr-hidden", nil)
			folder("conn", "sr-conn", nil)
			grant := func(folder, capability, person string) {
				w.Exec(`INSERT INTO permissions (project_id, user_id, group_id, capability) VALUES ($1, $2, NULL, $3)`,
					ids[folder], w.People[person], capability)
			}
			for _, f := range []string{"a", "b"} {
				grant(f, "view", "writer")
			}
			grant("conn", "connect", "writer")
			grant("a", "view", "editor")
			ds := func(key, name, folder, dsKey string) {
				ids[key] = w.Insert(`INSERT INTO datasources (name, description, project_id, bucket, key, rows, columns, sort_keys,
					column_descriptions, kind, created_at, updated_at) VALUES ($1, '', $2, 'data-prep', $3, 1, '[]', '[]', '{}', 'flow',
					'2026-01-02', '2026-01-03') RETURNING id`, name, ids[folder], dsKey)
			}
			ds("vendite", "Vendite", "a", "datasets/v.parquet")
			ds("clienti", "Clienti", "a", "datasets/c.parquet")
			ds("citta", "Città Nord", "a", "datasets/n.parquet")
			ds("twin_a", "Doppio", "a", "datasets/d1.parquet")
			ds("twin_b", "Doppio", "b", "datasets/d2.parquet")
			ds("north_a", "Ordini", "a2", "datasets/o1.parquet")
			ds("north_b", "Ordini", "b2", "datasets/o2.parquet")
			ds("case_lower", "mixed", "a", "datasets/m1.parquet")
			ds("case_upper", "Mixed", "b", "datasets/m2.parquet")
			ds("empty", "Vuoto", "a", "")
			ds("secret", "Segreto", "hidden", "datasets/s.parquet")
			conn := func(key, name, folder, dbType string) {
				ids[key] = w.Insert(`INSERT INTO connections (name, description, project_id, db_type, host, username, password_encrypted,
					database, db_schema, extra, created_at, updated_at) VALUES ($1, '', $2, $3, 'db.test', 'u', '', 'd', '', '{}',
					'2026-01-01', '2026-01-01') RETURNING id`, name, ids[folder], dbType)
			}
			conn("pg", "pg-run", "conn", "postgresql")
			conn("mail", "mail-run", "conn", "smtp")
			conn("hidden_pg", "pg-hidden", "hidden", "postgresql")
			script := func(key, name, folder string) {
				ids[key] = w.Insert(`INSERT INTO sql_scripts (name, description, project_id, owner_id, current_version, created_at, updated_at)
					VALUES ($1, '', $2, $3, 1, '2026-02-01', '2026-02-01') RETURNING id`, name, ids[folder], w.People["admin"])
				w.Exec(`INSERT INTO sql_script_versions (script_id, version, content, note, created_at, created_by)
					VALUES ($1, 1, '{"sql": "SELECT 1"}', 'created', '2026-02-01', $2)`, ids[key], w.People["admin"])
			}
			script("seen", `Q3: "vendite"/nord.`, "a")
			script("unseen", "Nascosto", "hidden")
		},
		Engine: []EngineRule{
			{Method: "GET", Path: "^/engines$", Body: []any{map[string]any{"id": "polars", "available": true},
				map[string]any{"id": "duckdb", "available": true}, map[string]any{"id": "chdb", "available": false}}},
			{Method: "POST", Path: "^/tasks/preview$", Body: map[string]any{"columns": []any{map[string]any{"name": "a", "dtype": "Int64"}},
				"rows": []any{[]any{1}, []any{2.5}, []any{"è"}}, "truncated": true}},
			{Method: "POST", Path: "^/db/query$", Body: map[string]any{"columns": []any{}, "rows": []any{[]any{1}}}},
			{Method: "POST", Path: "^/(tasks|db)/exports$", Body: map[string]any{"task_id": "x-1", "status": "queued"}},
		},
		Steps: func(w *World) []Step {
			var s []Step
			as := w.As
			id := func(k string) int64 { return ids[k] }
			step := func(name, path string, auth func(*http.Request), body any, engine ...EngineRule) {
				st := Step{Name: name, Method: "POST", Path: path, Auth: auth, JSON: body, Mask: []string{"elapsed_ms"}}
				if engine != nil {
					st.Engine = append(engine, EngineRule{Method: "GET", Path: "^/engines$", Body: []any{
						map[string]any{"id": "polars", "available": true}, map[string]any{"id": "duckdb", "available": true}}})
				}
				s = append(s, st)
			}
			run := func(name string, auth func(*http.Request), body map[string]any, engine ...EngineRule) {
				step("run: "+name, "/sql-scripts/run", auth, body, engine...)
			}
			sql := func(text string) map[string]any { return map[string]any{"sql": text} }

			// on the catalog: what resolve finds, and how it writes it
			run("a datasource", as("writer"), sql("SELECT * FROM Vendite"))
			run("quoted, on duckdb, with a limit", as("writer"), map[string]any{"sql": `SELECT a FROM "Città Nord" WHERE a > 1;;`,
				"engine": "duckdb", "limit": 5})
			run("by folder and name", as("writer"), sql(`SELECT * FROM "sr-a".Doppio`))
			run("aliases, a join, a subquery", as("writer"), sql("SELECT v.a FROM vendite v JOIN Clienti AS c ON v.a = c.a "+
				"WHERE v.a IN (SELECT a FROM Vendite)"))
			run("a CTE is not a datasource", as("writer"), sql("WITH x AS (SELECT * FROM Vendite) SELECT * FROM x JOIN Clienti ON 1 = 1"))
			run("comments go", as("writer"), sql("SELECT /* Segreto */ * FROM Vendite -- and Segreto"))
			run("the same case wins", as("writer"), sql("SELECT * FROM Mixed"))
			run("on bigquery's dialect", as("writer"), map[string]any{"sql": "SELECT * FROM `Città Nord`", "engine": "polars"})
			run("in two folders", as("writer"), sql("SELECT * FROM Doppio"))
			// the folder the refusal suggests is the first of a list ordered by name only: between
			// two datasources of one name, the table's physical order, which the two databases
			// need not share (a Python bug, noted): the suggestion is not compared
			s[len(s)-1].Scrub = []string{`write it as "sr-[ab]"`}
			run("in two folders of one name", as("writer"), sql("SELECT * FROM Nord.Ordini"))
			run("in two folders, by case", as("writer"), sql("SELECT * FROM MIXED"))
			run("one it cannot read", as("writer"), sql("SELECT * FROM Segreto"))
			run("an admin can", as("admin"), sql("SELECT * FROM Segreto s"))
			run("one with no data", as("writer"), sql("SELECT * FROM Vuoto"))
			run("a catalog", as("writer"), sql("SELECT * FROM db.sr.Vendite"))
			run("two queries", as("writer"), sql("SELECT * FROM Vendite; SELECT 2"))
			run("not a query", as("writer"), sql("DELETE FROM Vendite"))
			run("unreadable", as("writer"), sql("SELECT FROM ((("))
			run("empty", as("writer"), sql(" -- nothing\n ; ;"))
			run("no datasource", as("writer"), sql("SELECT 1"))
			run("a table function", as("writer"), sql("SELECT * FROM read_parquet('x.parquet')"))
			run("an engine not allowed", as("writer"), map[string]any{"sql": "SELECT * FROM Vendite", "engine": "spark"})
			run("an engine not available", as("writer"), map[string]any{"sql": "SELECT * FROM Vendite", "engine": "chdb"})
			run("a saved script", as("writer"), map[string]any{"sql": "SELECT * FROM Vendite", "script_id": id("seen")})
			run("a script it cannot see", as("writer"), map[string]any{"sql": "SELECT * FROM Vendite", "script_id": id("unseen")})
			run("a script that is not there", as("writer"), map[string]any{"sql": "SELECT 1", "script_id": 999999})
			run("with a write token", Bearer("tab_sqlrun_writer_write_000"), sql("SELECT * FROM Vendite"))
			run("with a read token", Bearer("tab_sqlrun_writer_read_0000"), sql("SELECT * FROM Vendite"))
			run("without a session", nil, sql("SELECT * FROM Vendite"))
			run("an observer", as("obs_flag"), sql("SELECT * FROM Vendite"))
			run("no sql", as("writer"), map[string]any{})
			run("a limit out of range", as("writer"), map[string]any{"sql": "SELECT 1", "limit": 0})
			run("a long engine name", as("writer"), map[string]any{"sql": "SELECT 1", "engine": fmt.Sprintf("%050d", 1)})

			// the engine's answers, in the script's words
			alias := fmt.Sprintf("ds_%d", id("vendite"))
			run("the engine refuses, naming the alias", as("writer"), sql("SELECT * FROM Vendite"),
				EngineRule{Method: "POST", Path: "^/tasks/preview$", Status: 422, Body: map[string]any{"detail": "sql: no column x in " + alias +
					" (" + alias + "x, x" + alias + "): the query can read only the input of the node (`self` or `input`). " +
					"Try again: to combine other data use a Join or Union node."}})
			run("the engine stops it", as("writer"), sql("SELECT * FROM Vendite"),
				EngineRule{Method: "POST", Path: "^/tasks/preview$", Status: 504, Body: map[string]any{"detail": "x"}})
			run("the engine fails in text", as("writer"), sql("SELECT * FROM Vendite"),
				EngineRule{Method: "POST", Path: "^/tasks/preview$", Status: 500, Text: "boom", Type: "text/plain"})
			run("the engine is busy", as("writer"), sql("SELECT * FROM Vendite"),
				EngineRule{Method: "POST", Path: "^/tasks/preview$", Status: 429, Body: map[string]any{"detail": "Too many"}})
			run("the engine answers a list", as("writer"), sql("SELECT * FROM Vendite"),
				EngineRule{Method: "POST", Path: "^/tasks/preview$", Body: []any{1}})
			run("the engine answers no rows", as("writer"), sql("SELECT * FROM Vendite"),
				EngineRule{Method: "POST", Path: "^/tasks/preview$", Body: map[string]any{"rows": nil, "columns": nil}})

			// on a connection
			conn := func(name string, auth func(*http.Request), text string, connection int64, engine ...EngineRule) {
				run(name, auth, map[string]any{"sql": text, "connection_id": connection}, engine...)
			}
			conn("on a connection", as("writer"), "SELECT * FROM t", id("pg"))
			conn("on a connection, saved", as("writer"), "SELECT 1", id("pg"))
			conn("empty on a connection", as("writer"), "/* x */ ;;", id("pg"))
			conn("without CONNECT", as("editor"), "SELECT 1", id("pg"))
			conn("on a hidden connection", as("writer"), "SELECT 1", id("hidden_pg"))
			conn("on a mail connection", as("writer"), "SELECT 1", id("mail"))
			conn("on no connection", as("writer"), "SELECT 1", 999999)
			conn("the database refuses", as("writer"), "SELECT x", id("pg"),
				EngineRule{Method: "POST", Path: "^/db/query$", Status: 400, Body: map[string]any{"detail": "column x does not exist"}})

			// exports
			export := func(name string, auth func(*http.Request), body map[string]any, engine ...EngineRule) {
				step("export: "+name, "/sql-scripts/export", auth, body, engine...)
			}
			export("on the catalog", as("writer"), sql("SELECT * FROM Vendite v JOIN Clienti ON 1 = 1"))
			export("as xlsx, of a saved script", as("writer"), map[string]any{"sql": "SELECT * FROM Vendite", "format": "xlsx", "script_id": id("seen")})
			export("with a token, it waits its turn", Bearer("tab_sqlrun_writer_write_000"), sql("SELECT * FROM Vendite"))
			export("on a connection", as("writer"), map[string]any{"sql": "SELECT 1", "connection_id": id("pg")})
			export("empty on a connection", as("writer"), map[string]any{"sql": "--", "connection_id": id("pg")})
			export("one it cannot read", as("writer"), sql("SELECT * FROM Segreto"))
			export("a bad format", as("writer"), map[string]any{"sql": "SELECT 1", "format": "pdf"})
			export("the engine is full", as("writer"), sql("SELECT * FROM Vendite"),
				EngineRule{Method: "POST", Path: "^/tasks/exports$", Status: 429, Body: map[string]any{"detail": "Too many exports"}})
			export("the engine refuses it", as("writer"), sql("SELECT * FROM Vendite"),
				EngineRule{Method: "POST", Path: "^/tasks/exports$", Status: 422, Body: map[string]any{"detail": "no " + alias}})
			export("the engine answers no task", as("writer"), sql("SELECT * FROM Vendite"),
				EngineRule{Method: "POST", Path: "^/tasks/exports$", Body: map[string]any{"queued": true}})
			export("a script it cannot see", as("writer"), map[string]any{"sql": "SELECT 1", "script_id": id("unseen")})
			return s
		},
	})
}
