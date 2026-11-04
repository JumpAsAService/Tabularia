package main

import (
	"fmt"
	"time"
)

// notebooks_loop (services/notebook_runtime.py reaper_loop) one round at a time: the
// notebooks' pods looked after. The fake engine stands in for the Kubernetes API and for an
// editor's pod, whose file is read on port 2719 (the round servers reach their own engine as
// siem.test). The rounds run at the real moment (an audit event's time is the process's
// clock): the sessions' times are set from it, the same on both databases.
func init() {
	ids := map[string]int64{}
	register(Suite{
		Module: "jobs_notebooks",
		Seed: func(w *World) {
			ids = map[string]int64{}
			folder := w.Insert(`INSERT INTO projects (name, description, created_at) VALUES ('jk-nb-folder', '', '2026-01-01') RETURNING id`)
			env := w.Insert(`INSERT INTO script_environments (name, description, language, enabled, cpu_request, cpu_limit, memory_request,
				memory_limit, max_timeout_seconds, created_at, updated_at) VALUES ('jk-nb-py', '', 'python', true, '1', '1', '1Gi', '1Gi', 60,
				'2026-01-01', '2026-01-01') RETURNING id`)
			for _, key := range []string{"a", "e", "s", "y", "f"} {
				ids[key] = w.Insert(`INSERT INTO notebooks (name, description, project_id, environment_id, current_version, created_at,
					updated_at) VALUES ($1, '', $2, $3, 1, '2026-01-01', '2026-01-01') RETURNING id`, "jk-nb-"+key, folder, env)
				w.Exec(`INSERT INTO notebook_versions (notebook_id, version, content, note, created_at, created_by) VALUES
					($1, 1, '{"source": "import marimo\n"}', 'created', '2026-01-01', $2)`, ids[key], w.People["writer"])
			}
		},
		Steps: func(w *World) []Step {
			on := kubeEnv(map[string]string{"SCRIPTS__ENABLED": "true", "SCRIPTS__NAMESPACE": "scripts", "SCRIPTS__REGISTRY": "registry.test/tab"})
			type session struct {
				nb, mode, status, podIP string
				started, seen           time.Duration // how long ago
			}
			name := func(nb, mode string) string { return fmt.Sprintf("nb-%s-%s", nb, mode) }
			sessions := func(list ...session) func(*World) {
				return func(w *World) {
					releaseLocks(w)
					mine := []int64{ids["a"], ids["e"], ids["s"], ids["y"], ids["f"]}
					w.Exec(`DELETE FROM notebook_sessions WHERE notebook_id = ANY($1)`, mine)
					// a visitor fresh now may be stale a few minutes later, between the two rounds
					w.Exec(`DELETE FROM notebook_visitors WHERE notebook_id = ANY($1)`, mine)
					now := time.Now().UTC()
					for _, s := range list {
						var ip any
						if s.podIP != "" {
							ip = s.podIP
						}
						w.Exec(`INSERT INTO notebook_sessions (notebook_id, mode, user_id, version, namespace, job_name, pod_ip, status, token_fp,
							started_at, last_seen_at) VALUES ($1, $2, $3, 1, 'scripts', $4, $5, $6, 'fp', $7, $8)`, ids[s.nb], s.mode,
							w.People["writer"], name(s.nb, s.mode), ip, s.status, now.Add(-s.started), now.Add(-s.seen))
					}
				}
			}
			jobsPath := "^/apis/batch/v1/namespaces/scripts/jobs$"
			jobPath := func(n string) string { return "^/apis/batch/v1/namespaces/scripts/jobs/" + n + "$" }
			running := func(n string) map[string]any {
				return map[string]any{"metadata": map[string]any{"name": n}, "status": map[string]any{"active": 1}}
			}
			listing := func(items ...any) EngineRule {
				return EngineRule{Method: "GET", Path: jobsPath, Body: map[string]any{"kind": "JobList", "items": items}}
			}
			pods := func(items ...any) EngineRule {
				return EngineRule{Method: "GET", Path: "^/api/v1/namespaces/scripts/pods$", Body: map[string]any{"items": items}}
			}
			readyPod := map[string]any{"metadata": map[string]any{"name": "p-1", "creationTimestamp": "2026-03-01T09:00:00Z"},
				"status": map[string]any{"podIP": "10.1.2.3", "conditions": []any{map[string]any{"type": "Ready", "status": "True"}},
					"containerStatuses": []any{map[string]any{"name": "marimo", "state": map[string]any{"running": map[string]any{}}}}}}
			olderPod := map[string]any{"metadata": map[string]any{"name": "p-0", "creationTimestamp": "2026-03-01T08:00:00Z"},
				"status": map[string]any{"podIP": "10.1.2.2"}}
			marimoPod := func(state map[string]any, more map[string]any) map[string]any {
				status := map[string]any{"containerStatuses": []any{map[string]any{"name": "files", "state": map[string]any{"running": map[string]any{}}},
					map[string]any{"name": "marimo", "state": state}}}
				for k, v := range more {
					status[k] = v
				}
				return map[string]any{"metadata": map[string]any{"name": "p-2", "creationTimestamp": "2026-03-01T09:00:00Z"},
					"spec": map[string]any{"containers": []any{map[string]any{"name": "marimo", "resources": map[string]any{
						"limits": map[string]any{"memory": "2Gi"}}}}}, "status": status}
			}
			file := func(text string, status int) EngineRule {
				if status != 0 {
					return EngineRule{Method: "GET", Path: "^/notebook.py$", Status: status, Text: "not found", Type: "text/html"}
				}
				return EngineRule{Method: "GET", Path: "^/notebook.py$", Text: text, Type: "text/x-python"}
			}
			tick := func(nm string, e map[string]string, engine []EngineRule, before func(*World)) Step {
				return Step{Name: nm, Method: "TICK", Path: "notebooks", Tick: "notebooks", Env: e, Engine: engine, Before: before}
			}
			minute := time.Minute
			app := func(nb, status string, started, seen time.Duration) session {
				return session{nb, "app", status, "", started, seen}
			}
			return []Step{
				tick("scripting off: the loop has no work", kubeEnv(map[string]string{"SCRIPTS__ENABLED": "false"}), []EngineRule{listing()},
					sessions(app("a", "ready", 30*minute, 20*minute))),
				tick("another orchestrator reaps: nothing", on, []EngineRule{listing()}, func(w *World) { holdLock(w, 7402140) }),
				tick("no sessions: the Job nobody holds deleted, not the finished nor the nameless; the stale visitors cleared", on,
					[]EngineRule{listing(running("nb-orphan"), map[string]any{"metadata": map[string]any{"name": "nb-done"},
						"status": map[string]any{"conditions": []any{map[string]any{"type": "Complete", "status": "True"}}}},
						map[string]any{"metadata": map[string]any{}})},
					func(w *World) {
						sessions()(w)
						now := time.Now().UTC()
						w.Exec(`INSERT INTO notebook_visitors (notebook_id, user_id, opened_at, last_seen_at) VALUES ($1, $2, $3, $4), ($1, $5, $3, $3)`,
							ids["a"], w.People["writer"], now.Add(-20*minute), now.Add(-10*minute), w.People["editor"])
					}),
				tick("an app in use, its marimo running: nothing", on, []EngineRule{listing(running(name("a", "app"))),
					{Method: "GET", Path: jobPath(name("a", "app")), Body: running(name("a", "app"))}, pods(olderPod, readyPod)},
					sessions(app("a", "ready", 30*minute, minute))),
				tick("an app idle for 20 minutes: stopped, in the log", on, []EngineRule{listing(running(name("a", "app")))},
					sessions(app("a", "ready", 30*minute, 20*minute))),
				tick("an editor idle: its file kept as the draft, then stopped", on, []EngineRule{listing(running(name("e", "edit"))),
					file("import marimo\napp = marimo.App()\n", 0)},
					sessions(session{"e", "edit", "ready", "siem.test", 30 * minute, 20 * minute})),
				tick("an editor in use: its file is the version again, the draft goes", on, []EngineRule{listing(running(name("e", "edit"))),
					{Method: "GET", Path: jobPath(name("e", "edit")), Body: running(name("e", "edit"))}, pods(readyPod), file("import marimo\n", 0)},
					func(w *World) {
						sessions(session{"e", "edit", "ready", "siem.test", 30 * minute, minute})(w)
						w.Exec(`UPDATE notebooks SET draft = 'old draft', draft_by = $2, draft_at = '2026-01-05' WHERE id = $1`, ids["e"], w.People["editor"])
					}),
				tick("an editor whose file cannot be read: the draft stays", on, []EngineRule{listing(running(name("e", "edit"))),
					{Method: "GET", Path: jobPath(name("e", "edit")), Body: running(name("e", "edit"))}, pods(readyPod), file("", 404)},
					func(w *World) {
						sessions(session{"e", "edit", "ready", "siem.test", 30 * minute, minute})(w)
						w.Exec(`UPDATE notebooks SET draft = 'old draft', draft_by = $2, draft_at = '2026-01-05' WHERE id = $1`, ids["e"], w.People["editor"])
					}),
				tick("a session whose Job is gone from the cluster: stopped", on, []EngineRule{listing(running("nb-other"))},
					sessions(app("a", "ready", 30*minute, minute))),
				tick("a session whose Job finished: stopped", on, []EngineRule{listing(map[string]any{"metadata": map[string]any{"name": name("a", "app")},
					"status": map[string]any{"succeeded": 1}})}, sessions(app("a", "ready", 30*minute, minute))),
				tick("the Jobs not listed (500): the sessions checked all the same, nothing deleted", on, []EngineRule{
					{Method: "GET", Path: jobsPath, Status: 500, Body: map[string]any{"message": "etcd timeout"}},
					{Method: "GET", Path: jobPath(name("a", "app")), Body: running(name("a", "app"))}, pods(readyPod)},
					sessions(app("a", "ready", 30*minute, minute))),
				tick("an app whose marimo ran out of memory: stopped", on, []EngineRule{listing(running(name("a", "app"))),
					{Method: "GET", Path: jobPath(name("a", "app")), Body: running(name("a", "app"))},
					pods(marimoPod(map[string]any{"terminated": map[string]any{"exitCode": 137, "reason": "OOMKilled"}}, nil)),
					{Method: "GET", Path: "^/api/v1/namespaces/scripts/pods/p-2/log$", Text: "\n1\n2\n3\n4\n5\n6\n7\n8\nloading\nKilled\n\n", Type: "text/plain"}},
					sessions(app("a", "ready", 30*minute, minute))),
				tick("an app whose Job the API does not give now (500): kept", on, []EngineRule{listing(running(name("a", "app"))),
					{Method: "GET", Path: jobPath(name("a", "app")), Status: 500, Body: map[string]any{"message": "x"}}},
					sessions(app("a", "ready", 30*minute, minute))),
				tick("an app whose Job answers nothing: stopped", on, []EngineRule{listing(running(name("a", "app")))},
					sessions(app("a", "ready", 30*minute, minute))),
				tick("starting, its pod ready: ready at its address", on, []EngineRule{listing(running(name("s", "app"))),
					{Method: "GET", Path: jobPath(name("s", "app")), Body: running(name("s", "app"))}, pods(readyPod, olderPod)},
					sessions(app("s", "starting", 30*time.Second, 30*time.Second))),
				tick("no Job for 5 minutes: failed; a young one waits; an old failure cleared, a young one kept", on, []EngineRule{
					listing(), {Method: "GET", Path: "^/apis/batch/v1/namespaces/scripts/jobs/nb-[sy]-app$", Status: 404, Body: map[string]any{}},
					{Method: "DELETE", Path: "^/apis/batch/v1/namespaces/scripts/jobs/nb-s-app$", Status: 404, Body: map[string]any{}}},
					sessions(app("s", "starting", 5*minute, 5*minute), app("y", "starting", 30*time.Second, 30*time.Second),
						session{"f", "edit", "failed", "", 20 * minute, 20 * minute}, app("f", "failed", minute, minute))),
				tick("starting, its Job past its deadline: failed in the cluster's words", on, []EngineRule{listing(),
					{Method: "GET", Path: jobPath(name("s", "app")), Body: map[string]any{"status": map[string]any{"conditions": []any{
						map[string]any{"type": "Failed", "status": "True", "reason": "DeadlineExceeded", "message": "Job was active longer than specified deadline"}}}}}},
					sessions(app("s", "starting", 30*time.Second, 30*time.Second))),
				tick("starting, marimo not in the image: failed saying so", on, []EngineRule{listing(),
					{Method: "GET", Path: jobPath(name("s", "app")), Body: running(name("s", "app"))},
					pods(marimoPod(map[string]any{"terminated": map[string]any{"exitCode": 1, "reason": "Error"}}, nil)),
					{Method: "GET", Path: "^/api/v1/namespaces/scripts/pods/p-2/log$", Text: "Traceback\nModuleNotFoundError: No module named 'marimo'\n",
						Type: "text/plain"}},
					sessions(app("s", "starting", 30*time.Second, 30*time.Second))),
				tick("starting, marimo stopped and its log not given: failed with the exit", on, []EngineRule{listing(),
					{Method: "GET", Path: jobPath(name("s", "app")), Body: running(name("s", "app"))},
					pods(marimoPod(map[string]any{"terminated": map[string]any{"exitCode": 143}}, nil)),
					{Method: "GET", Path: "^/api/v1/namespaces/scripts/pods/p-2/log$", Status: 503, Body: map[string]any{}}},
					sessions(app("s", "starting", 30*time.Second, 30*time.Second))),
				tick("starting, its image not downloaded: failed saying why", on, []EngineRule{listing(),
					{Method: "GET", Path: jobPath(name("s", "app")), Body: running(name("s", "app"))},
					pods(marimoPod(map[string]any{"waiting": map[string]any{"reason": "ErrImagePull",
						"message": "failed to pull and unpack image: 403 Forbidden"}}, nil))},
					sessions(app("s", "starting", 30*time.Second, 30*time.Second))),
				tick("starting for 6 minutes, no node for it: failed", on, []EngineRule{listing(),
					{Method: "GET", Path: jobPath(name("s", "app")), Body: running(name("s", "app"))},
					pods(map[string]any{"metadata": map[string]any{"name": "p-3"}, "status": map[string]any{"conditions": []any{
						map[string]any{"type": "PodScheduled", "status": "False", "reason": "Unschedulable",
							"message": "0/3 nodes are available: 3 Insufficient memory."}}}})},
					sessions(app("s", "starting", 6*minute, 6*minute))),
				tick("starting, the cluster refusing its Pod: failed", on, []EngineRule{listing(),
					{Method: "GET", Path: jobPath(name("s", "app")), Body: map[string]any{"metadata": map[string]any{"name": "x"}}},
					{Method: "GET", Path: "^/api/v1/namespaces/scripts/events$", Body: map[string]any{"items": []any{
						map[string]any{"reason": "FailedCreate", "message": "exceeded quota: notebooks"}}}}},
					sessions(app("s", "starting", minute, minute))),
				tick("starting, its Pod taken by the cluster: failed", on, []EngineRule{listing(),
					{Method: "GET", Path: jobPath(name("s", "app")), Body: running(name("s", "app"))},
					pods(marimoPod(map[string]any{"running": map[string]any{}}, map[string]any{"reason": "Evicted"}))},
					sessions(app("s", "starting", 30*time.Second, 30*time.Second))),
				tick("starting, the API forbidding one (403): failed; down for the other (503): it waits", on, []EngineRule{listing(),
					{Method: "GET", Path: jobPath(name("s", "app")), Status: 403, Body: map[string]any{"message": "jobs.batch \"nb-s-app\" is forbidden"}},
					{Method: "GET", Path: jobPath(name("y", "app")), Status: 503, Text: "down", Type: "text/plain"}},
					sessions(app("s", "starting", 30*time.Second, 30*time.Second), app("y", "starting", 30*time.Second, 30*time.Second))),
				tick("nothing left: nothing", on, []EngineRule{listing()}, sessions()),
			}
		},
	})
}
