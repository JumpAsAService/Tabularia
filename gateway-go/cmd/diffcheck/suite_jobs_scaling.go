package main

// scaling_loop (services/kube_scaling.py reapply_loop) one round at a time: the workers'
// autoscaling bounds saved from the panel put back where the cluster has others. The fake
// engine stands in for the Kubernetes API (an HPA for the run workers, a KEDA ScaledObject
// for the preview workers).
func init() {
	register(Suite{
		Module: "jobs_scaling",
		Seed: func(w *World) {
			w.Exec(`DELETE FROM worker_scaling`)
			w.Exec(`INSERT INTO worker_scaling (kind, min_replicas, max_replicas, updated_at, updated_by) VALUES
				('run', 2, 8, '2026-01-01', 'a@x'), ('preview', 0, 4, '2026-01-01', 'a@x')`)
		},
		Steps: func(w *World) []Step {
			at := "2026-03-01T10:00:00Z"
			on := map[string]string{"KUBERNETES__SCALING": "true", "KUBERNETES__NAMESPACE": "tab",
				"KUBERNETES__RUN_WORKER_DEPLOYMENT": "tab-worker", "KUBERNETES__RUN_WORKER_AUTOSCALER": "hpa/tab-worker",
				"KUBERNETES__RUN_WORKER_MAX_REPLICAS": "6", "KUBERNETES__PREVIEW_WORKER_DEPLOYMENT": "tab-preview",
				"KUBERNETES__PREVIEW_WORKER_AUTOSCALER": " ScaledObject / tab-preview "}
			with := func(more map[string]string) map[string]string {
				env := kubeEnv(on)
				for k, v := range more {
					if v == "" {
						delete(env, k)
					} else {
						env[k] = v
					}
				}
				return env
			}
			const hpa = "^/apis/autoscaling/v2/namespaces/tab/horizontalpodautoscalers/tab-worker$"
			const keda = "^/apis/keda.sh/v1alpha1/namespaces/tab/scaledobjects/tab-preview$"
			bounds := func(hpaSpec, kedaSpec any) []EngineRule {
				return []EngineRule{{Method: "GET", Path: hpa, Body: hpaSpec}, {Method: "GET", Path: keda, Body: kedaSpec}}
			}
			saved := bounds(map[string]any{"spec": map[string]any{"minReplicas": 2, "maxReplicas": 6}},
				map[string]any{"spec": map[string]any{"minReplicaCount": 0, "maxReplicaCount": 4}})
			tick := func(name string, env map[string]string, engine []EngineRule, before func(*World)) Step {
				return Step{Name: name, Method: "TICK", Path: "scaling", Tick: "scaling", Now: at, Env: env, Engine: engine, Before: before}
			}
			return []Step{
				tick("off Kubernetes: the loop has no work", kubeEnv(nil), saved, nil),
				tick("KUBERNETES__SCALING false: the loop has no work", with(map[string]string{"KUBERNETES__SCALING": "false"}), saved, nil),
				tick("the cluster has the saved bounds (the ceiling lowered): nothing put back", with(nil), saved, nil),
				tick("other bounds on both: both put back", with(nil), bounds(
					map[string]any{"spec": map[string]any{"minReplicas": 1, "maxReplicas": 3}},
					map[string]any{"spec": map[string]any{}}), nil),
				tick("an HPA without its fields and KEDA's as text: the defaults, then compared", with(nil), bounds(
					map[string]any{"kind": "HorizontalPodAutoscaler"},
					map[string]any{"spec": map[string]any{"minReplicaCount": "0", "maxReplicaCount": 4.0}}), nil),
				tick("a ceiling above the saved maximum: the saved one", with(map[string]string{"KUBERNETES__RUN_WORKER_MAX_REPLICAS": "20"}),
					saved, nil),
				tick("the run workers have no autoscaler: skipped", with(map[string]string{"KUBERNETES__RUN_WORKER_AUTOSCALER": "deployment/x"}),
					bounds(map[string]any{}, map[string]any{"spec": map[string]any{"minReplicaCount": 1, "maxReplicaCount": 4}}), nil),
				tick("the API answers 503: the round fails", with(nil), []EngineRule{
					{Method: "GET", Path: hpa, Status: 503, Body: map[string]any{"message": "the server is restarting"}}}, nil),
				tick("the patch is refused (403): the round fails", with(nil), append([]EngineRule{
					{Method: "PATCH", Path: hpa, Status: 403, Text: "forbidden", Type: "text/plain"}},
					bounds(map[string]any{"spec": map[string]any{"minReplicas": 1, "maxReplicas": 3}}, map[string]any{})...), nil),
				tick("a bound that is no number: the round fails", with(nil), bounds(
					map[string]any{"spec": map[string]any{"minReplicas": nil, "maxReplicas": 6}}, map[string]any{}), nil),
				tick("no namespace, no namespace file: the round fails", with(map[string]string{"KUBERNETES__NAMESPACE": ""}), saved, nil),
				tick("no service account token: the round fails", with(map[string]string{"KUBERNETES__TOKEN_FILE": "/nonexistent"}), saved, nil),
				tick("someone is saving the bounds (the lock is taken): skipped", with(nil), bounds(map[string]any{}, map[string]any{}),
					func(w *World) { holdLock(w, 7402119) }),
				tick("the lock free again: put back", with(nil), bounds(map[string]any{}, map[string]any{}), releaseLocks),
				tick("no bounds saved: nothing to do", with(nil), saved, func(w *World) { w.Exec(`DELETE FROM worker_scaling`) }),
			}
		},
	})
}
