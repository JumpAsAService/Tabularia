package main

// settings_rollout_loop (services/settings_rollout.py rollout_loop) one step at a time: the
// pods of the components whose restart an administrator asked for, evicted one at a time.
// The fake engine stands in for the Kubernetes API: one answer per step for the pods (a step
// reads one component's).
func init() {
	register(Suite{
		Module: "jobs_settings_rollout",
		Seed: func(w *World) {
			w.Exec(`DELETE FROM settings_rollouts`)
			w.Exec(`INSERT INTO settings_rollouts (component, changed_at, requested_at, requested_by) VALUES
				('worker', '2026-03-01 09:59:00', '2026-03-01 10:00:00.250', 'a@x'),
				('beat', '2026-03-01 09:59:00', NULL, NULL),
				('engine', '2026-03-01 09:59:00', '2026-03-01 10:00:00', 'a@x'),
				('orchestrator', '2026-03-01 09:59:00', '2026-03-01 10:00:00', 'a@x')`)
		},
		Steps: func(w *World) []Step {
			at := "2026-03-01T10:05:00Z"
			on := map[string]string{"KUBERNETES__ROLLOUT": "true", "KUBERNETES__RELEASE": "tab", "KUBERNETES__NAMESPACE": "tab",
				"HOSTNAME": "tab-orchestrator-0"}
			env := func(more map[string]string) map[string]string {
				e := kubeEnv(on)
				for k, v := range more {
					if v == "" {
						delete(e, k)
					} else {
						e[k] = v
					}
				}
				return e
			}
			pod := func(name, created, phase string, ready bool, deleted string) map[string]any {
				meta := map[string]any{"name": name}
				if created != "" {
					meta["creationTimestamp"] = created
				}
				if deleted != "" {
					meta["deletionTimestamp"] = deleted
				}
				st := "False"
				if ready {
					st = "True"
				}
				return map[string]any{"metadata": meta, "status": map[string]any{"phase": phase,
					"conditions": []any{map[string]any{"type": "PodScheduled", "status": "True"}, map[string]any{"type": "Ready", "status": st}}}}
			}
			pods := func(items ...map[string]any) []EngineRule {
				list := make([]any, len(items))
				for i, p := range items {
					list[i] = p
				}
				return []EngineRule{{Method: "GET", Path: "^/api/v1/namespaces/tab/pods$", Body: map[string]any{"kind": "PodList", "items": list}}}
			}
			const eviction = "^/api/v1/namespaces/tab/pods/[^/]+/eviction$"
			tick := func(name string, e map[string]string, engine []EngineRule, before func(*World)) Step {
				return Step{Name: name, Method: "TICK", Path: "settings_rollout", Tick: "settings_rollout", Now: at, Env: e, Engine: engine,
					Before: before}
			}
			old := pod("w-old", "2026-03-01T09:00:00Z", "Running", true, "")
			return []Step{
				tick("Docker Compose (no token, no KUBERNETES_SERVICE_HOST): the loop has no work",
					env(map[string]string{"KUBERNETES__TOKEN_FILE": "/nonexistent"}), pods(old), nil),
				tick("on Kubernetes without the chart's right: the loop has no work", env(map[string]string{"KUBERNETES__ROLLOUT": "false"}),
					pods(old), nil),
				tick("on Kubernetes without a release: the loop has no work", env(map[string]string{"KUBERNETES__RELEASE": ""}), pods(old), nil),
				tick("another orchestrator holds the step: nothing", env(nil), pods(old), func(w *World) { holdLock(w, 7402121) }),
				tick("the worker's old pods: the one not ready first", env(nil), pods(
					old,
					pod("w-old-sick", "2026-03-01T09:30:00.5+00:00", "Running", false, ""),
					pod("w-done", "2026-03-01T08:00:00Z", "Succeeded", false, ""),
					pod("w-no-moment", "", "Running", true, "")), releaseLocks),
				tick("a new pod not ready yet: waiting", env(nil), pods(old, pod("w-new", "2026-03-01T10:02:00Z", "Pending", false, "")), nil),
				tick("a pod on its way out: waiting", env(nil), pods(pod("w-old-2", "2026-03-01T09:00:00Z", "Running", false,
					"2026-03-01T10:04:00Z"), pod("w-new", "2026-03-01T10:02:00Z", "Running", true, "")), nil),
				tick("a pod leaving for an hour no longer holds it; a budget says not now: blocked", env(nil), append(pods(
					pod("w-stuck", "2026-03-01T08:00:00Z", "Running", false, "2026-03-01T09:00:00Z"),
					pod("w-old-3", "2026-03-01T08:30:00Z", "Running", true, ""),
					pod("w-new", "2026-03-01T10:02:00Z", "Running", true, "")),
					EngineRule{Method: "POST", Path: eviction, Status: 429, Body: map[string]any{"message": "Cannot evict pod"}}), nil),
				tick("an unreadable moment of deletion is on its way out: waiting", env(nil), pods(
					pod("w-odd", "2026-03-01T08:00:00Z", "Running", false, "yesterday")), nil),
				tick("no old pod left: the worker's request closed", env(nil), pods(pod("w-new", "2026-03-01T10:02:00Z", "Running", true, "")), nil),
				tick("the engine's pods cannot be read: the step fails", env(nil), []EngineRule{
					{Method: "GET", Path: "^/api/v1/namespaces/tab/pods$", Status: 403, Body: map[string]any{"message": "pods is forbidden"}}}, nil),
				tick("the engine's old pod already gone (404): evicted", env(nil), append(pods(pod("e-old", "2026-03-01T09:00:00Z", "Running",
					true, "")), EngineRule{Method: "POST", Path: eviction, Status: 404, Body: map[string]any{"message": "not found"}}), nil),
				tick("the eviction fails (500): the step fails", env(nil), append(pods(pod("e-old", "2026-03-01T09:00:00Z", "Running",
					true, "")), EngineRule{Method: "POST", Path: eviction, Status: 500, Text: "boom", Type: "text/plain"}), nil),
				tick("the engine done", env(nil), pods(), nil),
				tick("the orchestrators, of this chart only: itself last", env(map[string]string{"KUBERNETES__APP_NAME": "tabularia"}), pods(
					pod("tab-orchestrator-0", "2026-03-01T08:00:00Z", "Running", true, ""),
					pod("tab-orchestrator-1", "2026-03-01T09:00:00Z", "Running", true, "")), nil),
				tick("only itself left: it evicts itself", env(nil), pods(
					pod("tab-orchestrator-0", "2026-03-01T08:00:00Z", "Running", true, "")), nil),
				tick("the request cancelled: nothing to do", env(nil), pods(old), func(w *World) {
					w.Exec(`UPDATE settings_rollouts SET requested_at = NULL WHERE component = 'orchestrator'`)
				}),
				// the same file in both round servers (the gateway's image): its text is the namespace
				tick("a request for the gateway, the namespace read from the pod's file: done",
					env(map[string]string{"KUBERNETES__NAMESPACE": "", "KUBERNETES__NAMESPACE_FILE": "/etc/debian_version"}),
					[]EngineRule{{Method: "GET", Path: "^/api/v1/namespaces/[^/]+/pods$", Body: map[string]any{"items": []any{}}}}, func(w *World) {
						w.Exec(`INSERT INTO settings_rollouts (component, changed_at, requested_at, requested_by) VALUES
							('gateway', NULL, '2026-03-01 10:01:00', 'b@x')`)
					}),
				tick("nothing asked: nothing", env(nil), pods(old), func(w *World) { w.Exec(`DELETE FROM settings_rollouts`) }),
			}
		},
	})
}
