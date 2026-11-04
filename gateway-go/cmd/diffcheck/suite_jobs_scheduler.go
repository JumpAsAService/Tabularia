package main

import (
	"fmt"
	"time"
)

// SC: what the scheduler's suite seeds.
var SC = struct {
	Main, Locked                                             int64
	PG, SFTP, S3, API, GSheets, SharePoint, SMTP, LockedConn int64
	DS                                                       map[string]int64
	Flows, Groups, Scripts, SQL                              map[string]int64
	Env                                                      int64
}{}

// services/scheduler.py scheduler_loop, one round (_tick) at a time: the runs reconciled,
// the schedules fired (datasources, flows, flow groups, saved scripts, SQL scripts), the
// contracts' freshness and notices, the cleanups and the blobs past their grace.
func init() {
	// the parquet's name ends with 8 random hex digits (routes/runs.py snapshot_key)
	for _, tc := range [][2]string{{"runs", "output_key"}, {"datasources", "key"}, {"pending_blob_deletions", "key"},
		{"data_contract_results", "snapshot_key"}} {
		scrubColumn(tc[0], tc[1], `-[0-9a-f]{8}\.parquet`)
	}
	register(Suite{
		Module: "jobs_scheduler",
		Seed:   seedScheduler,
		Steps:  schedulerSteps,
	})
}

