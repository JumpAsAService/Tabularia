package main

import (
	"fmt"
	"net/http"
	"time"
)

// routes/sql_scripts.py: the lists, the catalog, a script and its versions, a copy, deleting
// one, restoring a version; and the routes still forwarded (run, export, refresh, schedule,
// create, change), through the Go gateway.
func init() {
	ids := map[string]int64{}
	register(Suite{
		Module: "sql_scripts",
		Seed: func(w *World) {
			ids = map[string]int64{}
			folder := func(key, name string, parent *int64) int64 {
				ids[key] = w.Insert(`INSERT INTO projects (name, description, parent_id, created_at) VALUES ($1, '', $2, '2026-01-01')
					RETURNING id`, name, parent)
				return ids[key]
			}
			f1 := folder("f1", "sq-root", nil)
			folder("f2", "sq-edit", &f1)
			folder("f3", "sq-run", &f1)
			f4 := folder("f4", "sq-hidden", nil)
			folder("f5", "sq-conn", nil)
			folder("f6", "sq-edit", &f4) // a second folder of the same name
			grant := func(folder, capability string, user, group *int64) {
				w.Exec(`INSERT INTO permissions (project_id, user_id, group_id, capability) VALUES ($1, $2, $3, $4)`,
					ids[folder], user, group, capability)
			}
			person := func(p string) *int64 { v := w.People[p]; return &v }
			group := func(g string) *int64 { v := Groups[g]; return &v }
			grant("f1", "view", nil, group("Analysts"))
			grant("f2", "edit", person("writer"), nil)
			grant("f3", "view", person("writer"), nil)
			grant("f5", "connect", person("writer"), nil)
			grant("f3", "edit", person("editor"), nil)
			grant("f2", "run", person("editor"), nil)
			grant("f2", "view", person("obs_flag"), nil)
			grant("f1", "view", person("service"), nil)
			grant("f6", "view", person("sso"), nil)

			ds := func(key, name, folder, dsKey string, rows any, columns, kind string) {
				ids[key] = w.Insert(`INSERT INTO datasources (name, description, project_id, bucket, key, rows, columns, sort_keys,
					column_descriptions, kind, created_at, updated_at) VALUES ($1, '', $2, 'data-prep', $3, $4, $5, '[]', '{}', $6,
					'2026-01-02', '2026-01-03 10:00:00.123456') RETURNING id`, name, ids[folder], dsKey, rows, columns, kind)
			}
			ds("d1", "Vendite", "f2", "datasets/1.parquet", 10,
				`[{"name": "a", "dtype": "Int64"}, {"name": "b"}, 5, {"dtype": "Utf8"}, {"name": null, "dtype": 1.5}]`, "flow")
			ds("d2", "Clienti", "f1", "", nil, "not json", "flow")
			ds("d3", "Segreto", "f4", "datasets/3.parquet", 3, `{"a": 1}`, "database")
			ds("d4", "Vendite", "f3", "datasets/4.parquet", 0, "[]", "flow")
			ds("d5", "Uscite", "f2", "datasets/5.parquet", 1, "[]", "database")
			ds("d6", "Ombra", "f6", "datasets/6.parquet", 1, "", "flow")

			conn := func(key, name, folder, dbType string) {
				ids[key] = w.Insert(`INSERT INTO connections (name, description, project_id, db_type, host, username, password_encrypted,
					database, db_schema, extra, created_at, updated_at) VALUES ($1, '', $2, $3, 'db.test', '', '', '', '', '{}',
					'2026-01-01', '2026-01-01') RETURNING id`, name, ids[folder], dbType)
			}
			conn("c1", "pg-main", "f5", "postgresql")
			conn("c2", "mail", "f5", "smtp")
			conn("c3", "my-hidden", "f4", "mysql")

			script := func(key, name, folder string, current int, versions ...string) {
				ids[key] = w.Insert(`INSERT INTO sql_scripts (name, description, project_id, owner_id, current_version, created_at, updated_at)
					VALUES ($1, 'about ' || $1::varchar, $2, $3, $4, '2026-02-01 08:00:00', '2026-02-02 09:30:00.5') RETURNING id`,
					name, ids[folder], w.People["admin"], current)
				for i, content := range versions {
					if content == "-" {
						continue
					}
					w.Exec(`INSERT INTO sql_script_versions (script_id, version, content, note, created_at, created_by)
						VALUES ($1, $2, $3, $4, '2026-02-01 08:00:00', $5)`, ids[key], i+1, content, fmt.Sprintf("note %d", i+1),
						w.People[[]string{"admin", "writer", "editor"}[i%3]])
				}
			}
			out := func(name, folder string) string {
				return fmt.Sprintf(`{"name": %q, "project_id": %d}`, name, ids[folder])
			}
			script("s1", "Alpha", "f2", 2, `{"sql": "SELECT 1", "engine": "duckdb", "connection_id": null, "output": null}`,
				fmt.Sprintf(`{"sql": "SELECT * FROM Vendite", "engine": null, "connection_id": %d, "output": %s}`, ids["c1"], out("Vendite", "f2")))
			script("s2", "Beta", "f1", 1, fmt.Sprintf(`{"sql": "select 2", "engine": "polars", "output": {"name": "Segreto", "project_id": %d, "extra": 1.0}, "note": "café", "n": 1e20}`, ids["f4"]))
			script("s3", "Gamma", "f4", 1, fmt.Sprintf(`{"sql": "SELECT 3", "connection_id": %d}`, ids["c3"]))
			script("s4", "Delta", "f3", 1, `{`)
			script("s5", "Epsilon", "f2", 3, `{"sql": "a"}`, `{"sql": "b"}`)
			script("s6", "Alpha", "f3", 1, fmt.Sprintf(`{"sql": "SELECT 6", "connection_id": %d}`, ids["c2"]))
			script("s7", "Zeta", "f2", 2, fmt.Sprintf(`{"sql": "SELECT 1", "output": %s}`, out("Nuovo", "f2")), `{"sql": "SELECT 2", "output": null}`)
			script("s8", "Eta", "f2", 2, `{"sql": "SELECT 1"}`, fmt.Sprintf(`{"sql": "SELECT 1", "output": %s}`, out("Eta", "f2")))
			script("s9", "Theta", "f2", 2, `{"sql": "SELECT 9 -- old"}`, `{"sql": "SELECT 9"}`)
			script("s10", "Iota", "f2", 1, `{"sql": "SELECT 10"}`)
			script("s11", "Kappa", "f2", 1, `{"sql": "SELECT 11"}`)
			script("s12", "Lambda", "f2", 1, `{"sql": "SELECT 12"}`)
			script("s13", "Mu", "f2", 1, `{"sql": "SELECT 13"}`)
			script("s14", "Nu", "f2", 2, fmt.Sprintf(`{"sql": "SELECT 14", "output": %s}`, out("X", "f4")), `{"sql": "SELECT 14"}`)
			script("s15", "Xi", "f2", 2, fmt.Sprintf(`{"sql": "SELECT 15", "connection_id": %d}`, ids["c3"]), `{"sql": "SELECT 15"}`)
			script("s16", "Omicron", "f2", 2, `{"sql": 5}`, `{"sql": "SELECT 16"}`)
			script("s17", "Pi", "f2", 2, `{"sql": "SELECT 17", "output": {"name": "Y", "project_id": 999999}}`, `{"sql": "SELECT 17"}`)
			script("s18", "Rho", "f2", 2, fmt.Sprintf(`{"sql": "SELECT 18", "output": %s}`, out("Uscite", "f2")), `{"sql": "SELECT 18"}`)
			script("s19", "Sigma", "f2", 2, `{"sql": "SELECT 19", "engine": "spark"}`, `{"sql": "SELECT 19"}`)
			script("s20", "Tau", "f2", 3, fmt.Sprintf(`{"sql": "SELECT 20", "connection_id": %d, "engine": "duckdb", "keep": [1, 2.5, "è"]}`, ids["c1"]),
				`{"sql": "x", "output": {"name": "  ", "project_id": 1}}`, `{"sql": "SELECT 20"}`)
			script("s21", "Upsilon", "f2", 2, `{"sql": "SELECT 21", "engine": "", "connection_id": true}`, `{"sql": "SELECT 21"}`)
			script("s22", "Phi", "f2", 2, `{"sql": "SELECT 22", "output": "Vendite"}`, `{"sql": "SELECT 22"}`)
			w.Exec(`UPDATE sql_scripts SET run_schedule = '0 * * * *', run_scheduled_by = $1, next_run_at = '2026-03-01 10:00:00'
				WHERE id = $2`, w.People["admin"], ids["s1"])
			w.Exec(`UPDATE sql_scripts SET run_schedule = '5 * * * *', run_scheduled_by = $1, next_run_at = '2026-03-01 10:05:00'
				WHERE id = ANY($2)`, w.People["editor"], []int64{ids["s9"], ids["s20"]})
			w.Exec(`UPDATE sql_scripts SET run_schedule = '7 * * * *', run_scheduled_by = $1 WHERE id = $2`, w.People["writer"], ids["s7"])

			run := func(script, status string) {
				w.Exec(`INSERT INTO runs (kind, task_id, status, launched_by, trigger_type, input_key, output_bucket, output_key,
					publish_description, publish_overwrite, publish_sort_keys, started_at, sql_script_id)
					VALUES ('sql', 't', $1, $2, 'manual', '', '', '', '', true, '[]', '2026-02-03', $3)`, status, w.People["writer"], ids[script])
			}
			w.Exec(`INSERT INTO runs (kind, task_id, status, launched_by, trigger_type, input_key, output_bucket, output_key,
				publish_description, publish_overwrite, publish_sort_keys, started_at, finished_at, sql_script_id, datasource_id,
				rows_written, error, parent_run_id) VALUES
				('sql', 't1', 'SUCCESS', $1, 'manual', '', '', '', '', true, '[]', '2026-02-03 10:00:00', '2026-02-03 10:01:00.25', $2, $3, 10, NULL, NULL),
				('sql', 't2', 'FAILURE', NULL, 'schedule', '', '', '', '', true, '[]', '2026-02-04', NULL, $2, NULL, NULL, 'boom', NULL),
				('sql', 't3', 'SUCCESS', $4, 'group', '', '', '', '', true, '[]', '2026-02-05', '2026-02-05', $2, $5, 3, NULL, 1),
				('flow', 't4', 'SUCCESS', $4, 'manual', '', '', '', '', true, '[]', '2026-02-05', '2026-02-05', $2, NULL, 3, NULL, NULL)`,
				w.People["writer"], ids["s1"], ids["d1"], w.People["admin"], ids["d3"])
			run("s12", "STARTED")
			run("s13", "SUCCESS")
			run("s13", "FAILURE")

			flowGroup := func(key, name, definition string, by string) {
				var who *int64
				var cron *string
				if by != "" {
					who, cron = person(by), ptr("0 1 * * *")
				}
				ids[key] = w.Insert(`INSERT INTO flow_groups (name, description, project_id, definition, run_schedule, run_scheduled_by,
					next_run_at, created_at, updated_at) VALUES ($1, '', $2, $3, $4, $5, CASE WHEN $5::int IS NULL THEN NULL
					ELSE '2026-03-02 01:00:00'::timestamp END, '2026-01-01', '2026-01-01') RETURNING id`, name, ids["f2"], definition, cron, who)
			}
			report := func(key, table, name, folder, content string) {
				fk := map[string]string{"reports": "report_id", "dashboards": "dashboard_id"}[table]
				ids[key] = w.Insert(fmt.Sprintf(`INSERT INTO %s (name, description, project_id, current_version, created_at, updated_at)
					VALUES ($1, '', $2, 1, '2026-01-01', '2026-01-01') RETURNING id`, table), name, ids[folder])
				w.Exec(fmt.Sprintf(`INSERT INTO %s (%s, version, content, note, created_at) VALUES ($1, 1, $2, '', '2026-01-01')`,
					table[:len(table)-1]+"_versions", fk), ids[key], content)
			}
			reads := func(script string) string {
				return fmt.Sprintf(`{"datasets": [{"name": "x", "source": {"kind": "datasource", "datasource_id": 1}},
					{"name": "y", "source": {"kind": "sql_script", "script_id": %d}}]}`, ids[script])
			}
			report("r1", "reports", "sq-r1", "f2", reads("s9"))
			report("r2", "reports", "sq-r2", "f4", reads("s10"))
			report("r3", "reports", "sq-r3", "f2", `{"datasets": "script_id"`)
			report("db1", "dashboards", "sq-db1", "f2", reads("s9"))
			report("db2", "dashboards", "sq-db2", "f2", reads("s11"))
			report("db3", "dashboards", "sq-db3", "f4", reads("s11"))
			flowGroup("g1", "sq-g1", fmt.Sprintf(`{"steps": [[{"kind": "sql", "id": %d}, null], [{"kind": "flow", "id": 1}]]}`, ids["s8"]), "")
			flowGroup("g2", "sq-g2", fmt.Sprintf(`{"steps": [[{"kind": "sql", "id": %d}]]}`, ids["s9"]), "admin")
			flowGroup("g3", "sq-g3", fmt.Sprintf(`{"steps": [[{"kind": "pushmail", "id": 1, "pushmail": {"reports": [{"reportId": %d}],
				"dashboards": []}}]]}`, ids["r1"]), "editor")
			flowGroup("g4", "sq-g4", fmt.Sprintf(`{"steps": [[{"kind": "pushmail", "id": 2, "pushmail": {"reports": [],
				"dashboards": [{"dashboardId": %d}]}}]]}`, ids["db1"]), "writer")
			flowGroup("g5", "sq-g5", `{"steps": "sql"}`, "admin")
			flowGroup("g6", "sq-g6", fmt.Sprintf(`{"steps": [[{"kind": "sql", "id": %d}], [{"kind": "pushmail", "id": 3, "pushmail":
				{"dashboards": [{"dashboardId": %d}]}}]]}`, ids["s9"], ids["db1"]), "obs_flag")

			w.Token("writer", "tab_sq_writer_read_000000000", "read", time.Hour)
			w.Token("writer", "tab_sq_writer_write_00000000", "write", time.Hour)
		},
		Steps: func(w *World) []Step {
			var s []Step
			id := func(k string) int64 { return ids[k] }
			add := func(name, method, path string, auth func(*http.Request), body any) {
				s = append(s, Step{Name: name, Method: method, Path: path, Auth: auth, JSON: body})
			}
			readers := []string{"admin", "editor", "writer", "obs_flag", "obs_group", "admin_group", "service", "sso"}
			few := []string{"admin", "editor", "writer", "obs_group"}
			as := func(p string) func(*http.Request) {
				if p == "nobody" {
					return nil
				}
				return w.As(p)
			}
			for _, p := range append(readers, "inactive", "nobody") {
				add(p+" lists the scripts", "GET", "/sql-scripts", as(p), nil)
				// ordered by name only too: two scripts of one name, in the table's physical order
				s[len(s)-1].Unordered = []string{""}
				// ordered by name only: two datasources with one name (notebook_data's «nd-twin»)
				// come in the table's physical order, which the two databases need not share
				s = append(s, Step{Name: p + " reads the catalog", Method: "GET", Path: "/sql-scripts/catalog", Auth: as(p), Unordered: []string{""}})
			}
			for _, p := range readers {
				for _, f := range []string{"f1", "f2", "f3", "f4", "f5", "f6"} {
					add(p+" lists a folder", "GET", fmt.Sprintf("/projects/%d/sql-scripts", id(f)), as(p), nil)
				}
			}
			for _, path := range []string{"/projects/999999/sql-scripts", "/projects/abc/sql-scripts", "/projects/%20%2B5.0/sql-scripts",
				"/projects/1_0/sql-scripts", "/projects/1e3/sql-scripts", "/projects/99999999999999999999/sql-scripts"} {
				add("a folder's odd id", "GET", path, as("writer"), nil)
				add("a folder's odd id, nobody", "GET", path, nil, nil)
			}
			scripts := []string{"s1", "s2", "s3", "s4", "s5", "s6", "s7", "s20", "s21", "s22"}
			for _, p := range append(few, "obs_flag", "sso", "nobody") {
				for _, k := range scripts {
					add(p+" opens "+k, "GET", fmt.Sprintf("/sql-scripts/%d", id(k)), as(p), nil)
				}
				add(p+" opens one that does not exist", "GET", "/sql-scripts/999999", as(p), nil)
				add(p+" opens a script by a word", "GET", "/sql-scripts/abc", as(p), nil)
				add(p+" lists s1's versions", "GET", fmt.Sprintf("/sql-scripts/%d/versions", id("s1")), as(p), nil)
				add(p+" lists s4's versions", "GET", fmt.Sprintf("/sql-scripts/%d/versions", id("s4")), as(p), nil)
				add(p+" lists s3's versions", "GET", fmt.Sprintf("/sql-scripts/%d/versions", id("s3")), as(p), nil)
				for _, v := range []string{"1", "2", "9", "abc"} {
					add(p+" reads s1's version "+v, "GET", fmt.Sprintf("/sql-scripts/%d/versions/%s", id("s1"), v), as(p), nil)
				}
				add(p+" reads s2's version", "GET", fmt.Sprintf("/sql-scripts/%d/versions/1", id("s2")), as(p), nil)
				add(p+" reads s4's broken version", "GET", fmt.Sprintf("/sql-scripts/%d/versions/1", id("s4")), as(p), nil)
				add(p+" reads two bad ids", "GET", "/sql-scripts/x/versions/y", as(p), nil)
			}
			add("versions of a script that does not exist", "GET", "/sql-scripts/999999/versions", as("admin"), nil)
			add("a read token reads", "GET", fmt.Sprintf("/sql-scripts/%d", id("s1")), Bearer("tab_sq_writer_read_000000000"), nil)

			// copies
			dup := func(name, key, p string, auth func(*http.Request)) {
				if auth == nil && p != "nobody" {
					auth = as(p)
				}
				add(name, "POST", fmt.Sprintf("/sql-scripts/%d/duplicate", id(key)), auth, nil)
			}
			dup("writer copies Alpha", "s1", "writer", nil)
			dup("writer copies Alpha again", "s1", "writer", nil)
			dup("writer copies Epsilon (no current version)", "s5", "writer", nil)
			dup("writer copies Beta (VIEW only)", "s2", "writer", nil)
			dup("editor copies Delta (broken content)", "s4", "editor", nil)
			dup("editor copies Alpha (RUN only)", "s1", "editor", nil)
			dup("obs_group copies Alpha", "s1", "obs_group", nil)
			dup("nobody copies Alpha", "s1", "nobody", nil)
			dup("a read token copies", "s1", "", Bearer("tab_sq_writer_read_000000000"))
			dup("a write token copies", "s20", "", Bearer("tab_sq_writer_write_00000000"))
			dup("admin copies Gamma", "s3", "admin", nil)
			add("a copy of nothing", "POST", "/sql-scripts/999999/duplicate", as("admin"), nil)
			add("a copy of a word", "POST", "/sql-scripts/abc/duplicate", as("admin"), nil)
			add("the list after the copies", "GET", "/sql-scripts", as("admin"), nil)
			add("a cookie without the CSRF header copies", "POST", fmt.Sprintf("/sql-scripts/%d/duplicate", id("s1")),
				Cookie(w.JWT("writer", "s-writer")), nil)

			// restores
			restore := func(name, key, version, p string) {
				add(name, "POST", fmt.Sprintf("/sql-scripts/%d/versions/%s/restore", id(key), version), as(p), nil)
			}
			restore("editor restores Alpha (RUN only)", "s1", "1", "editor")
			restore("obs_group restores Alpha", "s1", "1", "obs_group")
			restore("nobody restores Alpha", "s1", "1", "nobody")
			restore("writer restores a version that does not exist", "s1", "9", "writer")
			restore("writer restores by words", "s1", "x", "writer")
			restore("writer restores the current version", "s1", "2", "writer")
			restore("writer restores Alpha v1: its schedule goes", "s1", "1", "writer")
			restore("writer restores Alpha v2", "s1", "2", "writer")
			restore("writer restores Eta v1: a group writes its datasource", "s8", "1", "writer")
			restore("writer restores Theta v1: groups go", "s9", "1", "writer")
			restore("writer restores Nu v1: a folder not theirs", "s14", "1", "writer")
			restore("writer restores Xi v1: a connection not theirs", "s15", "1", "writer")
			restore("writer restores Omicron v1: SQL not a text", "s16", "1", "writer")
			restore("writer restores Pi v1: a folder gone", "s17", "1", "writer")
			restore("writer restores Rho v1: a database's datasource", "s18", "1", "writer")
			restore("writer restores Sigma v1: an unknown engine", "s19", "1", "writer")
			restore("writer restores Tau v2: a datasource without a name", "s20", "2", "writer")
			restore("writer restores Tau v1: a connection theirs, extra keys", "s20", "1", "writer")
			restore("writer restores Upsilon v1: a connection true", "s21", "1", "writer")
			restore("writer restores Phi v1: an output that is a text", "s22", "1", "writer")
			restore("writer restores Zeta v1: a datasource of theirs", "s7", "1", "writer")
			restore("admin restores Xi v1", "s15", "1", "admin")
			restore("admin restores Nu v1", "s14", "1", "admin")
			restore("editor restores Delta (no such version)", "s4", "2", "editor")
			restore("editor restores Epsilon v1 (current missing)", "s5", "1", "editor")
			restore("writer restores Epsilon v1 (current missing)", "s5", "1", "writer")
			add("the groups after the restores", "GET", "/sql-scripts", as("admin"), nil)

			// deletes
			del := func(name, key, p string) {
				add(name, "DELETE", fmt.Sprintf("/sql-scripts/%d", id(key)), as(p), nil)
			}
			del("editor deletes Alpha (RUN only)", "s1", "editor")
			del("obs_group deletes Alpha", "s1", "obs_group")
			del("nobody deletes Alpha", "s1", "nobody")
			del("writer deletes Lambda while it writes", "s12", "writer")
			del("writer deletes Eta, a group runs it", "s8", "writer")
			del("writer deletes Iota, a report they cannot see reads it", "s10", "writer")
			del("admin deletes Iota, a report reads it", "s10", "admin")
			del("writer deletes Kappa, dashboards read it", "s11", "writer")
			del("admin deletes Kappa", "s11", "admin")
			del("writer deletes Mu", "s13", "writer")
			del("writer deletes Mu again", "s13", "writer")
			del("writer deletes Theta, a group runs it", "s9", "writer")
			add("a delete of a word", "DELETE", "/sql-scripts/abc", as("admin"), nil)
			add("a delete by a read token", "DELETE", fmt.Sprintf("/sql-scripts/%d", id("s7")), Bearer("tab_sq_writer_read_000000000"), nil)
			add("a delete by a write token", "DELETE", fmt.Sprintf("/sql-scripts/%d", id("s7")), Bearer("tab_sq_writer_write_00000000"), nil)

			// the writes of the datasource
			for _, p := range []string{"admin", "writer", "editor", "obs_group", "nobody"} {
				add(p+" lists Alpha's runs", "GET", fmt.Sprintf("/sql-scripts/%d/runs", id("s1")), as(p), nil)
			}
			for _, q := range []string{"?limit=1", "?limit=0", "?limit=101", "?limit=x", "?limit=2&limit=3", "?limit=", "?other=1"} {
				add("Alpha's runs "+q, "GET", fmt.Sprintf("/sql-scripts/%d/runs%s", id("s1"), q), as("writer"), nil)
			}
			add("runs by words", "GET", "/sql-scripts/x/runs?limit=0", as("writer"), nil)
			add("runs of a script being written (forwarded)", "GET", fmt.Sprintf("/sql-scripts/%d/runs", id("s12")), as("writer"), nil)

			// create
			create := func(name, folder, p string, body any) {
				s = append(s, Step{Name: name, Method: "POST", Path: fmt.Sprintf("/projects/%v/sql-scripts", folder), Auth: as(p), JSON: body})
			}
			f2 := id("f2")
			create("writer creates one", fmt.Sprint(f2), "writer", map[string]any{"name": "  New one ", "description": " d ",
				"content": map[string]any{"sql": "SELECT 1", "engine": "duckdb", "x": 1.5}})
			create("writer creates it again", fmt.Sprint(f2), "writer", map[string]any{"name": "New one"})
			create("writer creates one without content", fmt.Sprint(f2), "writer", map[string]any{"name": "Empty one"})
			create("writer creates one on a connection", fmt.Sprint(f2), "writer", map[string]any{"name": "On pg",
				"content": map[string]any{"sql": "SELECT 1", "engine": "duckdb", "connection_id": id("c1"),
					"output": map[string]any{"name": " Out ", "project_id": f2}}})
			create("writer creates one on a connection not theirs", fmt.Sprint(f2), "writer", map[string]any{"name": "On my",
				"content": map[string]any{"connection_id": id("c3")}})
			create("writer creates one on a mail connection", fmt.Sprint(f2), "writer", map[string]any{"name": "On mail",
				"content": map[string]any{"connection_id": id("c2")}})
			create("writer creates one writing elsewhere", fmt.Sprint(f2), "writer", map[string]any{"name": "Out there",
				"content": map[string]any{"output": map[string]any{"name": "Z", "project_id": id("f4")}}})
			create("writer creates one with a bad engine", fmt.Sprint(f2), "writer", map[string]any{"name": "Bad",
				"content": map[string]any{"engine": []any{1, "a"}}})
			create("writer creates one with a blank name", fmt.Sprint(f2), "writer", map[string]any{"name": "   "})
			create("writer creates one with no name", fmt.Sprint(f2), "writer", map[string]any{})
			create("writer creates one with a long name", fmt.Sprint(f2), "writer", map[string]any{"name": string(make([]byte, 201))})
			create("writer creates one with a content that is a list", fmt.Sprint(f2), "writer", map[string]any{"name": "L", "content": []any{1}})
			create("writer creates one in a folder of VIEW", fmt.Sprint(id("f3")), "writer", map[string]any{"name": "V"})
			create("writer creates one in no folder", "999999", "writer", map[string]any{"name": "V"})
			create("writer creates one in a word", "abc", "writer", map[string]any{"name": 5})
			create("nobody creates one", fmt.Sprint(f2), "nobody", map[string]any{"name": "N"})
			s = append(s, Step{Name: "broken JSON, nobody", Method: "POST", Path: fmt.Sprintf("/projects/%d/sql-scripts", f2),
				Raw: []byte(`{"name": `), ContentType: "application/json"})
			s = append(s, Step{Name: "no body", Method: "POST", Path: fmt.Sprintf("/projects/%d/sql-scripts", f2), Auth: as("writer")})
			s = append(s, Step{Name: "a text body", Method: "POST", Path: fmt.Sprintf("/projects/%d/sql-scripts", f2), Auth: as("writer"),
				Raw: []byte(`name=x`), ContentType: "text/plain"})

			// change
			patch := func(name, key, p string, body any) {
				s = append(s, Step{Name: name, Method: "PATCH", Path: fmt.Sprintf("/sql-scripts/%v", key), Auth: as(p), JSON: body})
			}
			sid := func(k string) string { return fmt.Sprint(id(k)) }
			patch("writer changes nothing", sid("s7"), "writer", map[string]any{})
			patch("writer changes Zeta's description", sid("s7"), "writer", map[string]any{"description": " new "})
			patch("writer renames Zeta onto Theta", sid("s7"), "writer", map[string]any{"name": "Theta"})
			patch("writer renames Zeta", sid("s7"), "writer", map[string]any{"name": "Zeta 2"})
			patch("writer moves Zeta to a folder of VIEW", sid("s7"), "writer", map[string]any{"project_id": id("f3")})
			patch("writer moves Zeta nowhere", sid("s7"), "writer", map[string]any{"project_id": 999999})
			patch("editor moves Delta to sq-edit (RUN only there)", sid("s4"), "editor", map[string]any{"project_id": f2})
			patch("admin moves Zeta to sq-run", sid("s7"), "admin", map[string]any{"project_id": id("f3"), "name": "Zeta"})
			patch("writer changes Theta's SQL: schedules go", sid("s9"), "writer", map[string]any{"content": map[string]any{"sql": "SELECT 99"}})
			patch("writer gives Tau the same content", sid("s20"), "writer", map[string]any{"content": map[string]any{"sql": "SELECT 20"}})
			patch("writer drops Eta's datasource: a group writes it", sid("s8"), "writer", map[string]any{"content": map[string]any{"output": nil}})
			patch("writer points Alpha at a connection not theirs", sid("s1"), "writer", map[string]any{"content": map[string]any{"connection_id": id("c3")}})
			patch("writer changes Alpha's SQL only", sid("s1"), "writer", map[string]any{"content": map[string]any{"sql": "SELECT 2"}, "description": "x"})
			patch("writer changes Alpha with a bad SQL", sid("s1"), "writer", map[string]any{"content": map[string]any{"sql": nil}})
			patch("writer changes a script they cannot see", sid("s3"), "writer", map[string]any{"name": "G"})
			patch("writer changes with bad fields", "abc", "writer", map[string]any{"name": 5, "project_id": "x", "content": 3})
			patch("nobody changes Alpha", sid("s1"), "nobody", map[string]any{"name": "N"})
			patch("editor renames Alpha (RUN only)", sid("s1"), "editor", map[string]any{"name": "N"})
			add("the list after the changes", "GET", "/sql-scripts", as("admin"), nil)
			// ordered by name only: the two «Alpha» in the table's physical order, as above
			s[len(s)-1].Unordered = []string{""}

			// the schedule: a clear here, a cron forwarded (croniter)
			sched := func(name, key, p string, body any) {
				add(name, "PUT", fmt.Sprintf("/sql-scripts/%v/schedule", key), as(p), body)
			}
			sched("writer clears Tau's schedule", sid("s20"), "writer", map[string]any{"cron": "  "})
			sched("writer clears it again", sid("s20"), "writer", map[string]any{})
			sched("editor clears Alpha's (RUN)", sid("s1"), "editor", map[string]any{"cron": nil})
			sched("obs_flag clears (VIEW)", sid("s1"), "obs_flag", map[string]any{})
			sched("a clear by words", "x", "writer", map[string]any{"cron": 5})
			sched("nobody clears", sid("s1"), "nobody", map[string]any{})
			sched("writer sets a cron (forwarded)", sid("s20"), "writer", map[string]any{"cron": "0 3 * * *"})
			sched("writer sets a bad cron (forwarded)", sid("s20"), "writer", map[string]any{"cron": "x"})

			// still forwarded to Python
			add("forwarded: schedule", "PUT", fmt.Sprintf("/sql-scripts/%d/schedule", id("s7")), as("writer"), map[string]any{"cron": ""})
			add("forwarded: runs", "GET", fmt.Sprintf("/sql-scripts/%d/runs?limit=5", id("s13")), as("writer"), nil)
			s = append(s, Step{Name: "forwarded: run on a connection", Method: "POST", Path: "/sql-scripts/run", Auth: as("writer"),
				JSON: map[string]any{"sql": "SELECT 1", "connection_id": id("c1")}, Mask: []string{"elapsed_ms"}},
				Step{Name: "forwarded: run on the catalog", Method: "POST", Path: "/sql-scripts/run", Auth: as("writer"),
					JSON: map[string]any{"sql": "SELECT * FROM Vendite"}, Mask: []string{"elapsed_ms"}})
			add("forwarded: a POST on the catalog's path", "POST", "/sql-scripts/catalog", as("writer"), nil)
			add("forwarded: a DELETE on the catalog's path", "DELETE", "/sql-scripts/catalog", as("writer"), nil)
			return s
		},
	})
}
