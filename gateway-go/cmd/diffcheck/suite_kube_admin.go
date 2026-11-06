package main

import (
	"fmt"
	"net/http"
	"strings"
	"time"
)

// The administration panels on Kubernetes (routes/cluster.py, routes/workers.py with
// KUBERNETES__SCALING on, routes/backups.py), the fake engine standing in for the Kubernetes
// API. Asked suites: run them together with
//
//	DIFF_SHARE=<a folder holding an executable «pg_dump»>
//	DIFF_ENV="KUBERNETES__API_URL=http://{engine}:8000 KUBERNETES__TOKEN_FILE=/etc/hostname
//	  KUBERNETES__CLUSTER_VIEW=true KUBERNETES__SCALING=true KUBERNETES__NAMESPACE=tab
//	  KUBERNETES__RUN_WORKER_DEPLOYMENT=tab-worker KUBERNETES__RUN_WORKER_AUTOSCALER=hpa/tab-worker
//	  KUBERNETES__PREVIEW_WORKER_DEPLOYMENT=tab-preview KUBERNETES__PREVIEW_WORKER_AUTOSCALER=scaledobject/tab-preview
//	  SCRIPTS__ENABLED=true SCRIPTS__NAMESPACE=tab-scripts SCRIPTS__TRIAL_NAMESPACE=tab-trials
//	  CONTAINERS__ENABLED=true CONTAINERS__NAMESPACE=tab CONTAINERS__TRIAL_NAMESPACE=tab-ctrials
//	  BACKUP__PG_DUMP=/share/pg_dump"
//	DIFF_ONLY=cluster_kube,workers_kube,backups_kube
//
// (the token file is any file both images have: it makes the gateways «on Kubernetes»; the
// pg_dump in /share is what the image has, so that both say backups are available)

// kubeAdmin are the ids the three suites read.
var kubeAdmin = map[string]int64{}

var kubeAdminSeeded bool

func seedKubeAdmin(w *World) {
	seedData(w)
	if kubeAdminSeeded {
		return
	}
	kubeAdminSeeded = true
	admin := w.People["admin"]
	env := w.Insert(`INSERT INTO script_environments (name, description, language, enabled, cpu_request, cpu_limit, memory_request,
		memory_limit, max_timeout_seconds, created_at, updated_at) VALUES ('kube-env', '', 'python', true, '1', '1', '1Gi', '1Gi', 3600,
		'2026-01-01', '2026-01-01') RETURNING id`)
	script := w.Insert(`INSERT INTO saved_scripts (name, description, project_id, owner_id, environment_id, current_version, created_at,
		updated_at) VALUES ('kube-script', '', $1, $2, $3, 1, '2026-01-01', '2026-01-01') RETURNING id`, Data["root"], admin, env)
	group := w.Insert(`INSERT INTO flow_groups (name, description, project_id, owner_id, definition, created_at, updated_at)
		VALUES ('kube-group', '', $1, $2, '{"steps": []}', '2026-01-01', '2026-01-01') RETURNING id`, Data["root"], admin)
	run := func(kind string, flow, script, group, parent any) int64 {
		return w.Insert(`INSERT INTO runs (kind, task_id, status, launched_by, trigger_type, input_key, output_bucket, output_key,
			publish_description, publish_overwrite, publish_sort_keys, started_at, flow_id, saved_script_id, flow_group_id, parent_run_id)
			VALUES ($1::text, 'kube-' || $1::text, 'STARTED', $2, 'manual', '', '', '', '', false, '[]', '2026-02-04', $3, $4, $5, $6) RETURNING id`,
			kind, admin, flow, script, group, parent)
	}
	kubeAdmin["env"] = env
	kubeAdmin["group-run"] = run("orchestration", nil, nil, group, nil)
	kubeAdmin["group-cell"] = run("container", nil, nil, nil, kubeAdmin["group-run"])
	kubeAdmin["flow-run"] = run("script", Data["flow:duck"], nil, nil, nil)
	kubeAdmin["script-run"] = run("script", nil, script, nil, nil)
	kubeAdmin["flow-cell"] = run("orchestration", Data["flow:duck"], nil, nil, nil)
	kubeAdmin["flow-child"] = run("script", nil, nil, nil, kubeAdmin["flow-cell"])
	kubeAdmin["orphan"] = run("script", nil, nil, nil, nil)
	// a script of a flow group that runs a saved script: the script wins over the group
	kubeAdmin["group-script"] = run("script", nil, script, nil, kubeAdmin["group-run"])
}