func seedScheduler(w *World) {
	tidyRuns(w)
	admin, writer := w.People["admin"], w.People["writer"]
	folder := func(name string) int64 {
		return w.Insert(`INSERT INTO projects (name, description, parent_id, owner_id, created_at) VALUES ($1, '', NULL, $2, '2026-01-01')
			RETURNING id`, name, admin)
	}
	SC.Main, SC.Locked = folder("sc-Main"), folder("sc-Locked")
	for _, c := range []string{"edit", "connect"} {
		w.Exec(`INSERT INTO permissions (project_id, user_id, group_id, capability) VALUES ($1, $2, NULL, $3)`, SC.Main, writer, c)
	}
	conn := func(name string, project int64, kind, host string, port *int64, user, database, schema, extra string) int64 {
		return w.Insert(`INSERT INTO connections (name, description, project_id, owner_id, db_type, host, port, username,
			password_encrypted, database, db_schema, extra, private_key_encrypted, host_key_type, host_key_fingerprint, created_at, updated_at)
			VALUES ($1, '', $2, $3, $4, $5, $6, $7, 'enc-secret', $8, $9, $10, 'enc-key', 'ssh-ed25519', 'SHA256:abc', '2026-01-01',
			'2026-01-01') RETURNING id`, name, project, admin, kind, host, port, user, database, schema, extra)
	}
	SC.PG = conn("sc-pg", SC.Main, "postgresql", "db.x.test", ptr(int64(5432)), "u", "sales", "public", `{"tls": "verify", "ca_cert": " CA "}`)
	SC.SFTP = conn("sc-sftp", SC.Main, "sftp", " sftp.x.test ", nil, " bob ", "", "", `{"auth": "key"}`)
	SC.S3 = conn("sc-s3", SC.Main, "s3", "https://s3.x.test", nil, "AK", "bucket", "eu-1", `{}`)
	SC.API = conn("sc-api", SC.Main, "api", " https://api.x.test ", nil, " client ", "", "",
		`{"auth": "oauth2", "token_url": "https://api.x.test/token", "scope": " read ", "headers": {" X-A ": "1"}, "key_in": "body"}`)
	SC.GSheets = conn("sc-gsheets", SC.Main, "gsheets", "bot@x.iam", nil, "", "proj", "", `{}`)
	SC.SharePoint = conn("sc-sharepoint", SC.Main, "sharepoint", " https://x.sharepoint.com/sites/a ", nil, " cid ", " tid ", "",
		`{"library": " Docs "}`)
	SC.SMTP = conn("sc-smtp", SC.Main, "smtp", "smtp.x.test", nil, "mailer", "", "", `{"from_address": "noreply@x.test", "tls": "ssl"}`)
	SC.LockedConn = conn("sc-locked-pg", SC.Locked, "postgresql", "db2.x.test", nil, "u", "x", "", `{}`)

	// datasources with a schedule, not due until a step makes them so
	SC.DS = map[string]int64{}
	ds := func(key string, project int64, conn *int64, sourceType, sourceRef string, by *int64, schedule string, dsKey string) {
		SC.DS[key] = w.Insert(`INSERT INTO datasources (name, description, project_id, owner_id, bucket, key, columns, sort_keys,
			column_descriptions, kind, connection_id, source_type, source_ref, refresh_schedule, refresh_scheduled_by, created_at, updated_at,
			refreshed_at) VALUES ($1, '', $2, $3, 'data-prep', $4, '[]', '["a", "b"]', '{}', 'database', $5, $6, $7, $8, $9, '2026-01-01',
			'2026-01-01', '2026-02-01 06:00:00') RETURNING id`, "sc "+key, project, admin, dsKey, conn, sourceType, sourceRef, schedule, by)
	}
	hourly := "0 * * * *"
	ds("pg", SC.Main, &SC.PG, "table", "public.orders", &writer, hourly, "datasets/sc/pg.parquet")
	ds("sftp", SC.Main, &SC.SFTP, "sftp", `{"folder": "/in/{yyyy}", "pattern": "*.csv", "format": "csv", "mode": "append",
		"timezone": "Europe/Rome", "other": 1}`, &writer, hourly, "datasets/sc/sftp.parquet")
	ds("s3", SC.Main, &SC.S3, "s3", `{"bucket": "b", "folder": "x", "pattern": "*.parquet", "variables": {"a": "1"}, "keys": ["id"]}`,
		&writer, hourly, "")
	ds("api", SC.Main, &SC.API, "api", `{"path": "/items", "mode": "append", "params": {"since": "{last_refresh}"}}`, &writer, hourly,
		"datasets/sc/api.parquet")
	ds("gsheets", SC.Main, &SC.GSheets, "gsheets", `{"spreadsheet_id": "s1", "sheet": "Foglio", "extra": true}`, &writer, hourly, "")
	ds("sharepoint", SC.Main, &SC.SharePoint, "sharepoint", `{"path": "/a.xlsx", "format": "excel", "mode": "replace"}`, &writer, hourly, "")
	ds("noauthor", SC.Main, &SC.PG, "table", "t", nil, hourly, "")
	ds("inactive", SC.Main, &SC.PG, "table", "t", ptr(w.People["inactive"]), hourly, "")
	ds("noconn", SC.Main, nil, "table", "t", &writer, hourly, "")
	ds("norun", SC.Locked, &SC.PG, "table", "t", &writer, hourly, "")
	ds("noconnect", SC.Main, &SC.LockedConn, "table", "t", &writer, hourly, "")
	ds("busy", SC.Main, &SC.PG, "sql", "select 1", &writer, hourly, "")
	ds("badcron", SC.Main, &SC.PG, "table", "t", &writer, "61 * * * *", "")
	ds("taken", SC.Main, &SC.PG, "table", "t", &writer, hourly, "")
	ds("contract", SC.Main, &SC.PG, "table", "t", &writer, hourly, "datasets/sc/contract.parquet")
	ds("rome", SC.Main, &SC.PG, "table", "t", &writer, "30 2 * * *", "")
	w.Exec(`INSERT INTO data_contracts (datasource_id, version, document, enabled, status, errors, warnings, created_at, updated_at)
		VALUES ($1, 3, '{"description": "", "rules": [{"id": "r1", "kind": "not_null", "severity": "error", "column": "a"}]}', true,
		'passed', 0, 0, '2026-01-01', '2026-01-01')`, SC.DS["contract"])
	// a past successful refresh: {last_refresh}
	w.Exec(`INSERT INTO runs (kind, datasource_id, task_id, status, launched_by, trigger_type, input_key, output_bucket, output_key,
		publish_description, publish_overwrite, publish_sort_keys, started_at, finished_at) VALUES
		('ingest', $1, 't-sc-old', 'SUCCESS', $3, 'schedule', '', 'data-prep', 'datasets/sc/old.parquet', '', false, '[]',
		'2026-02-20 05:00:00.123456', '2026-02-20 05:01:00'),
		('ingest', $2, 't-sc-old2', 'SUCCESS', $3, 'schedule', '', 'data-prep', 'datasets/sc/old2.parquet', '', false, '[]',
		'2026-02-21 05:00:00', '2026-02-21 05:01:00')`, SC.DS["sftp"], SC.DS["api"], writer)
	// one refreshing already: the engine says it is still going
	w.Exec(`INSERT INTO runs (kind, datasource_id, task_id, status, launched_by, trigger_type, input_key, output_bucket, output_key,
		publish_description, publish_overwrite, publish_sort_keys, started_at, engine_started_at) VALUES
		('ingest', $1, 't-sc-busy', 'STARTED', $2, 'manual', '', 'data-prep', 'datasets/sc/busy.parquet', '', false, '[]',
		'2026-03-02 07:50:00', '2026-03-02 07:51:00')`, SC.DS["busy"], writer)

	// runs not finished, for the reconcile pass
	run := func(kind, task, status string, ds *int64, parent *int64, started, extra string, args ...any) int64 {
		id := w.Insert(`INSERT INTO runs (kind, datasource_id, parent_run_id, task_id, status, launched_by, trigger_type, input_key,
			output_bucket, output_key, publish_description, publish_overwrite, publish_sort_keys, started_at)
			VALUES ($1, $2, $3, $4, $5, $6, 'manual', '', 'data-prep', $7, '', false, '[]', $8) RETURNING id`,
			kind, ds, parent, task, status, writer, "datasets/sc/"+task+".parquet", started)
		if extra != "" {
			w.Exec(`UPDATE runs SET `+extra+` WHERE id = $1`, append([]any{id}, args...)...)
		}
		return id
	}
	contracted := w.Insert(`INSERT INTO datasources (name, description, project_id, owner_id, bucket, key, columns, sort_keys,
		column_descriptions, kind, connection_id, created_at, updated_at) VALUES ('sc contracted', '', $1, NULL, 'data-prep',
		'datasets/sc/contracted.parquet', '[]', '[]', '{}', 'database', $2, '2026-01-01', '2026-01-01') RETURNING id`, SC.Main, SC.PG)
	w.Exec(`INSERT INTO data_contracts (datasource_id, version, document, enabled, status, errors, warnings, notify_emails,
		notify_connection_id, created_at, updated_at) VALUES ($1, 2, '{"rules": [{"id": "r1", "kind": "not_null", "severity": "error",
		"column": "a"}]}', true, 'passed', 0, 0, 'editor@x.test, ext@y.test ,', $2, '2026-01-01', '2026-01-01')`, contracted, SC.SMTP)
	warned := w.Insert(`INSERT INTO datasources (name, description, project_id, owner_id, bucket, key, columns, sort_keys,
		column_descriptions, kind, connection_id, created_at, updated_at) VALUES ('sc warned', '', $1, NULL, 'data-prep',
		'datasets/sc/warned.parquet', '[]', '[]', '{}', 'database', $2, '2026-01-01', '2026-01-01') RETURNING id`, SC.Main, SC.PG)
	w.Exec(`INSERT INTO data_contracts (datasource_id, version, document, enabled, status, errors, warnings, created_at, updated_at)
		VALUES ($1, 1, '{"rules": [{"id": "r1", "kind": "unique", "severity": "warning", "columns": ["a", "b"]}]}', true, 'pending', 0, 0,
		'2026-01-01', '2026-01-01')`, warned)
	run("ingest", "t-sc-refused", "STARTED", &contracted, nil, "2026-03-02 07:40:00", `engine_started_at = '2026-03-02 07:41:00'`)
	run("ingest", "t-sc-warned", "STARTED", &warned, nil, "2026-03-02 07:40:01", `engine_started_at = '2026-03-02 07:41:00'`)
	run("flow", "t-sc-ok", "PENDING", nil, nil, "2026-03-02 07:40:02", "")
	run("flow", "t-sc-stale", "STARTED", nil, nil, "2026-02-27 07:00:00", `engine_started_at = '2026-02-27 07:00:00'`)
	run("flow", "pending-sc", "PENDING", nil, nil, "2026-03-02 07:00:00", "")
	run("flow", "pending-sc-new", "PENDING", nil, nil, "2026-03-02 07:59:00", "")
	parent := run("orchestration", "", "FAILURE", nil, nil, "2026-03-02 07:00:00", `finished_at = '2026-03-02 07:10:00'`)
	run("script", "job-sc-child", "STARTED", nil, &parent, "2026-03-02 07:01:00", `script = '{"node_id": "x", "namespace": "ns-own"}'`)
	run("container", "job-sc-box", "PENDING", nil, nil, "2026-03-02 07:30:00", "")
	run("script", "job-sc-trial", "STARTED", nil, nil, "2026-03-02 06:00:00", `heartbeat_at = '2026-03-02 07:59:30'`)
	run("script", "job-sc-dead", "STARTED", nil, nil, "2026-03-02 06:00:00", `heartbeat_at = '2026-03-02 07:50:00'`)
	run("script", "job-sc-budget", "STARTED", nil, nil, "2026-03-02 06:00:00", `script = '{"budget_seconds": "3600"}'`)
	run("orchestration", "", "PENDING", nil, nil, "2026-03-01 07:00:00", "")

	// freshness: a contract whose data grow old
	fresh := w.Insert(`INSERT INTO datasources (name, description, project_id, owner_id, bucket, key, columns, sort_keys,
		column_descriptions, kind, created_at, updated_at, refreshed_at) VALUES ('sc fresh', '', $1, NULL, 'data-prep',
		'datasets/sc/fresh.parquet', '[]', '[]', '{}', 'flow', '2026-01-01', '2026-01-01', '2026-03-01 06:00:00') RETURNING id`, SC.Main)
	w.Exec(`INSERT INTO data_contracts (datasource_id, version, document, enabled, status, errors, warnings, notify_emails,
		notify_connection_id, last_report, checked_at, created_at, updated_at) VALUES ($1, 4, '{"rules": [{"id": "r1", "kind": "freshness",
		"severity": "error", "max_age_hours": 30}, {"id": "r2", "kind": "freshness", "severity": "warning", "max_age_hours": 24.5},
		{"id": "r3", "kind": "freshness", "severity": "error", "max_age_hours": 1, "column": "d"}]}', true, 'passed', 0, 0,
		'writer@x.test', $2, '{"outcome": "passed", "errors": 0, "warnings": 0, "rows": 10, "rules": [{"id": "r1", "kind": "freshness",
		"severity": "error", "passed": true, "observed": 1.5, "expected": 30, "error": "x"}, {"id": "r2", "kind": "freshness",
		"severity": "warning", "passed": true}, {"id": "r3", "kind": "freshness", "severity": "error", "passed": true}]}',
		'2026-03-01 07:00:00', '2026-01-01', '2026-01-01')`, fresh, SC.SMTP)

	// flows, flow groups, saved scripts, SQL scripts with a schedule
	SC.Flows = map[string]int64{}
	flow := func(key string, by *int64, schedule string) {
		SC.Flows[key] = w.Insert(`INSERT INTO flows (name, description, project_id, owner_id, definition, engine, run_schedule,
			run_scheduled_by, created_at, updated_at) VALUES ($1, '', $2, $3, '{"nodes": [], "edges": []}', 'duckdb', $4, $5, '2026-01-01',
			'2026-01-01') RETURNING id`, "sc "+key, SC.Main, admin, schedule, by)
	}
	q := "*/15 * * * *"
	flow("ok", &writer, q)
	flow("noauthor", nil, q)
	flow("inactive", ptr(w.People["inactive"]), q)
	flow("running", &writer, q)
	flow("badcron", &writer, "* * 31 2 *")
	flow("taken", &writer, q)
	flow("rome", &writer, "0 3 * * *")
	w.Exec(`INSERT INTO runs (kind, flow_id, task_id, status, launched_by, trigger_type, engine_mode, input_key, output_bucket, output_key,
		publish_description, publish_overwrite, publish_sort_keys, started_at) VALUES ('orchestration', $1, '', 'STARTED', $2, 'manual',
		'development', '', '', '', '', false, '[]', '2026-03-02 07:59:00')`, SC.Flows["running"], writer)
	SC.Groups = map[string]int64{}
	group := func(key string, project int64, by *int64) {
		SC.Groups[key] = w.Insert(`INSERT INTO flow_groups (name, description, project_id, owner_id, definition, run_schedule,
			run_scheduled_by, created_at, updated_at) VALUES ($1, '', $2, $3, '{"steps": []}', $4, $5, '2026-01-01', '2026-01-01')
			RETURNING id`, "sc "+key, project, admin, q, by)
	}
	group("ok", SC.Main, &writer)
	group("norun", SC.Locked, &writer)
	group("gone", SC.Main, ptr(w.People["inactive"]))
	group("running", SC.Main, &writer)
	w.Exec(`INSERT INTO runs (kind, flow_group_id, task_id, status, launched_by, trigger_type, engine_mode, input_key, output_bucket,
		output_key, publish_description, publish_overwrite, publish_sort_keys, started_at) VALUES ('orchestration', $1, '', 'PENDING', $2,
		'manual', 'production', '', '', '', '', false, '[]', '2026-03-02 07:59:00')`, SC.Groups["running"], writer)
	SC.Env = w.Insert(`INSERT INTO script_environments (name, description, language, enabled, cpu_request, cpu_limit, memory_request,
		memory_limit, max_timeout_seconds, created_at, updated_at) VALUES ('sc-env', '', 'python', true, '1', '1', '1Gi', '1Gi', 3600,
		'2026-01-01', '2026-01-01') RETURNING id`)
	SC.Scripts = map[string]int64{}
	script := func(key string, by *int64) {
		SC.Scripts[key] = w.Insert(`INSERT INTO saved_scripts (name, description, project_id, owner_id, environment_id, current_version,
			run_schedule, run_scheduled_by, created_at, updated_at) VALUES ($1, '', $2, $3, $4, 1, $5, $6, '2026-01-01', '2026-01-01')
			RETURNING id`, "sc "+key, SC.Main, admin, SC.Env, q, by)
	}
	script("ok", &admin)
	script("blind", &writer) // the environment is not one the writer may use
	SC.SQL = map[string]int64{}
	sql := func(key string, content string) {
		SC.SQL[key] = w.Insert(`INSERT INTO sql_scripts (name, description, project_id, owner_id, current_version, run_schedule,
			run_scheduled_by, created_at, updated_at) VALUES ($1, '', $2, $3, 2, $4, $5, '2026-01-01', '2026-01-01') RETURNING id`,
			"sc sql "+key, SC.Main, admin, q, writer)
		w.Exec(`INSERT INTO sql_script_versions (script_id, version, content, note, created_at) VALUES ($1, 1, '{}', 'created',
			'2026-01-01'), ($1, 2, $2, 'edited', '2026-01-01')`, SC.SQL[key], content)
	}
	onConn := func(conn int64, project int64, out string) string {
		return fmt.Sprintf(`{"sql": "-- the orders\nselect * from orders;", "connection_id": %d, "output": {"name": %q, "project_id": %d}}`,
			conn, out, project)
	}
	sql("ok", onConn(SC.PG, SC.Main, "sc sql out"))
	sql("busy", onConn(SC.PG, SC.Main, "sc sql busy"))
	sql("locked", onConn(SC.PG, SC.Locked, "sc sql locked"))
	sql("noconnect", onConn(SC.LockedConn, SC.Main, "sc sql noconnect"))
	sql("nosql", fmt.Sprintf(`{"sql": "  -- nothing\n ; ", "connection_id": %d, "output": {"name": "x", "project_id": %d}}`, SC.PG, SC.Main))
	sql("over", onConn(SC.PG, SC.Main, "sc pg")) // a database's datasource: not overwritten
	sql("down", onConn(SC.PG, SC.Main, "sc sql down"))
	sql("gone", onConn(SC.PG, SC.Main, "sc sql gone"))
	sql("refused", onConn(SC.PG, SC.Main, "sc sql refused"))
	w.Exec(`INSERT INTO datasources (name, description, project_id, owner_id, bucket, key, columns, sort_keys, column_descriptions, kind,
		created_at, updated_at) VALUES ('sc sql out', 'kept description', $1, NULL, 'data-prep', 'datasets/sc/sqlout.parquet', '[]', '[]',
		'{}', 'flow', '2026-01-01', '2026-01-01')`, SC.Main)
	w.Exec(`INSERT INTO runs (kind, sql_script_id, task_id, status, launched_by, trigger_type, input_key, output_bucket, output_key,
		publish_description, publish_overwrite, publish_sort_keys, started_at) VALUES ('sql', $1, 't-sc-sqlbusy', 'STARTED', $2, 'manual',
		'', 'data-prep', 'datasets/sc/sqlbusy.parquet', '', false, '[]', '2026-03-02 07:59:00')`, SC.SQL["busy"], writer)

	// the shared state's rows, some expired at the rounds' moments
	w.Exec(`INSERT INTO login_attempts (ip, email, failures, last_at) VALUES ('10.0.0.1', 'sc-a@x.test', 3, '2026-03-02 07:50:00'),
		('10.0.0.1', 'sc-b@x.test', 3, '2026-03-02 07:56:00')`)
	t1 := time.Date(2026, 3, 2, 8, 0, 0, 0, time.UTC)
	w.Exec(`INSERT INTO flow_presence (flow_id, instance, user_id, email, full_name, since, seen) VALUES
		($1, 'sc-old', $2, 'writer@x.test', '', $3, $3), ($1, 'sc-new', $2, 'writer@x.test', '', $4, $4)`,
		SC.Flows["ok"], writer, float64(t1.Unix()-41), float64(t1.Unix()-39))
	w.Exec(`INSERT INTO user_sessions (id, user_id, created_at, last_seen_at, expires_at, method) VALUES
		('sc-expired', $1, '2026-03-01', '2026-03-02 07:59:00', '2026-03-02 07:59:59', 'password'),
		('sc-live', $1, '2026-03-01', '2026-03-02 07:59:00', '2026-03-02 09:00:00', 'password'),
		('sc-idle', $1, '2026-03-01', '2026-03-01 07:00:00', '2026-03-02 09:00:00', 'password')`, writer)
	w.Exec(`INSERT INTO api_tokens (user_id, name, prefix, token_hash, scope, created_at, expires_at) VALUES
		($1, 'sc old', 'tab_scol', 'sc-hash-old', 'read', '2026-01-01', '2026-03-02 08:00:00'),
		($1, 'sc new', 'tab_scne', 'sc-hash-new', 'read', '2026-01-01', '2026-03-02 08:00:01')`, writer)
	hook := w.Insert(`INSERT INTO webhooks (name, url, secret_enc, events, project_id, active, created_by, created_at)
		VALUES ('sc hook', 'https://hooks.x.test/sc', 'x', '["flow.run.succeeded"]', NULL, true, $1, '2026-01-01') RETURNING id`, admin)
	w.Exec(`INSERT INTO webhook_deliveries (webhook_id, event, payload, status, attempts, next_attempt_at, created_at) VALUES
		($1, 'ping', '{}', 'delivered', 1, '2026-01-01', '2026-01-30 07:00:00'),
		($1, 'ping', '{}', 'failed', 1, '2026-01-01', '2026-01-30 08:00:01'),
		($1, 'ping', '{}', 'pending', 0, '2026-01-01', '2026-01-01 07:00:00')`, hook)
	w.Exec(`INSERT INTO pending_blob_deletions (bucket, key, reason, delete_after, created_at) VALUES
		('data-prep', 'datasets/sc/gone 1.parquet', 'sc', '2026-03-02 07:00:00', '2026-03-01'),
		('data-prep', 'datasets/sc/later.parquet', 'sc', '2026-03-09 07:00:00', '2026-03-01')`)
}

