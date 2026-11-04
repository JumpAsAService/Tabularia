package main

// backup_loop (services/db_backup.py one_round): the backup planned, claimed when due or
// asked, pg_dump streamed to the engine (its bytes are each side's own: masked), the oldest
// beyond those to keep removed, the outcome on the row and in the audit log.
func init() {
	// how long pg_dump took is each side's own, and so its size: the dump holds the previous
	// backup's seconds
	maskColumns("database_backups", "last_seconds", "last_size")
	maskPattern("audit_logs", "detail", `"size": \d+, "removed"`)
	register(Suite{
		Module: "jobs_backup",
		Seed: func(w *World) {
			w.Exec(`DELETE FROM database_backups`)
		},
		Steps: func(w *World) []Step {
			k8s := map[string]string{"KUBERNETES_SERVICE_HOST": "10.0.0.1"}
			listing := func(names ...string) EngineRule {
				var items []any
				for _, n := range names {
					items = append(items, map[string]any{"name": n, "size": 10})
				}
				return EngineRule{Method: "GET", Path: "^/backups$", Body: map[string]any{"bucket": "b", "prefix": "p/", "items": items}}
			}
			stored := EngineRule{Method: "PUT", Path: "^/backups/objects/", Body: map[string]any{"ok": true}}
			gone := EngineRule{Method: "DELETE", Path: "^/backups/objects/", Body: map[string]any{}}
			four := listing("tabularia-2026-01-04T03-00-00Z.dump", "tabularia-2026-01-03T03-00-00Z.dump", "notes.txt",
				"tabularia-2026-01-02T03-00-00Z.dump", "tabularia-2026-01-01T03-00-00Z.dump")
			tick := func(name, now string, env map[string]string, engine []EngineRule, before func(*World)) Step {
				return Step{Name: name, Method: "TICK", Path: "backup", Tick: "backup", Now: now, Env: env, Engine: engine,
					Before: before, EngineMask: []string{"*.body"}, Mask: []string{"size"}}
			}
			ok := []EngineRule{stored, four, gone}
			return []Step{
				tick("off Kubernetes: nothing", "2026-02-01T01:00:00Z", nil, ok, nil),
				tick("on Kubernetes: the row made, the next one planned", "2026-02-01T01:00:00Z", k8s, ok, nil),
				tick("not due yet", "2026-02-01T02:59:59Z", k8s, ok, nil),
				tick("due: dumped, stored, none beyond 14", "2026-02-01T03:00:30Z", k8s, ok, nil),
				tick("asked now with 2 to keep: the oldest go", "2026-02-01T10:00:00Z", k8s, ok, func(w *World) {
					w.Exec(`UPDATE database_backups SET requested_at = '2026-02-01 09:59:00', requested_by = 'admin@x.test', keep = 2`)
				}),
				tick("the engine refuses to store it", "2026-02-02T03:00:00Z", k8s, []EngineRule{{Method: "PUT",
					Path: "^/backups/objects/", Status: 507, Body: map[string]any{"detail": "disk full"}}, four, gone}, nil),
				tick("stored, the listing fails", "2026-02-03T03:00:00Z", k8s, []EngineRule{stored,
					{Method: "GET", Path: "^/backups$", Status: 500, Text: "boom", Type: "text/plain"}, gone}, nil),
				tick("stored, a removal fails", "2026-02-04T03:00:00Z", k8s, []EngineRule{stored, four,
					{Method: "DELETE", Path: "^/backups/objects/", Status: 404, Body: map[string]any{"detail": "no such backup"}}}, nil),
				tick("another orchestrator is at it", "2026-02-05T03:00:00Z", k8s, ok, func(w *World) {
					w.Exec(`UPDATE database_backups SET running_since = '2026-02-05 02:59:00', alive_at = '2026-02-05 02:59:50',
						claimed_by = 'other:1:x'`)
				}),
				tick("its claim is dead: taken", "2026-02-05T03:05:00Z", k8s, ok, nil),
				tick("a new schedule: replanned in Rome", "2026-02-05T04:00:00Z", k8s, ok, func(w *World) {
					w.Exec(`UPDATE database_backups SET schedule = '30 5 * * *'`)
					w.Exec(`INSERT INTO gateway_settings (key, value, updated_at) VALUES ('app.timezone', '"Europe/Rome"', now())
						ON CONFLICT (key) DO UPDATE SET value = excluded.value`)
				}),
				tick("turned off: the plan cleared", "2026-02-05T05:00:00Z", k8s, ok, func(w *World) {
					w.Exec(`UPDATE database_backups SET enabled = false`)
				}),
				tick("off, but asked: made all the same", "2026-02-05T05:01:00Z", k8s, ok, func(w *World) {
					w.Exec(`UPDATE database_backups SET requested_at = '2026-02-05 05:00:30', requested_by = 'editor@x.test'`)
				}),
				tick("asked again, the plan off: the tidy-up", "2026-02-05T05:02:00Z", k8s, ok, func(w *World) {
					w.Exec(`UPDATE database_backups SET requested_at = '2026-02-05 05:01:30', requested_by = NULL`)
					w.Exec(`DELETE FROM gateway_settings WHERE key = 'app.timezone'`)
				}),
			}
		},
	})
}
