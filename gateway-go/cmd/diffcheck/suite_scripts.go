package main

import (
	"fmt"
	"net/http"
	"strings"
)

// routes/scripts.py: saved scripts. Without SCRIPTS__ENABLED (the default stack) the lists
// are empty and the writes answer 409; with DIFF_ENV="SCRIPTS__ENABLED=true
// SCRIPTS__NAMESPACE=scripts SCRIPTS__REGISTRY=registry.test/tab" every branch runs.
func init() {
	ids := map[string]int64{}
	register(Suite{
		Module: "scripts",
		Seed: func(w *World) {
			ids = map[string]int64{}
			folder := func(key, name string, parent *int64) int64 {
				ids[key] = w.Insert(`INSERT INTO projects (name, description, parent_id, created_at) VALUES ($1, '', $2, '2026-01-01')
					RETURNING id`, name, parent)
				return ids[key]
			}
			g1 := folder("g1", "ss-root", nil)
			folder("g2", "ss-edit", &g1)
			folder("g3", "ss-hidden", nil)
			grant := func(folder, capability, person string) {
				w.Exec(`INSERT INTO permissions (project_id, user_id, capability) VALUES ($1, $2, $3)`, ids[folder], w.People[person], capability)
			}
			grant("g2", "edit", "writer")
			grant("g1", "view", "editor")
			grant("g2", "run", "editor")
			grant("g2", "view", "obs_flag")
			grant("g1", "view", "sso")
			coders := w.Insert(`INSERT INTO groups (name, description, is_admin, is_observer, created_at) VALUES ('ss-coders', '', false, false,
				'2026-01-01') RETURNING id`)
			w.Exec(`INSERT INTO user_groups (user_id, group_id) VALUES ($1, $2), ($3, $2)`, w.People["writer"], coders, w.People["obs_flag"])
			env := func(key, name, language string, max int, groups ...int64) {
				ids[key] = w.Insert(`INSERT INTO script_environments (name, description, language, enabled, cpu_request, cpu_limit,
					memory_request, memory_limit, max_timeout_seconds, created_at, updated_at) VALUES ($1, 'd', $2, true, '500m', '2',
					'1Gi', '4Gi', $3, '2026-01-01', '2026-01-01') RETURNING id`, name, language, max)
				for _, g := range groups {
					w.Exec(`INSERT INTO script_environment_groups (environment_id, group_id) VALUES ($1, $2)`, ids[key], g)
				}
			}
			env("e1", "ss-py", "python", 600, coders, Groups["Analysts"])
			env("e2", "ss-r", "r", 3600, Groups["Sales"])
			env("e3", "ss-closed", "python", 3600)
			w.Exec(`INSERT INTO script_storage (id, endpoint, region, bucket, access_key) VALUES (1, '', '', ' scripts-bkt ', '')
				ON CONFLICT (id) DO UPDATE SET bucket = ' scripts-bkt '`)
			ds := func(key, name, folder, dsKey, columns string) {
				ids[key] = w.Insert(`INSERT INTO datasources (name, description, project_id, bucket, key, columns, sort_keys,
					column_descriptions, kind, created_at, updated_at) VALUES ($1, '', $2, 'data-prep', $3, $4, '[]', '{}', 'flow',
					'2026-01-02', '2026-01-03') RETURNING id`, name, ids[folder], dsKey, columns)
			}
			ds("d1", "ss-sales", "g2", "datasets/s1.parquet", `[{"name": "a"}, {"name": "b"}, 3]`)
			ds("d2", "ss-secret", "g3", "datasets/s2.parquet", "[]")
			ds("d3", "ss-empty", "g2", "", "not json")
			ds("d4", "ss-odd", "g1", "k", `[{"name": 5}]`)
			script := func(key, name, folder, environment string, current int, versions ...string) {
				ids[key] = w.Insert(`INSERT INTO saved_scripts (name, description, project_id, owner_id, environment_id, current_version,
					created_at, updated_at) VALUES ($1, 'about', $2, $3, $4, $5, '2026-02-01', '2026-02-02 10:00:00.75') RETURNING id`,
					name, ids[folder], w.People["admin"], ids[environment], current)
				for i, c := range versions {
					w.Exec(`INSERT INTO saved_script_versions (script_id, version, content, note, created_at, created_by)
						VALUES ($1, $2, $3, 'n', '2026-02-01', $4)`, ids[key], i+1, c, w.People[[]string{"admin", "writer"}[i%2]])
				}
			}
			code := func(body string) string {
				return fmt.Sprintf(`{"files": [{"path": "main.py", "content": %q}], "entrypoint": "main.py"}`, body)
			}
			script("x1", "Main", "g2", "e1", 2, `{"files": []}`, fmt.Sprintf(`{"files": [{"path": "main.py", "content": "print(1)"}],
				"entrypoint": "main.py", "inputs": [{"datasource_id": %d, "alias": "A"}, {"datasource_id": %d, "alias": "B"},
				{"datasource_id": 999999, "alias": "C"}, {"datasource_id": %d, "alias": "D"}, {"datasource_id": "x", "alias": 5}],
				"output": {"publish": false, "name": "", "project_id": null, "overwrite": true}, "extra": 1.5}`, ids["d1"], ids["d2"], ids["d3"]))
			script("x2", "Rscript", "g2", "e2", 1, `{"files": [{"path": "main.R", "content": "1"}], "entrypoint": "main.R"}`)
			script("x3", "Hidden", "g3", "e1", 1, code("x"))
			script("x4", "Broken", "g2", "e1", 1, `{`)
			script("x5", "UsedFlow", "g2", "e1", 1, code("5"))
			script("x6", "Running", "g2", "e1", 1, code("6"))
			script("x7", "Deletable", "g2", "e1", 1, code("7"))
			script("x8", "Restorable", "g2", "e1", 2, code("A"), code("B"))
			script("x9", "BadV1", "g2", "e1", 2, `{"files": [{"path": "../x.py", "content": "1"}]}`, code("9"))
			script("x10", "Inputs", "g2", "e1", 2, fmt.Sprintf(`{"files": [], "inputs": [{"datasource_id": %d, "alias": "S"}]}`, ids["d2"]), code("10"))
			script("x11", "Publish", "g2", "e1", 2, fmt.Sprintf(`{"files": [], "output": {"publish": true, "name": " Out ", "project_id": %d}}`, ids["g3"]), code("11"))
			script("x12", "Closed", "g2", "e3", 1, code("12"))
			script("x13", "Timeout", "g2", "e1", 2, `{"files": [], "timeout_seconds": 6000}`, code("13"))
			script("x14", "Bucket", "g2", "e1", 2, code("open('s3://scripts-bkt/x')"), code("14"))
			script("x15", "Empty", "g2", "e1", 1, `{"files": []}`)
			w.Exec(`UPDATE saved_scripts SET run_schedule = '0 * * * *', run_scheduled_by = $1, next_run_at = '2026-03-01' WHERE id = ANY($2)`,
				w.People["editor"], []int64{ids["x8"], ids["x1"]})
			// a flow with a Script node by reference, and groups with a script cell
			flow := func(name, def string, by string) {
				var who *int64
				var cron *string
				if by != "" {
					v := w.People[by]
					who, cron = &v, ptr("1 * * * *")
				}
				w.Exec(`INSERT INTO flows (name, description, project_id, definition, engine, run_schedule, run_scheduled_by, created_at,
					updated_at) VALUES ($1, '', $2, $3, 'polars', $4, $5, '2026-01-01', '2026-01-01')`, name, ids["g2"], def, cron, who)
			}
			flow("ss-flow-5", fmt.Sprintf(`{"nodes": [{"id": "n", "type": "script", "data": {"scriptId": %d}}]}`, ids["x5"]), "")
			flow("ss-flow-8", fmt.Sprintf(`{"nodes": [{"id": "n", "type": "script", "data": {"scriptId": %d}}]}`, ids["x8"]), "admin")
			flow("ss-flow-other", `{"nodes": [{"id": "n", "type": "script", "data": {"scriptId": true}}]}`, "admin")
			group := func(name, def, by string) {
				var who *int64
				var cron *string
				if by != "" {
					v := w.People[by]
					who, cron = &v, ptr("2 * * * *")
				}
				w.Exec(`INSERT INTO flow_groups (name, description, project_id, definition, run_schedule, run_scheduled_by, created_at,
					updated_at) VALUES ($1, '', $2, $3, $4, $5, '2026-01-01', '2026-01-01')`, name, ids["g2"], def, cron, who)
			}
			group("ss-group-5", fmt.Sprintf(`{"steps": [[{"kind": "script", "id": %d}]]}`, ids["x5"]), "")
			group("ss-group-8", fmt.Sprintf(`{"steps": [[{"kind": "script", "id": %d}]]}`, ids["x8"]), "obs_flag")
			rowsOf := func(kind string) any {
				if kind == "script" {
					return 5
				}
				return nil
			}
			dsOf := func(kind string) any {
				if kind == "script" {
					return ids["d1"]
				}
				return nil
			}
			run := func(script, kind, status, started string, parent any, scriptInfo any, outputKey string) int64 {
				return w.Insert(`INSERT INTO runs (kind, task_id, status, launched_by, trigger_type, input_key, output_bucket, output_key,
					publish_description, publish_overwrite, publish_sort_keys, started_at, saved_script_id, parent_run_id, script,
					rows_written, datasource_id) VALUES ($1, '', $2, $3, 'manual', '', 'out-bkt', $4, '', false, '[]', $5, $6, $7, $8,
					$9, $10) RETURNING id`,
					kind, status, w.People["writer"], outputKey, started, ids[script], parent, scriptInfo, rowsOf(kind), dsOf(kind))
			}
			run("x6", "orchestration", "PENDING", "2026-02-03", nil, nil, "")
			a := run("x7", "orchestration", "SUCCESS", "2026-02-03", nil, nil, "")
			ids["run_a"] = a
			ids["child_a"] = run("x7", "script", "SUCCESS", "2026-02-03", a,
				`{"script_version": 1, "attempts": 2, "exit_code": 0, "log": "hi", "columns": [{"name": "a"}], "log_expired": 1}`, "out/a.parquet")
			ids["run_b"] = run("x7", "orchestration", "FAILURE", "2026-02-04", nil, nil, "")
			c := run("x7", "orchestration", "SUCCESS", "2026-02-04", nil, nil, "")
			ids["run_c"] = c
			run("x7", "script", "SUCCESS", "2026-02-04", c, `{"log": ""}`, "")
			ids["run_d"] = run("x7", "orchestration", "SUCCESS", "2026-02-05", nil, nil, "")
			run("x7", "script", "FAILURE", "2026-02-05", ids["run_d"], nil, "out/d.parquet")
		},
		Engine: []EngineRule{{Method: "POST", Path: "^/tasks/preview$", Body: map[string]any{"columns": []any{"a"}, "rows": []any{[]any{1}}}}},
		Steps: func(w *World) []Step {
			var s []Step
			id := func(k string) int64 { return ids[k] }
			as := func(p string) func(*http.Request) {
				if p == "nobody" {
					return nil
				}
				return w.As(p)
			}
			add := func(name, method, path, p string, body any) {
				s = append(s, Step{Name: name, Method: method, Path: path, Auth: as(p), JSON: body})
			}
			people := []string{"admin", "editor", "writer", "obs_flag", "obs_group", "sso", "nobody"}
			for _, p := range people {
				add(p+" lists the scripts", "GET", "/scripts", p, nil)
				for _, f := range []string{"g1", "g2", "g3"} {
					add(p+" lists a folder", "GET", fmt.Sprintf("/projects/%d/scripts", id(f)), p, nil)
				}
				for _, k := range []string{"x1", "x2", "x3", "x4", "x12"} {
					add(p+" opens "+k, "GET", fmt.Sprintf("/scripts/%d", id(k)), p, nil)
				}
				add(p+" lists x1's versions", "GET", fmt.Sprintf("/scripts/%d/versions", id("x1")), p, nil)
				for _, v := range []string{"0", "1", "2", "3", "x"} {
					add(p+" reads x1's version "+v, "GET", fmt.Sprintf("/scripts/%d/versions/%s", id("x1"), v), p, nil)
				}
				add(p+" lists x7's runs", "GET", fmt.Sprintf("/scripts/%d/runs", id("x7")), p, nil)
			}
			add("a folder that does not exist", "GET", "/projects/999999/scripts", "writer", nil)
			add("a folder by a word", "GET", "/projects/x/scripts", "writer", nil)
			add("a script that does not exist", "GET", "/scripts/999999", "writer", nil)
			add("a script by a word", "GET", "/scripts/x", "writer", nil)
			add("a script with odd columns", "GET", fmt.Sprintf("/scripts/%d/versions/0", id("x4")), "admin", nil)
			for _, q := range []string{"?limit=1", "?limit=0", "?limit=101", "?limit=z"} {
				add("x7's runs "+q, "GET", fmt.Sprintf("/scripts/%d/runs%s", id("x7"), q), "writer", nil)
			}
			// previews
			prev := func(name, run, q, p string, engine []EngineRule) {
				s = append(s, Step{Name: name, Method: "GET", Path: fmt.Sprintf("/scripts/%d/runs/%v/preview%s", id("x7"), run, q),
					Auth: as(p), Engine: engine})
			}
			prev("the preview of run a", fmt.Sprint(id("run_a")), "", "writer", nil)
			prev("the preview of run a, 5 rows", fmt.Sprint(id("run_a")), "?limit=5", "writer", nil)
			prev("the preview of run a, too many rows", fmt.Sprint(id("run_a")), "?limit=1001", "writer", nil)
			prev("the preview of run b (no child)", fmt.Sprint(id("run_b")), "", "writer", nil)
			prev("the preview of run c (no output)", fmt.Sprint(id("run_c")), "", "writer", nil)
			prev("the preview of run d (failed)", fmt.Sprint(id("run_d")), "", "writer", nil)
			prev("the preview of a word", "w", "?limit=0", "writer", nil)
			prev("the preview by nobody", fmt.Sprint(id("run_a")), "", "nobody", nil)
			prev("the engine refuses", fmt.Sprint(id("run_a")), "", "writer",
				[]EngineRule{{Method: "POST", Path: "^/tasks/preview$", Status: 500, Body: map[string]any{"detail": strings.Repeat("é", 600)}}})
			prev("the engine answers text", fmt.Sprint(id("run_a")), "", "writer",
				[]EngineRule{{Method: "POST", Path: "^/tasks/preview$", Status: 400, Body: "plain"}})

			// create
			create := func(name string, folder any, p string, body any) {
				add(name, "POST", fmt.Sprintf("/projects/%v/scripts", folder), p, body)
			}
			g2, e1 := id("g2"), id("e1")
			py := func(path, content string) []any { return []any{map[string]any{"path": path, "content": content}} }
			create("writer creates one", g2, "writer", map[string]any{"name": " New ", "description": " d ", "environment_id": e1,
				"content": map[string]any{"files": py("main.py", "print(1)"), "entrypoint": "main.py",
					"inputs": []any{map[string]any{"datasource_id": id("d1"), "alias": "SALES"}}, "retries": true,
					"output": map[string]any{"publish": true, "name": " Pub ", "project_id": g2, "overwrite": 0}}})
			create("writer creates it again", g2, "writer", map[string]any{"name": "New", "environment_id": e1})
			create("writer creates an empty one", g2, "writer", map[string]any{"name": "Blank", "environment_id": e1, "content": map[string]any{}})
			create("writer creates one in R", g2, "writer", map[string]any{"name": "R", "environment_id": id("e2")})
			create("writer creates one in a closed env", g2, "writer", map[string]any{"name": "C", "environment_id": id("e3")})
			create("writer creates one in no env", g2, "writer", map[string]any{"name": "C", "environment_id": 999999})
			create("writer creates one reading a hidden source", g2, "writer", map[string]any{"name": "H", "environment_id": e1,
				"content": map[string]any{"inputs": []any{map[string]any{"datasource_id": id("d2"), "alias": "S"}}}})
			bad := map[string]any{
				"not-list files":   map[string]any{"files": "x"},
				"bad path":         map[string]any{"files": py("a b.py", "1")},
				"no text":          map[string]any{"files": []any{map[string]any{"path": "a.py"}}},
				"dup files":        map[string]any{"files": []any{map[string]any{"path": "a.py", "content": ""}, map[string]any{"path": "A.py", "content": ""}}},
				"bucket named":     map[string]any{"files": py("a.py", "x\nopen('S3://scripts-bkt/a')")},
				"bucket env":       map[string]any{"files": py("a.py", "scripts-bkt/env-1")},
				"wrong entry":      map[string]any{"files": py("a.py", "1"), "entrypoint": "b.py"},
				"R entry":          map[string]any{"files": py("a.R", "1"), "entrypoint": "a.R"},
				"short timeout":    map[string]any{"timeout_seconds": 5},
				"long timeout":     map[string]any{"timeout_seconds": 601},
				"float timeout":    map[string]any{"timeout_seconds": 60.0},
				"retries":          map[string]any{"retries": 6},
				"delay":            map[string]any{"retry_delay_seconds": -1},
				"inputs not list":  map[string]any{"inputs": map[string]any{"a": 1}},
				"too many inputs":  map[string]any{"inputs": make([]any, 21)},
				"input no id":      map[string]any{"inputs": []any{map[string]any{"alias": "A"}}},
				"bad alias":        map[string]any{"inputs": []any{map[string]any{"datasource_id": id("d1"), "alias": "a"}}},
				"twice alias":      map[string]any{"inputs": []any{map[string]any{"datasource_id": id("d1"), "alias": "A"}, map[string]any{"datasource_id": id("d3"), "alias": "A"}}},
				"twice ds":         map[string]any{"inputs": []any{map[string]any{"datasource_id": id("d1"), "alias": "A"}, map[string]any{"datasource_id": id("d1"), "alias": "B"}}},
				"missing ds":       map[string]any{"inputs": []any{map[string]any{"datasource_id": 999999, "alias": "A"}}},
				"output not obj":   map[string]any{"output": []any{1}},
				"publish no name":  map[string]any{"output": map[string]any{"publish": 1}},
				"publish no place": map[string]any{"output": map[string]any{"publish": true, "name": "x", "project_id": 999999}},
				"publish hidden":   map[string]any{"output": map[string]any{"publish": true, "name": "x", "project_id": id("g3")}},
				"content is list":  []any{1},
			}
			for _, k := range []string{"not-list files", "bad path", "no text", "dup files", "bucket named", "bucket env", "wrong entry",
				"R entry", "short timeout", "long timeout", "float timeout", "retries", "delay", "inputs not list", "too many inputs",
				"input no id", "bad alias", "twice alias", "twice ds", "missing ds", "output not obj", "publish no name",
				"publish no place", "publish hidden", "content is list"} {
				create("writer creates one: "+k, g2, "writer", map[string]any{"name": "K", "environment_id": e1, "content": bad[k]})
			}
			create("writer creates one in a folder of RUN", id("g1"), "editor", map[string]any{"name": "K", "environment_id": e1})
			create("writer creates one in no folder", "999999", "writer", map[string]any{"name": "K", "environment_id": e1})
			create("writer creates one with a blank name", g2, "writer", map[string]any{"name": " ", "environment_id": e1})
			create("writer creates one without an environment", g2, "writer", map[string]any{"name": "K"})
			create("nobody creates one", g2, "nobody", map[string]any{"name": "K", "environment_id": e1})
			s = append(s, Step{Name: "broken JSON", Method: "POST", Path: fmt.Sprintf("/projects/%d/scripts", g2), Raw: []byte("{"),
				ContentType: "application/json"})

			// change
			patch := func(name, key, p string, body any) {
				add(name, "PATCH", fmt.Sprintf("/scripts/%v", key), p, body)
			}
			sid := func(k string) string { return fmt.Sprint(id(k)) }
			patch("writer changes nothing", sid("x15"), "writer", map[string]any{})
			patch("writer renames Empty onto Main", sid("x15"), "writer", map[string]any{"name": "Main"})
			patch("writer renames Empty and describes it", sid("x15"), "writer", map[string]any{"name": "Empty 2", "description": "x"})
			patch("writer gives Empty code", sid("x15"), "writer", map[string]any{"content": map[string]any{"files": py("m.py", "1"), "entrypoint": "m.py"}})
			patch("writer moves Empty to RUN", sid("x15"), "writer", map[string]any{"project_id": id("g1")})
			patch("writer moves Empty nowhere", sid("x15"), "writer", map[string]any{"project_id": 999999})
			patch("writer moves Empty to R", sid("x15"), "writer", map[string]any{"environment_id": id("e2")})
			patch("admin moves Empty to R", sid("x15"), "admin", map[string]any{"environment_id": id("e2")})
			patch("admin moves Main to R (code is Python)", sid("x1"), "admin", map[string]any{"environment_id": id("e2")})
			patch("writer changes Restorable's code: schedules go", sid("x8"), "writer", map[string]any{"content": map[string]any{
				"files": py("main.py", "C"), "entrypoint": "main.py"}})
			patch("editor renames Main (RUN only)", sid("x1"), "editor", map[string]any{"name": "M"})
			patch("writer changes a hidden one", sid("x3"), "writer", map[string]any{"name": "M"})
			patch("bad fields", "x", "writer", map[string]any{"name": 1, "environment_id": "e", "content": 1})
			patch("nobody changes", sid("x1"), "nobody", map[string]any{"name": "M"})

			// copies
			for _, c := range []struct{ name, key, p string }{{"writer copies Main", "x1", "writer"}, {"writer copies Main again", "x1", "writer"},
				{"writer copies Broken", "x4", "writer"}, {"editor copies Main", "x1", "editor"}, {"obs_group copies Main", "x1", "obs_group"},
				{"nobody copies", "x1", "nobody"}, {"writer copies Rscript", "x2", "writer"}} {
				add(c.name, "POST", fmt.Sprintf("/scripts/%d/duplicate", id(c.key)), c.p, nil)
			}
			// restores
			restore := func(name, key, v, p string) {
				add(name, "POST", fmt.Sprintf("/scripts/%d/versions/%s/restore", id(key), v), p, nil)
			}
			restore("writer restores Restorable v1", "x8", "1", "writer")
			restore("writer restores Restorable v0 (current)", "x8", "0", "writer")
			restore("writer restores BadV1 v1", "x9", "1", "writer")
			restore("writer restores Inputs v1 (hidden input)", "x10", "1", "writer")
			restore("writer restores Publish v1 (hidden folder)", "x11", "1", "writer")
			restore("writer restores Timeout v1", "x13", "1", "writer")
			restore("writer restores Bucket v1", "x14", "1", "writer")
			restore("writer restores Main v9", "x1", "9", "writer")
			restore("writer restores Main v1", "x1", "1", "writer")
			restore("editor restores Main v1", "x1", "1", "editor")
			restore("admin restores Inputs v1", "x10", "1", "admin")
			restore("restore by words", "x1", "v", "writer")
			// runs
			runNow := func(name, key, p string) { add(name, "POST", fmt.Sprintf("/scripts/%d/run", id(key)), p, nil) }
			runNow("writer runs Empty (no code)", "x15", "writer")
			runNow("editor runs Deletable", "x7", "editor")
			runNow("editor runs it again", "x7", "editor")
			runNow("writer runs Running", "x6", "writer")
			runNow("obs_flag runs Main", "x1", "obs_flag")
			runNow("nobody runs Main", "x1", "nobody")
			add("run a word", "POST", "/scripts/x/run", "writer", nil)
			// deletes
			del := func(name, key, p string) { add(name, "DELETE", fmt.Sprintf("/scripts/%d", id(key)), p, nil) }
			del("writer deletes UsedFlow", "x5", "writer")
			del("writer deletes Running", "x6", "writer")
			del("writer deletes Deletable (a run queued)", "x7", "writer")
			del("writer deletes BadV1", "x9", "writer")
			del("writer deletes BadV1 again", "x9", "writer")
			del("editor deletes Main", "x1", "editor")
			del("admin deletes Rscript", "x2", "admin")
			add("delete a word", "DELETE", "/scripts/x", "writer", nil)
			add("the list at the end", "GET", "/scripts", "admin", nil)
			add("forwarded: schedule", "PUT", fmt.Sprintf("/scripts/%d/schedule", id("x8")), "writer", map[string]any{"cron": ""})
			return s
		},
	})
}
