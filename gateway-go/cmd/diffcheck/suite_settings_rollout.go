package main

// routes/gateway_settings.py rollout_router, on Kubernetes: run it with DIFF_ENV="KUBERNETES__API_URL=http://{engine}:8000
// KUBERNETES__TOKEN_FILE=/etc/hostname KUBERNETES__ROLLOUT=true KUBERNETES__RELEASE=rel KUBERNETES__NAMESPACE=ns
// KUBERNETES__APP_NAME=tab": the pods are read from the fake engine. Without it the mode is
// «compose» (the gateway_settings suite covers it too).
func init() {
	engineUnordered(`^/api/v1/namespaces/[^/]+/pods$`)
	pod := func(name, created string, deleted any, phase string, ready string) map[string]any {
		meta := map[string]any{"name": name, "creationTimestamp": created}
		if deleted != nil {
			meta["deletionTimestamp"] = deleted
		}
		return map[string]any{"metadata": meta, "status": map[string]any{"phase": phase,
			"conditions": []any{map[string]any{"type": "Initialized", "status": "True"}, map[string]any{"type": "Ready", "status": ready}}}}
	}
	pods := func(items ...any) []EngineRule {
		return []EngineRule{{Method: "GET", Path: "^/api/v1/namespaces/ns/pods$", Body: map[string]any{"kind": "PodList", "items": items}}}
	}
	register(Suite{
		Module: "settings_rollout",
		Seed: func(w *World) {
			w.Exec(`DELETE FROM settings_rollouts`)
			w.Exec(`INSERT INTO settings_rollouts (component, changed_at, requested_at, requested_by) VALUES
				('worker', '2026-05-01 10:00:00', NULL, NULL),
				('engine', '2026-05-01 10:00:00', '2026-04-01', 'carla.bianchi@example.test'),
				('beat', NULL, '2026-04-01', ''),
				('gateway', '2026-05-02 10:00:00.123456', '2026-05-03', 'x@example.test')`)
		},
		Engine: pods(
			pod("old-1", "2026-04-30T10:00:00Z", nil, "Running", "True"),
			pod("old-2", "2026-04-29T10:00:00+02:00", nil, "Pending", "False"),
			pod("leaving", "2026-04-01T00:00:00Z", "2026-05-05T00:00:00Z", "Running", "True"),
			pod("new", "2026-06-01T00:00:00.5Z", nil, "Running", "True"),
			pod("done", "2026-01-01T00:00:00Z", nil, "Succeeded", "False"),
			map[string]any{"metadata": map[string]any{"name": "bare"}},
			map[string]any{"metadata": map[string]any{"creationTimestamp": "not a date"}, "status": nil},
		),
		Steps: func(w *World) []Step {
			var s []Step
			for _, p := range everyone(w) {
				s = append(s, Step{Name: "the state · " + p.name, Method: "GET", Path: "/admin/settings-rollout", Auth: p.auth, EngineUnordered: true})
			}
			s = append(s,
				Step{Name: "the pods unreadable (403)", Method: "GET", Path: "/admin/settings-rollout", Auth: w.As("admin"), EngineUnordered: true,
					Engine: []EngineRule{{Method: "GET", Path: "^/api/v1/namespaces/ns/pods$", Status: 403, Body: map[string]any{"message": "pods is forbidden"}}}},
				Step{Name: "the pods unreadable (500 text)", Method: "GET", Path: "/admin/settings-rollout", Auth: w.As("obs_flag"), EngineUnordered: true,
					Engine: []EngineRule{{Method: "GET", Path: "^/api/v1/namespaces/ns/pods$", Status: 500, Text: "boom", Type: "text/plain"}}},
				Step{Name: "no pods", Method: "GET", Path: "/admin/settings-rollout", Auth: w.As("admin"), EngineUnordered: true,
					Engine: []EngineRule{{Method: "GET", Path: "^/api/v1/namespaces/ns/pods$", Body: map[string]any{"items": nil}}}},
				Step{Name: "items not a list", Method: "GET", Path: "/admin/settings-rollout", Auth: w.As("admin"), EngineUnordered: true,
					Engine: []EngineRule{{Method: "GET", Path: "^/api/v1/namespaces/ns/pods$", Body: map[string]any{"items": "x"}}}},
				Step{Name: "start", Method: "POST", Path: "/admin/settings-rollout", Auth: w.As("admin"), EngineUnordered: true,
					JSON: map[string]any{"components": []any{"orchestrator", "worker", "worker"}}},
				Step{Name: "start, an unknown one", Method: "POST", Path: "/admin/settings-rollout", Auth: w.As("admin"),
					JSON: map[string]any{"components": []any{"worker", "nope"}}},
				Step{Name: "start, too many", Method: "POST", Path: "/admin/settings-rollout", Auth: w.As("admin"),
					JSON: map[string]any{"components": []any{"a", "b", "c", "d", "e", "f", "g"}}},
				Step{Name: "start, not a list", Method: "POST", Path: "/admin/settings-rollout", Auth: w.As("admin"), JSON: map[string]any{"components": "worker"}},
				Step{Name: "start, a token", Method: "POST", Path: "/admin/settings-rollout", Auth: Bearer("tab_data_service_write_0000"),
					JSON: map[string]any{"components": []any{"worker"}}, Header: map[string]string{"Accept-Language": "it"}},
				Step{Name: "start, an editor", Method: "POST", Path: "/admin/settings-rollout", Auth: w.As("editor"), JSON: map[string]any{"components": 1}},
				Step{Name: "start, nobody", Method: "POST", Path: "/admin/settings-rollout", Raw: []byte("{"), ContentType: "application/json"},
				Step{Name: "cancel", Method: "DELETE", Path: "/admin/settings-rollout/worker", Auth: w.As("admin"), EngineUnordered: true},
				Step{Name: "cancel again", Method: "DELETE", Path: "/admin/settings-rollout/worker", Auth: w.As("admin_group"), EngineUnordered: true},
				Step{Name: "cancel an unknown one", Method: "DELETE", Path: "/admin/settings-rollout/n%C3%A9", Auth: w.As("admin"), EngineUnordered: true},
				Step{Name: "cancel, an observer", Method: "DELETE", Path: "/admin/settings-rollout/engine", Auth: w.As("obs_group")},
				Step{Name: "cancel, a token", Method: "DELETE", Path: "/admin/settings-rollout/engine", Auth: Bearer("tab_data_service_write_0000")},
			)
			return s
		},
	})
}