type km = map[string]any

func kaPod(name any, labels km, status km, spec km) km {
	meta := km{"name": name}
	if labels != nil {
		meta["labels"] = labels
	}
	return km{"metadata": meta, "status": status, "spec": spec}
}

func kaRunning(ready bool, restarts any) km {
	return km{"phase": "Running", "startTime": "2026-03-01T10:00:00Z", "containerStatuses": []any{
		km{"name": "main", "ready": ready, "restartCount": restarts, "state": km{"running": km{}}}}}
}

func kaLimits(cpu, memory any) km {
	return km{"nodeName": "node-1", "containers": []any{km{"name": "main", "resources": km{"limits": km{"cpu": cpu, "memory": memory}}}}}
}

func init() {
	id := func(key string) string { return fmt.Sprint(kubeAdmin[key]) }
	ns := func(name string) (string, string, string) {
		return "^/api/v1/namespaces/" + name + "/pods$", "^/apis/metrics.k8s.io/v1beta1/namespaces/" + name + "/pods$",
			"^/api/v1/namespaces/" + name + "/events$"
	}
	pods := func(name string, items ...any) EngineRule {
		p, _, _ := ns(name)
		return EngineRule{Method: "GET", Path: p, Body: km{"kind": "PodList", "items": items}}
	}
	metrics := func(name string, items ...any) EngineRule {
		_, mt, _ := ns(name)
		return EngineRule{Method: "GET", Path: mt, Body: km{"items": items}}
	}
	events := func(name string, items ...any) EngineRule {
		_, _, ev := ns(name)
		return EngineRule{Method: "GET", Path: ev, Body: km{"items": items}}
	}
	usage := func(name string, containers ...km) km {
		cs := []any{}
		for _, c := range containers {
			cs = append(cs, km{"usage": c})
		}
		return km{"metadata": km{"name": name}, "containers": cs}
	}
	world := func() []EngineRule {
		return []EngineRule{
			pods("tab",
				kaPod("tab-worker-b", km{"app.kubernetes.io/component": "worker"}, km{"phase": "Running", "startTime": "2026-03-01T09:00:00Z",
					"containerStatuses": []any{km{"ready": false, "restartCount": 4, "state": km{"waiting": km{"reason": "CrashLoopBackOff"}}}}},
					kaLimits("1", "2Gi")),
				kaPod("tab-gateway-a", km{"app": "gateway"}, kaRunning(true, 0), kaLimits("500m", "512Mi")),
				kaPod("tab-engine", km{"tabularia.io/stage": "engine"}, km{"phase": "Running", "containerStatuses": []any{
					km{"ready": true, "restartCount": "2", "state": km{"running": km{}}, "lastState": km{"terminated": km{"reason": "OOMKilled"}}},
					km{"ready": true, "restartCount": 1.0, "state": km{"waiting": km{"reason": "ContainerCreating"}}}}},
					km{"containers": []any{km{"resources": km{"limits": km{"cpu": "250000000n", "memory": "1e3"}}}, km{"resources": km{}},
						km{"resources": km{"limits": km{"cpu": 2, "memory": "x"}}}}}),
				kaPod("tab-pending", nil, km{"phase": "Pending", "conditions": []any{km{"type": "Ready", "status": "False"},
					km{"type": "PodScheduled", "status": "False"}}}, km{"containers": []any{km{}, km{}}}),
				kaPod("tab-crashed", nil, km{"phase": "Failed", "containerStatuses": []any{km{"state": km{"terminated": km{"reason": "Error"}}}}}, nil),
				kaPod(nil, nil, nil, nil),
				km{"metadata": km{"name": "tab-bare"}},
				kaPod("tab-group-cell", km{"tabularia.io/container-run": id("group-cell")}, kaRunning(true, nil), kaLimits("0.5", "1Mi")),
			),
			metrics("tab", usage("tab-gateway-a", km{"cpu": "12345678n", "memory": "100Mi"}, km{"cpu": "1m", "memory": "1Ki"}),
				usage("tab-engine", km{"cpu": "0", "memory": "0"}), usage("tab-worker-b", km{}), km{"metadata": km{}, "containers": nil}),
			events("tab",
				km{"involvedObject": km{"kind": "Pod", "name": "tab-worker-b"}, "reason": "BackOff", "message": "Back-off restarting " + strings.Repeat("x", 600),
					"count": 12, "lastTimestamp": "2026-03-01T10:05:00Z"},
				km{"involvedObject": km{"kind": "Job"}, "reason": "", "message": nil, "eventTime": "2026-03-01T10:06:00.000000Z"},
				km{"metadata": km{"creationTimestamp": "2026-03-01T09:00:00Z"}},
				km{"involvedObject": km{"kind": "Node", "name": 7}, "count": 0},
			),
			pods("tab-scripts",
				kaPod("run-flow", km{"tabularia.io/script-run": id("flow-run"), "tabularia.io/environment": id("env")}, kaRunning(true, 0), nil),
				kaPod("run-script", km{"tabularia.io/script-run": " " + id("script-run") + " ", "tabularia.io/environment": "x"}, kaRunning(true, 0), nil),
				kaPod("run-child", km{"tabularia.io/script-run": "0", "tabularia.io/container-run": id("flow-child")}, kaRunning(false, 0), nil),
				kaPod("run-orphan", km{"tabularia.io/script-run": id("orphan"), "tabularia.io/environment": "999999"}, kaRunning(true, 0), nil),
				kaPod("run-group-script", km{"tabularia.io/script-run": id("group-script")}, kaRunning(true, 0), nil),
				kaPod("run-gone", km{"tabularia.io/script-run": "987654321", "tabularia.io/environment": 1.5}, kaRunning(true, 0), nil),
			),
			metrics("tab-scripts", usage("run-flow", km{"cpu": "1", "memory": "1G"})),
			events("tab-scripts"),
			pods("tab-trials", kaPod("trial-1", nil, kaRunning(true, 0), nil)),
			{Method: "GET", Path: `^/apis/metrics.k8s.io/v1beta1/namespaces/tab-trials/pods$`, Status: 404, Body: km{"message": "no metrics"}},
			events("tab-trials", km{"involvedObject": km{"kind": "Pod", "name": "trial-1"}, "reason": "Failed", "lastTimestamp": "2026-03-01T10:05:00Z"}),
			{Method: "GET", Path: `^/api/v1/namespaces/tab-ctrials/pods$`, Status: 403, Body: km{"message": "forbidden"}},
		}
	}
	with := func(rules ...EngineRule) []EngineRule { return append(rules, world()...) }
	register(Suite{
		Module: "cluster_kube",
		Asked:  true,
		Seed:   seedKubeAdmin,
		Steps: func(w *World) []Step {
			var s []Step
			step := func(name string, auth func(*http.Request), rules []EngineRule) {
				s = append(s, Step{Name: name, Method: "GET", Path: "/admin/cluster", Auth: auth, Engine: rules, EngineUnordered: true,
					Mask: []string{"generated_at"}})
			}
			for _, p := range everyone(w) {
				step("the cluster · "+p.name, p.auth, world())
			}
			admin := w.As("admin")
			step("a namespace's pods: 500", admin, with(EngineRule{Method: "GET", Path: `^/api/v1/namespaces/tab-scripts/pods$`, Status: 500,
				Text: "boom", Type: "text/plain"}))
			step("the events refused (403): none", admin, with(EngineRule{Method: "GET", Path: `^/api/v1/namespaces/tab/events$`, Status: 403}))
			step("no metrics anywhere", w.As("obs_flag"), with(EngineRule{Method: "GET", Path: `^/apis/metrics`, Status: 404}))
			step("the API unreachable", admin, []EngineRule{{Method: "GET", Path: `^/api`, Drop: true}})
			step("pods null", admin, with(pods("tab-scripts"), EngineRule{Method: "GET", Path: `^/api/v1/namespaces/tab/pods$`, Text: "null"}))
			step("items null, metrics null, events null", admin, with(
				EngineRule{Method: "GET", Path: `^/api/v1/namespaces/tab/pods$`, Body: km{"items": nil}},
				EngineRule{Method: "GET", Path: `^/apis/metrics.k8s.io/v1beta1/namespaces/tab/pods$`, Text: "null"},
				EngineRule{Method: "GET", Path: `^/api/v1/namespaces/tab/events$`, Text: "null"}))
			step("the pods not JSON: 500", admin, with(EngineRule{Method: "GET", Path: `^/api/v1/namespaces/tab-trials/pods$`, Text: "<html>"}))
			step("the pods a list: 500", admin, with(EngineRule{Method: "GET", Path: `^/api/v1/namespaces/tab/pods$`, Body: []any{1}}))
			step("a pod that is not a dict: 500", admin, with(pods("tab", "x")))
			step("one pod that is a text: 500", admin, with(pods("tab", 5, kaPod("a", nil, nil, nil))))
			step("names of two types: 500", admin, with(pods("tab", kaPod("a", nil, nil, nil), kaPod(5, nil, nil, nil))))
			step("numbers as names", admin, with(pods("tab", kaPod(3, nil, nil, nil), kaPod(1.5, nil, nil, nil), kaPod(false, nil, nil, nil))))
			step("a status that is a list: 500", admin, with(pods("tab", kaPod("a", nil, nil, nil), km{"metadata": km{"name": "b"}, "status": []any{1}})))
			step("labels a text: 500", admin, with(pods("tab", km{"metadata": km{"name": "a", "labels": "x"}})))
			step("odd states", admin, with(pods("tab",
				kaPod("w1", nil, km{"phase": 3, "containerStatuses": []any{km{"state": km{"waiting": km{"reason": 7}}}}}, nil),
				kaPod("w2", nil, km{"containerStatuses": km{}}, km{"containers": "abc"}),
				kaPod("w3", nil, km{"phase": "", "containerStatuses": []any{km{"restartCount": 0, "lastState": km{"terminated": km{"reason": "OOMKilled"}}}}}, nil),
				kaPod("w4", km{"app": 0, "tabularia.io/stage": []any{"s"}}, km{"phase": "Pending", "conditions": nil}, km{"nodeName": 5}),
			)))
			step("a restart count that is no number: 500", admin, with(pods("tab", kaPod("a", nil,
				km{"containerStatuses": []any{km{"restartCount": "many"}}}, nil))))
			step("statuses a text: 500", admin, with(pods("tab", kaPod("a", nil, km{"containerStatuses": "ab"}, nil))))
			step("a metrics' name that is a list: 500", admin, with(metrics("tab", km{"metadata": km{"name": []any{1}}, "containers": []any{}})))
			step("a pod's name that is a dict: 500", admin, with(pods("tab", kaPod(km{"a": 1}, nil, nil, nil))))
			step("an event's kind that is a number: 500", admin, with(events("tab", km{"involvedObject": km{"kind": 5}})))
			step("an event's message that is a dict: 500", admin, with(events("tab", km{"message": km{"a": 1}})))
			step("an event's message that is a list", admin, with(events("tab", km{"message": []any{1, 2}, "lastTimestamp": "2026-04-01T00:00:00Z"})))
			step("events seen at times of two types: 500", admin, with(events("tab", km{"lastTimestamp": "2026"}, km{"lastTimestamp": 5})))
			many := []any{}
			for i := 0; i < 45; i++ {
				many = append(many, km{"reason": fmt.Sprintf("r%02d", i), "lastTimestamp": fmt.Sprintf("2026-03-01T10:%02d:00Z", i%7)})
			}
			step("more than 40 events, ties kept in order", admin, with(events("tab", many...)))
			step("odd quantities", admin, with(pods("tab", kaPod("q", nil, nil, km{"containers": []any{
				km{"resources": km{"limits": km{"cpu": "1.5", "memory": "1.5Gi"}}},
				km{"resources": km{"limits": km{"cpu": " 10u ", "memory": "2e2"}}},
				km{"resources": km{"limits": km{"cpu": "1e3", "memory": 1024}}},
				km{"resources": km{"limits": km{"cpu": true, "memory": "12Ti"}}},
				km{"resources": km{"limits": km{"cpu": 0.25, "memory": "3k"}}},
			}})), metrics("tab", usage("q", km{"cpu": "7u", "memory": "5M"}, km{"cpu": "x", "memory": nil}))))
			step("resources a list: 500", admin, with(pods("tab", kaPod("q", nil, nil, km{"containers": []any{km{"resources": []any{1}}}}))))
			step("a memory past a float: 500", admin, with(pods("tab", kaPod("q", nil, nil, km{"containers": []any{
				km{"resources": km{"limits": km{"memory": "1e400"}}}}}))))
			return s
		},
	})

	// ── Admin → Workers with KUBERNETES__SCALING on ─────────────────────────────
	const (
		depRun  = "^/apis/apps/v1/namespaces/tab/deployments/tab-worker$"
		depPrev = "^/apis/apps/v1/namespaces/tab/deployments/tab-preview$"
		hpa     = "^/apis/autoscaling/v2/namespaces/tab/horizontalpodautoscalers/tab-worker$"
		keda    = "^/apis/keda.sh/v1alpha1/namespaces/tab/scaledobjects/tab-preview$"
	)
	catalog := km{"items": []any{km{"key": "workers.run_concurrency", "min": 1, "max": 16, "value": 4}}}
	live := km{"workers": []any{km{"name": "w1", "kind": "run", "processes": 4}}, "queued": km{"celery": 3, "preview": 0}, "tick_seconds": 5}
	engine := []EngineRule{{Method: "GET", Path: "^/settings$", Body: catalog}, {Method: "GET", Path: "^/workers$", Body: live}}
	cluster := func(rules ...EngineRule) []EngineRule {
		out := append(append([]EngineRule{}, rules...), engine...)
		return append(out,
			EngineRule{Method: "GET", Path: depRun, Body: km{"spec": km{"replicas": 2}, "status": km{"replicas": 2, "readyReplicas": 1}}},
			EngineRule{Method: "GET", Path: hpa, Body: km{"spec": km{"minReplicas": 1, "maxReplicas": 4}, "status": km{"desiredReplicas": 3}}},
			EngineRule{Method: "GET", Path: depPrev, Body: km{"spec": km{}, "status": nil}},
			EngineRule{Method: "GET", Path: keda, Body: km{"spec": km{"minReplicaCount": 0}}},
		)
	}
	register(Suite{
		Module: "workers_kube",
		Asked:  true,
		Seed: func(w *World) {
			seedKubeAdmin(w)
			w.Exec(`DELETE FROM worker_scaling`)
			w.Exec(`INSERT INTO worker_scaling (kind, min_replicas, max_replicas, updated_at, updated_by) VALUES
				('preview', 0, 2, '2026-01-01 10:00:00', 'carla.bianchi@example.test')`)
		},
		Steps: func(w *World) []Step {
			var s []Step
			add := func(name, method, path string, auth func(*http.Request), body any, rules []EngineRule) {
				s = append(s, Step{Name: name, Method: method, Path: path, Auth: auth, JSON: body, Engine: rules})
			}
			for _, p := range everyone(w) {
				add("the workers · "+p.name, "GET", "/admin/workers", p.auth, nil, cluster())
			}
			admin := w.As("admin")
			get := func(name string, rules ...EngineRule) {
				add("the workers: "+name, "GET", "/admin/workers", admin, nil, cluster(rules...))
			}
			get("the run Deployment is not there (404)", EngineRule{Method: "GET", Path: depRun, Status: 404, Body: km{"message": "not found"}})
			get("the API unreachable", EngineRule{Method: "GET", Path: "^/apis/", Drop: true})
			get("the autoscaler refused (403, text)", EngineRule{Method: "GET", Path: keda, Status: 403, Text: "forbidden", Type: "text/plain"})
			get("replicas as text", EngineRule{Method: "GET", Path: depRun, Body: km{"spec": km{"replicas": "3"}, "status": km{"replicas": 2.0, "readyReplicas": true}}})
			get("replicas not a number: written as it came", EngineRule{Method: "GET", Path: depRun, Body: km{"spec": km{"replicas": "x"}}})
			get("replicas a fraction", EngineRule{Method: "GET", Path: depRun, Body: km{"spec": km{"replicas": 2.5}}})
			get("desired a dict", EngineRule{Method: "GET", Path: hpa, Body: km{"spec": km{}, "status": km{"desiredReplicas": km{}}}})
			get("a bound not a number: 500", EngineRule{Method: "GET", Path: hpa, Body: km{"spec": km{"minReplicas": "x"}}})
			get("the Deployment a list: 500", EngineRule{Method: "GET", Path: depRun, Body: []any{}})
			get("the Deployment's status a text: 500", EngineRule{Method: "GET", Path: depRun, Body: km{"status": "up"}})
			get("the HPA's spec a text: 500", EngineRule{Method: "GET", Path: hpa, Body: km{"spec": "x"}})
			get("the Deployment not JSON: 500", EngineRule{Method: "GET", Path: depRun, Text: "<html>"})
			get("queued a text after the cluster: 500", EngineRule{Method: "GET", Path: "^/workers$", Body: km{"queued": km{"celery": "x"}}})
			get("the engine's catalog fails: no cluster asked", EngineRule{Method: "GET", Path: "^/settings$", Status: 500})
			add("the workers, an observer", "GET", "/admin/workers", w.As("obs_flag"), nil, cluster())
			add("the workers, an administrator's token", "GET", "/admin/workers", Bearer("tab_data_admin_write_000000"), nil, cluster())

			put := func(name, kind string, lo, hi any, rules ...EngineRule) {
				add("scale: "+name, "PUT", "/admin/workers/"+kind+"/scaling", admin, km{"min_replicas": lo, "max_replicas": hi}, cluster(rules...))
			}
			put("below the HPA's floor", "run", 0, 3)
			put("the order", "run", 3, 2)
			put("above the ceiling", "run", 1, 11)
			put("the order, zero both", "preview", 0, 0)
			put("above the preview's ceiling", "preview", 0, 9)
			put("the run workers", "run", 2, 5)
			put("again, other bounds", "run", 1, 10)
			put("the preview workers to zero", "preview", 0, 1)
			put("the cluster refuses the patch (403)", "run", 2, 3, EngineRule{Method: "PATCH", Path: hpa, Status: 403, Body: km{"message": "patch is forbidden"}})
			put("the Deployment unreadable (500)", "preview", 0, 3, EngineRule{Method: "GET", Path: depPrev, Status: 500, Text: "x", Type: "text/plain"})
			put("the API unreachable", "run", 2, 3, EngineRule{Method: "GET", Path: "^/apis/", Drop: true})
			put("an HPA whose bound is no number: 500", "run", 2, 3, EngineRule{Method: "GET", Path: hpa, Body: km{"spec": km{"maxReplicas": []any{}}}})
			put("then the engine fails: 503 after the change", "run", 3, 4, EngineRule{Method: "GET", Path: "^/workers$", Status: 503})
			s = append(s, Step{Name: "scale while someone holds the lock: 409 after 10 s", Method: "PUT", Path: "/admin/workers/run/scaling", Auth: admin,
				JSON: km{"min_replicas": 1, "max_replicas": 2}, Engine: cluster(), Before: func(w *World) { holdLock(w, 7402119) }})
			s = append(s, Step{Name: "give back while someone holds the lock: 409", Method: "DELETE", Path: "/admin/workers/preview/scaling", Auth: admin,
				Engine: cluster()})
			s = append(s, Step{Name: "give back the preview workers", Method: "DELETE", Path: "/admin/workers/preview/scaling", Auth: admin,
				Engine: cluster(), Before: releaseLocks})
			add("give back again: nothing kept", "DELETE", "/admin/workers/preview/scaling", admin, nil, cluster())
			add("give back the run workers, another administrator", "DELETE", "/admin/workers/run/scaling", w.As("admin_group"), nil, cluster())
			add("scale · an observer", "PUT", "/admin/workers/run/scaling", w.As("obs_flag"), km{"min_replicas": 1, "max_replicas": 2}, cluster())
			add("scale · a token", "PUT", "/admin/workers/run/scaling", Bearer("tab_data_admin_write_000000"), km{"min_replicas": 1, "max_replicas": 2}, cluster())
			add("scale · an unknown kind", "PUT", "/admin/workers/big/scaling", admin, km{"min_replicas": 1, "max_replicas": 2}, cluster())
			add("scale · bad bounds", "PUT", "/admin/workers/run/scaling", admin, km{"min_replicas": "1", "max_replicas": 0}, cluster())
			add("give back · nobody", "DELETE", "/admin/workers/run/scaling", nil, nil, cluster())
			add("the workers at the end", "GET", "/admin/workers", admin, nil, cluster())
			return s
		},
	})

	// ── Administration → Backups on Kubernetes ─────────────────────────────────
	listing := km{"bucket": "tab-backups", "prefix": "backups/", "same_as_data": false, "items": []any{
		km{"name": "tabularia-2026-11-02T03-00-00Z.dump", "size": 2048, "modified": "2026-11-02T03:00:05+00:00", "etag": "x"},
		km{"name": "tabularia-2026-11-01T03-00-00Z.dump", "size": "1024"},
	}, "error": nil}
	listed := func(rules ...EngineRule) []EngineRule {
		return append(rules, EngineRule{Method: "GET", Path: "^/backups$", Body: listing})
	}
	register(Suite{
		Module: "backups_kube",
		Asked:  true,
		Seed: func(w *World) {
			seedKubeAdmin(w)
			w.Exec(`DELETE FROM database_backups`)
			w.Token("admin", "tab_bkk_admin_write_0000000", "write", time.Hour)
		},
		Steps: func(w *World) []Step {
			var s []Step
			admin := w.As("admin")
			add := func(name, method, path string, auth func(*http.Request), body any, rules []EngineRule, before func(*World)) {
				s = append(s, Step{Name: name, Method: method, Path: path, Auth: auth, JSON: body, Engine: rules, Before: before})
			}
			for _, p := range []string{"admin", "obs_flag", "editor"} {
				add("the state · "+p, "GET", "/admin/backups", w.As(p), nil, listed(), nil)
			}
			add("the state · nobody", "GET", "/admin/backups", nil, nil, listed(), nil)
			state := func(name string, auth func(*http.Request), rules ...EngineRule) {
				add("the state: "+name, "GET", "/admin/backups", auth, nil, rules, nil)
			}
			obs := w.As("obs_flag")
			for _, who := range []struct {
				name string
				auth func(*http.Request)
			}{{"admin", admin}, {"observer", obs}} {
				state("the storage failing · "+who.name, who.auth, EngineRule{Method: "GET", Path: "^/backups$", Status: 500, Body: km{"detail": "S3: access denied"}})
				state("the storage failing in text · "+who.name, who.auth, EngineRule{Method: "GET", Path: "^/backups$", Status: 502,
					Text: strings.Repeat("é", 900), Type: "text/plain"})
				// (not a dropped connection: Go's transport sends a GET again on a reused connection that
				// closed without an answer, httpx does not; true of every GET to the engine)
				state("the engine unavailable · "+who.name, who.auth, EngineRule{Method: "GET", Path: "^/backups$", Status: 503, Text: "busy", Type: "text/plain"})
				state("an error in a good answer · "+who.name, who.auth, EngineRule{Method: "GET", Path: "^/backups$", Body: km{"items": nil, "error": "half"}})
				state("an error that is a number · "+who.name, who.auth, EngineRule{Method: "GET", Path: "^/backups$", Body: km{"error": 5}})
			}
			state("an old engine (404)", admin, EngineRule{Method: "GET", Path: "^/backups$", Status: 404, Body: km{"detail": "Not Found"}})
			state("the listing not JSON: 500", admin, EngineRule{Method: "GET", Path: "^/backups$", Text: "<html>"})
			state("the listing a list: 500", admin, EngineRule{Method: "GET", Path: "^/backups$", Body: []any{}})
			state("the listing empty", admin, EngineRule{Method: "GET", Path: "^/backups$", Body: km{}})
			state("same_as_data as text", admin, EngineRule{Method: "GET", Path: "^/backups$", Body: km{"same_as_data": "yes", "bucket": nil}})
			state("same_as_data not a boolean: 500", admin, EngineRule{Method: "GET", Path: "^/backups$", Body: km{"same_as_data": "maybe"}})
			state("a bucket that is a number: 500", admin, EngineRule{Method: "GET", Path: "^/backups$", Body: km{"bucket": 5}})
			state("an item without a name: 500", admin, EngineRule{Method: "GET", Path: "^/backups$", Body: km{"items": []any{km{"size": 1}}}})
			state("a size that is a fraction: 500", admin, EngineRule{Method: "GET", Path: "^/backups$", Body: km{"items": []any{km{"name": "a", "size": 1.5}}}})
			state("items a dict: 500", admin, EngineRule{Method: "GET", Path: "^/backups$", Body: km{"items": km{"a": 1}}})
			state("an item that is a text: 500", admin, EngineRule{Method: "GET", Path: "^/backups$", Body: km{"items": []any{"x"}}})
			add("change · schedule and keep", "PUT", "/admin/backups", admin, km{"schedule": "30 2 * * *", "keep": 7}, listed(), nil)
			add("change · a bad schedule", "PUT", "/admin/backups", admin, km{"schedule": "61 * * * *"}, listed(), nil)
			add("change · a token", "PUT", "/admin/backups", Bearer("tab_bkk_admin_write_0000000"), km{"keep": 3}, listed(), nil)
			add("back up now · observer", "POST", "/admin/backups/run", obs, nil, listed(), nil)
			add("back up now · a token", "POST", "/admin/backups/run", Bearer("tab_bkk_admin_write_0000000"), nil, listed(), nil)
			add("back up now", "POST", "/admin/backups/run", admin, nil, listed(), nil)
			add("back up now again: already asked", "POST", "/admin/backups/run", admin, nil, listed(), nil)
			add("the state while asked · observer", "GET", "/admin/backups", obs, nil, listed(), nil)
			add("back up now while one runs", "POST", "/admin/backups/run", admin, nil, listed(), func(w *World) {
				w.Exec(`UPDATE database_backups SET requested_at = NULL, requested_by = NULL, running_since = now() AT TIME ZONE 'UTC',
					alive_at = now() AT TIME ZONE 'UTC'`)
			})
			add("the state while one runs", "GET", "/admin/backups", admin, nil, listed(), nil)
			add("back up now after a dead one", "POST", "/admin/backups/run", admin, nil, listed(), func(w *World) {
				w.Exec(`UPDATE database_backups SET alive_at = '2026-01-01', running_since = '2026-01-01', last_error = 'disk full',
					last_error_at = '2026-01-01 00:00:01', last_ok_at = '2025-12-31', last_name = 'tabularia-2025-12-31T03-00-00Z.dump',
					last_size = 99, last_seconds = 1.5`)
			})
			add("back up now: the listing failing after", "POST", "/admin/backups/run", admin, nil,
				[]EngineRule{{Method: "GET", Path: "^/backups$", Status: 500, Body: km{"detail": "s3"}}},
				func(w *World) { w.Exec(`UPDATE database_backups SET requested_at = NULL, running_since = NULL`) })
			add("the state, off", "GET", "/admin/backups", obs, nil, listed(), func(w *World) {
				w.Exec(`UPDATE database_backups SET enabled = false`)
			})
			add("the state without the row", "GET", "/admin/backups", admin, nil, listed(), func(w *World) { w.Exec(`DELETE FROM database_backups`) })
			good := "/admin/backups/tabularia-2026-11-01T03-00-00Z.dump/download"
			add("download", "GET", good, admin, nil, []EngineRule{{Path: "^/backups/objects/", Body: "PGDMP…"}}, nil)
			return s
		},
	})
}
