package main

import (
	"fmt"
	"strings"
)

// QU: what the queue's suite seeds.
var QU = struct {
	Main, Locked, PG, SMTP, Src, Env, Saved, Registry int64
	Flows                                             map[string]int64
}{}

// services/orchestrator.py queue_loop, one round at a time: the orchestrations taken from the
// queue and carried to their end (refresh, outputs, sub-flows), the refused launches, the runs
// of processes that are gone, the process's own at start and at stop, a heartbeat.
func init() {
	scrubColumn("runs", "output_key", `out/[0-9a-f]{32}\.parquet`)
	// a refresh's new snapshot (a random name) is what the outputs after it read
	scrubColumn("runs", "input_key", `-[0-9a-f]{8}\.parquet`, `out/[0-9a-f]{32}\.parquet`)
	// a Script or Container run's step: a uuid of its own
	for _, c := range []string{"script", "destination"} {
		scrubColumn("runs", c, `[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}`)
	}
	scrubColumn("datasources", "key", `out/[0-9a-f]{32}\.parquet`)
	// a saved script's run: a random task id
	scrubColumn("runs", "task_id", `^[0-9a-f]{32}$`)
	register(Suite{Module: "jobs_queue", Seed: seedQueue, Steps: queueSteps})
}

func seedQueue(w *World) {
	admin, writer := w.People["admin"], w.People["writer"]
	folder := func(name string) int64 {
		return w.Insert(`INSERT INTO projects (name, description, parent_id, owner_id, created_at) VALUES ($1, '', NULL, $2, '2026-01-01')
			RETURNING id`, name, admin)
	}
	QU.Main, QU.Locked = folder("qu-Main"), folder("qu-Locked")
	for _, c := range []string{"edit", "connect"} {
		w.Exec(`INSERT INTO permissions (project_id, user_id, group_id, capability) VALUES ($1, $2, NULL, $3)`, QU.Main, writer, c)
	}
	w.Exec(`INSERT INTO permissions (project_id, user_id, group_id, capability) VALUES ($1, $2, NULL, 'view')`, QU.Locked, writer)
	conn := func(name, kind, extra string) int64 {
		return w.Insert(`INSERT INTO connections (name, description, project_id, owner_id, db_type, host, port, username, password_encrypted,
			database, db_schema, extra, created_at, updated_at) VALUES ($1, '', $2, $3, $4, 'h.x.test', NULL, 'u', 'enc', 'db', '', $5,
			'2026-01-01', '2026-01-01') RETURNING id`, name, QU.Main, admin, kind, extra)
	}
	QU.PG = conn("qu-pg", "postgresql", `{}`)
	QU.SMTP = conn("qu-smtp", "smtp", `{"allowed_domains": ["x.test"]}`)
	QU.Src = w.Insert(`INSERT INTO datasources (name, description, project_id, owner_id, bucket, key, columns, sort_keys, column_descriptions,
		kind, connection_id, source_type, source_ref, created_at, updated_at) VALUES ('qu src', '', $1, $2, 'data-prep',
		'datasets/qu/src.parquet', '[]', '[]', '{}', 'database', $3, 'table', 'public.src', '2026-01-01', '2026-01-01') RETURNING id`,
		QU.Main, admin, QU.PG)
	QU.Flows = map[string]int64{}
	flow := func(key, definition string, notify bool) {
		var emails, smtp any
		if notify {
			emails, smtp = "writer@x.test, editor@x.test", QU.SMTP
		}
		QU.Flows[key] = w.Insert(`INSERT INTO flows (name, description, project_id, owner_id, definition, engine, production_engine,
			notify_emails, notify_connection_id, created_at, updated_at) VALUES ($1, '', $2, $3, $4, 'duckdb', 'polars', $5, $6, '2026-01-01',
			'2026-01-01') RETURNING id`, "qu "+key, QU.Main, admin, definition, emails, smtp)
	}
	src := fmt.Sprintf(`{"id": "s", "type": "source", "data": {"datasourceId": %d, "filters": [{"column": "a", "operator": "gt", "value": 1},
		{"column": "b", "operator": "in", "value": []}], "sample": {"mode": "first", "rows": 10}}}`, QU.Src)
	flow("sub", fmt.Sprintf(`{"nodes": [%s, {"id": "o", "type": "output", "data": {"destType": "database", "connectionId": %d,
		"table": " sales.t ", "mode": "replace"}}], "edges": [{"source": "s", "target": "o"}]}`, src, QU.PG), false)
	flow("ok", fmt.Sprintf(`{"nodes": [%s, {"id": "f", "type": "operation", "data": {"opType": "filter", "params": {"column": "a"}}},
		{"id": "o", "type": "output", "data": {"name": " qu out ", "projectId": %d, "description": "made", "sortKeys": ["a", " "]}},
		{"id": "r", "type": "refresh", "data": {"datasourceId": %d}}, {"id": "x", "type": "runflow", "data": {"flowId": %d}},
		{"id": "y", "type": "runflow", "data": {"flowId": 999999}}],
		"edges": [{"source": "s", "target": "f"}, {"source": "f", "target": "o"}, {"source": "o", "target": "x", "targetHandle": "seq-in"}]}`,
		src, QU.Main, QU.Src, QU.Flows["sub"]), false)
	flow("email", fmt.Sprintf(`{"nodes": [%s, {"id": "e", "type": "output", "data": {"destType": "email", "connectionId": %d,
		"emailTo": ["boss@x.test", " "], "emailSubject": "Daily", "attachmentName": "", "attachmentFormat": "csv"}},
		{"id": "o", "type": "output", "data": {"name": "qu after", "projectId": %d}}],
		"edges": [{"source": "s", "target": "e"}, {"source": "s", "target": "o"}, {"source": "e", "target": "o", "targetHandle": "seq-in"}]}`,
		src, QU.SMTP, QU.Main), false)
	flow("outside", fmt.Sprintf(`{"nodes": [%s, {"id": "e", "type": "output", "data": {"destType": "email", "connectionId": %d,
		"emailTo": ["who@y.test"]}}], "edges": [{"source": "s", "target": "e"}]}`, src, QU.SMTP), false)
	flow("locked", fmt.Sprintf(`{"nodes": [%s, {"id": "o", "type": "output", "data": {"name": "qu locked", "projectId": %d}}],
		"edges": [{"source": "s", "target": "o"}]}`, src, QU.Locked), true)
	flow("broken", `{"nodes": [{"id": "o", "type": "output", "data": {"name": "nothing", "projectId": 1}}], "edges": []}`, false)
	flow("plain", fmt.Sprintf(`{"nodes": [%s, {"id": "o", "type": "output", "data": {"name": "qu plain", "projectId": %d}}],
		"edges": [{"source": "s", "target": "o"}]}`, src, QU.Main), true)
	// scripting: the bucket, an environment with a built version and its variables, flows with a Script node
	secretKey, _ := fernet.Encrypt("sk-1")
	token, _ := fernet.Encrypt("t0k")
	w.Exec(`INSERT INTO script_storage (id, endpoint, region, bucket, access_key, secret_key_enc) VALUES (1, 'http://minio:9000', '',
		'scripts', 'AK', $1) ON CONFLICT (id) DO UPDATE SET endpoint = EXCLUDED.endpoint, region = '', bucket = 'scripts', access_key = 'AK',
		secret_key_enc = EXCLUDED.secret_key_enc`, secretKey)
	QU.Env = w.Insert(`INSERT INTO script_environments (name, description, language, enabled, cpu_request, cpu_limit, memory_request,
		memory_limit, max_timeout_seconds, created_at, updated_at) VALUES ('qu-env', '', 'python', true, '500m', '1', '1Gi', '2Gi', 3600,
		'2026-01-01', '2026-01-01') RETURNING id`)
	version := w.Insert(`INSERT INTO script_environment_versions (environment_id, number, language_version, base_image, packages, status,
		image_ref, arch, created_at) VALUES ($1, 3, '3.12', 'python:3.12-slim', '', 'ready', 'reg.x.test/env@sha256:abc', 'amd64', '2026-01-01')
		RETURNING id`, QU.Env)
	w.Exec(`UPDATE script_environments SET current_version_id = $2 WHERE id = $1`, QU.Env, version)
	w.Exec(`INSERT INTO script_environment_variables (environment_id, name, secret, value, value_enc, from_tabularia, updated_at) VALUES
		($1, 'MODE', false, 'prod', NULL, false, '2026-01-01'), ($1, 'TOKEN', true, '', $2, false, '2026-02-03 04:05:06')`, QU.Env, token)
	script := fmt.Sprintf(`{"id": "s1", "type": "script", "data": {"label": "Clean", "environmentId": %d, "entrypoint": "main.py",
		"files": [{"path": "main.py", "content": "import util\nprint(1)"}, {"path": "util.py", "content": "X = 1"}], "timeoutSeconds": 60}}`, QU.Env)
	flow("script", fmt.Sprintf(`{"nodes": [%s, %s, {"id": "o", "type": "output", "data": {"name": "qu scripted", "projectId": %d}}],
		"edges": [{"source": "s", "target": "s1"}, {"source": "s1", "target": "o"}]}`, src, script, QU.Main), false)

	// containers: a registry reached as siem.test (each stack's own fake engine), a flow with a Container node
	QU.Registry = w.Insert(`INSERT INTO container_registries (name, description, host, protocol, tls, ca_cert, username, repositories,
		enabled, last_test_message, created_at, updated_at) VALUES ('qu-reg', '', 'siem.test:8000', 'http', 'public', '', '', 'team/*', true,
		'', '2026-01-01', '2026-01-01') RETURNING id`)
	apiKey, _ := fernet.Encrypt("k-9")
	w.Exec(`INSERT INTO container_registry_variables (registry_id, name, secret, value, value_enc, updated_at) VALUES
		($1, 'LEVEL', false, '3', NULL, '2026-01-01'), ($1, 'API_KEY', true, '', $2, '2026-01-01')`, QU.Registry, apiKey)
	box := fmt.Sprintf(`{"id": "c1", "type": "container", "data": {"label": "Box", "registryId": %d, "repository": "team/app",
		"tag": "1.0", "command": ["run"], "args": ["--x", 2], "variables": [{"name": "N", "value": "v"}], "timeoutSeconds": 60}}`, QU.Registry)
	flow("boxed", fmt.Sprintf(`{"nodes": [%s, %s, {"id": "o", "type": "output", "data": {"name": "qu boxed", "projectId": %d}}],
		"edges": [{"source": "s", "target": "c1"}, {"source": "c1", "target": "o"}]}`, src, box, QU.Main), false)
	QU.Saved = w.Insert(`INSERT INTO saved_scripts (name, description, project_id, owner_id, environment_id, current_version, created_at,
		updated_at) VALUES ('qu saved', '', $1, $2, $3, 2, '2026-01-01', '2026-01-01') RETURNING id`, QU.Main, admin, QU.Env)
	w.Exec(`INSERT INTO saved_script_versions (script_id, version, content, note, created_at, created_by) VALUES ($1, 1, '{}', 'created',
		'2026-01-01', $2), ($1, 2, $3, 'edited', '2026-01-01', $2)`, QU.Saved, admin, fmt.Sprintf(`{"files": [{"path": "run.py",
		"content": "print(2)"}], "entrypoint": "run.py", "inputs": [{"alias": "SALES", "datasource_id": %d}], "output": {"publish": true,
		"name": "qu saved out", "project_id": %d, "overwrite": true}, "timeout_seconds": 120, "retries": 0, "retry_delay_seconds": 60}`,
		QU.Src, QU.Main))

	// the previous scheduled run of «plain» went well: its failure is news
	w.Exec(`INSERT INTO runs (kind, flow_id, task_id, status, launched_by, trigger_type, engine_mode, input_key, output_bucket, output_key,
		publish_description, publish_overwrite, publish_sort_keys, started_at, finished_at) VALUES ('orchestration', $1, '', 'SUCCESS', $2,
		'schedule', 'production', '', '', '', '', false, '[]', '2026-04-01 03:00:00', '2026-04-01 03:01:00')`, QU.Flows["plain"], writer)
}

