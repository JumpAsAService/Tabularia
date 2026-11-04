package main

import (
	"fmt"
	"time"
)

// routes/sql_scripts.py refresh_sql_script: a SQL script's result written into its
// datasource (services/sql_script_runs.py launch), on the catalog or on a connection.
func init() {
	ids := map[string]int64{}
	register(Suite{
		Module: "sql_scripts_refresh",
		Seed: func(w *World) {
			maskPattern("runs", "output_key", snapshotStamp)
			w.Token("writer", "tab_ssr_writer_write_000000", "write", time.Hour)
			w.Token("writer", "tab_ssr_writer_read_0000000", "read", time.Hour)
			folder := func(key string) {
				ids[key] = w.Insert(`INSERT INTO projects (name, description, parent_id, created_at) VALUES ($1, '', NULL, '2026-01-01')
					RETURNING id`, "ssr-"+key)
			}
			for _, f := range []string{"scripts", "out", "src", "conn", "viewonly", "hidden"} {
				folder(f)
			}
			grant := func(f, capability, person string) {
				w.Exec(`INSERT INTO permissions (project_id, user_id, group_id, capability) VALUES ($1, $2, NULL, $3)`,
					ids[f], w.People[person], capability)
			}
			grant("scripts", "run", "writer")
			grant("scripts", "view", "writer")
			grant("out", "edit", "writer")
			grant("src", "view", "writer")
			grant("conn", "connect", "writer")
			grant("viewonly", "view", "writer")
			grant("scripts", "view", "editor")
			ds := func(key, name, folder, kind, desc string) {
				ids[key] = w.Insert(`INSERT INTO datasources (name, description, project_id, bucket, key, rows, columns, sort_keys,
					column_descriptions, kind, created_at, updated_at) VALUES ($1, $5, $2, 'data-prep', $3, 1, '[]', '[]', '{}', $4,
					'2026-01-02', '2026-01-03') RETURNING id`, name, ids[folder], "datasets/"+key+".parquet", kind, desc)
			}
			ds("vendite", "Vendite", "src", "flow", "")
			ds("clienti", "Clienti", "src", "flow", "")
			ds("written", "Scritto", "out", "flow", "a description kept")
			ds("dbsnap", "Snapshot", "out", "database", "")
			w.Exec(`INSERT INTO data_contracts (datasource_id, version, document, enabled, status, errors, warnings, created_by, updated_by,
				created_at, updated_at) VALUES ($1, 5, '{"rules": [{"id": "r"}]}', true, 'passed', 0, 0, NULL, NULL, '2026-02-01', '2026-02-01')`,
				ids["written"])
			conn := func(key, name, folder, dbType string) {
				ids[key] = w.Insert(`INSERT INTO connections (name, description, project_id, db_type, host, username, password_encrypted,
					database, db_schema, extra, created_at, updated_at) VALUES ($1, '', $2, $3, 'db.test', 'u', $4, 'd', '', '{"tls": "trust"}',
					'2026-01-01', '2026-01-01') RETURNING id`, name, ids[folder], dbType, seal("pw"))
			}
			conn("pg", "ssr-pg", "conn", "postgresql")
			conn("mail", "ssr-mail", "conn", "smtp")
			conn("hidden_pg", "ssr-pg-hidden", "hidden", "postgresql")
			script := func(key, folder, content string) {
				ids[key] = w.Insert(`INSERT INTO sql_scripts (name, description, project_id, owner_id, current_version, created_at, updated_at)
					VALUES ($1, '', $2, $3, 2, '2026-02-01', '2026-02-01') RETURNING id`, "ssr-"+key, ids[folder], w.People["admin"])
				w.Exec(`INSERT INTO sql_script_versions (script_id, version, content, note, created_at, created_by)
					VALUES ($1, 1, '{"sql": "SELECT old"}', 'created', '2026-02-01', $2)`, ids[key], w.People["admin"])
				w.Exec(`INSERT INTO sql_script_versions (script_id, version, content, note, created_at, created_by)
					VALUES ($1, 2, $2, 'edited', '2026-02-02', $3)`, ids[key], content, w.People["admin"])
			}
			out := func(name, folder string) string {
				return fmt.Sprintf(`"output": {"name": %q, "project_id": %d}`, name, ids[folder])
			}
			script("catalog", "scripts", `{"sql": "SELECT * FROM Vendite v JOIN Clienti c ON v.a = c.a", `+out("Nuovo", "out")+`}`)
			script("again", "scripts", `{"sql": "SELECT * FROM Vendite", "engine": "duckdb", `+out("Scritto", "out")+`}`)
			script("conn", "scripts", `{"sql": "select 1;", "connection_id": `+fmt.Sprint(ids["pg"])+`, `+out("Da DB", "out")+`}`)
			script("mailconn", "scripts", `{"sql": "select 1", "connection_id": `+fmt.Sprint(ids["mail"])+`, `+out("X", "out")+`}`)
			script("hiddenconn", "scripts", `{"sql": "select 1", "connection_id": `+fmt.Sprint(ids["hidden_pg"])+`, `+out("Y", "out")+`}`)
			script("nooutput", "scripts", `{"sql": "SELECT * FROM Vendite"}`)
			script("nosql", "scripts", `{"sql": " -- nothing\n ; ", `+out("Z", "out")+`}`)
			script("snapshot", "scripts", `{"sql": "SELECT * FROM Vendite", `+out("Snapshot", "out")+`}`)
			script("noedit", "scripts", `{"sql": "SELECT * FROM Vendite", `+out("W", "viewonly")+`}`)
			script("gone", "scripts", `{"sql": "SELECT * FROM Vendite", "output": {"name": "G", "project_id": 999999}}`)
			script("noread", "scripts", `{"sql": "SELECT * FROM Segreto", `+out("V", "out")+`}`)
			script("badsql", "scripts", `{"sql": "SELEC FROM (", `+out("B", "out")+`}`)
			script("hidden", "hidden", `{"sql": "SELECT 1", `+out("H", "out")+`}`)
			script("live", "scripts", `{"sql": "SELECT * FROM Clienti", `+out("Vivo", "out")+`}`)
			w.Exec(`INSERT INTO runs (kind, sql_script_id, task_id, status, launched_by, trigger_type, input_key, output_bucket, output_key,
				publish_description, publish_overwrite, publish_sort_keys, started_at) VALUES ('sql', $1, 't-live', 'STARTED', NULL, 'manual', '',
				'data-prep', 'datasets/x.parquet', '', true, '[]', '2026-02-03')`, ids["live"])
		},
		Engine: []EngineRule{
			{Method: "GET", Path: "^/engines$", Body: []any{map[string]any{"id": "polars", "available": true},
				map[string]any{"id": "duckdb", "available": true}, map[string]any{"id": "chdb", "available": false}}},
			{Method: "POST", Path: "^/(tasks/transform-data|db/ingest)$", Body: map[string]any{"task_id": "t-ssr"}},
		},
		Steps: func(w *World) []Step {
			var s []Step
			masked := []string{"*.body.output_key"}
			path := func(k string) string { return fmt.Sprintf("/sql-scripts/%d/refresh", ids[k]) }
			add := func(person, name, k string, rules ...EngineRule) {
				st := Step{Name: name + " · " + person, Method: "POST", Path: path(k), Auth: w.As(person), EngineMask: masked}
				if len(rules) > 0 {
					st.Engine = rules
				}
				s = append(s, st)
			}
			fails := []EngineRule{
				{Method: "POST", Path: "^/(tasks/transform-data|db/ingest)$", Status: 400, Body: map[string]any{"detail": "column a: bad"}},
				{Method: "POST", Path: "^/(tasks/transform-data|db/ingest)$", Status: 504, Text: "slow"},
				{Method: "POST", Path: "^/(tasks/transform-data|db/ingest)$", Status: 503, Body: map[string]any{"detail": "down"}},
				{Method: "POST", Path: "^/(tasks/transform-data|db/ingest)$", Status: 429, Body: map[string]any{"detail": "busy"}},
				{Method: "POST", Path: "^/(tasks/transform-data|db/ingest)$", Status: 400, Body: []any{1}},
				{Method: "POST", Path: "^/(tasks/transform-data|db/ingest)$", Body: map[string]any{"task_id": ""}},
			}
			for i, f := range fails {
				add("admin", fmt.Sprintf("refresh on the catalog, the engine answers %d (%d)", f.Status, i), "catalog",
					EngineRule{Method: "GET", Path: "^/engines$", Body: []any{map[string]any{"id": "duckdb", "available": true}}}, f)
				add("admin", fmt.Sprintf("refresh on a connection, the engine answers %d (%d)", f.Status, i), "conn", f)
			}
			for _, st := range forEveryone(w, "refresh", "POST", path("again"), nil) {
				st.EngineMask = masked
				s = append(s, st)
			}
			for _, k := range []string{"catalog", "again", "conn", "mailconn", "hiddenconn", "nooutput", "nosql", "snapshot", "noedit", "gone",
				"noread", "badsql", "hidden", "live"} {
				add("writer", "refresh "+k, k)
			}
			add("admin", "refresh again, as admin", "again")
			add("editor", "refresh without RUN", "catalog")
			add("admin", "refresh, the engines' list down", "catalog", EngineRule{Method: "GET", Path: "^/engines$", Status: 500, Text: "x"},
				EngineRule{Method: "POST", Path: "^/tasks/transform-data$", Body: map[string]any{"task_id": "t-ssr"}})
			s = append(s, Step{Name: "refresh an unknown script · writer", Method: "POST", Path: "/sql-scripts/999999/refresh", Auth: w.As("writer")})
			s = append(s, Step{Name: "refresh an odd script · writer", Method: "POST", Path: "/sql-scripts/x/refresh", Auth: w.As("writer")})
			s = append(s, Step{Name: "refresh with the writer's write token", Method: "POST", Path: path("conn"),
				Auth: Bearer("tab_ssr_writer_write_000000"), EngineMask: masked})
			s = append(s, Step{Name: "refresh with the writer's read token", Method: "POST", Path: path("conn"),
				Auth: Bearer("tab_ssr_writer_read_0000000"), EngineMask: masked})
			return append(s, settleRandomKeys("sql_scripts_refresh"))
		},
	})
}
