package main

import (
	"fmt"
	"strings"
)

// routes/datasources.py: the datasources a connection brings, created with their first
// import, and their copy, refresh, schedule and import history.

// snapshotStamp is what snapshot_key draws: the moment and eight random hex digits.
const snapshotStamp = `\d{8}T\d{6}Z-[0-9a-f]{8}`

// DSC are the ids of this suite's seed.
var DSC = map[string]int64{}

func init() {
	register(Suite{
		Module: "datasources_create",
		Seed: func(w *World) {
			seedData(w)
			maskPattern("runs", "output_key", snapshotStamp)
			maskPattern("datasources", "key", snapshotStamp)
			folder := func(key string) {
				DSC[key] = w.Insert(`INSERT INTO projects (name, description, parent_id, owner_id, created_at)
					VALUES ($1, '', NULL, $2, '2026-01-02') RETURNING id`, "dsc-"+key, w.People["admin"])
			}
			folder("dest")
			folder("conn")
			folder("view")
			editor := w.People["editor"]
			for _, g := range []struct{ f, c string }{{"dest", "edit"}, {"dest", "run"}, {"conn", "connect"}, {"view", "view"}} {
				w.Exec(`INSERT INTO permissions (project_id, user_id, group_id, capability) VALUES ($1, $2, NULL, $3)`, DSC[g.f], editor, g.c)
			}
			conn := func(key, folder, dbType, host, database, extra string) {
				DSC["conn:"+key] = w.Insert(`INSERT INTO connections (name, description, project_id, owner_id, db_type, host, port, username,
					password_encrypted, database, db_schema, extra, private_key_encrypted, host_key_type, host_key_fingerprint, created_at, updated_at)
					VALUES ($1, '', $2, $3, $4, $5, NULL, 'u', $6, $7, '', $8, '', '', '', '2026-01-06', '2026-01-06') RETURNING id`,
					"dsc-"+key, DSC[folder], w.People["admin"], dbType, host, seal("pw"), database, extra)
			}
			conn("pg", "conn", "postgresql", "pg.test", "sales", `{"tls": "trust"}`)
			conn("sftp", "conn", "sftp", "sftp.test", "", `{}`)
			conn("s3", "conn", "s3", "", "", `{}`)
			conn("s3b", "conn", "s3", "http://minio.test", "lake", `{}`)
			conn("sp", "conn", "sharepoint", "https://c.sharepoint.test/sites/a", "tenant", `{"library": "Docs"}`)
			conn("api", "conn", "api", "https://api.test", "", `{"auth": "bearer"}`)
			conn("gs", "conn", "gsheets", "sa@x.test", "proj", `{}`)
			conn("smtp", "conn", "smtp", "mail.test", "", `{}`)
			conn("viewpg", "view", "postgresql", "pg2.test", "x", `{}`)
			ds := func(key, folder, kind string, conn *int64, sourceType, sourceRef *string, sortKeys, dsKey string) {
				DSC["ds:"+key] = w.Insert(`INSERT INTO datasources (name, description, project_id, owner_id, bucket, key, rows, columns,
					sort_keys, column_descriptions, kind, connection_id, source_type, source_ref, refreshed_at, created_at, updated_at)
					VALUES ($1, 'about', $2, $3, 'data-prep', $4, 7, '[{"name": "a", "dtype": "Int64"}]', $5, '{"a": "the a"}', $6, $7, $8, $9,
					'2026-02-01', '2026-01-04', '2026-01-04') RETURNING id`,
					"dsc-ds-"+key, DSC[folder], w.People["admin"], dsKey, sortKeys, kind, conn, sourceType, sourceRef)
			}
			id := func(k string) *int64 { v := DSC["conn:"+k]; return &v }
			ds("db", "dest", "database", id("pg"), ptr("table"), ptr("public.t"), `["a"]`, "datasets/db.parquet")
			ds("sftp", "dest", "database", id("sftp"), ptr("sftp"), ptr(`{"folder": "in/{today}", "pattern": "*.csv", "format": "csv", "mode": "append", "keys": ["id"], "timezone": "Europe/Rome"}`), `[]`, "datasets/sftp.parquet")
			ds("api", "dest", "database", id("api"), ptr("api"), ptr(`{"method": "GET", "path": "/x", "mode": "append"}`), `[]`, "datasets/api.parquet")
			ds("sp", "dest", "database", id("sp"), ptr("sharepoint"), ptr(`{"path": "/a.xlsx", "format": "auto", "csv": {"separator": ";"}, "variables": [{"name": "v"}]}`), `[]`, "")
			ds("s3", "dest", "database", id("s3b"), ptr("s3"), ptr(`{"bucket": "", "folder": "f", "pattern": "*.parquet", "x": 1}`), `[]`, "datasets/s3.parquet")
			ds("gs", "dest", "database", id("gs"), ptr("gsheets"), ptr(`{"spreadsheet_id": "abc", "sheet_id": 0, "range": "A1:B2", "spreadsheet_name": "n"}`), `[]`, "")
			ds("flow", "dest", "flow", nil, nil, nil, `[]`, "datasets/flow.parquet")
			ds("orphan", "dest", "database", nil, ptr("table"), ptr("t"), `[]`, "")
			ds("live", "dest", "database", id("pg"), ptr("sql"), ptr("select 1"), `[]`, "")
			ds("broken", "dest", "database", id("sftp"), ptr("sftp"), ptr(`not json`), `[]`, "")
			ds("brokenkeys", "dest", "database", id("pg"), ptr("table"), ptr("t"), `nope`, "")
			ds("list", "dest", "database", id("sftp"), ptr("sftp"), ptr(`[1]`), `[]`, "")
			ds("viewonly", "view", "database", id("viewpg"), ptr("table"), ptr("t"), `[]`, "datasets/v.parquet")
			ds("otherconn", "dest", "database", id("viewpg"), ptr("table"), ptr("t"), `[]`, "datasets/o.parquet")
			ds("nofile", "dest", "flow", nil, nil, nil, `[]`, "")
			contract := func(key string, enabled bool, doc string) {
				w.Exec(`INSERT INTO data_contracts (datasource_id, version, document, enabled, status, checked_at, errors, warnings,
					last_report, blocked_at, blocked_run_id, notify_emails, notify_connection_id, created_by, updated_by, created_at, updated_at)
					VALUES ($1, 4, $2, $3, 'passed', '2026-02-03 01:02:03', 0, 1, NULL, NULL, NULL, 'x@y.test', NULL, $4, $4, '2026-02-01', '2026-02-03')`,
					DSC["ds:"+key], doc, enabled, w.People["admin"])
			}
			contract("db", true, `{"description": "d", "rules": [{"id": "r1", "kind": "not_null", "severity": "error", "column": "a"}], "version": 1}`)
			contract("sftp", false, `{"rules": [{"id": "r1"}]}`)
			contract("api", true, `{"rules": []}`)
			contract("flow", true, `{"description": "é", "rules": [{"id": "r1", "kind": "unique", "severity": "warning", "column": "a"}]}`)
			contract("s3", true, `[1]`)
			run := func(ds, status, task string, started string) {
				w.Exec(`INSERT INTO runs (kind, task_id, status, launched_by, trigger_type, input_key, output_bucket, output_key, datasource_id,
					publish_description, publish_overwrite, publish_sort_keys, started_at) VALUES ('ingest', $1, $2, $3, 'manual', '', 'data-prep',
					'datasets/r.parquet', $4, '', false, '[]', `+started+`)`, task, status, w.People["admin"], DSC["ds:"+ds])
			}
			run("sftp", "SUCCESS", "dsc-t1", "'2026-02-05 10:00:00.123456'")
			run("sftp", "SUCCESS", "dsc-t0", "'2026-02-04 10:00:00'")
			run("api", "SUCCESS", "dsc-t2", "'2026-02-06 00:00:00'")
			run("live", "PENDING", "pending-dsc-live", "now()")
			run("db", "FAILURE", "dsc-t3", "'2026-02-01'")
		},
		Engine: []EngineRule{
			{Method: "POST", Path: "/ingest$", Body: map[string]any{"task_id": "t-dsc"}},
			{Method: "POST", Path: "/inspect$", Body: map[string]any{"ok": true}},
			{Method: "POST", Path: "^/files/copy$", Body: map[string]any{}},
			{Method: "GET", Path: "^/tasks/", Body: map[string]any{"status": "PENDING"}},
		},
		Steps: datasourcesCreateSteps,
	})
}

