package main

import (
	"fmt"
)

// services/openlineage.py with OpenLineage on: what the routes tell the collector (the fake
// engine, «siem.test»: its Marquez API too) and the file. Run it with
// DIFF_ENV="OPENLINEAGE__URL=http://siem.test:8000 OPENLINEAGE__FILE=/share/ol-{stack}.jsonl SCRIPTS__ENABLED=true
// SCRIPTS__NAMESPACE=scripts SCRIPTS__REGISTRY=registry.test/tab" DIFF_SHARE=<an empty folder, writable by all>
// DIFF_SETTLE_MS=700: the events go out from a queue after the answer. Every rename and move
// (a folder, a flow, a datasource, a SQL script, a saved script, a notebook, a flow group), a
// notebook deleted with its live sessions, and the runs that close on a read (an Output's, a
// refresh from each kind of origin, a SQL script's write, a Pushmail cell's), the collector
// refusing and Marquez's deletes. The other suites run with OpenLineage on too, the same way.
func init() {
	ids := map[string]int64{}
	register(Suite{
		Module: "openlineage_x",
		Asked:  true,
		Seed: func(w *World) {
			ids = map[string]int64{}
			folder := func(key, name string, parent any) {
				ids[key] = w.Insert(`INSERT INTO projects (name, description, parent_id, created_at) VALUES ($1, '', $2, '2026-01-01') RETURNING id`,
					name, parent)
			}
			folder("top", "olx top", nil)
			folder("sub", "olx sub", ids["top"])
			folder("dest", "olx dest", nil)
			w.Exec(`INSERT INTO permissions (project_id, user_id, capability) VALUES ($1, $2, 'edit'), ($3, $2, 'edit')`, ids["top"],
				w.People["editor"], ids["dest"])
			conn := func(key, name, dbType, host string, port any, database, schema, user string) {
				ids[key] = w.Insert(`INSERT INTO connections (name, description, project_id, db_type, host, port, username, password_encrypted,
					database, db_schema, extra, created_at, updated_at) VALUES ($1, '', $2, $3, $4, $5, $6, '', $7, $8, '{}', '2026-01-01',
					'2026-01-01') RETURNING id`, name, ids["top"], dbType, host, port, user, database, schema)
			}
			conn("pg", "olx pg", "postgresql", "db.olx", nil, "shop", "", "u")
			conn("ora", "olx ora", "oracle", "ora.olx", 1522, "ORCL", "", "scott")
			conn("ms", "olx ms", "sqlserver", "ms.olx", nil, "dw", "", "u")
			conn("sftp", "olx sftp", "sftp", "sftp.olx", 2222, "", "", "u")
			conn("s3", "olx s3", "s3", "https://storage.googleapis.com", nil, "olx-bucket", "", "")
			conn("api", "olx api", "api", "https://API.olx.test:8443/v2/", nil, "", "", "")
			conn("sp", "olx sp", "sharepoint", "https://contoso.sharepoint.com/sites/olx/", nil, "", "", "")
			conn("gs", "olx gs", "gsheets", "", nil, "", "", "")
			conn("smtp", "olx smtp", "smtp", "smtp.olx", 2525, "", "", "")
			ds := func(key, name, project, kind string, connKey, sourceType, sourceRef any) {
				var c any
				if k, ok := connKey.(string); ok {
					c = ids[k]
				}
				ids[key] = w.Insert(`INSERT INTO datasources (name, description, project_id, bucket, key, rows, columns, sort_keys,
					column_descriptions, kind, connection_id, source_type, source_ref, created_at, updated_at) VALUES ($1::varchar, '', $2, 'data-prep',
					$7, 7, '[{"name": "id", "dtype": "Int64"}, {"name": "b"}]', '[]', '{}', $3, $4, $5, $6,
					'2026-01-01', '2026-01-01') RETURNING id`, name, ids[project], kind, c, sourceType, sourceRef, "datasets/"+name+".parquet")
			}
			ds("q", "olx query", "sub", "database", "pg", "sql", "WITH x AS (SELECT 1) SELECT * FROM public.orders o JOIN customers c ON 1=1 JOIN x ON 1=1")
			ds("t", "olx table", "sub", "database", "ora", "table", "sales")
			ds("ms", "olx ms table", "sub", "database", "ms", "table", "[sales].[t]")
			ds("sp", "olx sp ds", "sub", "database", "sp", "table", `{"path": "/Shared/Book.xlsx", "sheet": "S1"}`)
			ds("api", "olx api ds", "sub", "database", "api", "table", `{"path": "items/{{ today }}?key=secret"}`)
			ds("sftp", "olx sftp ds", "sub", "database", "sftp", "table", `{"folder": "/in/", "pattern": "*.csv"}`)
			ds("s3", "olx s3 ds", "sub", "database", "s3", "table", `{"folder": "f", "pattern": "x-*.parquet"}`)
			ds("gs", "olx gs ds", "sub", "database", "gs", "table", `{"spreadsheet_id": "abc", "sheet": "Tab"}`)
			ds("bad", "olx bad ref", "sub", "database", "sp", "table", `[1]`)
			ds("up", "olx upload", "top", "upload", nil, nil, nil)
			w.Exec(`UPDATE datasources SET description = 'the uploaded one' WHERE id = $1`, ids["up"])
			flow := func(key, name, project, def string) {
				ids[key] = w.Insert(`INSERT INTO flows (name, description, project_id, definition, engine, created_at, updated_at)
					VALUES ($1, 'a flow', $2, $3, 'duckdb', '2026-01-01', '2026-01-01') RETURNING id`, name, ids[project], def)
			}
			flow("f", "olx flow", "sub", fmt.Sprintf(`{"nodes": [
				{"id": "s1", "type": "source", "data": {"datasourceId": %d}},
				{"id": "s2", "type": "source", "data": {"datasourceId": %d}},
				{"id": "s3", "type": "source", "data": {"parquetKey": "up/z.parquet"}},
				{"id": "sc", "type": "script", "data": {"label": "clean"}},
				{"id": "c1", "type": "container", "data": {"label": "box", "repository": "team/app"}},
				{"id": "o1", "type": "output", "data": {"name": "olx upload", "projectId": %d}},
				{"id": "o2", "type": "output", "data": {"destType": "database", "connectionId": %d, "table": "out"}},
				{"id": "o3", "type": "output", "data": {"destType": "email", "connectionId": %d}}],
				"edges": [{"source": "s1", "target": "sc"}, {"source": "s2", "target": "sc", "targetHandle": "seq-in"},
				{"source": "sc", "target": "c1"}]}`, ids["up"], ids["q"], ids["top"], ids["pg"], ids["smtp"]))
			flow("reader", "olx reader", "dest", fmt.Sprintf(`{"nodes": [{"id": "s1", "type": "source", "data": {"datasourceId": %d}}]}`, ids["q"]))
			env := w.Insert(`INSERT INTO script_environments (name, description, language, enabled, cpu_request, cpu_limit, memory_request,
				memory_limit, max_timeout_seconds, created_at, updated_at) VALUES ('olx-py', '', 'python', true, '1', '1', '1Gi', '1Gi', 60,
				'2026-01-01', '2026-01-01') RETURNING id`)
			ids["script"] = w.Insert(`INSERT INTO saved_scripts (name, description, project_id, owner_id, environment_id, current_version, created_at,
				updated_at) VALUES ('olx script', 'reads', $1, $2, $3, 1, '2026-01-01', '2026-01-01') RETURNING id`, ids["sub"], w.People["admin"], env)
			w.Exec(`INSERT INTO saved_script_versions (script_id, version, content, note, created_at) VALUES ($1, 1, $2, '', '2026-01-01')`,
				ids["script"], fmt.Sprintf(`{"inputs": [{"datasource_id": %d, "alias": "q"}, {"datasource_id": 999999}], "output": {"publish": true,
				"name": "olx made", "project_id": %d}}`, ids["q"], ids["sub"]))
			sql := func(key, name, content string) {
				ids[key] = w.Insert(`INSERT INTO sql_scripts (name, description, project_id, owner_id, current_version, created_at, updated_at)
					VALUES ($1, '', $2, $3, 1, '2026-01-01', '2026-01-01') RETURNING id`, name, ids["sub"], w.People["admin"])
				w.Exec(`INSERT INTO sql_script_versions (script_id, version, content, note, created_at) VALUES ($1, 1, $2, '', '2026-01-01')`, ids[key], content)
			}
			sql("named", "olx named", fmt.Sprintf(`{"sql": "SELECT * FROM \"olx query\" JOIN \"olx sub\".\"olx table\" ON 1=1", "engine": "duckdb",
				"output": {"name": "olx sql out", "project_id": %d}}`, ids["sub"]))
			sql("onconn", "olx on conn", fmt.Sprintf(`{"sql": "SELECT a FROM s.t1, t2", "connection_id": %d}`, ids["pg"]))
			ids["nb"] = w.Insert(`INSERT INTO notebooks (name, description, project_id, environment_id, current_version, created_at, updated_at)
				VALUES ('olx board', '', $1, $2, 2, '2026-01-01', '2026-01-01') RETURNING id`, ids["sub"], env)
			ids["nb2"] = w.Insert(`INSERT INTO notebooks (name, description, project_id, environment_id, current_version, created_at, updated_at)
				VALUES ('olx doomed', 'goes', $1, $2, 1, '2026-01-01', '2026-01-01') RETURNING id`, ids["sub"], env)
			for i, nb := range []string{"nb", "nb2", "nb2"} {
				w.Exec(`INSERT INTO notebook_reads (notebook_id, datasource_id, last_session, mode, first_read_at, read_at) VALUES ($1, $2, $3,
					$4, '2026-01-01', $5)`, ids[nb], []int64{ids["q"], ids["q"], ids["up"]}[i], []string{"j-olx", "j-olx-0", "j-olx-1"}[i],
					[]string{"app", "app", "edit"}[i], fmt.Sprintf("2026-01-0%d", i+1))
			}
			for i, status := range []string{"ready", "starting", "failed"} {
				mode := []string{"app", "edit", "app"}[i]
				nb := []string{"nb2", "nb2", "nb"}[i]
				w.Exec(`INSERT INTO notebook_sessions (notebook_id, mode, user_id, version, namespace, job_name, status, token_fp, started_at,
					last_seen_at) VALUES ($1, $2, $3, 1, 'scripts', $4, $5, $4, '2026-02-01 10:00:00.123456', '2026-02-02')`, ids[nb], mode,
					w.People["admin"], fmt.Sprintf("j-olx-%d", i), status)
			}
			ids["group"] = w.Insert(`INSERT INTO flow_groups (name, description, project_id, definition, created_at, updated_at)
				VALUES ('olx group', '', $1, $2, '2026-01-01', '2026-01-01') RETURNING id`, ids["sub"], fmt.Sprintf(`{"steps": [
				[{"kind": "flow", "id": %d}, {"kind": "container", "id": 7, "container": {"repository": "img", "label": "pack"}}],
				[{"kind": "pushmail", "id": 8, "pushmail": {"label": " Monthly ", "connectionId": %d, "to": ["a@x.test"]}},
				 {"kind": "pushmail", "id": 9, "pushmail": {"label": "", "to": ["b@x.test"]}}]]}`, ids["f"], ids["smtp"]))
			// runs that close at the next read (the engine's answer: see the steps' rules)
			run := func(key, kind string, extra string, args ...any) {
				ids[key] = w.Insert(`INSERT INTO runs (kind, task_id, status, launched_by, trigger_type, input_key, output_bucket, output_key,
					publish_description, publish_overwrite, publish_sort_keys, started_at) VALUES ($1, $2, 'STARTED', $3, 'schedule', '', 'data-prep',
					'out/x.parquet', '', false, '[]', '2026-03-01 08:00:00.987654') RETURNING id`, kind, "t-olx-"+key, w.People["admin"])
				if extra != "" {
					w.Exec(`UPDATE runs SET `+extra+` WHERE id = $1`, append([]any{ids[key]}, args...)...)
				}
			}
			for _, k := range []string{"q", "t", "ms", "sp", "api", "sftp", "s3", "gs", "bad"} {
				run("ing-"+k, "ingest", `datasource_id = $2`, ids[k])
			}
			ids["orch"] = w.Insert(`INSERT INTO runs (kind, flow_id, task_id, status, trigger_type, input_key, output_bucket, output_key,
				publish_description, publish_overwrite, publish_sort_keys, started_at, finished_at) VALUES ('orchestration', $1, '', 'SUCCESS',
				'manual', '', '', '', '', false, '[]', '2026-03-01', '2026-03-01') RETURNING id`, ids["f"])
			run("out", "flow", `flow_id = $2, parent_run_id = $3, destination = $4, mirror = $5, email = $6, engine = 'duckdb'`, ids["f"], ids["orch"],
				fmt.Sprintf(`{"type": "database", "connection_id": %d, "table": "public.out", "mode": "replace"}`, ids["pg"]),
				`{"endpoint": "https://storage.googleapis.com", "bucket": "m", "key": "/k/m.parquet", "ok": true}`,
				fmt.Sprintf(`{"connection_id": %d, "to": ["z@x.test"]}`, ids["smtp"]))
			run("out-s3", "flow", `flow_id = $2, destination = $3`, ids["f"],
				`{"type": "s3", "bucket": "b", "key": "exports/{{ today }}.csv", "format": "csv", "written_key": "exports/2026-03-01.csv"}`)
			run("out-sftp", "flow", `flow_id = $2, destination = $3`, ids["f"],
				`{"type": "sftp", "host": "sftp.olx:2222", "folder": "/out", "file_name": "r-{{ today }}.csv", "path": "/out/r-2026.csv", "format": "csv"}`)
			run("out-input", "flow", `flow_id = $2, destination = '{"type": "script_input"}'`, ids["f"])
			run("sql-named", "sql", `sql_script_id = $2, script = '{"version": 1}', publish_name = 'olx sql out', publish_project_id = $3, engine = 'duckdb'`,
				ids["named"], ids["sub"])
			run("sql-conn", "sql", `sql_script_id = $2, script = $3`, ids["onconn"], fmt.Sprintf(`{"version": 1, "connection_id": %d}`, ids["pg"]))
			run("sql-ds", "sql", `sql_script_id = $2, script = $3, datasource_id = $4`, ids["named"], fmt.Sprintf(`{"datasources": [%d, 999999]}`,
				ids["up"]), ids["up"])
			ids["grouprun"] = w.Insert(`INSERT INTO runs (kind, flow_group_id, task_id, status, trigger_type, input_key, output_bucket,
				output_key, publish_description, publish_overwrite, publish_sort_keys, started_at) VALUES ('orchestration', $1, '', 'STARTED',
				'manual', '', '', '', '', false, '[]', '2026-03-01') RETURNING id`, ids["group"])
			run("push", "pushmail", `parent_run_id = $2, email = $3`, ids["grouprun"], fmt.Sprintf(`{"cell": " Monthly ", "connection_id": %d,
				"datasource_ids": [%d, %d], "report_datasource_ids": [%d, 999999]}`, ids["smtp"], ids["q"], ids["up"], ids["q"]))
			run("push-host", "pushmail", `parent_run_id = $2, email = '{"host": "mail.x.test"}'`, ids["grouprun"])
		},
		Steps: func(w *World) []Step {
			var s []Step
			admin := w.As("admin")
			add := func(name, method, path string, body any, rules ...EngineRule) {
				if rules == nil {
					rules = []EngineRule{}
				}
				s = append(s, Step{Name: name, Method: method, Path: path, JSON: body, Auth: admin, Engine: rules})
			}
			id := func(k string) string { return fmt.Sprint(ids[k]) }
			// the runs that close on a read: the engine's answer for each task
			task := func(key, status string, extra map[string]any) EngineRule {
				body := map[string]any{"status": status}
				for k, v := range extra {
					body[k] = v
				}
				return EngineRule{Method: "GET", Path: "^/tasks/t-olx-" + key + "$", Body: body}
			}
			failed := map[string]any{"error": "anna.rossi@acme.example cannot read it; ask bob@x.test", "error_detail": "Traceback"}
			for _, k := range []string{"q", "t", "ms", "sp", "api", "sftp", "s3", "gs", "bad"} {
				add("a refresh closes · "+k, "GET", "/runs/"+id("ing-"+k), nil, task("ing-"+k, "FAILURE", failed))
			}
			add("an Output closes", "GET", "/runs/"+id("out"), nil, task("out", "FAILURE", failed))
			add("an S3 Output closes", "GET", "/runs/"+id("out-s3"), nil, task("out-s3", "FAILURE", nil))
			add("an SFTP Output closes", "GET", "/runs/"+id("out-sftp"), nil, task("out-sftp", "FAILURE", nil))
			add("a Script node's input closes", "GET", "/runs/"+id("out-input"), nil, task("out-input", "FAILURE", nil))
			add("a SQL script's write closes", "GET", "/runs/"+id("sql-named"), nil, task("sql-named", "FAILURE", nil))
			add("a SQL script on a connection closes", "GET", "/runs/"+id("sql-conn"), nil, task("sql-conn", "FAILURE", nil))
			add("a SQL script's recorded datasources", "GET", "/runs/"+id("sql-ds"), nil, task("sql-ds", "FAILURE", nil))
			add("a Pushmail cell closes", "GET", "/runs/"+id("push"), nil, task("push", "FAILURE", nil))
			add("a Pushmail cell without a connection", "GET", "/runs/"+id("push-host"), nil, task("push-host", "FAILURE", nil))
			add("read again: closed already", "GET", "/runs/"+id("push"), nil)
			// the collector refusing: retried on a 5xx, not on a 4xx; a run closing still answers
			refuse := func(status int) EngineRule {
				return EngineRule{Method: "POST", Path: "^/api/v1/lineage$", Status: status, Body: map[string]any{"x": 1}}
			}
			add("the collector down", "GET", "/flows/"+id("f")+"/runs", nil, refuse(503))
			// renames and moves
			marquez := EngineRule{Method: "DELETE", Path: "^/api/v1/namespaces/", Status: 404}
			add("a flow renamed", "PATCH", "/flows/"+id("f"), map[string]any{"name": "olx flow 2"})
			add("a flow renamed, Marquez lost the old name", "PATCH", "/flows/"+id("f"), map[string]any{"name": "olx flow 3"}, marquez)
			add("a flow, the same name", "PATCH", "/flows/"+id("f"), map[string]any{"name": " olx flow 3 "})
			add("a flow moved", "PATCH", "/flows/"+id("f"), map[string]any{"project_id": ids["top"]})
			add("a flow, only its description", "PATCH", "/flows/"+id("f"), map[string]any{"description": "x"})
			add("a datasource renamed", "PATCH", "/datasources/"+id("q"), map[string]any{"name": "olx query 2"})
			add("a datasource moved", "PATCH", "/datasources/"+id("up"), map[string]any{"project_id": ids["dest"]})
			add("a datasource, a name taken", "PATCH", "/datasources/"+id("up"), map[string]any{"name": "olx query 2", "project_id": ids["sub"]})
			add("a SQL script renamed", "PATCH", "/sql-scripts/"+id("named"), map[string]any{"name": "olx named 2"})
			add("a SQL script on a connection moved", "PATCH", "/sql-scripts/"+id("onconn"), map[string]any{"project_id": ids["dest"]})
			add("a saved script renamed", "PATCH", "/scripts/"+id("script"), map[string]any{"name": "olx script 2"})
			add("a notebook renamed", "PATCH", "/notebooks/"+id("nb"), map[string]any{"name": "olx board 2"})
			add("a flow group renamed", "PATCH", "/flow-groups/"+id("group"), map[string]any{"name": "olx group 2"})
			add("a folder renamed", "PATCH", "/projects/"+id("sub"), map[string]any{"name": "olx sub 2"})
			add("a folder moved", "PATCH", "/projects/"+id("sub"), map[string]any{"parent_id": ids["dest"]}, marquez)
			add("a folder, only its description", "PATCH", "/projects/"+id("sub"), map[string]any{"description": "d"})
			add("a folder renamed, the collector refusing", "PATCH", "/projects/"+id("top"), map[string]any{"name": "olx top 2"}, refuse(400))
			s = append(s, Step{Name: "an editor renames a flow", Method: "PATCH", Path: "/flows/" + id("reader"), JSON: map[string]any{"name": "olx r"},
				Auth: w.As("editor"), Engine: []EngineRule{}})
			add("a notebook deleted with its live sessions", "DELETE", "/notebooks/"+id("nb2"), nil)
			add("the lineage of the moved flow", "GET", "/flows/"+id("f")+"/openlineage", nil)
			return s
		},
	})
}
