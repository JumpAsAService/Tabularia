package main

import (
	"fmt"
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
	// the odd JSON a datasource row may hold, and what the listings compute from it
	odd := func(key, name, columns, sortKeys, descriptions, description string) {
		Data["ds:"+key] = w.Insert(`INSERT INTO datasources (name, description, project_id, owner_id, bucket, key, rows, columns,
			sort_keys, column_descriptions, kind, connection_id, source_type, source_ref, refresh_schedule, refreshed_at,
			next_refresh_at, created_at, updated_at) VALUES ($1, $2, $3, NULL, 'b', $4, NULL, $5, $6, $7, 'database', NULL, 'table',
			'public.x', '0 3 * * *', '2026-02-01 10:00:00', '2026-02-02 03:00:00.5', '2026-01-04', '2026-01-04 08:00:00')
			RETURNING id`, name, description, Data["child"], "datasets/"+key+".parquet", columns, sortKeys, descriptions)
	}
	odd("ready", "data-ds-ready", `[{"name": "a", "dtype": "Int64"}, {"name": "b", "dtype": "Utf8", "description": "own"}]`,
		`["a", "b"]`, `{"a": "the a"}`, "a ready one")
	odd("half", "data-ds-half", `[{"name": "a"}, {"name": "b", "description": "  "}, {"name": ""}, {"x": 1}, {"name": 5, "n": 1.0}]`,
		`{"a": 1}`, `{"a": 1, "b": [1, "x", null, true, 2.5], "c": null, "d": {"k": "v"}, "5": "five"}`, "  ")
	odd("broken", "data-ds-broken", `not json`, `nope`, `[1]`, "described")
	odd("nulls", "data-ds-nulls", ``, ``, ``, "")
	contract := func(ds string, enabled, blocked bool, status string) {
		var blockedAt *string
		if blocked {
			blockedAt = ptr("2026-02-03 00:00:00")
		}
		w.Exec(`INSERT INTO data_contracts (datasource_id, version, document, enabled, status, checked_at, errors, warnings,
			last_report, blocked_at, blocked_run_id, notify_emails, notify_connection_id, created_by, updated_by, created_at, updated_at)
			VALUES ($1, 3, '{"description": "", "rules": [{"id": "r1", "kind": "not_null", "severity": "error", "column": "a"}]}', $2, $3,
			'2026-02-03 01:02:03.000004', 1, 2, '{"outcome": "warning", "rules": []}', $4, NULL, 'x@y.test', NULL, $5, $5, '2026-02-01', '2026-02-03')`,
			Data["ds:"+ds], enabled, status, blockedAt, w.People["admin"])
	}
	contract("root", true, false, "warning")
	contract("run", true, false, "pending")
	w.Exec(`UPDATE data_contracts SET document = '{}', last_report = NULL WHERE datasource_id = $1`, Data["ds:run"])
	contract("broken", true, false, "passed")
	w.Exec(`UPDATE data_contracts SET document = '[1]' WHERE datasource_id = $1`, Data["ds:broken"])
	contract("nulls", true, false, "passed")
	w.Exec(`UPDATE data_contracts SET last_report = '[2]' WHERE datasource_id = $1`, Data["ds:nulls"])
	for i, report := range []string{`{"outcome": "passed", "rules": []}`, `[1]`, `not json`, `{"outcome": "failed"}`} {
		w.Exec(`INSERT INTO data_contract_results (datasource_id, contract_version, trigger, run_id, snapshot_key, outcome, blocked,
			errors, warnings, rows, report, evaluated_at, notify) VALUES ($1, 3, 'refresh', NULL, 'datasets/a', 'passed', $2, 1, 0, 10, $3,
			$4, NULL)`, Data["ds:root"], i == 3, report, fmt.Sprintf("2026-02-0%d 00:00:00", 1+i%2))
	}
	contract("ready", true, true, "passed")
	contract("half", false, false, "failed")
	w.Exec(`INSERT INTO runs (kind, task_id, status, launched_by, trigger_type, input_key, output_bucket, output_key, datasource_id,
		publish_description, publish_overwrite, publish_sort_keys, started_at) VALUES ('ingest', 'data-t1', 'STARTED', $1, 'manual', '',
		'data-prep', 'datasets/x.parquet', $2, '', false, '[]', '2026-02-04')`, w.People["admin"], Data["ds:ready"])
	w.Exec(`INSERT INTO runs (kind, task_id, status, launched_by, trigger_type, input_key, output_bucket, output_key, datasource_id,
		publish_description, publish_overwrite, publish_sort_keys, started_at) VALUES ('ingest', 'data-t2', 'SUCCESS', $1, 'manual', '',
		'data-prep', 'datasets/y.parquet', $2, '', false, '[]', '2026-02-04')`, w.People["admin"], Data["ds:half"])

	conn := func(key, name, project, dbType, host, database, password, extra string, port *int) {
		Data["conn:"+key] = w.Insert(`INSERT INTO connections (name, description, project_id, owner_id, db_type, host, port, username,
			password_encrypted, database, db_schema, extra, private_key_encrypted, host_key_type, host_key_fingerprint, created_at, updated_at)
			VALUES ($1, '', $2, $3, $4, $5, $6, 'u', $7, $8, '', $9, '', '', '', '2026-01-06', '2026-01-06 12:00:00.000001') RETURNING id`,
			name, Data[project], w.People["admin"], dbType, host, port, password, database, extra)
	}
	conn("pg", "data-conn-pg", "connect", "postgresql", "db.example.test", "sales", "not-a-real-token", "{}", ptr(5432))
	conn("smtp", "data-conn-smtp", "connect", "smtp", "mail.example.test", "", "", `{"sender": "a@b.test"}`, nil)
	conn("root", "data-conn-root", "root", "clickhouse", "ch.example.test", "warehouse", "", "{}", nil)
	conn("hidden", "data-conn-hidden", "hidden", "mysql", "my.example.test", "x", "t", "{}", ptr(3306))

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

var engineRowsSeeded bool

// seedEngineRows is the data lane's seed with the engine settings saved from the panel.
func seedEngineRows(w *World) {
	seedData(w)
	if engineRowsSeeded {
		return
	}
	engineRowsSeeded = true
	w.Exec(`INSERT INTO engine_settings (key, value, value_json, updated_at, updated_by) VALUES
		('workers.run_concurrency', 6, NULL, '2026-03-01 10:00:00', 'alice.rossi@example.test'),
		('ingest.parquet_row_group_rows', 5, '5.5', '2026-03-01', NULL),
		('preview.cache', 1, 'true', '2026-03-02', '@x.test'),
		('cache.ratio', 2, '2.5', '2026-03-02', 'é@x.test'),
		('clickhouse_external.password', 0, '"gAAAAA-not-a-token"', '2026-03-03', ' '),
		('clickhouse_external.ai_password', 0, 'null', '2026-03-03', 'nobody'),
		('upload.max_bytes', 1048576, NULL, '2026-03-03', 'a@x.test'),
		('bad.json', 7, 'not json', '2026-03-03', NULL)`)
}
