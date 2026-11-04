package main

import (
	"fmt"
	"net/http"
)

// FG: the flow groups' seed.
var FG = struct {
	seeded                                            bool
	Plain, Boxed, Mail, Empty, Sched, Doomed, SalesDB int64
}{}

func (w *World) seedFlowGroups() {
	w.seedRuns()
	if FG.seeded {
		return
	}
	FG.seeded = true
	FG.SalesDB = w.Insert(`INSERT INTO datasources (name, description, project_id, owner_id, bucket, key, columns, sort_keys,
		column_descriptions, kind, created_at, updated_at) VALUES ('fl sales db', '', $1, $2, 'data-prep', 'datasets/sdb.parquet', '[]',
		'[]', '{}', 'database', '2026-01-01', '2026-01-01') RETURNING id`, FL.Sales, w.People["admin"])
	group := func(name string, project int64, definition string, schedule *string) int64 {
		var by *int64
		if schedule != nil {
			by = ptr(w.People["admin"])
		}
		return w.Insert(`INSERT INTO flow_groups (name, description, project_id, owner_id, definition, run_schedule, run_scheduled_by,
			created_at, updated_at) VALUES ($1, 'g', $2, $3, $4, $5, $6, '2026-01-02', '2026-01-02 08:00:00.5') RETURNING id`,
			name, project, w.People["admin"], definition, schedule, by)
	}
	FG.Plain = group("fl grid", FL.Sales, fmt.Sprintf(`{"steps": [[{"kind": "flow", "id": %d}, null, {"kind": "refresh", "id": %d}],
		[{"kind": "flow", "id": %d}, {"kind": "script", "id": %d}, {"kind": "sql", "id": %d}, {"kind": "flow", "id": 999999}]]}`,
		FL.Alpha, FG.SalesDB, FL.Gamma, FL.Script, RN.SQL), nil)
	FG.Boxed = group("fl boxed grid", FL.Sales, fmt.Sprintf(`{"steps": [[{"kind": "container", "id": 1, "container": {"registryId": %d,
		"repository": "team/app", "tag": "v1", "label": "Box", "n": 1.5}}]]}`, FL.Registry), nil)
	FG.Mail = group("fl mail grid", FL.Sales, `{"steps": [[{"kind": "pushmail", "id": 1, "pushmail": {"label": " Weekly ", "to": ["a@x.it"]}}]]}`, nil)
	FG.Empty = group("fl empty grid", FL.Sales, `not json`, nil)
	FG.Sched = group("fl scheduled grid", FL.Sales, fmt.Sprintf(`{"steps": [[{"kind": "flow", "id": %d}]]}`, FL.Alpha), ptr("0 4 * * *"))
	FG.Doomed = group("fl doomed grid", FL.Sales, `{"steps": [[]]}`, nil)
	cells := fmt.Sprintf(`[[{"kind": "flow", "id": %d, "state": "succeeded", "run_id": 5, "error": null},
		{"kind": "flow", "id": %d, "state": "failed", "run_id": 6, "error": "boom"}, null],
		[{"kind": "script", "id": %d, "state": "failed", "run_id": 7, "error": "x"}, {"kind": "container", "id": 1, "error": "c"},
		{"kind": "sql", "id": %d, "error": "s", "run_id": 8}, {"kind": "refresh", "id": 999999, "error": "r", "run_id": 9}, "junk"]]`,
		FL.Alpha, FL.Secret, FL.Script, RN.SQL)
	for _, gc := range []string{cells, `not json`, `{"a": 1}`} {
		w.Exec(`INSERT INTO runs (kind, flow_group_id, task_id, status, launched_by, trigger_type, input_key, output_bucket, output_key,
			publish_description, publish_overwrite, publish_sort_keys, started_at, finished_at, group_cells)
			VALUES ('orchestration', $1, '', 'SUCCESS', $2, 'manual', '', '', '', '', false, '[]', '2026-01-03', '2026-01-03 00:01', $3)`,
			FG.Plain, w.People["editor"], gc)
	}
	w.Exec(`INSERT INTO runs (kind, flow_group_id, task_id, status, launched_by, trigger_type, input_key, output_bucket, output_key,
		publish_description, publish_overwrite, publish_sort_keys, started_at) VALUES ('orchestration', $1, '', 'SUCCESS', $2, 'manual',
		'', '', '', '', false, '[]', '2026-01-03')`, FG.Doomed, w.People["editor"])
}

