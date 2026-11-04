package main

import (
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"net/http"
)

// routes/notebook_data.py: what a notebook's pod reads with its token. The comparison sends
// X-Forwarded-For, which the pod's check refuses: run it also with
// DIFF_ENV="NOTEBOOKS__POD_IP_CHECK=false" to reach the reads.
func init() {
	ids := map[string]int64{}
	fp := func(token string) string {
		sum := sha256.Sum256([]byte(token))
		return hex.EncodeToString(sum[:])
	}
	register(Suite{
		Module: "notebook_data",
		Seed: func(w *World) {
			ids = map[string]int64{}
			root := w.Insert(`INSERT INTO projects (name, description, created_at) VALUES ('nd-root', '', '2026-01-01') RETURNING id`)
			sub := w.Insert(`INSERT INTO projects (name, description, parent_id, created_at) VALUES (' nd sub ', '', $1, '2026-01-01') RETURNING id`, root)
			other := w.Insert(`INSERT INTO projects (name, description, created_at) VALUES ('nd-other', '', '2026-01-01') RETURNING id`)
			w.Exec(`INSERT INTO permissions (project_id, user_id, capability) VALUES ($1, $2, 'view')`, root, w.People["writer"])
			ds := func(key, name string, folder int64, dsKey, bucket string) {
				ids[key] = w.Insert(`INSERT INTO datasources (name, description, project_id, bucket, key, rows, columns, sort_keys,
					column_descriptions, kind, created_at, updated_at) VALUES ($1, '', $2, $3, $4, 7, '[{"name": "a"}, {"dtype": "x"}, 1]',
					'[]', '{}', 'flow', '2026-01-02', '2026-01-03') RETURNING id`, name, folder, bucket, dsKey)
			}
			ds("sales", "nd-sales", root, "datasets/nd1.parquet", "data-prep")
			ds("twin1", "nd-twin", root, "datasets/nd2.parquet", "data-prep")
			ds("twin2", "nd-twin", sub, "datasets/nd3.parquet", "data-prep")
			ds("odd", "nd-odd", sub, "elsewhere/x.parquet", "data-prep")
			ds("bucket", "nd-bucket", sub, "datasets/nd5.parquet", "other-bucket")
			ds("hidden", "nd-hidden", other, "datasets/nd6.parquet", "data-prep")
			env := w.Insert(`INSERT INTO script_environments (name, description, language, enabled, cpu_request, cpu_limit, memory_request,
				memory_limit, max_timeout_seconds, created_at, updated_at) VALUES ('nd-py', '', 'python', true, '1', '1', '1Gi', '1Gi', 60,
				'2026-01-01', '2026-01-01') RETURNING id`)
			ids["nb"] = w.Insert(`INSERT INTO notebooks (name, description, project_id, environment_id, current_version, created_at,
				updated_at) VALUES ('nd-board', '', $1, $2, 1, '2026-01-01', '2026-01-01') RETURNING id`, root, env)
			session := func(mode, person, status, token string) {
				nb := w.Insert(`INSERT INTO notebooks (name, description, project_id, environment_id, current_version, created_at,
					updated_at) VALUES ($1, '', $2, $3, 1, '2026-01-01', '2026-01-01') RETURNING id`, "nd-board-"+token, root, env)
				w.Exec(`INSERT INTO notebook_sessions (notebook_id, mode, user_id, version, namespace, job_name, status, pod_ip, token_fp,
					started_at, last_seen_at) VALUES ($1, $2, $3, 1, 'scripts', $4, $5, '10.0.0.9', $6, '2026-02-01', '2026-02-01')`,
					nb, mode, w.People[person], "nd-job-"+mode+"-"+person, status, fp(token))
			}
			session("app", "writer", "ready", "nbk_writer")
			session("edit", "inactive", "ready", "nbk_inactive")
			session("edit", "admin", "failed", "nbk_failed")
			session("app", "admin", "starting", "nbk_admin")
			w.Exec(`INSERT INTO reports (name, description, project_id, current_version, created_at, updated_at) VALUES
				('nd-report', '', $1, 1, '2026-01-01', '2026-01-01'), ('nd-empty', '', $1, 3, '2026-01-01', '2026-01-01'),
				('nd-hidden', '', $2, 1, '2026-01-01', '2026-01-01')`, root, other)
			w.Exec(`INSERT INTO report_versions (report_id, version, content, note, created_at) SELECT id, 1, $1, '', '2026-01-01'
				FROM reports WHERE name IN ('nd-report', 'nd-hidden')`, `{"parameters": [{"name": "from", "type": "date", "required": 1,
				"default": "2026-01-01"}, 3, {"type": "text", "multi": true}]}`)
		},
		Engine: []EngineRule{{Method: "GET", Path: "^/files/presigned$", Body: map[string]any{"url": "https://s3.test/x?sig=1"}}},
		Steps: func(w *World) []Step {
			var s []Step
			pod := func(token string) func(*http.Request) {
				return func(r *http.Request) { r.Header.Set("Authorization", "Bearer "+token) }
			}
			for _, t := range []string{"nbk_writer", "nbk_inactive", "nbk_failed", "nbk_admin", "nbk_nobody", "tab_x", ""} {
				s = append(s, Step{Name: "datasources with " + t, Method: "GET", Path: "/notebook-data/datasources", Auth: pod(t)},
					Step{Name: "reports with " + t, Method: "GET", Path: "/notebook-data/reports", Auth: pod(t)})
			}
			s = append(s, Step{Name: "a basic token", Method: "GET", Path: "/notebook-data/datasources",
				Header: map[string]string{"Authorization": "Basic nbk_writer"}})
			read := func(name, token string, which any, engine []EngineRule) {
				s = append(s, Step{Name: name, Method: "POST", Path: "/notebook-data/read", Auth: pod(token),
					JSON: map[string]any{"datasource": which}, Engine: engine})
			}
			read("by id", "nbk_writer", ids["sales"], nil)
			read("by id again (read in the session already)", "nbk_writer", ids["sales"], nil)
			read("by id as text", "nbk_writer", fmt.Sprintf(" %d ", ids["sales"]), nil)
			read("by name", "nbk_writer", "nd-sales", nil)
			read("by a name two have", "nbk_writer", "nd-twin", nil)
			read("by path", "nbk_writer", " nd-root / nd-twin ", nil)
			read("by a sub path", "nbk_writer", "nd-root/nd sub/nd-twin", nil)
			read("by a path that is not there", "nbk_writer", "nd-root/nope", nil)
			read("by a name that is not there", "nbk_writer", "nope", nil)
			read("by nothing", "nbk_writer", " / ", nil)
			read("one hidden", "nbk_writer", ids["hidden"], nil)
			read("one outside the areas", "nbk_writer", "nd-odd", nil)
			read("one in another bucket", "nbk_writer", "nd-bucket", nil)
			read("by a float", "nbk_writer", 1.5, nil)
			read("the engine has no file", "nbk_writer", "nd-sales", []EngineRule{{Method: "GET", Path: "^/files/presigned$", Status: 404,
				Body: map[string]any{"detail": "no such key"}}})
			read("the engine fails", "nbk_writer", "nd-sales", []EngineRule{{Method: "GET", Path: "^/files/presigned$", Status: 500, Body: map[string]any{}}})
			read("the inactive one reads", "nbk_inactive", "nd-sales", nil)
			read("nobody reads", "nbk_nobody", "nd-sales", nil)
			s = append(s, Step{Name: "broken JSON", Method: "POST", Path: "/notebook-data/read", Raw: []byte("{"), ContentType: "application/json"},
				Step{Name: "no body", Method: "POST", Path: "/notebook-data/read", Auth: pod("nbk_writer")},
				Step{Name: "a session cookie", Method: "GET", Path: "/notebook-data/datasources", Auth: w.As("writer")})
			return s
		},
	})
}
