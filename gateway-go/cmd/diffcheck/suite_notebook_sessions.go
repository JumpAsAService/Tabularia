package main

import (
	"fmt"
	"net/http"
	"time"
)

// routes/notebook_sessions.py: a notebook's sessions and the frame's ticket. With
// DIFF_ENV="… NOTEBOOKS__ORIGIN=https://nb.test" the ticket is issued; without, it is
// refused (409), or answered for the same origin with NOTEBOOKS__ALLOW_SAME_ORIGIN=true.
func init() {
	maskColumns("notebook_tickets", "token_fp")
	ids := map[string]int64{}
	register(Suite{
		Module: "notebook_sessions",
		Seed: func(w *World) {
			ids = map[string]int64{}
			folder := w.Insert(`INSERT INTO projects (name, description, created_at) VALUES ('ns-folder', '', '2026-01-01') RETURNING id`)
			w.Exec(`INSERT INTO permissions (project_id, user_id, capability) VALUES ($1, $2, 'edit'), ($1, $3, 'view')`, folder,
				w.People["writer"], w.People["editor"])
			group := w.Insert(`INSERT INTO groups (name, description, is_admin, is_observer, created_at) VALUES ('ns-coders', '', false,
				false, '2026-01-01') RETURNING id`)
			w.Exec(`INSERT INTO user_groups (user_id, group_id) VALUES ($1, $2), ($3, $2)`, w.People["writer"], group, w.People["editor"])
			env := w.Insert(`INSERT INTO script_environments (name, description, language, enabled, cpu_request, cpu_limit, memory_request,
				memory_limit, max_timeout_seconds, created_at, updated_at) VALUES ('ns-py', '', 'python', true, '1', '1', '1Gi', '1Gi', 60,
				'2026-01-01', '2026-01-01') RETURNING id`)
			w.Exec(`INSERT INTO script_environment_groups (environment_id, group_id) VALUES ($1, $2)`, env, group)
			nb := func(key, name string, maxVisitors any) {
				ids[key] = w.Insert(`INSERT INTO notebooks (name, description, project_id, environment_id, current_version, created_at,
					updated_at, max_visitors) VALUES ($1, '', $2, $3, 1, '2026-01-01', '2026-01-01', $4) RETURNING id`, name, folder, env, maxVisitors)
			}
			nb("quiet", "ns-quiet", nil)
			nb("busy", "ns-busy", int64(2))
			nb("starting", "ns-starting", int64(99))
			now := time.Now().UTC()
			session := func(nbKey, mode, person, status string) {
				w.Exec(`INSERT INTO notebook_sessions (notebook_id, mode, user_id, version, namespace, job_name, status, error, token_fp,
					started_at, last_seen_at) VALUES ($1, $2, $3, 1, 'scripts', $4, $5::varchar, CASE WHEN $5::varchar = 'failed' THEN 'no image' END, $4,
					'2026-02-01', '2026-02-02 10:00:00.5')`, ids[nbKey], mode, w.People[person], fmt.Sprintf("j-%s-%s", nbKey, mode), status)
			}
			session("busy", "app", "writer", "ready")
			session("busy", "edit", "editor", "failed")
			session("starting", "app", "writer", "starting")
			w.Exec(`INSERT INTO notebook_visitors (notebook_id, user_id, opened_at, last_seen_at) VALUES ($1, $2, $3, $3), ($1, $4, $3, $3),
				($1, $5, $3, $6)`, ids["busy"], w.People["writer"], now.Add(-time.Minute), w.People["editor"], w.People["admin"], now.Add(-10*time.Minute))
			w.Exec(`INSERT INTO notebook_tickets (token_fp, notebook_id, mode, user_id, sid, frame_origin, expires_at) VALUES
				('old', $1, 'app', $2, 's', 'https://x', '2026-01-01')`, ids["quiet"], w.People["writer"])
			w.Token("writer", "tab_ns_writer_write_00000000", "write", time.Hour)
		},
		Steps: func(w *World) []Step {
			var s []Step
			id := func(k string) int64 { return ids[k] }
			as := func(p string) func(*http.Request) {
				if p == "nobody" {
					return nil
				}
				return w.As(p)
			}
			for _, p := range []string{"admin", "writer", "editor", "obs_flag", "nobody"} {
				for _, k := range []string{"quiet", "busy", "starting"} {
					s = append(s, Step{Name: p + " lists the sessions of " + k, Method: "GET",
						Path: fmt.Sprintf("/notebooks/%d/sessions", id(k)), Auth: as(p)})
				}
			}
			s = append(s, Step{Name: "sessions of a word", Method: "GET", Path: "/notebooks/x/sessions", Auth: as("writer")})
			ticket := func(name, p string, mode any, origin, host string) {
				st := Step{Name: name, Method: "POST", Path: fmt.Sprintf("/notebooks/%d/ticket", id("quiet")), Auth: as(p),
					JSON: map[string]any{"mode": mode}, Mask: []string{"url"}, Header: map[string]string{}}
				if origin != "" {
					st.Header["Origin"] = origin
				}
				if host != "" {
					st.Header["Host"] = host
				}
				s = append(s, st)
			}
			ticket("writer asks for the app, from the page", "writer", "app", "https://app.test", "app.test")
			ticket("writer asks for the editor, from the page", "writer", "edit", "https://app.test/", "APP.test")
			ticket("writer asks from a CORS origin", "writer", "app", "http://localhost:3000", "gateway:8000")
			ticket("writer asks from another site", "writer", "app", "https://evil.test", "app.test")
			ticket("writer asks without an origin", "writer", "app", "", "")
			ticket("writer asks from a path origin", "writer", "app", "https://app.test/x", "app.test")
			ticket("editor asks for the editor (VIEW)", "editor", "edit", "https://app.test", "app.test")
			ticket("editor asks for the app", "editor", "app", "https://app.test", "app.test")
			ticket("writer asks for a bad mode", "writer", "view", "https://app.test", "app.test")
			ticket("obs_flag asks", "obs_flag", "app", "https://app.test", "app.test")
			ticket("nobody asks", "nobody", "app", "https://app.test", "app.test")
			s = append(s, Step{Name: "a token asks", Method: "POST", Path: fmt.Sprintf("/notebooks/%d/ticket", id("quiet")),
				Auth: Bearer("tab_ns_writer_write_00000000"), JSON: map[string]any{"mode": "app"},
				Header: map[string]string{"Origin": "https://app.test", "Host": "app.test"}})
			s = append(s, Step{Name: "a ticket for nothing", Method: "POST", Path: "/notebooks/999999/ticket", Auth: as("writer"),
				JSON: map[string]any{"mode": "app"}})
			return s
		},
	})
}
