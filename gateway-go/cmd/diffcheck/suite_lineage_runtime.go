package main

import (
	"fmt"
	"net/http"
)

// services/lineage.py on a folder of its own: every kind of node and edge (sources,
// refreshes, «Run flow», database, S3 and SFTP outputs, Container images, SQL scripts that name
// datasources (sqlglot in the AI service), notebooks' reads, flow groups with Pushmail cells),
// references to what is hidden or removed, and the staleness. The graph's lists come in the
// tables' physical order (Unordered); a subgraph's nodes from a Python set (no order at all).
func init() {
	ids := map[string]int64{}
	register(Suite{
		Module: "lineage_runtime",
		Seed: func(w *World) {
			ids = map[string]int64{}
			folder := func(key, name string) {
				ids[key] = w.Insert(`INSERT INTO projects (name, description, created_at) VALUES ($1, '', '2026-01-01') RETURNING id`, name)
			}
			folder("pmain", "lr-main")
			folder("hidden", "lr-hidden")
			w.Exec(`INSERT INTO permissions (project_id, user_id, capability) VALUES ($1, $2, 'view'), ($1, $3, 'edit')`, ids["pmain"],
				w.People["editor"], w.People["writer"])
			ds := func(key, name, project, kind string, refreshed any) {
				ids[key] = w.Insert(`INSERT INTO datasources (name, description, project_id, bucket, key, rows, columns, sort_keys,
					column_descriptions, kind, refreshed_at, created_at, updated_at) VALUES ($1, '', $2, 'data-prep', $1, 7, '[]', '[]', '{}', $3,
					$4, '2026-01-01', '2026-01-01') RETURNING id`, name, ids[project], kind, refreshed)
			}
			ds("sales", "lr_sales", "pmain", "database", "2026-03-01 10:00:00")
			ds("sales2", "LR_Sales", "hidden", "upload", nil)
			ds("clients", "lr clients", "pmain", "upload", nil)
			ds("secret", "lr secret", "hidden", "upload", nil)
			ids["conn"] = w.Insert(`INSERT INTO connections (name, description, project_id, db_type, host, port, username, password_encrypted,
				database, db_schema, extra, created_at, updated_at) VALUES ('lr pg', '', $1, 'postgresql', 'db.lr', 5432, 'u', '', 'shop', '',
				'{}', '2026-01-01', '2026-01-01') RETURNING id`, ids["pmain"])
			ids["sftp"] = w.Insert(`INSERT INTO connections (name, description, project_id, db_type, host, port, username, password_encrypted,
				database, db_schema, extra, created_at, updated_at) VALUES ('lr sftp', '', $1, 'sftp', 'sftp.lr', 22, 'u', '', '', '',
				'{}', '2026-01-01', '2026-01-01') RETURNING id`, ids["pmain"])
			w.Exec(`UPDATE datasources SET connection_id = $1 WHERE id = $2`, ids["conn"], ids["sales"])
			flow := func(key, name, project, def string, schedule any) {
				ids[key] = w.Insert(`INSERT INTO flows (name, description, project_id, definition, engine, run_schedule, created_at, updated_at)
					VALUES ($1, '', $2, $3, 'duckdb', $4, '2026-01-01', '2026-01-01') RETURNING id`, name, ids[project], def, schedule)
			}
			flow("hiddenflow", "lr hidden flow", "hidden", `{"nodes": []}`, nil)
			flow("sub", "lr sub", "pmain", `not json`, nil)
			flow("main", "lr main", "pmain", fmt.Sprintf(`{"nodes": [
				{"id": "s1", "type": "source", "data": {"datasourceId": %d}},
				{"id": "s2", "type": "source", "data": {"datasourceId": %d}},
				{"id": "s3", "type": "source", "data": {"datasourceId": 999999}},
				{"id": "s4", "type": "source", "data": {}},
				{"id": "r1", "type": "refresh", "data": {"datasourceId": %d}},
				{"id": "f1", "type": "runflow", "data": {"flowId": %d}},
				{"id": "f2", "type": "runflow", "data": {"flowId": %d}},
				{"id": "f3", "type": "runflow", "data": {"flowId": 999998}},
				{"id": "o1", "type": "output", "data": {"destType": "database", "connectionId": %d, "table": " public.out ", "mode": "replace"}},
				{"id": "o2", "type": "output", "data": {"destType": "database", "connectionId": 999999}},
				{"id": "o3", "type": "output", "data": {"destType": "s3", "s3Bucket": " b ", "s3Key": "k/x.parquet", "partitionBy": ["a", "b"]}},
				{"id": "o4", "type": "output", "data": {"destType": "s3"}},
				{"id": "o5", "type": "output", "data": {"destType": "sftp", "connectionId": %d, "sftpFolder": "/in", "sftpFileName": "x.csv"}},
				{"id": "o6", "type": "output", "data": {}},
				{"id": "c1", "type": "container", "data": {"registryId": 3, "repository": " team/app ", "tag": "v1"}},
				{"id": "c2", "type": "container", "data": {"repository": ""}}], "edges": []}`,
				ids["sales"], ids["secret"], ids["clients"], ids["sub"], ids["hiddenflow"], ids["conn"], ids["sftp"]), "0 * * * *")
			flow("reader", "lr reader", "pmain", fmt.Sprintf(`{"nodes": [{"id": "s1", "type": "source", "data": {"datasourceId": %d}},
				{"id": "s2", "type": "source", "data": {"datasourceId": %d}},
				{"id": "s3", "type": "source", "data": {"parquetKey": "up/x.parquet"}},
				{"id": "s4", "type": "source", "data": {"parquetKey": "up/y.parquet", "bucket": "other"}},
				{"id": "o1", "type": "output", "data": {"name": " lr clients ", "projectId": %d, "mirrorEnabled": true, "mirrorConnectionId": %d,
				 "mirrorKey": "m/k.parquet"}},
				{"id": "o2", "type": "output", "data": {"name": "lr new", "projectId": %d}},
				{"id": "o3", "type": "output", "data": {"name": "lr nowhere"}},
				{"id": "o4", "type": "output", "data": {"destType": "database", "connectionId": %d, "table": "sales"}},
				{"id": "o5", "type": "output", "data": {"destType": "database", "connectionId": %d, "table": "s.t", "mode": "replace"}},
				{"id": "o6", "type": "output", "data": {"destType": "s3", "connectionId": %d, "s3Key": "/a/b.csv", "s3Format": "csv"}},
				{"id": "o7", "type": "output", "data": {"destType": "sftp", "connectionId": %d, "sftpFolder": "/in/", "sftpFileName": "f.csv"}},
				{"id": "o8", "type": "output", "data": {"destType": "email", "connectionId": %d}},
				{"id": "o9", "type": "output", "data": {"destType": "email", "connectionId": %d}}]}`,
				ids["clients"], ids["sales"], ids["pmain"], ids["conn"], ids["pmain"], ids["conn"], ids["conn"], ids["conn"], ids["sftp"], ids["sftp"],
				ids["conn"]), nil)
			w.Exec(`UPDATE datasources SET description = 'the clients', columns = '[{"name": "id", "dtype": "Int64"}, {"x": 1}, "y", {"name": ""}]'
				WHERE id = $1`, ids["clients"])
			w.Exec(`UPDATE flows SET description = 'reads' WHERE id = $1`, ids["reader"])
			w.Exec(`UPDATE datasources SET flow_id = $1 WHERE id = $2`, ids["main"], ids["clients"])
			run := func(flow, status string, started, finished any) {
				w.Exec(`INSERT INTO runs (kind, flow_id, task_id, status, trigger_type, input_key, output_bucket, output_key, publish_description,
					publish_overwrite, publish_sort_keys, started_at, finished_at) VALUES ('flow', $1, '', $2, 'manual', '', '', '', '', false, '[]',
					$3, $4)`, ids[flow], status, started, finished)
			}
			run("main", "SUCCESS", "2026-02-01 09:00:00", "2026-02-01 09:10:00")
			run("main", "FAILURE", "2026-03-02 09:00:00", nil)
			run("reader", "SUCCESS", "2026-03-05 09:00:00", nil)
			sql := func(name, content string) {
				id := w.Insert(`INSERT INTO sql_scripts (name, description, project_id, current_version, created_at, updated_at, run_schedule)
					VALUES ($1, '', $2, 1, '2026-01-01', '2026-01-01', NULL) RETURNING id`, name, ids["pmain"])
				w.Exec(`INSERT INTO sql_script_versions (script_id, version, content, note, created_at) VALUES ($1, 1, $2, '', '2026-01-01')`, id, content)
			}
			sql("lr q1", `{"sql": "-- x\nWITH t AS (SELECT 1) SELECT * FROM lr_sales s JOIN \"lr clients\" c ON 1=1 JOIN t ON 1=1 JOIN lr_main.lr_sales x ON 1=1;", "engine": "duckdb",
				"output": {"project_id": `+fmt.Sprint(ids["pmain"])+`, "name": "lr clients"}}`)
			sql("lr q2", fmt.Sprintf(`{"sql": "SELECT 1", "connection_id": %d}`, ids["conn"]))
			sql("lr q3", `{"sql": "SELECT * FROM a; SELECT 2"}`)
			sql("lr q4", `{"sql": "SELEC FROM"}`)
			sql("lr q5", `not json`)
			env := w.Insert(`INSERT INTO script_environments (name, description, language, enabled, cpu_request, cpu_limit, memory_request,
				memory_limit, max_timeout_seconds, created_at, updated_at) VALUES ('lr-py', '', 'python', true, '1', '1', '1Gi', '1Gi', 60,
				'2026-01-01', '2026-01-01') RETURNING id`)
			group := w.Insert(`INSERT INTO groups (name, description, is_admin, is_observer, created_at) VALUES ('lr-coders', '', false, false,
				'2026-01-01') RETURNING id`)
			w.Exec(`INSERT INTO user_groups (user_id, group_id) VALUES ($1, $2)`, w.People["writer"], group)
			w.Exec(`INSERT INTO script_environment_groups (environment_id, group_id) VALUES ($1, $2)`, env, group)
			nb := w.Insert(`INSERT INTO notebooks (name, description, project_id, environment_id, current_version, created_at, updated_at)
				VALUES ('lr board', '', $1, $2, 3, '2026-01-01', '2026-01-01') RETURNING id`, ids["pmain"], env)
			ids["nb"] = nb
			for i, d := range []int64{ids["sales"], ids["secret"], ids["sales"]} {
				mode := []string{"app", "edit", "edit"}[i]
				w.Exec(`INSERT INTO notebook_reads (notebook_id, datasource_id, last_session, mode, first_read_at, read_at) VALUES ($1, $2, $3, $4, '2026-01-01',
					'2026-01-01')`, nb, d, fmt.Sprintf("job-%d", i), mode)
			}
			ids["group"] = w.Insert(`INSERT INTO flow_groups (name, description, project_id, definition, run_schedule, created_at, updated_at)
				VALUES ('lr group', '', $1, $2, '5 * * * *', '2026-01-01', '2026-01-01') RETURNING id`, ids["pmain"], fmt.Sprintf(`{"steps": [
				[{"kind": "flow", "id": %d}, {"kind": "flow", "id": %d}, null],
				[{"kind": "refresh", "id": %d}, {"kind": "script", "id": 5}, {"kind": "sql", "id": 999999}],
				[{"kind": "container", "id": 7, "container": {"registryId": "r", "repository": "img"}},
				 {"kind": "pushmail", "id": 8, "pushmail": {"label": "  ", "connectionId": %d, "to": ["a@x", " "], "cc": ["b@x"],
				  "attachments": [{"datasourceId": %d}, {"datasourceId": 999997}, {}], "reports": [{"reportId": 1}]}}]]}`,
				ids["main"], ids["hiddenflow"], ids["sales"], ids["conn"], ids["clients"]))
			w.Exec(`INSERT INTO flow_groups (name, description, project_id, definition, created_at, updated_at)
				VALUES ('lr broken group', '', $1, 'not json', '2026-01-01', '2026-01-01')`, ids["pmain"])
		},
		Steps: func(w *World) []Step {
			var s []Step
			add := func(name, query, p string) {
				var auth func(*http.Request)
				if p != "nobody" {
					auth = w.As(p)
				}
				st := Step{Name: name + " · " + p, Method: "GET", Path: "/lineage" + query, Auth: auth, Unordered: []string{"nodes", "edges"}}
				s = append(s, st)
			}
			for _, p := range []string{"admin", "editor", "writer", "obs_flag", "service", "nobody"} {
				add("the whole graph", "", p)
				add("around the main flow", fmt.Sprintf("?type=flow&id=%d", ids["main"]), p)
			}
			for _, q := range []string{
				fmt.Sprintf("?type=datasource&id=%d&direction=downstream&depth=8", ids["sales"]),
				fmt.Sprintf("?type=datasource&id=%d&direction=upstream&depth=1", ids["clients"]),
				fmt.Sprintf("?type=connection&id=%d&depth=2", ids["conn"]),
				fmt.Sprintf("?type=notebook&id=%d", ids["nb"]),
				fmt.Sprintf("?type=group&id=%d&direction=downstream", ids["group"]),
				fmt.Sprintf("?type=datasource&id=%d", ids["secret"]),
				"?type=sql&id=1&depth=x", "?type=script&id=99999999999999999999", "?direction=both&depth=8",
			} {
				add("the graph "+q, q, "admin")
				add("the graph "+q, q, "editor")
			}
			ol := func(name, flow, p, method string) {
				var auth func(*http.Request)
				if p != "nobody" {
					auth = w.As(p)
				}
				s = append(s, Step{Name: "OpenLineage: " + name + " · " + p, Method: method,
					Path: fmt.Sprintf("/flows/%s/openlineage", flow), Auth: auth})
			}
			for _, p := range []string{"admin", "editor", "obs_flag", "nobody"} {
				ol("the main flow", fmt.Sprint(ids["main"]), p, "GET")
				ol("emit, not configured", fmt.Sprint(ids["main"]), p, "POST")
			}
			ol("a definition not JSON", fmt.Sprint(ids["sub"]), "admin", "GET")
			ol("a hidden flow", fmt.Sprint(ids["hiddenflow"]), "editor", "GET")
			ol("the reader", fmt.Sprint(ids["reader"]), "editor", "GET")
			return s
		},
	})
}

