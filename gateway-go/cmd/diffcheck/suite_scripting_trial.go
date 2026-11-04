package main

import (
	"fmt"
	"net/http"
	"strings"
	"time"
)

// routes/scripting.py trial_node: POST /scripting/flows/{flow_id}/nodes/{node_id}/run, the
// editor's «Run» of a Script or Container node. The trial goes on after the answer (run_trial):
// a step that starts one waits for it (Settle) before reading the engine and the databases.
//
// Run it with the scripting environments and containers on, Kubernetes and the registry
// answered by the stack's fake engine:
//
//	DIFF_ENV="SCRIPTS__ENABLED=true SCRIPTS__NAMESPACE=scripts SCRIPTS__REGISTRY=reg.x.test
//	  CONTAINERS__ENABLED=true CONTAINERS__NAMESPACE=boxes KUBERNETES__API_URL=http://{engine}:8000
//	  KUBERNETES__TOKEN_FILE=/etc/hostname" DIFF_ONLY=scripting_trial gateway-go/scripts/diffcheck.sh
func init() {
	register(Suite{Module: "scripting_trial", Seed: seedTrial, Steps: trialSteps})
}

// TR: what the trial's suite seeds.
var TR = struct {
	Flows map[string]int64
}{}

func seedTrial(w *World) {
	admin, writer := w.People["admin"], w.People["writer"]
	folder := func(name string) int64 {
		return w.Insert(`INSERT INTO projects (name, description, parent_id, owner_id, created_at) VALUES ($1, '', NULL, $2, '2026-01-01')
			RETURNING id`, name, admin)
	}
	main, other := folder("tr-Main"), folder("tr-Other")
	w.Exec(`INSERT INTO permissions (project_id, user_id, group_id, capability) VALUES ($1, $2, NULL, 'edit')`, main, writer)
	group := w.Insert(`INSERT INTO groups (name, description, is_admin, is_observer, created_at) VALUES ('tr-coders', '', false, false,
		'2026-01-01') RETURNING id`)
	w.Exec(`INSERT INTO user_groups (user_id, group_id) VALUES ($1, $2)`, writer, group)
	secretKey, _ := fernet.Encrypt("sk-tr")
	w.Exec(`INSERT INTO script_storage (id, endpoint, region, bucket, access_key, secret_key_enc) VALUES (1, '', 'eu-1', 'scripts', 'AK', $1)
		ON CONFLICT (id) DO UPDATE SET endpoint = '', region = 'eu-1', bucket = 'scripts', access_key = 'AK',
		secret_key_enc = EXCLUDED.secret_key_enc`, secretKey)
	env := func(name string, allowed bool) int64 {
		id := w.Insert(`INSERT INTO script_environments (name, description, language, enabled, cpu_request, cpu_limit, memory_request,
			memory_limit, max_timeout_seconds, created_at, updated_at) VALUES ($1, '', 'python', true, '500m', '1', '1Gi', '2Gi', 3600,
			'2026-01-01', '2026-01-01') RETURNING id`, name)
		v := w.Insert(`INSERT INTO script_environment_versions (environment_id, number, language_version, base_image, packages, status,
			image_ref, arch, created_at) VALUES ($1, 1, '3.12', 'python:3.12-slim', '', 'ready', 'reg.x.test/tr@sha256:abc', NULL,
			'2026-01-01') RETURNING id`, id)
		w.Exec(`UPDATE script_environments SET current_version_id = $2 WHERE id = $1`, id, v)
		if allowed {
			w.Exec(`INSERT INTO script_environment_groups (environment_id, group_id) VALUES ($1, $2)`, id, group)
		}
		return id
	}
	open, closed := env("tr-env", true), env("tr-env-closed", false)
	registry := w.Insert(`INSERT INTO container_registries (name, description, host, protocol, tls, ca_cert, username, repositories,
		enabled, last_test_message, created_at, updated_at) VALUES ('tr-reg', '', 'siem.test:8000', 'http', 'public', '', '', 'team/*', true,
		'', '2026-01-01', '2026-01-01') RETURNING id`)
	src := w.Insert(`INSERT INTO datasources (name, description, project_id, owner_id, bucket, key, columns, sort_keys, column_descriptions,
		kind, created_at, updated_at) VALUES ('tr src', '', $1, $2, 'data-prep', 'datasets/tr/src.parquet', '[]', '[]', '{}', 'flow',
		'2026-01-01', '2026-01-01') RETURNING id`, main, admin)
	TR.Flows = map[string]int64{}
	flow := func(key string, project int64, definition string) {
		TR.Flows[key] = w.Insert(`INSERT INTO flows (name, description, project_id, owner_id, definition, engine, created_at, updated_at)
			VALUES ($1, '', $2, $3, $4, 'duckdb', '2026-01-01', '2026-01-01') RETURNING id`, "tr "+key, project, admin, definition)
	}
	script := func(id string, envID any) string {
		return fmt.Sprintf(`{"id": %q, "type": "script", "data": {"label": "Step %s", "environmentId": %v, "entrypoint": "main.py",
			"files": [{"path": "main.py", "content": "print(1)"}], "timeoutSeconds": 60}}`, id, id, envID)
	}
	nodes := fmt.Sprintf(`[{"id": "s", "type": "source", "data": {"datasourceId": %d, "sample": {"mode": "first", "rows": 5}}},
		%s, %s, %s, {"id": "c1", "type": "container", "data": {"registryId": %d, "repository": "team/app", "tag": "2.0", "label": "Box"}},
		{"id": "c2", "type": "container", "data": {"label": "No image"}},
		{"id": "o", "type": "output", "data": {"name": "tr out", "projectId": %d}}]`,
		src, script("s1", open), script("s2", closed), script("s3", "null"), registry, main)
	edges := `[{"source": "s", "target": "s1"}, {"source": "s1", "target": "o"}, {"source": "s1", "target": "s2"}]`
	flow("main", main, `{"nodes": `+nodes+`, "edges": `+edges+`}`)
	flow("other", other, `{"nodes": `+nodes+`, "edges": `+edges+`}`)
	flow("broken", main, `not json`)
}

