package main

import (
	"fmt"
)

// QU: what the queue's suite seeds.
var QU = struct {
	Main, Locked, PG, SMTP, Src int64
	Flows                       map[string]int64
}{}

// services/orchestrator.py queue_loop, one round at a time: the orchestrations taken from the
// queue and carried to their end (refresh, outputs, sub-flows), the refused launches, the runs
// of processes that are gone, the process's own at start and at stop, a heartbeat.
func init() {
	scrubColumn("runs", "output_key", `out/[0-9a-f]{32}\.parquet`)
	// a refresh's new snapshot (a random name) is what the outputs after it read
	scrubColumn("runs", "input_key", `-[0-9a-f]{8}\.parquet`)
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
		tick("the parquets' random names put away", "queue.beat", ok, putAwayRandomKeys),
	}
}