func schedulerSteps(w *World) []Step {
	const t1 = "2026-03-02T08:00:00Z"
	tick := func(name, now string, env map[string]string, engine []EngineRule, before func(*World)) Step {
		return Step{Name: name, Method: "TICK", Path: "scheduler", Tick: "scheduler", Now: now, Env: env, Engine: engine, Before: before,
			EngineMask: []string{"*.body.output_key"},
			// the due rows and the runs to reconcile are read without ORDER BY: the tables'
			// physical order, which the two sides' writes leave differently
			EngineUnordered: true}
	}
	due := func(table, column string, ids []int64, at string) func(*World) {
		return func(w *World) {
			for _, id := range ids {
				w.Exec(fmt.Sprintf(`UPDATE %s SET %s = $2 WHERE id = $1`, table, column), id, at)
			}
		}
	}
	dsIDs := func(keys ...string) []int64 {
		var out []int64
		for _, k := range keys {
			out = append(out, SC.DS[k])
		}
		return out
	}
	ids := func(m map[string]int64, keys ...string) []int64 {
		var out []int64
		for _, k := range keys {
			out = append(out, m[k])
		}
		return out
	}
	kube := map[string]string{"CONTAINERS__ENABLED": "true", "CONTAINERS__NAMESPACE": "boxes", "SCRIPTS__TRIAL_NAMESPACE": "trials",
		"KUBERNETES__API_URL": "http://{engine}", "KUBERNETES__TOKEN_FILE": "/etc/hostname"}
	tasks := []EngineRule{
		{Method: "GET", Path: "^/tasks/t-sc-refused$", Body: map[string]any{"status": "SUCCESS", "result": map[string]any{"rows_written": 4,
			"contract": map[string]any{"outcome": "failed", "errors": 1, "rows": 4, "rules": []any{
				map[string]any{"id": "r1", "kind": "not_null", "column": "a", "severity": "error", "passed": false, "violations": 2},
				map[string]any{"id": "r2", "kind": "row_count", "severity": "warning", "passed": false, "observed": 4}}}}}},
		{Method: "GET", Path: "^/tasks/t-sc-warned$", Body: map[string]any{"status": "SUCCESS", "result": map[string]any{"rows_written": 9,
			"columns": []any{map[string]any{"name": "a"}}, "contract": map[string]any{"outcome": "warning", "warnings": 1.0, "rows": 9,
				"rules": []any{map[string]any{"id": "r1", "kind": "unique", "columns": []any{"a", "b"}, "severity": "warning", "passed": false}}}}}},
		{Method: "GET", Path: "^/tasks/t-sc-ok$", Body: map[string]any{"status": "SUCCESS", "result": map[string]any{"rows_written": 3}}},
		{Method: "GET", Path: "^/tasks/t-sc-busy$", Body: map[string]any{"status": "STARTED"}},
		{Method: "GET", Path: "^/tasks/t-sc-stale$", Body: map[string]any{"status": "STARTED"}},
		{Method: "DELETE", Path: "^/tasks/", Body: map[string]any{"ok": true}},
		{Method: "DELETE", Path: "^/apis/batch/v1/namespaces/", Body: map[string]any{"kind": "Status"}},
		{Method: "POST", Path: "^/db/notify$", Body: map[string]any{"ok": true}},
		{Method: "DELETE", Path: "^/files/object$", Status: 404, Body: map[string]any{"detail": "gone"}},
		{Method: "GET", Path: "^/tasks/", Body: map[string]any{"status": "PENDING"}},
	}
	ingest := append([]EngineRule{
		{Method: "POST", Path: "^/db/ingest$", Body: map[string]any{"task_id": "t-sc-db"}},
		{Method: "POST", Path: "^/sftp/ingest$", Body: map[string]any{"task_id": "t-sc-sftp"}},
		{Method: "POST", Path: "^/s3/ingest$", Body: map[string]any{"task_id": "t-sc-s3"}},
		{Method: "POST", Path: "^/http-api/ingest$", Body: map[string]any{"task_id": "t-sc-api", "x": 1}},
		{Method: "POST", Path: "^/gsheets/ingest$", Status: 500, Text: "the sheet is gone"},
		{Method: "POST", Path: "^/sharepoint/ingest$", Body: map[string]any{"task_id": ""}},
	}, tasks...)
	sqlRules := func(first EngineRule) []EngineRule { return append([]EngineRule{first}, tasks...) }
	return []Step{
		tick("the runs reconciled, contracts' freshness and notices, the cleanups, the blobs", t1, kube, tasks, func(w *World) {
			w.Exec(`CREATE OR REPLACE FUNCTION sc_taken() RETURNS trigger AS $$ BEGIN
				IF OLD.name = 'sc taken' AND TG_OP = 'UPDATE' THEN RETURN NULL; END IF; RETURN NEW; END $$ LANGUAGE plpgsql`)
			for _, t := range []string{"datasources", "flows"} {
				w.Exec(fmt.Sprintf(`DROP TRIGGER IF EXISTS sc_taken ON %s`, t))
				w.Exec(fmt.Sprintf(`CREATE TRIGGER sc_taken BEFORE UPDATE ON %s FOR EACH ROW EXECUTE FUNCTION sc_taken()`, t))
			}
		}),
		tick("again: nothing more to do", t1, kube, tasks, nil),
		tick("the datasources' refreshes: every kind of source, authors and rights, busy, refused", "2026-03-02T09:00:00Z", nil, ingest,
			due("datasources", "next_refresh_at", dsIDs("pg", "sftp", "s3", "api", "gsheets", "sharepoint", "noauthor", "inactive", "noconn",
				"norun", "noconnect", "busy", "badcron", "taken", "contract"), "2026-03-02 08:59:00")),
		tick("the slots moved: nothing due an hour later minus a second", "2026-03-02T09:59:59Z", nil, ingest, nil),
		tick("the engine refuses the database's refresh", "2026-03-02T10:00:00Z", nil, append([]EngineRule{
			{Method: "POST", Path: "^/db/ingest$", Status: 503, Text: "busy"}}, tasks...), func(w *World) {
			w.Exec(`UPDATE runs SET status = 'FAILURE', finished_at = '2026-03-02 09:30:00' WHERE task_id = 't-sc-db'`)
		}),
		tick("the flows, the groups: queued, skipped, turned off", "2026-03-02T10:15:00Z", nil, tasks, func(w *World) {
			due("flows", "next_run_at", ids(SC.Flows, "ok", "noauthor", "inactive", "running", "badcron", "taken"), "2026-03-02 10:14:00")(w)
			due("flow_groups", "next_run_at", ids(SC.Groups, "ok", "norun", "gone", "running"), "2026-03-02 10:15:00")(w)
			due("saved_scripts", "next_run_at", ids(SC.Scripts, "ok", "blind"), "2026-03-02 10:00:00")(w)
		}),
		tick("the saved scripts wait while scripting is off; on, they are queued", "2026-03-02T10:20:00Z",
			map[string]string{"SCRIPTS__ENABLED": "true", "SCRIPTS__NAMESPACE": "scripts", "SCRIPTS__REGISTRY": "registry.x.test"}, tasks, nil),
		tick("the SQL scripts write their datasource, or say why not", "2026-03-02T10:30:00Z", nil,
			sqlRules(EngineRule{Method: "POST", Path: "^/db/ingest$", Body: map[string]any{"task_id": "t-sc-sql"}}),
			due("sql_scripts", "next_run_at", ids(SC.SQL, "ok", "busy", "locked", "noconnect", "nosql", "over"), "2026-03-02 10:30:00")),
		tick("a SQL script the engine refuses", "2026-03-02T10:45:00Z", nil,
			sqlRules(EngineRule{Method: "POST", Path: "^/db/ingest$", Status: 422, Body: map[string]any{"detail": "relation «orders» missing"}}),
			due("sql_scripts", "next_run_at", ids(SC.SQL, "refused"), "2026-03-02 10:44:00")),
		tick("a SQL script while the engine goes away", "2026-03-02T11:00:00Z", nil,
			sqlRules(EngineRule{Method: "POST", Path: "^/db/ingest$", Drop: true}),
			due("sql_scripts", "next_run_at", ids(SC.SQL, "down"), "2026-03-02 11:00:00")),
		tick("a SQL script with no task from the engine", "2026-03-02T11:15:00Z", nil,
			sqlRules(EngineRule{Method: "POST", Path: "^/db/ingest$", Body: map[string]any{"no": "task"}}),
			due("sql_scripts", "next_run_at", ids(SC.SQL, "gone"), "2026-03-02 11:15:00")),
		tick("Rome: the night the clocks go forward", "2026-03-29T00:59:00Z", map[string]string{"APP__TIMEZONE": "Europe/Rome"}, ingest,
			func(w *World) {
				// only Rome's: several due at once fire in the tables' physical order
				w.Exec(`UPDATE datasources SET next_refresh_at = NULL WHERE name LIKE 'sc %'`)
				for _, t := range []string{"flows", "flow_groups", "saved_scripts", "sql_scripts"} {
					w.Exec(fmt.Sprintf(`UPDATE %s SET next_run_at = NULL WHERE name LIKE 'sc %%'`, t))
				}
				due("datasources", "next_refresh_at", dsIDs("rome"), "2026-03-28 01:30:00")(w)
				due("flows", "next_run_at", ids(SC.Flows, "rome"), "2026-03-28 02:00:00")(w)
			}),
		tick("the panel's zone over the environment's", "2026-03-30T02:00:00Z", map[string]string{"APP__TIMEZONE": "Europe/Rome"}, ingest,
			func(w *World) {
				w.Exec(`INSERT INTO gateway_settings (key, value, updated_at) VALUES ('app.timezone', '"America/New_York"', now())`)
				due("datasources", "next_refresh_at", dsIDs("rome"), "2026-03-30 01:00:00")(w)
				due("flows", "next_run_at", ids(SC.Flows, "rome"), "2026-03-30 01:00:00")(w)
			}),
		tick("days later: blobs past their grace refused by the engine, the tokens and sessions gone", "2026-03-10T08:00:00Z", nil,
			append([]EngineRule{{Method: "DELETE", Path: "^/files/object$", Status: 500, Text: "nope"}}, tasks...), func(w *World) {
				w.Exec(`DELETE FROM gateway_settings WHERE key = 'app.timezone'`)
				for _, t := range []string{"datasources", "flows"} {
					w.Exec(fmt.Sprintf(`DROP TRIGGER IF EXISTS sc_taken ON %s`, t))
				}
			}),
		tick("the parquets' random names put away", "2026-03-10T08:00:00Z", nil, tasks, putAwayRandomKeys),
		tidyRunsStep,
	}
}

