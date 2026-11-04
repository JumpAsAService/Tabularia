package main

import (
	"fmt"
	"net/http"
	"time"
)

// RN: what the runs' suite adds to the flows' seed.
var RN = struct {
	seeded                                  bool
	Live                                    map[string]int64
	Group, SQL, Ingest, GroupRun, ScriptRun int64
	Recent                                  time.Time
}{}

func (w *World) seedRuns() {
	w.seedFlows()
	if RN.seeded {
		return
	}
	RN.seeded = true
	RN.Recent = time.Now().UTC().Add(-50 * time.Hour).Truncate(time.Second)
	RN.Group = w.Insert(`INSERT INTO flow_groups (name, description, project_id, owner_id, definition, created_at, updated_at)
		VALUES ('fl group', '', $1, $2, '{"steps": []}', '2026-01-01', '2026-01-01') RETURNING id`, FL.Other, w.People["admin"])
	RN.SQL = w.Insert(`INSERT INTO sql_scripts (name, description, project_id, owner_id, current_version, created_at, updated_at)
		VALUES ('fl sql', '', $1, $2, 1, '2026-01-01', '2026-01-01') RETURNING id`, FL.Hidden, w.People["admin"])
	run := func(kind string, flow, ds, group, script, sql *int64, status, trigger, by string, started time.Time, err *string) int64 {
		return w.Insert(`INSERT INTO runs (kind, flow_id, datasource_id, flow_group_id, saved_script_id, sql_script_id, task_id, status,
			launched_by, trigger_type, input_key, output_bucket, output_key, publish_description, publish_overwrite, publish_sort_keys,
			started_at, finished_at, error) VALUES ($1, $2, $3, $4, $5, $6, 't', $7, $8, $9, '', 'data-prep', 'out/k.parquet', '', false,
			'[]', $10::timestamp, $10::timestamp + interval '5 seconds', $11) RETURNING id`, kind, flow, ds, group, script, sql, status, w.People[by], trigger,
			started, err)
	}
	day := func(d, h int) time.Time {
		return RN.Recent.Add(time.Duration(-d)*24*time.Hour + time.Duration(h)*time.Hour)
	}
	run("flow", &FL.Gamma, nil, nil, nil, nil, "SUCCESS", "schedule", "admin", day(0, 0), nil)
	run("flow", &FL.Gamma, nil, nil, nil, nil, "FAILURE", "manual", "writer", day(1, 3), ptr("boom: Kaputt"))
	run("orchestration", &FL.Secret, nil, nil, nil, nil, "FAILURE", "schedule", "admin", day(2, 5), ptr("secret failure"))
	RN.Ingest = run("ingest", nil, &FL.HiddenDS, nil, nil, nil, "SUCCESS", "manual", "admin", day(3, 1), nil)
	run("ingest", nil, &FL.OtherDS, nil, nil, nil, "PENDING", "schedule", "admin", day(0, 23), nil)
	RN.GroupRun = run("orchestration", nil, nil, &RN.Group, nil, nil, "SUCCESS", "manual", "editor", day(4, 2), nil)
	RN.ScriptRun = run("script", nil, nil, nil, &FL.Script, nil, "FAILURE", "manual", "admin", day(5, 0), ptr("script error"))
	run("sql", nil, nil, nil, nil, &RN.SQL, "SUCCESS", "manual", "writer", day(6, 4), nil)
	run("flow", &FL.Alpha, nil, nil, nil, nil, "STARTED", "manual", "obs_flag", day(400, 0), nil)

	// runs not finished, each with a task of its own on the engine (see the suite's rules)
	now := time.Now().UTC().Truncate(time.Second)
	RN.Live = map[string]int64{}
	n := 0
	live := func(name, kind string, flow, ds *int64, status, trigger, by string, age time.Duration, extra string, args ...any) {
		n++
		started := now.Add(-age - time.Duration(n)*time.Second) // never two at once: their order is the history's
		id := w.Insert(`INSERT INTO runs (kind, flow_id, datasource_id, task_id, status, launched_by, trigger_type, input_key,
			output_bucket, output_key, publish_description, publish_overwrite, publish_sort_keys, started_at)
			VALUES ($1, $2, $3, $4, $5, $6, $7, 'datasets/in.parquet', 'data-prep', $8, '', false, '["a"]', $9) RETURNING id`,
			kind, flow, ds, "t-"+name, status, w.People[by], trigger, "datasets/new/"+name+".parquet", started)
		if extra != "" {
			w.Exec(`UPDATE runs SET `+extra+` WHERE id = $1`, append([]any{id}, args...)...)
		}
		RN.Live[name] = id
	}
	h := time.Hour
	live("starts", "flow", &FL.Gamma, nil, "PENDING", "manual", "writer", h, "")
	live("succeeds", "flow", &FL.Gamma, nil, "STARTED", "manual", "writer", h, `destination = $2, email = $3`,
		`{"type": "sftp", "host": "h:22", "file_name": "a.csv"}`, `{"connection_id": 1, "to": ["a@x.it"], "ok": null}`)
	live("fails", "flow", &FL.Gamma, nil, "STARTED", "manual", "writer", h, `email = $2`, `{"to": ["a@x.it"], "ok": null}`)
	live("oom", "flow", &FL.Gamma, nil, "STARTED", "manual", "writer", h, "")
	live("revoked", "flow", &FL.Gamma, nil, "STARTED", "manual", "writer", h, "")
	live("stale", "flow", &FL.Gamma, nil, "STARTED", "manual", "writer", 3*h, `engine_started_at = $2`, now.Add(-2*h))
	live("silent", "flow", &FL.Gamma, nil, "PENDING", "manual", "writer", h, "")
	live("garbled", "flow", &FL.Gamma, nil, "PENDING", "manual", "writer", h, "")
	live("listed", "flow", &FL.Gamma, nil, "PENDING", "manual", "writer", h, "")
	live("publishes", "flow", &FL.Gamma, nil, "STARTED", "manual", "writer", h, `publish_name = 'fl published', publish_project_id = $2,
		publish_description = 'out'`, FL.Other)
	live("clashes", "flow", &FL.Gamma, nil, "STARTED", "manual", "writer", h, `publish_name = 'fl-other-ds', publish_project_id = $2`, FL.Other)
	live("overwrites", "flow", &FL.Gamma, nil, "STARTED", "manual", "writer", h, `publish_name = 'fl overwritten', publish_project_id = $2,
		publish_overwrite = true`, FL.Other)
	w.Exec(`INSERT INTO datasources (name, description, project_id, owner_id, bucket, key, columns, sort_keys, column_descriptions, kind,
		created_at, updated_at) VALUES ('fl overwritten', '', $1, NULL, 'data-prep', 'datasets/old.parquet', '[]', '[]', '{}', 'flow',
		'2026-01-01', '2026-01-01')`, FL.Other)
	live("refreshes", "ingest", nil, &FL.OtherDS, "STARTED", "manual", "admin", h, "")
	live("placeholder", "flow", &FL.Gamma, nil, "PENDING", "manual", "writer", 10*time.Minute, `task_id = 'pending-abc'`)
	live("queued", "orchestration", &FL.Gamma, nil, "PENDING", "manual", "writer", 20*time.Minute, "")
	live("scheduled", "orchestration", &FL.Gamma, nil, "PENDING", "schedule", "admin", 20*time.Minute, "")
	live("fresh", "orchestration", &FL.Gamma, nil, "PENDING", "manual", "writer", time.Minute, "")
	live("script", "script", &FL.Gamma, nil, "STARTED", "manual", "writer", h, "")
	contracted := w.Insert(`INSERT INTO datasources (name, description, project_id, owner_id, bucket, key, columns, sort_keys,
		column_descriptions, kind, created_at, updated_at) VALUES ('fl contracted', '', $1, NULL, 'data-prep', 'datasets/c.parquet', '[]',
		'[]', '{}', 'database', '2026-01-01', '2026-01-01') RETURNING id`, FL.Other)
	w.Exec(`INSERT INTO data_contracts (datasource_id, version, document, enabled, status, errors, warnings, created_at, updated_at)
		VALUES ($1, 1, '{"rules": [{"kind": "not_null", "column": "a"}]}', true, 'unknown', 0, 0, '2026-01-01', '2026-01-01')`, contracted)
	live("contracted", "ingest", nil, &contracted, "STARTED", "manual", "admin", h, "")
	live("orphan", "ingest", nil, nil, "SUCCESS", "manual", "writer", h, "")
	w.Exec(`INSERT INTO webhooks (name, url, secret_enc, events, project_id, active, created_at) VALUES
		('fl all', 'https://hooks.x.test/a', 'x', '["flow.run.failed", "flow.run.succeeded", "datasource.refresh.succeeded"]', NULL, true, '2026-01-01'),
		('fl other', 'https://hooks.x.test/b', 'x', '["flow.run.failed"]', $1, true, '2026-01-01'),
		('fl sales', 'https://hooks.x.test/c', 'x', '["flow.run.failed"]', $2, true, '2026-01-01'),
		('fl off', 'https://hooks.x.test/d', 'x', '["flow.run.failed"]', NULL, false, '2026-01-01')`, FL.Other, FL.Sales)
}