// routes/flow_groups.py: groups of flows.
func init() {
	register(Suite{
		Module: "flow_groups",
		Seed:   func(w *World) { w.seedFlowGroups() },
		Steps: func(w *World) []Step {
			var s []Step
			f := fmt.Sprintf
			add := func(name, method, path string, auth func(*http.Request), body any) {
				s = append(s, Step{Name: name, Method: method, Path: path, Auth: auth, JSON: body})
			}
			for _, p := range []string{"admin", "editor", "writer", "obs_flag"} {
				add("groups · "+p, "GET", "/flow-groups", w.As(p), nil)
				add("Sales's groups · "+p, "GET", f("/projects/%d/flow-groups", FL.Sales), w.As(p), nil)
				for _, g := range []int64{FG.Plain, FG.Boxed, FG.Mail, FG.Empty} {
					add(f("group %d · %s", g, p), "GET", f("/flow-groups/%d", g), w.As(p), nil)
				}
			}
			add("groups · nobody", "GET", "/flow-groups", nil, nil)
			add("folder's groups · no such folder", "GET", "/projects/999999/flow-groups", w.As("admin"), nil)
			add("group · no such group", "GET", "/flow-groups/999999", w.As("admin"), nil)
			add("group · not a number", "GET", "/flow-groups/x", w.As("admin"), nil)

			grid := func(rows ...[]any) map[string]any { return map[string]any{"steps": rows} }
			flowCell := func(id int64) map[string]any { return map[string]any{"kind": "flow", "id": id} }
			create := func(name string, project any, auth func(*http.Request), body any) {
				add("create · "+name, "POST", f("/projects/%v/flow-groups", project), auth, body)
			}
			create("editor", FL.Sales, w.As("editor"), map[string]any{"name": " fl new grid ", "description": " d ",
				"definition": grid([]any{flowCell(FL.Alpha), nil, map[string]any{"kind": "refresh", "id": FG.SalesDB}})})
			create("no definition", FL.Sales, w.As("editor"), map[string]any{"name": "fl bare grid"})
			create("a name taken", FL.Sales, w.As("editor"), map[string]any{"name": "fl new grid"})
			create("no name", FL.Sales, w.As("editor"), map[string]any{"name": "  "})
			create("problems", FL.Sales, w.As("editor"), map[string]any{"name": "fl bad grid", "definition": grid(
				[]any{flowCell(FL.Alpha), flowCell(FL.Alpha), flowCell(FL.Secret), map[string]any{"kind": "refresh", "id": FL.SalesDS}},
				[]any{map[string]any{"kind": "sql", "id": RN.SQL}, flowCell(FL.Boxed), map[string]any{"kind": "script", "id": FL.Script}},
				[]any{map[string]any{"kind": "container", "id": 1, "container": map[string]any{"repository": "x"}}})})
			create("Gamma without RUN", FL.Sales, w.As("writer"), map[string]any{"name": "fl w", "definition": grid([]any{flowCell(FL.Gamma)})})
			create("bad cells", FL.Sales, w.As("editor"), map[string]any{"name": "x", "definition": map[string]any{"steps": []any{[]any{
				map[string]any{"kind": "nope", "id": "x"}}}}})
			create("a pushmail cell (Python)", FL.Sales, w.As("editor"), map[string]any{"name": "fl mail 2", "definition": grid(
				[]any{map[string]any{"kind": "pushmail", "id": 1, "pushmail": map[string]any{"to": []any{"a@x.it"}}}})})
			create("no such folder", 999999, w.As("editor"), map[string]any{"name": "x"})
			create("nobody", FL.Sales, nil, map[string]any{"name": "x"})

			add("duplicate · editor", "POST", f("/flow-groups/%d/duplicate", FG.Plain), w.As("admin"), nil)
			add("duplicate again", "POST", f("/flow-groups/%d/duplicate", FG.Plain), w.As("admin"), nil)
			add("duplicate · editor, cells it cannot add", "POST", f("/flow-groups/%d/duplicate", FG.Plain), w.As("editor"), nil)
			add("duplicate · the mail grid (Python)", "POST", f("/flow-groups/%d/duplicate", FG.Mail), w.As("admin"), nil)
			add("duplicate · writer", "POST", f("/flow-groups/%d/duplicate", FG.Plain), w.As("writer"), nil)

			patch := func(name string, g int64, auth func(*http.Request), body any) {
				add("patch · "+name, "PATCH", f("/flow-groups/%d", g), auth, body)
			}
			patch("rename", FG.Mail, w.As("editor"), map[string]any{"name": " fl mail grid bis ", "description": "x"})
			patch("a name taken", FG.Mail, w.As("editor"), map[string]any{"name": "fl grid"})
			patch("empty name", FG.Mail, w.As("editor"), map[string]any{"name": ""})
			patch("someone else's schedule, a cell added", FG.Sched, w.As("editor"), map[string]any{"definition": grid(
				[]any{flowCell(FL.Alpha), map[string]any{"kind": "refresh", "id": FG.SalesDB}})})
			patch("move to Dest", FG.Doomed, w.As("editor"), map[string]any{"project_id": FL.Dest})
			patch("move to Hidden", FG.Doomed, w.As("editor"), map[string]any{"project_id": FL.Hidden})
			patch("the mail grid's own cell, unchanged", FG.Mail, w.As("editor"), map[string]any{"definition": map[string]any{"steps": []any{
				[]any{map[string]any{"kind": "pushmail", "id": 1, "pushmail": map[string]any{"label": " Weekly ", "to": []any{"a@x.it"}}}}}}})
			patch("writer", FG.Plain, w.As("writer"), map[string]any{"name": "x"})
			patch("bad body", FG.Plain, w.As("editor"), map[string]any{"project_id": "x"})

			sched := func(name string, g int64, auth func(*http.Request), body any) {
				add("schedule · "+name, "PUT", f("/flow-groups/%d/schedule", g), auth, body)
			}
			sched("set", FG.Plain, w.As("editor"), map[string]any{"cron": " 0 5 * * * "})
			sched("invalid", FG.Plain, w.As("editor"), map[string]any{"cron": "0 5 * *"})
			sched("empty group", FG.Empty, w.As("editor"), map[string]any{"cron": "0 5 * * *"})
			sched("clear", FG.Plain, w.As("editor"), map[string]any{})
			sched("writer", FG.Plain, w.As("writer"), map[string]any{"cron": "0 5 * * *"})

			add("run now · empty", "POST", f("/flow-groups/%d/run-now", FG.Empty), w.As("editor"), nil)
			add("run now", "POST", f("/flow-groups/%d/run-now", FG.Plain), w.As("editor"), nil)
			s = append(s, Step{Name: "run now · again, in French", Method: "POST", Path: f("/flow-groups/%d/run-now", FG.Plain),
				Auth: w.As("editor"), Header: map[string]string{"Accept-Language": "fr"}})
			add("run now · writer", "POST", f("/flow-groups/%d/run-now", FG.Plain), w.As("writer"), nil)

			for _, p := range []string{"admin", "editor", "writer"} {
				add("group's runs · "+p, "GET", f("/flow-groups/%d/runs", FG.Plain), w.As(p), nil)
			}
			add("a group's run · editor", "GET", f("/flow-groups/%d/runs/%d", FG.Plain, RN.GroupRun), w.As("editor"), nil)
			add("a run of another group", "GET", f("/flow-groups/%d/runs/1", FG.Plain), w.As("admin"), nil)

			add("delete · queued", "DELETE", f("/flow-groups/%d", FG.Plain), w.As("editor"), nil)
			add("delete · writer", "DELETE", f("/flow-groups/%d", FG.Doomed), w.As("writer"), nil)
			add("delete", "DELETE", f("/flow-groups/%d", FG.Doomed), w.As("admin"), nil)
			add("delete · gone", "DELETE", f("/flow-groups/%d", FG.Doomed), w.As("admin"), nil)
			add("groups at the end · admin", "GET", "/flow-groups", w.As("admin"), nil)
			return s
		},
	})
}
