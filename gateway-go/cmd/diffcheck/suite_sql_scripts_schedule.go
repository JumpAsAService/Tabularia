package main

import (
	"fmt"
	"time"
)

// routes/sql_scripts.py set_sql_script_schedule and list_sql_script_runs, routes/scripts.py
// set_script_schedule: a schedule set with a cron (croniter's reading, the panel's time zone,
// what the schedule would lack to write), and the runs of a SQL script brought up to date with
// the engine (routes/runs._reconcile). The saved scripts' branches past the 409 need
// DIFF_ENV="SCRIPTS__ENABLED=true SCRIPTS__NAMESPACE=scripts SCRIPTS__REGISTRY=registry.test/tab";
// DIFF_ENV="APP__TIMEZONE=Europe/Rome" reads the crons in another zone.
func init() {
	ids := map[string]int64{}
	register(Suite{
		Module: "sql_scripts_schedule",
		Seed: func(w *World) {
			ids = map[string]int64{}
			w.Token("writer", "tab_sss_writer_write_000000", "write", time.Hour)
			w.Token("writer", "tab_sss_writer_read_0000000", "read", time.Hour)
			folder := func(key string) {
				ids["f_"+key] = w.Insert(`INSERT INTO projects (name, description, parent_id, created_at) VALUES ($1, '', NULL, '2026-01-01')
					RETURNING id`, "sss-"+key)
			}
			for _, f := range []string{"scripts", "out", "src", "conn", "viewonly", "hidden"} {
				folder(f)
			}
			grant := func(f, capability, person string) {
				w.Exec(`INSERT INTO permissions (project_id, user_id, group_id, capability) VALUES ($1, $2, NULL, $3)`,
					ids["f_"+f], w.People[person], capability)
			}
			grant("scripts", "run", "writer")
			grant("scripts", "view", "writer")
			grant("out", "edit", "writer")
			grant("src", "view", "writer")
			grant("conn", "connect", "writer")
			grant("viewonly", "view", "writer")
			grant("scripts", "view", "editor")
			grant("scripts", "view", "obs_flag")
			ds := func(key, name, folder, kind string) {
				ids[key] = w.Insert(`INSERT INTO datasources (name, description, project_id, bucket, key, rows, columns, sort_keys,
					column_descriptions, kind, created_at, updated_at) VALUES ($1, '', $2, 'data-prep', $3, 1, '[]', '[]', '{}', $4,
					'2026-01-02', '2026-01-03') RETURNING id`, name, ids["f_"+folder], "datasets/"+key+".parquet", kind)
			}
			ds("vendite", "sss_vendite", "src", "flow")
			ds("written", "Scritto", "out", "flow")
			ds("dbsnap", "Snapshot", "out", "database")
			conn := func(key, name, folder, dbType string) {
				ids[key] = w.Insert(`INSERT INTO connections (name, description, project_id, db_type, host, username, password_encrypted,
					database, db_schema, extra, created_at, updated_at) VALUES ($1, '', $2, $3, 'db.test', 'u', $4, 'd', '', '{}',
					'2026-01-01', '2026-01-01') RETURNING id`, name, ids["f_"+folder], dbType, seal("pw"))
			}
			conn("pg", "sss-pg", "conn", "postgresql")
			conn("mail", "sss-mail", "conn", "smtp")
			conn("hidden_pg", "sss-pg-hidden", "hidden", "postgresql")
			script := func(key, folder, content string) {
				ids[key] = w.Insert(`INSERT INTO sql_scripts (name, description, project_id, owner_id, current_version, created_at, updated_at)
					VALUES ($1, '', $2, $3, 1, '2026-02-01', '2026-02-01') RETURNING id`, "sss-"+key, ids["f_"+folder], w.People["admin"])
				w.Exec(`INSERT INTO sql_script_versions (script_id, version, content, note, created_at, created_by)
					VALUES ($1, 1, $2, 'created', '2026-02-01', $3)`, ids[key], content, w.People["admin"])
			}
			out := func(name, folder string) string {
				return fmt.Sprintf(`"output": {"name": %q, "project_id": %d}`, name, ids["f_"+folder])
			}
			script("catalog", "scripts", `{"sql": "SELECT * FROM sss_vendite", `+out("Nuovo", "out")+`}`)
			script("again", "scripts", `{"sql": "SELECT * FROM sss_vendite", "engine": "duckdb", `+out("Scritto", "out")+`}`)
			script("conn", "scripts", `{"sql": "select 1;", "connection_id": `+fmt.Sprint(ids["pg"])+`, `+out("Da DB", "out")+`}`)
			script("mailconn", "scripts", `{"sql": "select 1", "connection_id": `+fmt.Sprint(ids["mail"])+`, `+out("X", "out")+`}`)
			script("hiddenconn", "scripts", `{"sql": "select 1", "connection_id": `+fmt.Sprint(ids["hidden_pg"])+`, `+out("Y", "out")+`}`)
			script("nooutput", "scripts", `{"sql": "SELECT * FROM sss_vendite"}`)
			script("nosql", "scripts", `{"sql": " -- nothing\n ; ", `+out("Z", "out")+`}`)
			script("snapshot", "scripts", `{"sql": "SELECT * FROM sss_vendite", `+out("Snapshot", "out")+`}`)
			script("noedit", "scripts", `{"sql": "SELECT * FROM sss_vendite", `+out("W", "viewonly")+`}`)
			script("gone", "scripts", `{"sql": "SELECT * FROM sss_vendite", "output": {"name": "G", "project_id": 999999}}`)
			script("noread", "scripts", `{"sql": "SELECT * FROM sss_segreto", `+out("V", "out")+`}`)
			script("badsql", "scripts", `{"sql": "SELEC FROM (", `+out("B", "out")+`}`)
			script("hidden", "hidden", `{"sql": "SELECT 1", `+out("H", "out")+`}`)
			w.Exec(`UPDATE sql_scripts SET run_schedule = '0 * * * *', run_scheduled_by = $1, next_run_at = '2026-03-01 10:00:00'
				WHERE id = $2`, w.People["admin"], ids["again"])

			// the runs: one still going per script, each answered by the engine its own way
			live := func(key, task string, publish bool, started string) {
				script(key, "scripts", `{"sql": "select 1", "connection_id": `+fmt.Sprint(ids["pg"])+`, `+out("Run "+key, "out")+`}`)
				var name, folder any
				if publish {
					name, folder = "Pubblicato "+key, ids["f_out"]
				}
				w.Exec(`INSERT INTO runs (kind, sql_script_id, task_id, status, launched_by, trigger_type, input_key, output_bucket, output_key,
					publish_name, publish_project_id, publish_description, publish_overwrite, publish_sort_keys, started_at)
					VALUES ('sql', $1, $2, 'STARTED', $3, 'manual', '', 'data-prep', $4, $5, $6, '', true, '[]', `+started+`)`,
					ids[key], task, w.People["writer"], "datasets/"+key+".parquet", name, folder)
				w.Exec(`INSERT INTO runs (kind, sql_script_id, task_id, status, launched_by, trigger_type, input_key, output_bucket, output_key,
					publish_description, publish_overwrite, publish_sort_keys, started_at, finished_at, datasource_id, rows_written)
					VALUES ('sql', $1, 'old', 'SUCCESS', $2, 'schedule', '', '', '', '', true, '[]', '2026-01-01', '2026-01-01', $3, 7)`,
					ids[key], w.People["admin"], ids["written"])
			}
			live("r_ok", "t-ok", true, "now() at time zone 'utc'")
			live("r_okplain", "t-okplain", false, "now() at time zone 'utc'")
			live("r_fail", "t-fail", false, "now() at time zone 'utc'")
			live("r_going", "t-going", false, "now() at time zone 'utc'")
			live("r_down", "t-down", false, "now() at time zone 'utc'")
			live("r_stale", "t-stale", false, "'2026-01-01'")
			live("r_never", "pending-abc", false, "'2026-01-01'")
			live("r_bad", "t-bad", false, "now() at time zone 'utc'")
			live("r_revoked", "t-revoked", false, "now() at time zone 'utc'")

			// saved scripts
			coders := w.Insert(`INSERT INTO groups (name, description, is_admin, is_observer, created_at) VALUES ('sss-coders', '', false, false,
				'2026-01-01') RETURNING id`)
			w.Exec(`INSERT INTO user_groups (user_id, group_id) VALUES ($1, $2)`, w.People["writer"], coders)
			env := w.Insert(`INSERT INTO script_environments (name, description, language, enabled, cpu_request, cpu_limit,
				memory_request, memory_limit, max_timeout_seconds, created_at, updated_at) VALUES ('sss-py', 'd', 'python', true, '500m', '2',
				'1Gi', '4Gi', 600, '2026-01-01', '2026-01-01') RETURNING id`)
			w.Exec(`INSERT INTO script_environment_groups (environment_id, group_id) VALUES ($1, $2)`, env, coders)
			saved := func(key, folder, content string) {
				ids[key] = w.Insert(`INSERT INTO saved_scripts (name, description, project_id, owner_id, environment_id, current_version,
					created_at, updated_at) VALUES ($1, 'about', $2, $3, $4, 1, '2026-02-01', '2026-02-02') RETURNING id`,
					"sss-"+key, ids["f_"+folder], w.People["admin"], env)
				w.Exec(`INSERT INTO saved_script_versions (script_id, version, content, note, created_at, created_by)
					VALUES ($1, 1, $2, 'n', '2026-02-01', $3)`, ids[key], content, w.People["admin"])
			}
			saved("code", "scripts", `{"files": [{"path": "main.py", "content": "print(1)"}], "entrypoint": "main.py"}`)
			saved("empty", "scripts", `{"files": []}`)
			saved("nofiles", "scripts", `{"entrypoint": "main.py"}`)
			saved("secret", "hidden", `{"files": [{"path": "main.py", "content": "1"}]}`)
		},
		Engine: []EngineRule{
			{Method: "GET", Path: "^/engines$", Body: []any{map[string]any{"id": "duckdb", "available": true}}},
			{Method: "GET", Path: "^/tasks/t-ok$", Body: map[string]any{"status": "SUCCESS", "result": map[string]any{"rows_written": 12,
				"columns": []any{map[string]any{"name": "a", "dtype": "Int64"}}, "rows": 12}}},
			{Method: "GET", Path: "^/tasks/t-okplain$", Body: map[string]any{"status": "SUCCESS", "result": map[string]any{"rows_written": 3}}},
			{Method: "GET", Path: "^/tasks/t-fail$", Body: map[string]any{"status": "FAILURE", "error": "PostgreSQL: relation does not exist",
				"error_detail": "trace"}},
			{Method: "GET", Path: "^/tasks/t-going$", Body: map[string]any{"status": "STARTED", "progress": map[string]any{"phase": "reading"}}},
			{Method: "GET", Path: "^/tasks/t-down$", Status: 500, Text: "boom"},
			{Method: "GET", Path: "^/tasks/t-stale$", Body: map[string]any{"status": "STARTED"}},
			{Method: "DELETE", Path: "^/tasks/t-stale$", Body: map[string]any{"ok": true}},
			{Method: "GET", Path: "^/tasks/t-bad$", Body: map[string]any{"status": "SUCCESS", "result": []any{1}}},
			{Method: "GET", Path: "^/tasks/t-revoked$", Body: map[string]any{"status": "REVOKED"}},
		},
		Steps: func(w *World) []Step {
			var s []Step
			sqlPath := func(k string) string { return fmt.Sprintf("/sql-scripts/%d/schedule", ids[k]) }
			put := func(name, k, person string, body any) {
				s = append(s, Step{Name: name + " · " + person, Method: "PUT", Path: sqlPath(k), Auth: w.As(person), JSON: body})
			}
			daily := map[string]any{"cron": "0 3 * * *"}
			// who may: everyone tries the same cron (an admin first)
			s = append(s, forEveryone(w, "set Da DB's schedule", "PUT", sqlPath("conn"), daily)...)
			for _, k := range []string{"catalog", "again", "conn", "mailconn", "hiddenconn", "nooutput", "nosql", "snapshot", "noedit", "gone",
				"noread", "badsql", "hidden"} {
				put("schedule "+k, k, "writer", daily)
			}
			for i, cron := range []any{"x", " 0 4 * * 1 ", "", "   ", nil, "0 0 31 2 *", "@daily", "0 3 * * * *", "61 * * * *",
				"0 2 1 * *", 5, []any{"0 3 * * *"}, "0 3 * * *\n", "0 3 * * MON-FRI"} {
				put(fmt.Sprintf("the cron %v (%d)", cron, i), "conn", "writer", map[string]any{"cron": cron})
			}
			put("a cron that never fires, the script lacking its output", "nooutput", "writer", map[string]any{"cron": "0 0 31 2 *"})
			put("a bad cron, the script lacking its output", "nooutput", "writer", map[string]any{"cron": "nope"})
			put("a clear of what the schedule lacks", "nooutput", "writer", map[string]any{"cron": ""})
			put("the editor (VIEW only) sets one", "conn", "editor", daily)
			put("no body", "conn", "writer", nil)
			s = append(s,
				Step{Name: "schedule an unknown script", Method: "PUT", Path: "/sql-scripts/999999/schedule", Auth: w.As("writer"), JSON: daily},
				Step{Name: "schedule a word", Method: "PUT", Path: "/sql-scripts/x/schedule", Auth: w.As("writer"), JSON: daily},
				Step{Name: "schedule with the write token", Method: "PUT", Path: sqlPath("again"), Auth: Bearer("tab_sss_writer_write_000000"),
					JSON: map[string]any{"cron": "15 6 * * *"}},
				Step{Name: "schedule with the read token", Method: "PUT", Path: sqlPath("again"), Auth: Bearer("tab_sss_writer_read_0000000"),
					JSON: daily},
				Step{Name: "the list with the schedules", Method: "GET", Path: "/sql-scripts", Auth: w.As("writer"), Unordered: []string{""}})

			// the runs, brought up to date with the engine
			runs := func(name, k, person string) {
				s = append(s, Step{Name: name + " · " + person, Method: "GET", Path: fmt.Sprintf("/sql-scripts/%d/runs", ids[k]), Auth: w.As(person)})
			}
			runs("a run that succeeded and publishes", "r_ok", "editor")
			runs("the same, read again", "r_ok", "writer")
			runs("a run that succeeded", "r_okplain", "writer")
			runs("a run that failed", "r_fail", "writer")
			runs("a run still going", "r_going", "writer")
			runs("the engine down", "r_down", "admin")
			runs("a run gone stale", "r_stale", "writer")
			runs("a run never queued", "r_never", "writer")
			runs("the engine's result not an object", "r_bad", "writer")
			runs("a run revoked", "r_revoked", "obs_flag")
			s = append(s,
				Step{Name: "runs, limit 1", Method: "GET", Path: fmt.Sprintf("/sql-scripts/%d/runs?limit=1", ids["r_going"]), Auth: w.As("writer")},
				Step{Name: "runs, nobody", Method: "GET", Path: fmt.Sprintf("/sql-scripts/%d/runs", ids["r_going"])},
				Step{Name: "runs, a read token", Method: "GET", Path: fmt.Sprintf("/sql-scripts/%d/runs", ids["r_going"]),
					Auth: Bearer("tab_sss_writer_read_0000000")},
				Step{Name: "runs of a hidden script", Method: "GET", Path: fmt.Sprintf("/sql-scripts/%d/runs", ids["hidden"]), Auth: w.As("writer")})

			// saved scripts
			scPath := func(k string) string { return fmt.Sprintf("/scripts/%d/schedule", ids[k]) }
			s = append(s, forEveryone(w, "set a saved script's schedule", "PUT", scPath("code"), daily)...)
			for _, k := range []string{"code", "empty", "nofiles", "secret"} {
				s = append(s, Step{Name: "saved script " + k, Method: "PUT", Path: scPath(k), Auth: w.As("writer"), JSON: daily})
			}
			for i, cron := range []any{"bad cron", "0 0 31 2 *", " */30 1 * * * ", "", nil, 3} {
				s = append(s, Step{Name: fmt.Sprintf("saved script, the cron %v (%d)", cron, i), Method: "PUT", Path: scPath("code"),
					Auth: w.As("writer"), JSON: map[string]any{"cron": cron}})
			}
			s = append(s,
				Step{Name: "saved script: a bad cron on one without code", Method: "PUT", Path: scPath("empty"), Auth: w.As("writer"),
					JSON: map[string]any{"cron": "x"}},
				Step{Name: "saved script: the editor (VIEW only)", Method: "PUT", Path: scPath("code"), Auth: w.As("editor"), JSON: daily},
				Step{Name: "saved script unknown", Method: "PUT", Path: "/scripts/999999/schedule", Auth: w.As("writer"), JSON: daily},
				Step{Name: "saved script, a write token", Method: "PUT", Path: scPath("code"), Auth: Bearer("tab_sss_writer_write_000000"),
					JSON: map[string]any{"cron": "0 5 * * *"}})
			return s
		},
	})
}
