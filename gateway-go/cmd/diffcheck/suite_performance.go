package main

import "time"

// routes/performance.py: Administration → Performance.
func init() {
	register(Suite{
		Module: "performance",
		Seed: func(w *World) {
			seedData(w)
			base := time.Now().UTC().Add(-26 * time.Hour).Truncate(time.Second)
			run := func(kind, status string, flow, ds *int64, started time.Duration, wait, took *time.Duration, rows *int64, errText *string) {
				s := base.Add(started)
				var engine, finished *time.Time
				if wait != nil {
					e := s.Add(*wait)
					engine = &e
				}
				if took != nil {
					f := s.Add(*took)
					finished = &f
				}
				w.Exec(`INSERT INTO runs (kind, task_id, status, launched_by, trigger_type, input_key, output_bucket, output_key, flow_id,
					datasource_id, rows_written, error, publish_description, publish_overwrite, publish_sort_keys, started_at,
					engine_started_at, finished_at) VALUES ($1, 'perf', $2, NULL, 'manual', '', 'b', 'k', $3, $4, $5, $6, '', false, '[]', $7, $8, $9)`,
					kind, status, flow, ds, rows, errText, s, engine, finished)
			}
			dur := func(d time.Duration) *time.Duration { return &d }
			duck, chdb := Data["flow:duck"], Data["flow:polars-chdb"]
			ready := Data["ds:ready"]
			run("flow", "SUCCESS", &duck, nil, 0, dur(1500*time.Millisecond), dur(10*time.Second+123456*time.Microsecond), ptr[int64](100), nil)
			run("flow", "SUCCESS", &duck, nil, time.Minute, nil, dur(3*time.Second), nil, nil)
			run("flow", "SUCCESS", &duck, nil, 2*time.Minute, dur(-time.Second), dur(7*time.Second), ptr[int64](5), nil)
			run("flow", "FAILURE", &duck, nil, 3*time.Minute, dur(time.Second), dur(time.Second), nil, ptr(""))
			run("flow", "FAILURE", &duck, nil, 4*time.Minute, nil, nil, nil, ptr("boom: èèè"))
			run("flow", "STARTED", &chdb, nil, 5*time.Minute, dur(2*time.Second), nil, nil, nil)
			run("ingest", "SUCCESS", nil, &ready, 6*time.Minute, dur(500*time.Millisecond), dur(2*time.Second), ptr[int64](42), nil)
			run("group", "SUCCESS", nil, nil, 7*time.Minute, nil, dur(4*time.Second), nil, nil)
			run("ingest", "SUCCESS", nil, nil, 8*time.Minute, nil, dur(4*time.Second), nil, nil)
			run("flow", "SUCCESS", &chdb, nil, -10*24*time.Hour, nil, dur(time.Hour), nil, nil)
		},
		Engine: []EngineRule{
			{Method: "GET", Path: "^/observability/previews$", Body: map[string]any{"items": []any{map[string]any{"ms": 1.5}}}},
			{Method: "GET", Path: "^/observability/warehouse$", Body: map[string]any{"queries": []any{}, "z": 1}},
		},
		Steps: func(w *World) []Step {
			s := forEveryone(w, "the runs", "GET", "/admin/performance/runs", nil)
			s = append(s, forEveryone(w, "the previews", "GET", "/admin/performance/previews", nil)...)
			s = append(s, forEveryone(w, "the warehouse", "GET", "/admin/performance/warehouse", nil)...)
			as := func(name, path string, rules []EngineRule) {
				s = append(s, Step{Name: name, Method: "GET", Path: path, Auth: w.As("admin"), Engine: rules})
			}
			for _, q := range []string{"?days=1", "?days=30", "?days=90", "?days=0", "?days=91", "?days=x", "?days=2.0"} {
				as("runs "+q, "/admin/performance/runs"+q, nil)
			}
			for _, q := range []string{"?limit=1", "?limit=50", "?limit=0", "?limit=51", "?limit=x"} {
				as("previews "+q, "/admin/performance/previews"+q, nil)
			}
			for _, q := range []string{"?minutes=5&limit=1", "?minutes=4", "?minutes=1441&limit=0", "?limit=7"} {
				as("warehouse "+q, "/admin/performance/warehouse"+q, nil)
			}
			as("previews: the engine refuses", "/admin/performance/previews", []EngineRule{{Path: ".", Status: 404, Text: "x"}})
			as("previews: not JSON", "/admin/performance/previews", []EngineRule{{Path: ".", Text: "x"}})
			as("warehouse: the engine fails", "/admin/performance/warehouse", []EngineRule{{Path: ".", Status: 500, Body: map[string]any{}}})
			s = append(s, Step{Name: "a bad query, an editor: 403 first", Method: "GET", Path: "/admin/performance/runs?days=x", Auth: w.As("writer")})
			return s
		},
	})
}
