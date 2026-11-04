package main

import (
	"net/http"
	"time"
)

// routes/backups.py with Docker Compose: the section greyed out («kubernetes»), its settings
// kept for when it runs, «Back up now» refused, a download asked of the engine.
func init() {
	obj := "^/backups/objects/"
	register(Suite{
		Module: "backups",
		Seed: func(w *World) {
			w.Exec(`DELETE FROM database_backups`)
			w.Token("admin", "tab_bk_admin_write_00000000", "write", time.Hour)
		},
		Phases: []Phase{
			{Name: "environment's settings"},
			{Name: "panel: Rome's time", Panel: map[string]string{"app.timezone": `"Europe/Rome"`}},
		},
		Steps: func(w *World) []Step {
			var s []Step
			admin := w.As("admin")
			add := func(name, method, path string, auth func(*http.Request), body any, rules []EngineRule) {
				s = append(s, Step{Name: name, Method: method, Path: path, Auth: auth, JSON: body, Engine: rules})
			}
			for _, p := range []string{"admin", "obs_flag", "editor"} {
				add("the state · "+p, "GET", "/admin/backups", w.As(p), nil, nil)
			}
			add("the state · nobody", "GET", "/admin/backups", nil, nil, nil)
			s = append(s, Step{Name: "a broken JSON from nobody", Method: "PUT", Path: "/admin/backups", Raw: []byte("{"), ContentType: "application/json"})
			add("change · observer", "PUT", "/admin/backups", w.As("obs_flag"), map[string]any{}, nil)
			add("change · a token", "PUT", "/admin/backups", Bearer("tab_bk_admin_write_00000000"), map[string]any{"keep": 0}, nil)
			add("change · bad types", "PUT", "/admin/backups", admin, map[string]any{"enabled": "x", "keep": 0, "schedule": 3, "reset": "keep"}, nil)
			add("change · keep too high, schedule too long", "PUT", "/admin/backups", admin, map[string]any{"keep": 1001, "schedule": repeat("*", 121)}, nil)
			add("change · nothing", "PUT", "/admin/backups", admin, map[string]any{}, nil)
			add("change · reset something unknown", "PUT", "/admin/backups", admin, map[string]any{"reset": []string{"keep", "nope"}}, nil)
			add("change · a bad schedule", "PUT", "/admin/backups", admin, map[string]any{"schedule": "61 * * * *"}, nil)
			add("change · an empty schedule", "PUT", "/admin/backups", admin, map[string]any{"schedule": "  "}, nil)
			add("change · schedule and keep", "PUT", "/admin/backups", admin, map[string]any{"schedule": " 30 2 * * 1-5 ", "keep": 7}, nil)
			add("change · off", "PUT", "/admin/backups", admin, map[string]any{"enabled": false}, nil)
			add("the state when off · observer", "GET", "/admin/backups", w.As("obs_flag"), nil, nil)
			add("change · reset then set", "PUT", "/admin/backups", admin, map[string]any{"reset": []string{"keep", "enabled", "keep"}, "keep": 3}, nil)
			add("change · every 15 minutes", "PUT", "/admin/backups", admin, map[string]any{"schedule": "*/15 * * * *"}, nil)
			add("change · reset all", "PUT", "/admin/backups", admin, map[string]any{"reset": []string{"enabled", "schedule", "keep"}}, nil)
			add("back up now · observer", "POST", "/admin/backups/run", w.As("obs_flag"), nil, nil)
			add("back up now · a token", "POST", "/admin/backups/run", Bearer("tab_bk_admin_write_00000000"), nil, nil)
			add("back up now", "POST", "/admin/backups/run", admin, nil, nil)
			good := "/admin/backups/tabularia-2026-11-01T03-00-00Z.dump/download"
			add("download · observer", "GET", good, w.As("obs_flag"), nil, nil)
			add("download · a token", "GET", good, Bearer("tab_bk_admin_write_00000000"), nil, nil)
			add("download · not a backup's name", "GET", "/admin/backups/passwd/download", admin, nil, nil)
			add("download · not there", "GET", good, admin, nil, []EngineRule{{Path: obj, Status: 404, Body: map[string]any{"detail": "no"}}})
			add("download · the storage failing", "GET", good, admin, nil, []EngineRule{{Path: obj, Status: 500, Body: map[string]any{"detail": "s3"}}})
			add("download", "GET", good, admin, nil, []EngineRule{{Path: obj, Body: "PGDMP…"}})
			add("the state at the end", "GET", "/admin/backups", admin, nil, nil)
			return s
		},
	})
}
