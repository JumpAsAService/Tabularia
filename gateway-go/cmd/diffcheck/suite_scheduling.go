package main

import (
	"net/http"
	"time"
)

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
		// a person who sees this folder only: the flows the other suites changed are read in the
		// heap's order (no ORDER BY in Python), which differs with the writes of each side
		w.People["fl_sched"] = w.Insert(`INSERT INTO users (email, full_name, hashed_password, is_active, is_superuser, is_observer,
			is_service, created_at) VALUES ('flsched@x.test', 'Person fl_sched', 'x', true, false, false, false, '2026-01-01') RETURNING id`)
		w.Session("fl_sched", "s-fl_sched", 5*time.Minute, time.Hour)
		w.Exec(`INSERT INTO permissions (project_id, user_id, group_id, capability) VALUES ($1, $2, NULL, 'view')`, folder, w.People["fl_sched"])
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
			get("load "+q+" · fl_sched", q, w.As("fl_sched"))
		}
		for _, p := range []string{"writer", "obs_flag"} {
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