func datasourcesCreateSteps(w *World) []Step {
	var s []Step
	masked := []string{"*.body.output_key", "*.query"}
	scrub := []string{snapshotStamp}
	add := func(person, name, method, path string, body any, rules ...EngineRule) {
		st := Step{Name: name + " · " + person, Method: method, Path: path, JSON: body, Auth: w.As(person), EngineMask: masked, Scrub: scrub}
		if len(rules) > 0 {
			st.Engine = rules
		}
		s = append(s, st)
	}
	everyone := func(name, method, path string, body any) {
		for _, st := range forEveryone(w, name, method, path, body) {
			st.EngineMask, st.Scrub = masked, scrub
			s = append(s, st)
		}
	}
	c := func(k string) int64 { return DSC["conn:"+k] }
	ds := func(k string) string { return fmt.Sprintf("/datasources/%d", DSC["ds:"+k]) }
	create := func(kind string) string { return fmt.Sprintf("/projects/%d/datasources/%s", DSC["dest"], kind) }
	engine := func(path string, status int, body any) EngineRule {
		return EngineRule{Method: "POST", Path: path, Status: status, Body: body}
	}
	text := func(path string, status int, t string) EngineRule {
		return EngineRule{Method: "POST", Path: path, Status: status, Text: t}
	}
	ingestFailures := []struct {
		name string
		rule EngineRule
	}{
		{"the engine refuses", engine("/ingest$", 400, map[string]any{"detail": "bad"})},
		{"the engine fails as text", text("/ingest$", 503, strings.Repeat("x", 600))},
		{"no task id", engine("/ingest$", 200, map[string]any{"id": "x"})},
		{"an empty task id", engine("/ingest$", 200, map[string]any{"task_id": ""})},
		{"a task id not text", engine("/ingest$", 200, map[string]any{"task_id": 5})},
		{"an answer not JSON", text("/ingest$", 200, "ok")},
		{"an answer a list", engine("/ingest$", 200, []any{"t"})},
	}

	// ── a database ──
	db := func(name string, more map[string]any) map[string]any {
		b := map[string]any{"name": name, "connection_id": c("pg"), "source_type": "table", "source_ref": "public.t"}
		for k, v := range more {
			b[k] = v
		}
		return b
	}
	everyone("create from a database", "POST", create("database"), db("dsc-everyone", nil))
	for _, b := range []map[string]any{
		db(" dsc-new-db ", map[string]any{"description": "d", "sort_keys": []any{" b ", "a", "b", " "}}),
		db("dsc-new-db", nil), db("dsc-sql", map[string]any{"source_type": "sql", "source_ref": "select 1"}),
		db("dsc-bad-type", map[string]any{"source_type": "view"}), db("dsc-empty", map[string]any{"source_ref": "  "}),
		db("  ", nil), db("dsc-long-key", map[string]any{"sort_keys": []any{strings.Repeat("k", 256)}}),
		db("dsc-s3", map[string]any{"connection_id": c("s3")}), db("dsc-smtp", map[string]any{"connection_id": c("smtp")}),
		db("dsc-sftp", map[string]any{"connection_id": c("sftp")}), db("dsc-noconn", map[string]any{"connection_id": 999999}),
		db("dsc-bigconn", map[string]any{"connection_id": 99999999999}), db("dsc-noview", map[string]any{"connection_id": c("viewpg")}),
		{"name": "x"}, {"name": "x", "connection_id": "y", "source_type": 1, "source_ref": "t", "sort_keys": "a"},
	} {
		add("editor", fmt.Sprintf("create from a database %.120v", b), "POST", create("database"), b)
	}
	add("editor", "create in a folder without EDIT", "POST", fmt.Sprintf("/projects/%d/datasources/database", DSC["conn"]), db("dsc-x", nil))
	add("editor", "create in an unknown folder", "POST", "/projects/999999/datasources/database", db("dsc-x", nil))
	add("editor", "create in an odd folder, a bad body", "POST", "/projects/x/datasources/database", map[string]any{})
	for _, f := range ingestFailures {
		add("editor", "create from a database: "+f.name, "POST", create("database"), db("dsc-fails", nil), f.rule)
	}
	s = append(s, Step{Name: "create with the write token", Method: "POST", Path: create("database"), JSON: db("dsc-token", nil),
		Auth: Bearer("tab_data_admin_write_000000"), EngineMask: masked, Scrub: scrub})

	// ── files: SharePoint, SFTP, S3 ──
	for _, b := range []map[string]any{
		{"name": "dsc-sp", "connection_id": c("sp"), "path": " /a/b.xlsx ", "sheet": " S "},
		{"name": "dsc-sp-csv", "connection_id": c("sp"), "path": "/a/{today}.csv", "format": "csv", "csv": map[string]any{"separator": ";",
			"null_values": []any{"NA"}}, "timezone": "Europe/Rome", "variables": []any{map[string]any{"name": "v", "values": []any{"a", "b"}}}},
		{"name": "dsc-sp-auto", "connection_id": c("sp"), "path": "/x", "format": "auto", "mode": "append", "keys": []any{"id"}},
		{"name": "dsc-sp", "connection_id": c("sp"), "path": "/again"}, {"name": " ", "connection_id": c("sp"), "path": "/a"},
		{"name": "dsc-sp-x", "connection_id": c("sp"), "path": "  "}, {"name": "dsc-sp-pg", "connection_id": c("pg"), "path": "/a"},
		{"name": "dsc-sp-bad", "connection_id": c("sp"), "path": "/a", "format": "pdf", "csv": map[string]any{"separator": ";;"}},
	} {
		add("editor", fmt.Sprintf("create from SharePoint %.140v", b), "POST", create("sharepoint"), b)
	}
	for _, b := range []map[string]any{
		{"name": "dsc-sftp", "connection_id": c("sftp"), "folder": " in ", "pattern": " *.csv ", "sheet": " "},
		{"name": "dsc-sftp-t", "connection_id": c("sftp"), "folder": "in/{today:%Y}", "pattern": "a.csv", "last_refresh_initial": " 2026-01-01 "},
		{"name": "dsc-sftp-brace", "connection_id": c("sftp"), "pattern": "a}.csv"},
		{"name": "dsc-sftp-x", "connection_id": c("sftp"), "pattern": " "}, {"name": "dsc-sftp-up", "connection_id": c("sftp"), "folder": "../x", "pattern": "a"},
		{"name": "dsc-sftp-s3", "connection_id": c("s3"), "pattern": "a"},
		{"name": "dsc-sftp-fmt", "connection_id": c("sftp"), "pattern": "a", "format": "ndjson", "csv": map[string]any{"encoding": "windows-1252", "has_header": false}},
	} {
		add("editor", fmt.Sprintf("create from SFTP %.140v", b), "POST", create("sftp"), b)
	}
	for _, b := range []map[string]any{
		{"name": "dsc-s3", "connection_id": c("s3b"), "folder": " /a/b/ ", "pattern": " *.parquet "},
		{"name": "dsc-s3-own", "connection_id": c("s3"), "bucket": " own ", "pattern": "*.csv", "keys": []any{" k ", ""}, "mode": "append"},
		{"name": "dsc-s3-none", "connection_id": c("s3"), "pattern": "*.csv"}, {"name": "dsc-s3-pg", "connection_id": c("pg"), "pattern": "a"},
		{"name": " ", "connection_id": c("s3b"), "pattern": "a"}, {"name": "dsc-s3-tz", "connection_id": c("s3b"), "pattern": "a", "timezone": " UTC "},
	} {
		add("editor", fmt.Sprintf("create from S3 %.140v", b), "POST", create("s3"), b)
	}
	validates := []struct {
		name string
		rule EngineRule
	}{
		{"the engine refuses the templates", engine("/inspect$", 400, map[string]any{"detail": "bad placeholder {x}"})},
		{"the engine fails checking", text("/inspect$", 500, "boom")},
		{"the engine's other refusal", engine("/inspect$", 409, map[string]any{"detail": map[string]any{"code": "host_key_unknown", "host": "h"}})},
	}
	for _, v := range validates {
		add("editor", "create from SFTP: "+v.name, "POST", create("sftp"), map[string]any{"name": "dsc-sftp-v", "connection_id": c("sftp"),
			"folder": "{today}", "pattern": "a"}, v.rule)
		add("editor", "create from S3: "+v.name, "POST", create("s3"), map[string]any{"name": "dsc-s3-v", "connection_id": c("s3b"),
			"pattern": "a", "mode": "append"}, v.rule)
		add("editor", "create from an API: "+v.name, "POST", create("api"), map[string]any{"name": "dsc-api-v", "connection_id": c("api")}, v.rule)
	}
	for _, f := range ingestFailures[:2] {
		add("editor", "create from SFTP: "+f.name, "POST", create("sftp"), map[string]any{"name": "dsc-sftp-f", "connection_id": c("sftp"), "pattern": "a"}, f.rule)
	}

	// ── an API, Google Sheets ──
	for _, b := range []map[string]any{
		{"name": "dsc-api", "connection_id": c("api")},
		{"name": "dsc-api-full", "connection_id": c("api"), "method": "POST", "path": "/v/{today}", "body": "{}", "keys": []any{" id ", " "},
			"mode": "append", "limits": map[string]any{"max_rows": 10}, "pagination": map[string]any{"type": "page", "param": "p"},
			"headers": []any{map[string]any{"name": "H", "value": "v"}}},
		{"name": "dsc-api-interval", "connection_id": c("api"), "limits": map[string]any{"min_interval_seconds": 2}},
		{"name": "", "connection_id": c("api")}, {"name": "dsc-api-pg", "connection_id": c("pg")}, {"name": " ", "connection_id": c("api")},
		{"name": "dsc-api", "connection_id": c("api")},
	} {
		add("editor", fmt.Sprintf("create from an API %.140v", b), "POST", create("api"), b)
	}
	for _, b := range []map[string]any{
		{"name": "dsc-gs", "connection_id": c("gs"), "spreadsheet_id": "abcdefghijklmnopqrstuvwxyz", "sheet_id": 3, "range": " a1:b9 ",
			"spreadsheet_name": " Budget "},
		{"name": "dsc-gs-title", "connection_id": c("gs"), "spreadsheet_id": "abcdefghijklmnopqrstuvwxyz", "sheet": " Tab "},
		{"name": "dsc-gs-none", "connection_id": c("gs"), "spreadsheet_id": "abcdefghijklmnopqrstuvwxyz", "sheet": " "},
		{"name": "dsc-gs-pg", "connection_id": c("pg"), "spreadsheet_id": "abcdefghijklmnopqrstuvwxyz", "sheet": "T"},
		{"name": "dsc-gs-bad", "connection_id": c("gs"), "spreadsheet_id": "x"},
	} {
		add("editor", fmt.Sprintf("create from Google Sheets %.140v", b), "POST", create("gsheets"), b)
	}

	// ── refresh ──
	everyone("refresh", "POST", ds("db")+"/refresh", nil)
	for _, k := range []string{"db", "sftp", "api", "sp", "s3", "gs", "flow", "orphan", "live", "broken", "brokenkeys", "list", "viewonly", "otherconn"} {
		add("editor", "refresh "+k, "POST", ds(k)+"/refresh", nil)
		add("admin", "refresh "+k, "POST", ds(k)+"/refresh", nil)
	}
	for _, f := range ingestFailures {
		add("admin", "refresh: "+f.name, "POST", ds("gs")+"/refresh", nil, f.rule)
	}
	add("admin", "refresh an unknown one", "POST", "/datasources/999999/refresh", nil)
	add("admin", "refresh an odd id", "POST", "/datasources/x/refresh", nil)

	// ── the schedule ──
	for _, b := range []map[string]any{{"cron": " 0 3 * * * "}, {"cron": "*/5 * * * *"}, {"cron": "61 * * * *"}, {"cron": ""}, {"cron": nil},
		{}, {"cron": 5}} {
		add("editor", fmt.Sprintf("schedule %v", b), "PUT", ds("db")+"/schedule", b)
	}
	everyone("schedule", "PUT", ds("sftp")+"/schedule", map[string]any{"cron": "0 4 * * 1"})
	add("editor", "schedule a flow's", "PUT", ds("flow")+"/schedule", map[string]any{"cron": "0 3 * * *"})
	add("editor", "schedule an orphan", "PUT", ds("orphan")+"/schedule", map[string]any{"cron": "0 3 * * *"})
	add("editor", "schedule without CONNECT", "PUT", ds("otherconn")+"/schedule", map[string]any{"cron": "0 3 * * *"})
	add("editor", "schedule without RUN", "PUT", ds("viewonly")+"/schedule", map[string]any{"cron": "0 3 * * *"})
	add("editor", "schedule an unknown one", "PUT", "/datasources/999999/schedule", map[string]any{"cron": "0 3 * * *"})
	add("editor", "schedule an odd one, no body", "PUT", "/datasources/x/schedule", nil)

	// ── the import history ──
	everyone("the imports", "GET", ds("sftp")+"/runs", nil)
	for _, k := range []string{"db", "live", "viewonly", "flow"} {
		add("editor", "the imports of "+k, "GET", ds(k)+"/runs", nil)
	}
	add("editor", "the imports of an unknown one", "GET", "/datasources/999999/runs", nil)

	// ── duplicate ──
	everyone("duplicate", "POST", ds("db")+"/duplicate", nil)
	for _, k := range []string{"db", "db", "sftp", "api", "flow", "s3", "nofile", "orphan", "otherconn", "viewonly"} {
		add("editor", "duplicate "+k, "POST", ds(k)+"/duplicate", nil)
	}
	add("admin", "duplicate, the file missing", "POST", ds("sftp")+"/duplicate", nil, engine("^/files/copy$", 404, map[string]any{"detail": "no"}))
	add("admin", "duplicate, the engine fails", "POST", ds("sftp")+"/duplicate", nil, text("^/files/copy$", 500, "x"))
	add("admin", "duplicate an unknown one", "POST", "/datasources/999999/duplicate", nil)
	for i := range s {
		// the seconds a queued import has waited: counted by each side a moment apart
		s[i].Mask = append(s[i].Mask, "*.progress.seconds")
	}
	return append(s, settleRandomKeys("datasources_create"))
}

// settleRandomKeys is a last step that writes the same text over the random parts of the keys
// this suite's launches drew (snapshot_key, out/<uuid>), in both databases: the suites after it
// list those runs and datasources.
func settleRandomKeys(name string) Step {
	return Step{Name: name + ": the random keys settled for the suites after", Method: "GET", Path: "/health", Before: func(w *World) {
		for _, t := range []struct{ table, column string }{{"runs", "output_key"}, {"datasources", "key"}} {
			w.Exec(fmt.Sprintf(`UPDATE %s SET %s = regexp_replace(regexp_replace(%s, '\d{8}T\d{6}Z-[0-9a-f]{8}', 'settled'),
				'out/[0-9a-f]{32}\.parquet', 'out/settled.parquet') WHERE %s ~ '(\d{8}T\d{6}Z-[0-9a-f]{8}|out/[0-9a-f]{32}\.parquet)'`,
				t.table, t.column, t.column, t.column))
		}
	}}
}