// putAwayRandomKeys gives the parquets the rounds named at random a fixed name, at the end
// of a jobs' suite: the suites after it list the runs and the datasources (GET /runs), and
// would see the two sides' random names.
func putAwayRandomKeys(w *World) {
	for _, tc := range [][2]string{{"runs", "output_key"}, {"runs", "input_key"}, {"datasources", "key"}, {"pending_blob_deletions", "key"},
		{"data_contract_results", "snapshot_key"}} {
		w.Exec(fmt.Sprintf(`UPDATE %s SET %s = regexp_replace(regexp_replace(%s, '-[0-9a-f]{8}\.parquet$', '-00000000.parquet'),
			'^out/[0-9a-f]{32}\.parquet$', 'out/0.parquet')`, tc[0], tc[1], tc[1]))
	}
	// a Script run's step (a uuid) and a saved script's run (a random task id)
	w.Exec(`UPDATE runs SET script = regexp_replace(script, '[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', 'step', 'g'),
		destination = regexp_replace(destination, '[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', 'step', 'g'),
		task_id = regexp_replace(task_id, '^[0-9a-f]{32}$', 'task')`)
	// the runs a round wrote together start at the same frozen moment: apart by their id, or
	// the history (ORDER BY started_at) lists them in the tables' physical order
	w.Exec(`UPDATE runs SET started_at = started_at + make_interval(secs => id / 1000000.0)
		WHERE sql_script_id IN (SELECT id FROM sql_scripts WHERE name LIKE 'sc sql %')
		OR flow_id IN (SELECT id FROM flows WHERE name LIKE 'sc %' OR name LIKE 'qu %')
		OR datasource_id IN (SELECT id FROM datasources WHERE name LIKE 'sc %' OR name LIKE 'qu %')`)
}
