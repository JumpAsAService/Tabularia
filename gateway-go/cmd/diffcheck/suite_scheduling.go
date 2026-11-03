package main

import "net/http"

// routes/scheduling.py: the schedules' forecast load.
func init() {
	seed := func(w *World) {
		w.seedFlows()
		if FL.schedSeeded {
			return
		}
		FL.schedSeeded = true
		folder := w.Insert(`INSERT INTO projects (name, description, parent_id, owner_id, created_at)
			VALUES ('fl-Schedules', '', NULL, $1, '2026-01-01') RETURNING id`, w.People["admin"])
		sales := Groups["Sales"]
		w.Exec(`INSERT INTO permissions (project_id, user_id, group_id, capability) VALUES ($1, NULL, $2, 'view')`, folder, sales)
		for _, s := range [][2]string{{"fl every 15", "*/15 * * * *"}, {"fl hourly", "0 * * * *"}, {"fl at three", "0 3 * * *"},
			{"fl at three too", "0 3 * * *"}, {"fl weekdays", "0 9 * * mon-fri"}, {"fl broken cron", "61 * * * *"},
			{"fl at three too", "0 3 * * *"}, {"fl last day", "0 0 L * *"}} {
			w.Exec(`INSERT INTO flows (name, description, project_id, owner_id, definition, engine, run_schedule, run_scheduled_by,
				created_at, updated_at) VALUES ($1, '', $2, $3, '{}', 'polars', $4, $3, '2026-01-01', '2026-01-01')`,
				s[0], folder, w.People["admin"], s[1])
		}
		w.Exec(`INSERT INTO datasources (name, description, project_id, owner_id, bucket, key, columns, sort_keys, column_descriptions,
			kind, refresh_schedule, created_at, updated_at) VALUES ('fl db refresh', '', $1, $2, 'data-prep', 'datasets/r.parquet', '[]',
			'[]', '{}', 'database', '0 3 * * *', '2026-01-01', '2026-01-01')`, folder, w.People["admin"])
	}
	steps := func(w *World) []Step {
		var s []Step
		get := func(name, q string, auth func(*http.Request)) {
			s = append(s, Step{Name: name, Method: "GET", Path: "/schedule/load" + q, Auth: auth})
		}
		for _, q := range []string{"", "?days=1", "?days=31", "?days=0", "?days=32", "?days=x", "?days=2.0"} {
			get("load "+q+" · editor", q, w.As("editor"))
		}
		for _, p := range []string{"admin", "writer", "obs_flag"} {
			get("load · "+p, "", w.As(p))
		}
		get("load · nobody", "", nil)
		get("load · nobody, bad days", "?days=0", nil)
		return s
	}
	register(Suite{Module: "scheduling", Seed: seed, Steps: steps})
	register(Suite{Module: "scheduling", Seed: seed, Steps: steps, Phases: []Phase{{Name: "panel: Rome, one worker",
		Panel: map[string]string{"app.timezone": `"Europe/Rome"`, "scheduling.worker_capacity": "1"}}}})
}
