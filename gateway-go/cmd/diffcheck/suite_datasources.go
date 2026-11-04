package main

import "fmt"

// routes/datasources.py: the catalog of datasources.
func init() {
	register(Suite{
		Module: "datasources",
		Seed: func(w *World) {
			seedData(w)
			maskColumns("pending_blob_deletions", "delete_after") // now + the grace, a few ms apart
			mk := func(key, kind, blob string) {
				Data["ds:"+key] = w.Insert(`INSERT INTO datasources (name, description, project_id, owner_id, bucket, key, columns, sort_keys,
					column_descriptions, kind, source_type, refresh_schedule, refresh_scheduled_by, created_at, updated_at)
					VALUES ($1, '', $2, NULL, 'data-prep', $3, '[{"name": "a"}, {"name": "b"}]', '[]', '{}', $4, $5, '0 3 * * *', $6,
					'2026-01-04', '2026-01-04') RETURNING id`, "data-"+key, Data["child"], blob, kind, map[string]any{"database": "table", "flow": nil}[kind],
					w.People["admin"])
			}
			mk("del-free", "database", "datasets/del-free.parquet")
			mk("del-nokey", "database", "")
			mk("del-read", "database", "datasets/del-read.parquet")
			mk("mine", "database", "datasets/mine.parquet")
			w.Exec(`UPDATE datasources SET refresh_scheduled_by = $1 WHERE id = $2`, w.People["editor"], Data["ds:mine"])
			w.Exec(`UPDATE datasources SET source_type = 'sftp' WHERE id = $1`, Data["ds:del-nokey"])
			w.Exec(`INSERT INTO data_contracts (datasource_id, version, document, enabled, status, errors, warnings, created_at, updated_at)
				VALUES ($1, 1, '{}', true, 'pending', 0, 0, '2026-01-01', '2026-01-01')`, Data["ds:del-free"])
			w.Exec(`INSERT INTO runs (kind, task_id, status, launched_by, trigger_type, input_key, output_bucket, output_key, datasource_id,
				publish_description, publish_overwrite, publish_sort_keys, started_at) VALUES ('ingest', 'data-del', 'SUCCESS', NULL, 'manual', '',
				'data-prep', 'k', $1, '', false, '[]', '2026-02-04')`, Data["ds:del-free"])
			rep := w.Insert(`INSERT INTO reports (name, description, project_id, owner_id, current_version, created_at, updated_at, views_total)
				VALUES ('data-ds-report', '', $1, NULL, 1, '2026-01-07', '2026-01-07', 0) RETURNING id`, Data["hidden"])
			w.Exec(`INSERT INTO report_versions (report_id, version, content, note, created_at, created_by) VALUES ($1, 1, $2, '', '2026-01-07', NULL)`,
				rep, fmt.Sprintf(`{"datasets": [{"source": {"kind": "datasource", "datasource_id": %d}}]}`, Data["ds:del-read"]))
		},
		Steps: func(w *World) []Step {
			p := func(k string) string { return fmt.Sprint(Data[k]) }
			s := forEveryone(w, "every datasource", "GET", "/datasources", nil)
			s = append(s, forEveryone(w, "search", "GET", "/datasources/search", nil)...)
			s = append(s, forEveryone(w, "a folder's datasources", "GET", "/projects/"+p("child")+"/datasources", nil)...)
			as := func(person, name, method, path string, body any) {
				s = append(s, Step{Name: name + " · " + person, Method: method, Path: path, JSON: body, Auth: w.As(person)})
			}
			for _, q := range []string{"?q=ROOT", "?q=about", "?q=", "?q=%25", "?q=_", "?limit=1", "?limit=2&offset=1", "?offset=100",
				"?limit=0", "?limit=201", "?limit=x&offset=-1", "?offset=99999999999999999999", "?offset=9223372036854775807",
				"?q=a&q=ready", "?limit=1.0", "?limit=%201", "?q=%C3%A8"} {
				as("editor", "search "+q, "GET", "/datasources/search"+q, nil)
				as("admin", "search "+q, "GET", "/datasources/search"+q, nil)
			}
			s = append(s, Step{Name: "a bad query, nobody: 401 first", Method: "GET", Path: "/datasources/search?limit=x"})
			as("obs_flag", "search, nothing readable", "GET", "/datasources/search?limit=x", nil)
			for _, f := range []string{"root", "run", "hidden", "connect"} {
				as("editor", "the folder "+f, "GET", "/projects/"+p(f)+"/datasources", nil)
			}
			for _, id := range []string{"999999", "x", "99999999999", "-1"} {
				as("editor", "the folder "+id, "GET", "/projects/"+id+"/datasources", nil)
			}
			// changing
			patch := func(person, name, ds string, body any) {
				as(person, "change: "+name, "PATCH", "/datasources/"+fmt.Sprint(Data["ds:"+ds]), body)
			}
			patch("writer", "VIEW only", "half", map[string]any{"name": "x"})
			patch("editor", "rename", "half", map[string]any{"name": "  data-ds-half2 "})
			patch("editor", "a name taken", "half", map[string]any{"name": "data-ds-ready"})
			patch("editor", "a blank name", "half", map[string]any{"name": "  "})
			patch("editor", "a description too long", "half", map[string]any{"description": repeat("é", 4001)})
			patch("editor", "a description", "half", map[string]any{"description": "  là  "})
			patch("editor", "the columns' descriptions", "half", map[string]any{"column_descriptions": map[string]any{" a ": " the a ", "b": "", "": "x", "a": "again"}})
			patch("editor", "a column's description too long", "half", map[string]any{"column_descriptions": map[string]any{"a": repeat("x", 2001)}})
			patch("editor", "sort keys of a flow's", "root", map[string]any{"sort_keys": []any{"a"}})
			patch("editor", "sort keys of an SFTP import", "del-nokey", map[string]any{"sort_keys": []any{"a"}})
			patch("editor", "sort keys unknown", "mine", map[string]any{"sort_keys": []any{"a", "zz", "yy"}})
			patch("editor", "sort keys", "mine", map[string]any{"sort_keys": []any{" b ", "a", "b", "", "a"}})
			patch("editor", "a sort key too long", "mine", map[string]any{"sort_keys": []any{repeat("k", 256)}})
			patch("editor", "too many sort keys", "mine", map[string]any{"sort_keys": make([]any, 33)})
			patch("editor", "sort keys without columns", "broken", map[string]any{"sort_keys": []any{"q"}})
			patch("editor", "move where RUN only", "half", map[string]any{"project_id": Data["run"]})
			patch("editor", "move to a hidden folder", "half", map[string]any{"project_id": Data["hidden"]})
			patch("editor", "move to an unknown folder", "half", map[string]any{"project_id": 99999999999})
			patch("editor", "move, the schedule someone else set", "del-read", map[string]any{"project_id": Data["root"], "name": "data-moved"})
			patch("editor", "move, the schedule one set", "mine", map[string]any{"project_id": Data["root"]})
			patch("editor", "stay in the folder", "mine", map[string]any{"project_id": Data["root"]})
			patch("editor", "a bad body", "mine", map[string]any{"sort_keys": "a", "column_descriptions": []any{}})
			patch("editor", "nothing", "mine", map[string]any{})
			as("editor", "change an unknown one", "PATCH", "/datasources/999999", map[string]any{})
			s = append(s, Step{Name: "change with a broken JSON, nobody", Method: "PATCH", Path: "/datasources/1", Raw: []byte("{"), ContentType: "application/json"})
			// deleting
			del := func(person, name, ds string) {
				as(person, "delete: "+name, "DELETE", "/datasources/"+fmt.Sprint(Data["ds:"+ds]), nil)
			}
			del("writer", "VIEW only", "del-free")
			del("editor", "saved views use it", "root")
			del("editor", "a report reads it", "del-read")
			del("admin", "a report reads it, the administrator", "del-read")
			del("editor", "free", "del-free")
			del("editor", "free, again", "del-free")
			del("editor", "never imported", "del-nokey")
			as("editor", "delete an odd id", "DELETE", "/datasources/x", nil)
			return s
		},
	})
}
