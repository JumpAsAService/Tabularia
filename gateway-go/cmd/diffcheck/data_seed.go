package main

import (
	"net/http"
	"time"
)

// The data lane's common seed (engine policy, settings, datasources, connections,
// contracts, saved views, lineage, queue, workers, performance, cluster): seeded once,
// whichever of these suites runs first, so any of them can run alone with DIFF_ONLY.
//
//	data-root         editor: EDIT (by the Analysts group); writer: VIEW
//	  data-child      inherits
//	data-run          editor: RUN
//	data-hidden       nobody but the administrators
//	data-connect      editor: CONNECT
var dataSeeded bool

// Data are the ids of the data lane's seed.
var Data = map[string]int64{}

func seedData(w *World) {
	if dataSeeded {
		return
	}
	dataSeeded = true
	folder := func(key, name string, parent *int64) int64 {
		id := w.Insert(`INSERT INTO projects (name, description, parent_id, owner_id, created_at)
			VALUES ($1, '', $2, $3, '2026-01-02') RETURNING id`, name, parent, w.People["admin"])
		Data[key] = id
		return id
	}
	root := folder("root", "data-root", nil)
	folder("child", "data-child", &root)
	folder("run", "data-run", nil)
	folder("hidden", "data-hidden", nil)
	folder("connect", "data-connect", nil)
	grant := func(project string, user, group *int64, capability string) {
		w.Exec(`INSERT INTO permissions (project_id, user_id, group_id, capability) VALUES ($1, $2, $3, $4)`,
			Data[project], user, group, capability)
	}
	analysts, editor, writer := Groups["Analysts"], w.People["editor"], w.People["writer"]
	grant("root", nil, &analysts, "edit")
	grant("root", &writer, nil, "view")
	grant("run", &editor, nil, "run")
	grant("connect", &editor, nil, "connect")

	flow := func(key, name, project, engine string, production *string) {
		Data["flow:"+key] = w.Insert(`INSERT INTO flows (name, description, project_id, owner_id, definition, engine,
			production_engine, created_at, updated_at) VALUES ($1, '', $2, $3, '{}', $4, $5, '2026-01-03', '2026-01-03') RETURNING id`,
			name, Data[project], w.People["admin"], engine, production)
	}
	flow("duck", "data-flow-duck", "root", "duckdb", nil)
	flow("polars-chdb", "data-flow-polars-chdb", "root", "polars", ptr("chdb"))
	flow("duck-duck", "data-flow-duck-duck", "hidden", "duckdb", ptr("duckdb"))

	ds := func(key, name, project, kind string) {
		Data["ds:"+key] = w.Insert(`INSERT INTO datasources (name, description, project_id, owner_id, bucket, key, rows, columns,
			sort_keys, column_descriptions, kind, created_at, updated_at) VALUES ($1::text, 'about ' || $1::text, $2, $3, 'data-prep', $4, 42,
			'[{"name": "a", "dtype": "Int64"}, {"name": "b", "dtype": "Utf8"}]', '[]', '{}', $5, '2026-01-04', '2026-01-04') RETURNING id`,
			name, Data[project], w.People["admin"], "datasets/"+key+".parquet", kind)
	}
	ds("root", "data-ds-root", "root", "flow")
	ds("child", "data-ds-child", "child", "flow")
	ds("hidden", "data-ds-hidden", "hidden", "flow")
	ds("run", "data-ds-run", "run", "flow")
	view := func(key, name, project, datasource, spec string) {
		Data["view:"+key] = w.Insert(`INSERT INTO saved_views (name, description, project_id, owner_id, datasource_id, spec,
			created_at, updated_at) VALUES ($1, '', $2, $3, $4, $5, '2026-01-05', '2026-01-05 10:00:00.123456') RETURNING id`,
			name, Data[project], w.People["admin"], Data["ds:"+datasource], spec)
	}
	view("root", "data-view-b", "root", "root", `{"engine": "polars"}`)
	view("root2", "data-view-a", "root", "hidden", `{}`)
	view("child", "data-view-c", "child", "child", `{"filters": []}`)
	view("hidden", "data-view-h", "hidden", "hidden", `{}`)
	view("run", "data-view-r", "run", "run", `{}`)

	w.Token("writer", "tab_data_writer_write_00000", "write", time.Hour)
	w.Token("writer", "tab_data_writer_read_000000", "read", time.Hour)
	w.Token("service", "tab_data_service_write_0000", "write", time.Hour)
	w.Token("service", "tab_data_service_read_00000", "read", time.Hour)
}

// who is one way of asking: a person's session, a token, nobody.
type who struct {
	name string
	auth func(*http.Request)
}

// everyone is every kind of person the data lane's routes tell apart.
func everyone(w *World) []who {
	out := []who{}
	for _, p := range []string{"admin", "admin_group", "editor", "obs_flag", "obs_group", "writer", "service", "sso"} {
		out = append(out, who{p, w.As(p)})
	}
	return append(out,
		who{"nobody", nil},
		who{"writer's write token", Bearer("tab_data_writer_write_00000")},
		who{"writer's read token", Bearer("tab_data_writer_read_000000")},
		who{"service's write token", Bearer("tab_data_service_write_0000")},
		who{"service's read token", Bearer("tab_data_service_read_00000")},
		who{"cookie without the CSRF header", Cookie(w.JWT("admin", "s-admin"))},
	)
}

// forEveryone is one step per kind of person.
func forEveryone(w *World, name, method, path string, body any) []Step {
	var out []Step
	for _, p := range everyone(w) {
		out = append(out, Step{Name: name + " · " + p.name, Method: method, Path: path, JSON: body, Auth: p.auth})
	}
	return out
}
