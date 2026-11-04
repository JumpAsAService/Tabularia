package main

import (
	"fmt"
	"strings"
)

// script_builds_loop (services/script_builds.py build_loop) one round at a time: the images
// of the scripting environments built by a Job, then checked by another. The fake engine
// stands in for the Kubernetes API; each step puts in progress only the versions it is
// about (a round moves every one forward), and the answers are scripted per Job.
func init() {
	ids := map[string]int64{}
	register(Suite{
		Module: "jobs_script_builds",
		Seed: func(w *World) {
			ids = map[string]int64{}
			// nothing of the other suites in progress: a round would move it too
			w.Exec(`UPDATE script_environment_versions SET status = 'failed' WHERE status IN ('queued', 'building', 'checking')`)
			env := func(key, name, language string) {
				ids[key] = w.Insert(`INSERT INTO script_environments (name, description, language, enabled, cpu_request, cpu_limit,
					memory_request, memory_limit, max_timeout_seconds, created_at, updated_at) VALUES ($1, '', $2, true, '250m', '1',
					'512Mi', '2Gi', 600, '2026-01-01', '2026-01-01') RETURNING id`, name, language)
			}
			env("py", "jk-py", "python")
			env("r", "jk-r", "r")
			version := func(key, env string, number int, base, packages string) {
				ids[key] = w.Insert(`INSERT INTO script_environment_versions (environment_id, number, language_version, base_image,
					packages, status, created_at, created_by) VALUES ($1, $2, '3.12', $3, $4, 'failed', '2026-01-01', 'a@x') RETURNING id`,
					ids[env], number, base, packages)
			}
			version("p1", "py", 1, "python:3.12-slim", "polars==1.0\n# the data frames\npandas>=2,<3  # pinned\n\nscikit-learn[all]\n")
			version("p2", "py", 2, "python:3.12-slim", "polars\nrm -rf /\n")
			version("p3", "py", 3, "python:3.13-slim", "")
			version("r1", "r", 1, "rocker/r-ver:4.5.3", "data.table\nggplot2@3.5.0\r\n")
		},
		Steps: func(w *World) []Step {
			at := "2026-03-01T10:00:00Z"
			on := map[string]string{"SCRIPTS__ENABLED": "true", "SCRIPTS__NAMESPACE": "scripts", "SCRIPTS__BUILD_NAMESPACE": "builds",
				"SCRIPTS__REGISTRY": "registry.test/tab", "SCRIPTS__REGISTRY_PULL_SECRET": "pull"}
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
			// only these versions in progress, from the start of their stage
			progress := func(states map[string]string) func(*World) {
				return func(w *World) {
					releaseLocks(w)
					w.Exec(`UPDATE script_environment_versions SET status = 'failed' WHERE status IN ('queued', 'building', 'checking')`)
					for key, status := range states {
						stage := map[string]string{"building": "build", "checking": "check"}[status]
						var job, image any
						if stage != "" {
							var env, n int64
							w.a.db.QueryRow(w.ctx, `SELECT environment_id, number FROM script_environment_versions WHERE id = $1`, ids[key]).Scan(&env, &n)
							job = fmt.Sprintf("tabularia-%s-e%d-v%d", stage, env, n)
						}
						if status == "checking" {
							image = "registry.test/tab/tabularia-env-1@sha256:" + strings.Repeat("ab", 32)
						}
						w.Exec(`UPDATE script_environment_versions SET status = $2, job_name = $3, image_ref = $4, arch = NULL, lock = NULL,
							completions = NULL, build_log = NULL, error = NULL, started_at = NULL, finished_at = NULL WHERE id = $1`,
							ids[key], status, job, image)
					}
				}
			}
			and := func(fs ...func(*World)) func(*World) {
				return func(w *World) {
					for _, f := range fs {
						f(w)
					}
				}
			}
			panel := func(values map[string]string) func(*World) {
				return func(w *World) {
					w.Exec(`DELETE FROM gateway_settings WHERE key LIKE 'scripts.%' OR key LIKE 'kubernetes.%'`)
					for k, v := range values {
						w.Exec(`INSERT INTO gateway_settings (key, value, updated_at) VALUES ($1, $2, '2026-01-01')`, k, v)
					}
				}
			}
			job := func(stage, key string) string {
				env := ids["py"]
				if strings.HasPrefix(key, "r") {
					env = ids["r"]
				}
				n := map[string]int{"p1": 1, "p2": 2, "p3": 3, "r1": 1}[key]
				return fmt.Sprintf("tabularia-%s-e%d-v%d", stage, env, n)
			}
			jobPath := func(ns, name string) string { return "^/apis/batch/v1/namespaces/" + ns + "/jobs/" + name + "$" }
			cond := func(kind, reason, message string) map[string]any {
				return map[string]any{"status": map[string]any{"conditions": []any{map[string]any{"type": kind, "status": "True",
					"reason": reason, "message": message}}}, "metadata": map[string]any{"creationTimestamp": "2026-03-01T09:00:00Z"}}
			}
			terminated := func(name, created string, code any, reason string) map[string]any {
				return map[string]any{"metadata": map[string]any{"name": name, "creationTimestamp": created},
					"status": map[string]any{"containerStatuses": []any{map[string]any{"name": "c", "state": map[string]any{
						"terminated": map[string]any{"exitCode": code, "reason": reason}}}}}}
			}
			// a finished Job (its condition), its pod and its log
			finished := func(ns, name string, jobBody map[string]any, pod map[string]any, log string, logStatus int) []EngineRule {
				podName := pod["metadata"].(map[string]any)["name"].(string)
				rules := []EngineRule{
					{Method: "GET", Path: jobPath(ns, name), Body: jobBody},
					{Method: "GET", Path: "^/api/v1/namespaces/" + ns + "/pods$", Body: map[string]any{"items": []any{
						terminated("older", "2026-03-01T08:00:00Z", 0, "Completed"), pod}}},
				}
				if logStatus != 0 {
					return append(rules, EngineRule{Method: "GET", Path: "^/api/v1/namespaces/" + ns + "/pods/" + podName + "/log$", Status: logStatus,
						Body: map[string]any{"message": "the node is gone"}})
				}
				return append(rules, EngineRule{Method: "GET", Path: "^/api/v1/namespaces/" + ns + "/pods/" + podName + "/log$", Text: log,
					Type: "text/plain"})
			}
			digest := "sha256:" + strings.Repeat("0123456789abcdef", 4)
			built := "#1 [internal] load build definition\n#9 DONE 61.2s\n\n" + "TABULARIA_IMAGE_DIGEST=" + digest + "\nTABULARIA_IMAGE_ARCH=aarch64\n"
			buildFailed := func(log string, code any, reason, condReason string) []EngineRule {
				return finished("builds", job("build", "p1"), cond("Failed", condReason, "the Job failed"),
					terminated("b-pod", "2026-03-01T09:00:00Z", code, reason), log, 0)
			}
			checked := "Collecting\npolars==1.0\n  pandas==2.2.0  \nmarimo @ file:///x\nTABULARIA_COMPLETIONS not json\nwarning: slow\n" +
				`TABULARIA_COMPLETIONS {"polars": ["col", "lit"], "x": 1.0, "é": [], "big": 12345678901234567890}` + "\n"
			checkPassed := func(key string) []EngineRule {
				return finished("scripts", job("check", key), cond("Complete", "", ""), terminated("c-pod", "2026-03-01T09:00:00Z", 0, "Completed"),
					checked, 0)
			}
			young := map[string]any{"metadata": map[string]any{"creationTimestamp": "2099-01-01T00:00:00Z"}, "status": map[string]any{}}
			stale := map[string]any{"metadata": map[string]any{"creationTimestamp": "2020-01-01T00:00:00Z"}, "status": map[string]any{}}
			stalePod := map[string]any{"metadata": map[string]any{"creationTimestamp": "2020-01-01T00:00:00Z"}, "status": map[string]any{"active": 1}}
			waiting := func(reason, message string) map[string]any {
				return map[string]any{"items": []any{map[string]any{"metadata": map[string]any{"name": "w-pod"}, "status": map[string]any{
					"containerStatuses": []any{map[string]any{"state": map[string]any{"waiting": map[string]any{"reason": reason, "message": message}}}}}}}}
			}
			events := func(list ...[2]string) map[string]any {
				items := []any{}
				for _, e := range list {
					items = append(items, map[string]any{"reason": e[0], "message": e[1]})
				}
				return map[string]any{"items": items}
			}
			tick := func(name string, e map[string]string, engine []EngineRule, before func(*World)) Step {
				return Step{Name: name, Method: "TICK", Path: "script_builds", Tick: "script_builds", Now: at, Env: e, Engine: engine,
					Before: before}
			}
			none := []EngineRule{}
			return []Step{
				tick("scripting off: the loop has no work", env(map[string]string{"SCRIPTS__ENABLED": "false"}), none,
					progress(map[string]string{"p1": "queued"})),
				tick("no registry configured: the loop has no work", env(map[string]string{"SCRIPTS__REGISTRY": ""}), none, nil),
				tick("queued: the Python build's Job created with the panel's resources; packages that are none: failed", env(nil), none,
					and(progress(map[string]string{"p1": "queued", "p2": "queued"}), panel(map[string]string{
						"scripts.build_cpu": `" 500m "`, "scripts.build_memory": `"6Gi"`, "scripts.job_ttl_seconds": "7200",
						"scripts.build_timeout_seconds": "1800", "kubernetes.timeout_seconds": "30"}))),
				tick("queued R: a company authority, a push Secret, plain HTTP, a CRAN mirror; the panel's too small values dropped",
					env(map[string]string{"SCRIPTS__REGISTRY_CA_CONFIGMAP": "reg-ca", "SCRIPTS__REGISTRY_PUSH_SECRET": "push",
						"SCRIPTS__REGISTRY_INSECURE": "true", "SCRIPTS__CRAN_URL": " https://cran.example.org/ ", "SCRIPTS__REGISTRY": "registry.test:5000/tab/",
						"SCRIPTS__BUILD_CPU": "3", "SCRIPTS__BUILD_MEMORY": "5Gi"}), none,
					and(progress(map[string]string{"r1": "queued"}), panel(map[string]string{
						"scripts.build_cpu": `"0m"`, "scripts.build_memory": `"32Mi"`, "scripts.job_ttl_seconds": "1.5"}))),
				tick("queued with a PyPI mirror, its Job already there (409): read, building", env(map[string]string{
					"SCRIPTS__PYPI_INDEX_URL": "https://pypi.example.org/simple", "SCRIPTS__BUILDER_IMAGE": "buildkit:test"}), []EngineRule{
					{Method: "POST", Path: "^/apis/batch/v1/namespaces/builds/jobs$", Status: 409, Body: map[string]any{"message": "already exists"}},
					{Method: "GET", Path: jobPath("builds", job("build", "p1")), Body: map[string]any{"metadata": map[string]any{"uid": "u-1"}}}},
					and(progress(map[string]string{"p1": "queued"}), panel(nil))),
				tick("a registry that is no registry path: failed", env(map[string]string{"SCRIPTS__REGISTRY": "Registry!"}), none,
					progress(map[string]string{"p1": "queued"})),
				tick("a PyPI index that is no address: failed", env(map[string]string{"SCRIPTS__PYPI_INDEX_URL": "ftp://mirror"}), none,
					progress(map[string]string{"p1": "queued"})),
				tick("an R build with a CRAN that is no address: failed", env(map[string]string{"SCRIPTS__CRAN_URL": "cran"}), none,
					progress(map[string]string{"r1": "queued"})),
				tick("the cluster refuses the build's Job (422): failed in its words", env(nil), []EngineRule{
					{Method: "POST", Path: "^/apis/batch/v1/namespaces/builds/jobs$", Status: 422, Body: map[string]any{"message": "Job.batch is invalid"}}},
					progress(map[string]string{"p1": "queued"})),
				tick("the API server restarting (503): the version waits", env(nil), []EngineRule{
					{Method: "POST", Path: "^/apis/batch/v1/namespaces/builds/jobs$", Status: 503, Text: "unavailable", Type: "text/plain"}},
					progress(map[string]string{"p1": "queued"})),
				tick("building, young, no Pod yet: it waits", env(nil), []EngineRule{{Method: "GET", Path: jobPath("builds", job("build", "p1")),
					Body: young}}, progress(map[string]string{"p1": "building"})),
				tick("building, no Pod for long, the cluster refusing it: failed with its latest refusal", env(nil), []EngineRule{
					{Method: "GET", Path: jobPath("builds", job("build", "p1")), Body: stale},
					{Method: "GET", Path: "^/api/v1/namespaces/builds/events$", Body: events(
						[2]string{"FailedCreate", "pods \"x\" is forbidden: exceeded quota: scripts, requested: memory=4Gi"},
						[2]string{"FailedCreate", "pods \"x\" is forbidden: violates PodSecurity \"restricted:latest\""},
						[2]string{"SuccessfulCreate", "Created pod"})}},
					progress(map[string]string{"p1": "building"})),
				tick("building, no Pod for long, the quota full: failed saying so", env(nil), []EngineRule{
					{Method: "GET", Path: jobPath("builds", job("build", "p1")), Body: stale},
					{Method: "GET", Path: "^/api/v1/namespaces/builds/events$", Body: events(
						[2]string{"FailedCreate", "pods \"x\" is forbidden: exceeded quota: scripts, requested: memory=4Gi"})}},
					progress(map[string]string{"p1": "building"})),
				tick("building, its image not downloaded (back-off, the events say why): failed", env(nil), []EngineRule{
					{Method: "GET", Path: jobPath("builds", job("build", "p1")), Body: stalePod},
					{Method: "GET", Path: "^/api/v1/namespaces/builds/pods$", Body: waiting("ImagePullBackOff", "Back-off pulling image")},
					{Method: "GET", Path: "^/api/v1/namespaces/builds/events$", Body: events(
						[2]string{"Failed", "Failed to pull image \"b@" + digest + "\": tls: x509: certificate signed by unknown authority"},
						[2]string{"Failed", "Error: ImagePullBackOff"}, [2]string{"BackOff", "Back-off pulling image"},
						[2]string{"Failed", "Failed to pull image \"b@" + digest + "\": tls: x509: certificate signed by unknown authority"})}},
					progress(map[string]string{"p1": "building"})),
				tick("building, an image not downloaded but young: it waits", env(nil), []EngineRule{
					{Method: "GET", Path: jobPath("builds", job("build", "p1")), Body: young},
					{Method: "GET", Path: "^/api/v1/namespaces/builds/pods$", Body: waiting("ErrImagePull", "manifest unknown")}},
					progress(map[string]string{"p1": "building"})),
				tick("building, its container not created: failed", env(nil), []EngineRule{
					{Method: "GET", Path: jobPath("builds", job("build", "p1")), Body: stalePod},
					{Method: "GET", Path: "^/api/v1/namespaces/builds/pods$", Body: waiting("CreateContainerConfigError", "secret \"push\" not found")}},
					progress(map[string]string{"p1": "building"})),
				tick("built, its digest and arch in the log: the check's Job created, the build's deleted", env(nil),
					finished("builds", job("build", "p1"), cond("Complete", "", ""), terminated("b-pod", "2026-03-01T09:00:00Z", 0, "Completed"), built, 0),
					progress(map[string]string{"p1": "building"})),
				tick("R built on x86: its check runs Rscript", env(nil),
					finished("builds", job("build", "r1"), cond("SuccessCriteriaMet", "", ""), terminated("b-pod", "2026-03-01T09:00:00Z", 0, "Completed"),
						strings.Replace(built, "aarch64", "x86_64", 1), 0),
					progress(map[string]string{"r1": "building"})),
				tick("built on an arch unknown, the check's Job refused (403): failed", env(nil), append(
					finished("builds", job("build", "p1"), map[string]any{"status": map[string]any{"succeeded": 1}},
						terminated("b-pod", "2026-03-01T09:00:00Z", 0, "Completed"), strings.Replace(built, "aarch64", "mips", 1), 0),
					EngineRule{Method: "POST", Path: "^/apis/batch/v1/namespaces/scripts/jobs$", Status: 403, Body: map[string]any{"message": "forbidden"}}),
					progress(map[string]string{"p1": "building"})),
				tick("built, the check's Job not created now (500): the version waits with its image", env(nil), append(
					finished("builds", job("build", "p1"), cond("Complete", "", ""), terminated("b-pod", "2026-03-01T09:00:00Z", 0, "Completed"), built, 0),
					EngineRule{Method: "POST", Path: "^/apis/batch/v1/namespaces/scripts/jobs$", Status: 500, Body: map[string]any{"message": "etcd"}}),
					progress(map[string]string{"p1": "building"})),
				tick("built without a digest: failed with the log", env(nil),
					finished("builds", job("build", "p1"), cond("Complete", "", ""), terminated("b-pod", "2026-03-01T09:00:00Z", 0, "Completed"),
						"done\nTABULARIA_IMAGE_DIGEST=sha256:short\n", 0),
					progress(map[string]string{"p1": "building"})),
				tick("the push: an authority the build does not trust", env(nil),
					buildFailed("error: failed to push: x509: certificate signed by unknown authority\n", 1, "Error", "BackoffLimitExceeded"),
					progress(map[string]string{"p1": "building"})),
				tick("the push: a registry in plain HTTP", env(nil),
					buildFailed("http: server gave HTTP response to HTTPS client\n", 1, "Error", "BackoffLimitExceeded"),
					progress(map[string]string{"p1": "building"})),
				tick("the push: the credentials refused", env(nil),
					buildFailed("error: failed to push registry.test/tab:v1:\nunexpected status: 401 Unauthorized\n", 1, "Error", "BackoffLimitExceeded"),
					progress(map[string]string{"p1": "building"})),
				tick("the build out of memory: only the exit code says so", env(map[string]string{"SCRIPTS__BUILD_MEMORY": "8Gi"}),
					buildFailed("compiling arrow\n", 137, "Error", "BackoffLimitExceeded"), progress(map[string]string{"p1": "building"})),
				tick("the build out of memory: the compiler's line", env(nil),
					buildFailed("g++: fatal error: Killed signal terminated program cc1plus\n", 1, "Error", "BackoffLimitExceeded"),
					progress(map[string]string{"p1": "building"})),
				tick("the build past its deadline", env(nil),
					buildFailed("still compiling\n", nil, "", "DeadlineExceeded"), progress(map[string]string{"p1": "building"})),
				tick("the build failed otherwise: the Job's reason", env(nil),
					buildFailed("ERROR: No matching distribution found for nothing\n", 1, "Error", "BackoffLimitExceeded"),
					progress(map[string]string{"p1": "building"})),
				tick("the build failed, its log not given (500): said so", env(nil),
					finished("builds", job("build", "p1"), cond("Failed", "", ""), terminated("b-pod", "2026-03-01T09:00:00Z", 2, "Error"), "", 500),
					progress(map[string]string{"p1": "building"})),
				tick("the build failed, its container never started (log 400): the waiting reason", env(nil),
					finished("builds", job("build", "p1"), cond("Failed", "BackoffLimitExceeded", ""), map[string]any{
						"metadata": map[string]any{"name": "b-pod", "creationTimestamp": "2026-03-01T09:00:00Z"},
						"status": map[string]any{"containerStatuses": []any{map[string]any{"state": map[string]any{
							"waiting": map[string]any{"reason": "ErrImagePull", "message": "pull access denied"}}}}}}, "", 400),
					progress(map[string]string{"p1": "building"})),
				tick("the build evicted: the cluster took its Pod", env(nil),
					finished("builds", job("build", "p1"), cond("Failed", "PodFailurePolicy", ""), map[string]any{
						"metadata": map[string]any{"name": "b-pod", "creationTimestamp": "2026-03-01T09:00:00Z"},
						"status":   map[string]any{"reason": "Evicted"}}, "", 500),
					progress(map[string]string{"p1": "building"})),
				tick("the build's Job gone: failed", env(nil), []EngineRule{{Method: "GET", Path: jobPath("builds", job("build", "p1")), Status: 404,
					Body: map[string]any{"message": "not found"}}}, progress(map[string]string{"p1": "building"})),
				tick("the build's Job forbidden (403): failed in the cluster's words", env(nil), []EngineRule{{Method: "GET",
					Path: jobPath("builds", job("build", "p1")), Status: 403, Body: map[string]any{"message": "jobs.batch is forbidden"}}},
					progress(map[string]string{"p1": "building"})),
				tick("the API too many requests (429): it waits", env(nil), []EngineRule{{Method: "GET",
					Path: jobPath("builds", job("build", "p1")), Status: 429, Body: map[string]any{}}},
					progress(map[string]string{"p1": "building"})),
				tick("checked: ready, the lock and the completions kept, the environment's current version", env(nil), checkPassed("p1"),
					and(progress(map[string]string{"p1": "checking"}), func(w *World) {
						w.Exec(`UPDATE script_environments SET current_version_id = NULL WHERE id = $1`, ids["py"])
					})),
				tick("two versions checked in one round: the newer is the current one", env(nil), []EngineRule{
					{Method: "GET", Path: fmt.Sprintf("^/apis/batch/v1/namespaces/scripts/jobs/tabularia-check-e%d-v[0-9]+$", ids["py"]),
						Body: cond("Complete", "", "")},
					checkPassed("p1")[1], checkPassed("p1")[2]},
					and(progress(map[string]string{"p1": "checking", "p3": "checking"}), func(w *World) {
						w.Exec(`UPDATE script_environments SET current_version_id = NULL WHERE id = $1`, ids["py"])
					})),
				tick("an older version checked after a newer one: not the current one; completions that are no object", env(nil),
					finished("scripts", job("check", "p1"), cond("Complete", "", ""), terminated("c-pod", "2026-03-01T09:00:00Z", 0, "Completed"),
						"polars==1.0\nTABULARIA_COMPLETIONS [1, 2]\n", 0),
					progress(map[string]string{"p1": "checking"})),
				tick("the check failed: failed with its reason and its log", env(nil),
					finished("scripts", job("check", "p1"), cond("Failed", "BackoffLimitExceeded", "Job has reached the specified backoff limit"),
						terminated("c-pod", "2026-03-01T09:00:00Z", 1, "Error"), "Traceback (most recent call last):\nModuleNotFoundError: No module named 'polars'\n\n", 0),
					progress(map[string]string{"p1": "checking"})),
				tick("the check failed without a reason nor a log", env(nil),
					finished("scripts", job("check", "p1"), cond("Failed", "", ""), terminated("c-pod", "2026-03-01T09:00:00Z", 1, "Error"), "", 0),
					progress(map[string]string{"p1": "checking"})),
				tick("another process holds the round: nothing", env(nil), none, and(progress(map[string]string{"p1": "queued"}),
					func(w *World) { holdLock(w, 7402131) })),
				tick("nothing in progress: nothing", env(nil), none, progress(nil)),
			}
		},
	})
}