// orch inserts an orchestration row.
// orchSeq keeps the seeded runs' starts apart (never two at once: the history's order)
var orchSeq int

func orch(w *World, flow int64, status, trigger, by string, started string, claimed, beat any) int64 {
	orchSeq++
	return w.Insert(`INSERT INTO runs (kind, flow_id, task_id, status, launched_by, trigger_type, engine_mode, input_key, output_bucket,
		output_key, publish_description, publish_overwrite, publish_sort_keys, started_at, claimed_by, heartbeat_at)
		VALUES ('orchestration', $1, '', $2, $3, $4, $5, '', '', '', '', false, '[]', $6::timestamp + make_interval(secs => $9), $7, $8)
		RETURNING id`, flow, status, w.People[by], trigger, map[string]string{"manual": "development", "schedule": "production"}[trigger],
		started, claimed, beat, float64(orchSeq)/1000)
}

// orchSaved queues a run of the saved script.
func orchSaved(w *World, trigger, by string) {
	orchSeq++
	w.Exec(`INSERT INTO runs (kind, saved_script_id, task_id, status, launched_by, trigger_type, engine_mode, input_key, output_bucket,
		output_key, publish_description, publish_overwrite, publish_sort_keys, started_at) VALUES ('orchestration', $1, '', 'PENDING', $2, $3,
		'production', '', '', '', '', false, '[]', '2026-04-02 08:59:00'::timestamp + make_interval(secs => $4))`, QU.Saved, w.People[by],
		trigger, float64(orchSeq)/1000)
}