// routes/runs.py: the run history.
func init() {
	maskColumns("webhook_deliveries", "payload")          // a random id in the body
	maskColumns("pending_blob_deletions", "delete_after") // now + the grace, a few ms apart
	register(Suite{
		Module: "runs",
		Seed:   func(w *World) { w.seedRuns() },
		Engine: []EngineRule{
			{Method: "GET", Path: "^/tasks/t-starts$", Body: map[string]any{"status": "STARTED", "progress": map[string]any{"phase": "reading", "rows": 10, "x": 1}}},
			{Method: "GET", Path: "^/tasks/t-succeeds$", Body: map[string]any{"status": "SUCCESS", "result": map[string]any{"rows_written": 5,
				"mirror": map[string]any{"ok": true, "key": "k"}, "destination": map[string]any{"path": "/out/a.csv", "bytes": 10},
				"email": map[string]any{"ok": true}, "missing": []any{map[string]any{"date": "2026-01-01"}}, "expansions": 3}}},
			{Method: "GET", Path: "^/tasks/t-fails$", Body: map[string]any{"status": "FAILURE", "error": "PostgreSQL: out of memory",
				"error_detail": "trace"}},
			{Method: "GET", Path: "^/tasks/t-oom$", Body: map[string]any{"status": "FAILURE", "error": "boom", "error_detail": "WorkerLostError: SIGKILL"}},
			{Method: "GET", Path: "^/tasks/t-revoked$", Body: map[string]any{"status": "REVOKED"}},
			{Method: "GET", Path: "^/tasks/t-stale$", Body: map[string]any{"status": "STARTED"}},
			{Method: "GET", Path: "^/tasks/t-silent$", Body: map[string]any{"status": "PENDING"}},
			{Method: "GET", Path: "^/tasks/t-garbled$", Text: "not json"},
			{Method: "GET", Path: "^/tasks/t-publishes$", Body: map[string]any{"status": "SUCCESS", "result": map[string]any{"rows_written": 2,
				"columns": []any{map[string]any{"name": "città"}}}}},
			{Method: "GET", Path: "^/tasks/t-clashes$", Body: map[string]any{"status": "SUCCESS", "result": map[string]any{"rows_written": 1.0}}},
			{Method: "GET", Path: "^/tasks/t-overwrites$", Body: map[string]any{"status": "SUCCESS", "result": map[string]any{}}},
			{Method: "GET", Path: "^/tasks/t-refreshes$", Body: map[string]any{"status": "SUCCESS", "result": map[string]any{"rows_written": 7,
				"columns": []any{"a"}, "bucket": "other"}}},
			{Method: "GET", Path: "^/tasks/t-contracted$", Body: map[string]any{"status": "SUCCESS", "result": map[string]any{
				"contract": map[string]any{"outcome": "failed", "rules": []any{map[string]any{"kind": "not_null", "column": "a", "violations": 3}}}}}},
			{Method: "GET", Path: "^/tasks/", Body: map[string]any{"status": "PENDING"}},
		},
		Steps: func(w *World) []Step {
			var s []Step
			get := func(name, path string, auth func(*http.Request)) {
				// a run's age in seconds, counted when each gateway answers
				s = append(s, Step{Name: name, Method: "GET", Path: path, Auth: auth, Mask: []string{"progress.seconds", "*.progress.seconds"}})
			}
			for _, p := range []string{"admin", "editor", "writer", "obs_flag", "service"} {
				get("search · "+p, "/runs", w.As(p))
				get("activity · "+p, "/runs/activity?days=730", w.As(p))
			}
			for _, q := range []string{"?status=FAILURE", "?q=kaputt", "?q=Person%20writer", "?q=fl%20group", "?q=%25", "?status=&q=",
				"?limit=2&offset=1", "?limit=0", "?limit=x&offset=-2", "?q=fl%20sql", "?q=secret"} {
				get("search "+q+" · writer", "/runs"+q, w.As("writer"))
				get("search "+q+" · admin", "/runs"+q, w.As("admin"))
			}
			get("search · nobody", "/runs", nil)
			for _, q := range []string{"", "?days=1", "?days=3&tz_offset=-120", "?tz_offset=330&days=10", "?days=0", "?days=731",
				"?tz_offset=x", fmt.Sprintf("?day=%s", RN.Recent.Format("2006-01-02")),
				fmt.Sprintf("?day=%s&tz_offset=60", RN.Recent.Add(-24*time.Hour).Format("2006-01-02")),
				"?day=2026-1-5", "?day=2026-02-30", "?day=yesterday", "?day=", "?day=2026-01-05&days=2"} {
				get("activity "+q+" · admin", "/runs/activity"+q, w.As("admin"))
				get("activity "+q+" · writer", "/runs/activity"+q, w.As("writer"))
			}
			get("activity · nobody", "/runs/activity", nil)

			// one run, reconciled
			for _, name := range []string{"starts", "succeeds", "fails", "oom", "revoked", "stale", "silent", "garbled", "publishes",
				"clashes", "overwrites", "placeholder", "queued", "scheduled", "fresh", "script", "contracted"} {
				get("run · "+name, fmt.Sprintf("/runs/%d", RN.Live[name]), w.As("admin"))
			}
			get("run · refreshes, editor (not readable)", fmt.Sprintf("/runs/%d", RN.Live["refreshes"]), w.As("editor"))
			get("run · refreshes", fmt.Sprintf("/runs/%d", RN.Live["refreshes"]), w.As("admin"))
			get("run · again, closed", fmt.Sprintf("/runs/%d", RN.Live["succeeds"]), w.As("writer"))
			get("run · orphan ingest, writer launched it", fmt.Sprintf("/runs/%d", RN.Live["orphan"]), w.As("writer"))
			get("run · orphan ingest, editor", fmt.Sprintf("/runs/%d", RN.Live["orphan"]), w.As("editor"))
			get("run · hidden ingest, editor", fmt.Sprintf("/runs/%d", RN.Ingest), w.As("editor"))
			get("run · group's, writer", fmt.Sprintf("/runs/%d", RN.GroupRun), w.As("writer"))
			get("run · group's, editor", fmt.Sprintf("/runs/%d", RN.GroupRun), w.As("obs_flag"))
			get("run · saved script's, editor", fmt.Sprintf("/runs/%d", RN.ScriptRun), w.As("editor"))
			get("run · saved script's, admin", fmt.Sprintf("/runs/%d", RN.ScriptRun), w.As("admin"))
			get("run · no such run", "/runs/999999", w.As("admin"))
			get("run · not a number", "/runs/x", w.As("admin"))
			get("run · nobody", "/runs/1", nil)
			get("flow's runs · Gamma, writer", fmt.Sprintf("/flows/%d/runs", FL.Gamma), w.As("writer"))
			get("flow's runs · Gamma, again", fmt.Sprintf("/flows/%d/runs", FL.Gamma), w.As("writer"))
			get("flow's runs · Alpha, writer", fmt.Sprintf("/flows/%d/runs", FL.Alpha), w.As("writer"))
			get("flow's runs · Alpha, editor", fmt.Sprintf("/flows/%d/runs", FL.Alpha), w.As("editor"))
			get("flow's runs · no such flow", "/flows/999999/runs", w.As("admin"))
			return s
		},
	})
}