func trialSteps(w *World) []Step {
	path := func(flow any, node string) string { return fmt.Sprintf("/scripting/flows/%v/nodes/%s/run", flow, node) }
	digest := "sha256:" + strings.Repeat("cd34", 16)
	job := func(ns string) []EngineRule {
		return []EngineRule{
			{Method: "POST", Path: "^/apis/batch/v1/namespaces/" + ns + "/jobs$", Body: map[string]any{"metadata": map[string]any{"name": "j", "uid": "u-9"}}},
			{Method: "POST", Path: "^/api/v1/namespaces/" + ns + "/(configmaps|secrets)$", Body: map[string]any{}},
			{Method: "DELETE", Path: "^/api/v1/namespaces/" + ns + "/secrets/", Status: 404, Body: map[string]any{"message": "not found"}},
			{Method: "GET", Path: "^/apis/batch/v1/namespaces/" + ns + "/jobs/", Body: map[string]any{"status": map[string]any{"succeeded": 1}}},
			{Method: "GET", Path: "^/api/v1/namespaces/" + ns + "/pods$", Body: map[string]any{"items": []any{map[string]any{
				"metadata": map[string]any{"name": "p1", "creationTimestamp": "2026-04-02T09:00:00Z"},
				"status":   map[string]any{"containerStatuses": []any{map[string]any{"state": map[string]any{"terminated": map[string]any{"exitCode": 0}}}}}}}}},
			{Method: "GET", Path: "^/api/v1/namespaces/" + ns + "/pods/p1/log$", Text: "trial log", Type: "text/plain"},
			{Method: "DELETE", Path: "^/apis/batch/v1/namespaces/" + ns + "/jobs/", Body: map[string]any{}},
		}
	}
	rules := append(append(job("scripts"), job("boxes")...),
		EngineRule{Method: "GET", Path: "^/v2/$", Body: map[string]any{}},
		EngineRule{Method: "HEAD", Path: "^/v2/team/app/manifests/2.0$", Headers: map[string]string{"Docker-Content-Digest": digest}},
		EngineRule{Method: "POST", Path: "^/tasks/transform-data$", Body: map[string]any{"task_id": "t-tr-in"}},
		EngineRule{Method: "GET", Path: "^/tasks/t-tr-in$", Body: map[string]any{"status": "SUCCESS", "result": map[string]any{"rows_written": 5}}},
		EngineRule{Method: "POST", Path: "^/scripts/output/import$", Body: map[string]any{"found": true, "rows": 5}},
		EngineRule{Method: "POST", Path: "^/scripts/objects/delete$", Body: map[string]any{}},
	)
	main := TR.Flows["main"]
	step := func(name, p string, auth func(*http.Request), before func(*World)) Step {
		return Step{Name: name, Method: "POST", Path: p, Auth: auth, Before: before, Engine: rules,
			EngineScrub: []string{`[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}`, `out/[0-9a-f]{32}\.parquet`},
			EngineMask:  []string{"*.body.output_key"}}
	}
	started := func(name, p string, auth func(*http.Request), before func(*World)) Step {
		s := step(name, p, auth, before)
		s.Settle = 4 * time.Second
		return s
	}
	trialRow := func(status, beat string) func(*World) {
		return func(w *World) {
			w.Exec(`INSERT INTO runs (kind, flow_id, task_id, status, launched_by, trigger_type, input_key, output_bucket, output_key,
				publish_description, publish_overwrite, publish_sort_keys, script, started_at, heartbeat_at) VALUES ('script', $1, '', $2, $3,
				'trial', '', '', '', '', false, '[]', '{"node_id": "s1", "label": "Step s1", "trial": true, "budget_seconds": 1200}',
				now() - interval '10 minutes', now() - $4::interval)`, main, status, w.People["admin"], beat)
		}
	}
	return []Step{
		step("no session", path(main, "s1"), nil, nil),
		step("a flow id that is a word", path("x", "s1"), w.As("writer"), nil),
		step("a flow that does not exist", path(999999, "s1"), w.As("writer"), nil),
		step("RUN missing on the flow's folder", path(TR.Flows["other"], "s1"), w.As("writer"), nil),
		step("an observer", path(main, "s1"), w.As("obs_flag"), nil),
		step("a node not in the saved flow", path(main, "nope"), w.As("writer"), nil),
		step("a node that is no Script nor Container", path(main, "o"), w.As("writer"), nil),
		step("a Script node without an environment", path(main, "s3"), w.As("writer"), nil),
		step("an environment the person may not use", path(main, "s2"), w.As("writer"), nil),
		step("a Container node without an image", path(main, "c2"), w.As("writer"), nil),
		step("a definition that is not JSON", path(TR.Flows["broken"], "s1"), w.As("admin"), nil),
		started("a Script node's trial: its input on the sample, its Job, its output", path(main, "s1"), w.As("writer"), nil),
		started("again, after it ended: a new trial", path(main, "s1"), w.As("writer"), nil),
		started("a trial of the node still going is offered again", path(main, "s1"), w.As("admin"), trialRow("STARTED", "5 seconds")),
		started("one left by a process that stopped is closed, a new one starts", path(main, "s1"), w.As("admin"),
			func(w *World) {
				w.Exec(`UPDATE runs SET status = 'FAILURE' WHERE trigger_type = 'trial' AND status IN ('PENDING', 'STARTED') AND flow_id = $1`, main)
				trialRow("STARTED", "10 minutes")(w)
			}),
		started("a Container node's trial: the tag's digest, its Job, its output", path(main, "c1"), w.As("admin"), nil),
		started("a Script node downstream reads the trial's output of the one before", path(main, "s2"), w.As("admin"), nil),
	}
}