// the static lineage sent to a collector: run it with DIFF_ENV="OPENLINEAGE__URL=http://siem.test:8000"
// (the fake engine is the collector).
func init() {
	register(Suite{
		Module: "lineage_x_emit",
		Asked:  true,
		Steps: func(w *World) []Step {
			var flow int64
			_ = w.a.db.QueryRow(w.ctx, `SELECT id FROM flows WHERE name = 'lr reader'`).Scan(&flow)
			path := fmt.Sprintf("/flows/%d/openlineage", flow)
			emit := func(name string, rules ...EngineRule) Step {
				if rules == nil {
					rules = []EngineRule{}
				}
				return Step{Name: "emit: " + name, Method: "POST", Path: path, Auth: w.As("editor"), Engine: rules}
			}
			return []Step{
				emit("taken"),
				emit("refused", EngineRule{Method: "POST", Path: "^/api/v1/lineage$", Status: 400, Body: map[string]any{"x": 1}}),
				emit("an error that is retried", EngineRule{Method: "POST", Path: "^/api/v1/lineage$", Status: 502}),
				emit("an error that is not", EngineRule{Method: "POST", Path: "^/api/v1/lineage$", Status: 501}),
				{Name: "emit: not found", Method: "POST", Path: "/flows/999999/openlineage", Auth: w.As("admin")},
			}
		},
	})
}