func queueSteps(w *World) []Step {
	at := "2026-04-02T09:00:00Z"
	tick := func(name, round string, engine []EngineRule, before func(*World)) Step {
		return Step{Name: name, Method: "TICK", Path: round, Tick: round, Now: at, Engine: engine, Before: before,
			EngineMask: []string{"*.body.output_key", "*.body.input_key"}, Unordered: []string{"", "taken", "refused"}}
	}
	ok := []EngineRule{
		{Method: "POST", Path: "^/db/ingest$", Body: map[string]any{"task_id": "t-qu-ing"}},
		{Method: "GET", Path: "^/tasks/t-qu-ing$", Body: map[string]any{"status": "SUCCESS", "result": map[string]any{"rows_written": 7,
			"columns": []any{map[string]any{"name": "a"}}}}},
		{Method: "POST", Path: "^/tasks/transform-data$", Body: map[string]any{"task_id": "t-qu-out"}},
		{Method: "GET", Path: "^/tasks/t-qu-out$", Body: map[string]any{"status": "SUCCESS", "result": map[string]any{"rows_written": 2}}},
		{Method: "GET", Path: "^/tasks/t-qu-child$", Body: map[string]any{"status": "STARTED"}},
		{Method: "DELETE", Path: "^/tasks/", Body: map[string]any{"ok": true}},
		{Method: "POST", Path: "^/db/notify$", Body: map[string]any{"ok": true}},
		{Method: "GET", Path: "^/tasks/", Body: map[string]any{"status": "PENDING"}},
	}
	failing := append([]EngineRule{{Method: "GET", Path: "^/tasks/t-qu-out$", Body: map[string]any{"status": "FAILURE",
		"error": "SMTP: the server refused the message"}}}, ok...)
	kubeEnv := map[string]string{"SCRIPTS__ENABLED": "true", "SCRIPTS__NAMESPACE": "scripts", "SCRIPTS__REGISTRY": "reg.x.test",
		"KUBERNETES__API_URL": "http://{engine}", "KUBERNETES__TOKEN_FILE": "/etc/hostname"}
	kubeTick := func(name string, engine []EngineRule, before func(*World)) Step {
		st := tick(name, "queue", engine, before)
		st.Env = kubeEnv
		// the step's uuid, the outputs' random names
		st.EngineScrub = []string{`[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}`, `out/[0-9a-f]{32}\.parquet`, `-[0-9a-f]{8}\.parquet`}
		st.EngineMask = []string{"*.body.output_key", "*.body.input_key"}
		return st
	}
	jobIn := func(ns string, status map[string]any, pod map[string]any) []EngineRule {
		return append([]EngineRule{
			{Method: "POST", Path: "^/apis/batch/v1/namespaces/" + ns + "/jobs$", Body: map[string]any{"metadata": map[string]any{"name": "j", "uid": "u-1"}}},
			{Method: "POST", Path: "^/api/v1/namespaces/" + ns + "/(configmaps|secrets)$", Body: map[string]any{}},
			{Method: "GET", Path: "^/apis/batch/v1/namespaces/" + ns + "/jobs/", Body: map[string]any{"status": status}},
			{Method: "GET", Path: "^/api/v1/namespaces/" + ns + "/pods$", Body: map[string]any{"items": []any{map[string]any{
				"metadata": map[string]any{"name": "p1", "creationTimestamp": "2026-04-02T09:00:00Z"}, "status": pod}}}},
			{Method: "GET", Path: "^/api/v1/namespaces/" + ns + "/pods/p1/log$", Text: "hello\nworld", Type: "text/plain"},
			{Method: "DELETE", Path: "^/apis/batch/v1/namespaces/" + ns + "/jobs/", Body: map[string]any{}},
			{Method: "POST", Path: "^/scripts/output/import$", Body: map[string]any{"found": true, "rows": 4}},
			{Method: "POST", Path: "^/scripts/objects/delete$", Body: map[string]any{}},
		}, ok...)
	}
	boxTick := func(name string, engine []EngineRule, before func(*World)) Step {
		st := kubeTick(name, engine, before)
		st.Env = map[string]string{"CONTAINERS__ENABLED": "true", "CONTAINERS__NAMESPACE": "boxes", "KUBERNETES__API_URL": "http://{engine}",
			"KUBERNETES__TOKEN_FILE": "/etc/hostname"}
		return st
	}
	job := func(status map[string]any, pod map[string]any) []EngineRule { return jobIn("scripts", status, pod) }
	terminated := func(code int, reason string) map[string]any {
		return map[string]any{"containerStatuses": []any{map[string]any{"state": map[string]any{"terminated": map[string]any{
			"exitCode": code, "reason": reason}}}}}
	}
	kubeOK := job(map[string]any{"succeeded": 1}, terminated(0, "Completed"))
	savedOK := append([]EngineRule{{Method: "POST", Path: "^/scripts/input/export$", Body: map[string]any{"ok": true}},
		{Method: "POST", Path: "^/scripts/output/import$", Body: map[string]any{"found": true, "rows": 6,
			"columns": []any{map[string]any{"name": "a", "dtype": "Int64"}}}}}, kubeOK...)
	digest := "sha256:" + strings.Repeat("ab12", 16)
	boxOK := append([]EngineRule{{Method: "GET", Path: "^/v2/$", Body: map[string]any{}},
		{Method: "HEAD", Path: "^/v2/team/app/manifests/1.0$", Headers: map[string]string{"Docker-Content-Digest": digest}},
		{Method: "DELETE", Path: "^/api/v1/namespaces/boxes/secrets/", Status: 404, Body: map[string]any{"message": "not found"}}},
		jobIn("boxes", map[string]any{"succeeded": 1}, terminated(0, "Completed"))...)
	boxGone := append([]EngineRule{{Method: "GET", Path: "^/v2/$", Body: map[string]any{}},
		{Method: "HEAD", Path: "^/v2/team/app/manifests/1.0$", Status: 404}}, ok...)
	kubeFailed := job(map[string]any{"failed": 1, "conditions": []any{map[string]any{"type": "Failed", "status": "True",
		"reason": "BackoffLimitExceeded", "message": "Job has reached the specified backoff limit"}}}, terminated(1, "Error"))
	return []Step{
		tick("the orchestrations of a process that is gone, and one queued too long", "queue.orphans", ok, func(w *World) {
			gone := orch(w, QU.Flows["plain"], "STARTED", "manual", "writer", "2026-04-02 08:00:00", "other:9:aaaa", "2026-04-02 08:58:00")
			orch(w, QU.Flows["sub"], "STARTED", "manual", "writer", "2026-04-02 08:00:00", "other:9:bbbb", "2026-04-02 08:59:30")
			w.Exec(`INSERT INTO runs (kind, flow_id, parent_run_id, task_id, status, launched_by, trigger_type, input_key, output_bucket,
				output_key, publish_description, publish_overwrite, publish_sort_keys, started_at) VALUES
				('flow', $1, $2, 't-qu-child', 'STARTED', $3, 'manual', '', 'data-prep', 'out/c.parquet', '', false, '[]', '2026-04-02 08:01:00'),
				('script', $1, $2, 'job-qu', 'STARTED', $3, 'manual', '', '', '', '', false, '[]', '2026-04-02 08:02:00')`,
				QU.Flows["plain"], gone, w.People["writer"])
			orch(w, QU.Flows["plain"], "PENDING", "schedule", "writer", "2026-04-01 09:00:00", nil, nil)
		}),
		tick("at start: the runs of an earlier start of this process", "queue.mine_start", ok, func(w *World) {
			orch(w, QU.Flows["broken"], "STARTED", "manual", "writer", "2026-04-02 08:30:00", "diffcheck:1:00000000", "2026-04-02 08:59:00")
			orch(w, QU.Flows["outside"], "STARTED", "manual", "writer", "2026-04-02 08:30:00", "diffcheck:1:0000beef", "2026-04-02 08:59:00")
		}),
		tick("at stop: its own", "queue.mine_stop", ok, nil),
		tick("a heartbeat with nothing running", "queue.beat", ok, nil),
		tick("a run: refresh, published output, sub-flow to a table; a second launch refused, a slot skipped", "queue", ok,
			func(w *World) {
				orch(w, QU.Flows["ok"], "PENDING", "manual", "writer", "2026-04-02 08:59:00", nil, nil)
				orch(w, QU.Flows["ok"], "PENDING", "manual", "editor", "2026-04-02 08:59:10", nil, nil)
				orch(w, QU.Flows["ok"], "PENDING", "schedule", "writer", "2026-04-02 08:59:20", nil, nil)
			}),
		tick("an email that fails stops the sequence", "queue", failing, func(w *World) {
			orch(w, QU.Flows["email"], "PENDING", "manual", "writer", "2026-04-02 08:59:00", nil, nil)
		}),
		tick("an email outside the allowed domains is refused at launch", "queue", ok, func(w *World) {
			orch(w, QU.Flows["outside"], "PENDING", "manual", "writer", "2026-04-02 08:59:00", nil, nil)
		}),
		tick("scheduled runs: one without the rights (its notice goes out), one that runs", "queue", ok, func(w *World) {
			orch(w, QU.Flows["locked"], "PENDING", "schedule", "writer", "2026-04-02 08:59:00", nil, nil)
			orch(w, QU.Flows["plain"], "PENDING", "schedule", "writer", "2026-04-02 08:59:01", nil, nil)
		}),
		tick("an invalid user, a flow that cannot resolve", "queue", ok, func(w *World) {
			orch(w, QU.Flows["plain"], "PENDING", "manual", "inactive", "2026-04-02 08:59:00", nil, nil)
			orch(w, QU.Flows["broken"], "PENDING", "manual", "writer", "2026-04-02 08:59:01", nil, nil)
		}),
		tick("a scheduled run that fails: notice to the flow's addresses", "queue", failing, func(w *World) {
			orch(w, QU.Flows["plain"], "PENDING", "schedule", "admin", "2026-04-02 08:59:00", nil, nil)
		}),
		kubeTick("a Script node: its input prepared, its Job run, its output read and published", kubeOK, func(w *World) {
			orch(w, QU.Flows["script"], "PENDING", "manual", "admin", "2026-04-02 08:59:00", nil, nil)
		}),
		kubeTick("a Script node whose Job fails stops the flow", kubeFailed, func(w *World) {
			orch(w, QU.Flows["script"], "PENDING", "manual", "admin", "2026-04-02 08:59:00", nil, nil)
		}),
		kubeTick("a Script node in an environment its author may not use: the schedule does not start", kubeOK, func(w *World) {
			orch(w, QU.Flows["script"], "PENDING", "schedule", "writer", "2026-04-02 08:59:00", nil, nil)
		}),
		kubeTick("a Script node by hand by whoever may not use its environment", kubeOK, func(w *World) {
			orch(w, QU.Flows["script"], "PENDING", "manual", "writer", "2026-04-02 08:59:00", nil, nil)
		}),
		kubeTick("a saved script: its input copied, its Job run, its output published", savedOK, func(w *World) {
			orchSaved(w, "manual", "admin")
		}),
		kubeTick("a saved script's schedule whose author cannot use its environment", savedOK, func(w *World) {
			orchSaved(w, "schedule", "writer")
		}),
		boxTick("a Container node: the tag's digest, the pull Secret, its Job, its output published", boxOK, func(w *World) {
			orch(w, QU.Flows["boxed"], "PENDING", "manual", "admin", "2026-04-02 08:59:00", nil, nil)
		}),
		boxTick("a Container node whose tag is not in the registry", boxGone, func(w *World) {
			orch(w, QU.Flows["boxed"], "PENDING", "manual", "admin", "2026-04-02 08:59:00", nil, nil)
		}),
		boxTick("a Container node's schedule whose author may not use the registry", boxOK, func(w *World) {
			orch(w, QU.Flows["boxed"], "PENDING", "schedule", "writer", "2026-04-02 08:59:00", nil, nil)
		}),
		tick("the parquets' random names put away", "queue.beat", ok, putAwayRandomKeys),
	}
}
