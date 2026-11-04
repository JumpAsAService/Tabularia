package main

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strings"
	"time"
)

// routes/notebook_sessions.py (start, stop, save) and routes/notebook_proxy.py, with the pods
// at the fake engine (Kubernetes' API and the pod itself, podIP «siem.test»: marimo on 2718, the
// editor's file on 2719). Run it with DIFF_ENV="SCRIPTS__ENABLED=true SCRIPTS__NAMESPACE=scripts
// SCRIPTS__REGISTRY=registry.test/tab KUBERNETES__API_URL=http://{engine}:8000
// KUBERNETES__TOKEN_FILE=/etc/hostname NOTEBOOKS__ORIGIN=https://nb.test NOTEBOOKS__POD_IP_CHECK=false";
// without it the steps compare the refusals of an installation without notebooks.
func init() {
	ids := map[string]int64{}
	random := []string{`nb-\d+-(app|edit)-[0-9a-f]{6}`, `nbk_[A-Za-z0-9_-]{43}`}
	type side struct{ ticket, cookie string }
	sides := map[string]*side{}
	register(Suite{
		Module: "notebook_runtime",
		Seed: func(w *World) {
			maskColumns("notebook_sessions", "job_name", "token_fp")
			maskColumns("notebook_tickets", "token_fp")
			ids = map[string]int64{}
			folder := w.Insert(`INSERT INTO projects (name, description, created_at) VALUES ('nr-folder', '', '2026-01-01') RETURNING id`)
			w.Exec(`INSERT INTO permissions (project_id, user_id, capability) VALUES ($1, $2, 'edit'), ($1, $3, 'view')`, folder,
				w.People["writer"], w.People["editor"])
			group := w.Insert(`INSERT INTO groups (name, description, is_admin, is_observer, created_at) VALUES ('nr-coders', '', false, false,
				'2026-01-01') RETURNING id`)
			w.Exec(`INSERT INTO user_groups (user_id, group_id) VALUES ($1, $2), ($3, $2)`, w.People["writer"], group, w.People["editor"])
			env := func(key, name, lock, status string, enabled bool) {
				ids[key] = w.Insert(`INSERT INTO script_environments (name, description, language, enabled, cpu_request, cpu_limit, memory_request,
					memory_limit, max_timeout_seconds, created_at, updated_at) VALUES ($1, '', 'python', $2, '500m', '2', '1Gi', '2Gi', 60,
					'2026-01-01', '2026-01-01') RETURNING id`, name, enabled)
				w.Exec(`INSERT INTO script_environment_groups (environment_id, group_id) VALUES ($1, $2)`, ids[key], group)
				v := w.Insert(`INSERT INTO script_environment_versions (environment_id, number, language_version, base_image, packages, status,
					image_ref, lock, arch, created_at) VALUES ($1, 1, '3.12', 'python:3.12-slim', '', $2, 'registry.test/tab/env@sha256:1', $3,
					'amd64', '2026-01-01') RETURNING id`, ids[key], status, lock)
				w.Exec(`UPDATE script_environments SET current_version_id = $1 WHERE id = $2`, v, ids[key])
			}
			env("env", "nr-py", "marimo==0.9.0\npolars==1.0", "ready", true)
			env("nomarimo", "nr-nomarimo", "polars==1.0", "ready", true)
			env("building", "nr-building", "", "building", true)
			env("off", "nr-off", "", "ready", false)
			secret := "gAAAA-not-decryptable"
			if fernet != nil {
				secret, _ = fernet.Encrypt("hidden")
			}
			w.Exec(`INSERT INTO script_environment_variables (environment_id, name, secret, value, value_enc, from_tabularia, updated_at)
				VALUES ($1, 'PLAIN', false, 'p', NULL, false, '2026-01-01'), ($1, 'HOME', false, '/x', NULL, false, '2026-01-01'),
				($1, 'S', true, '', $2, false, '2026-01-01'), ($1, 'ROLE', false, 'APP__ROLE', NULL, true, '2026-01-01')`, ids["env"], secret)
			nb := func(key, name, env string, author any, draft any) {
				ids[key] = w.Insert(`INSERT INTO notebooks (name, description, project_id, environment_id, current_version, draft, created_at, updated_at)
					VALUES ($1, '', $2, $3, 1, $4, '2026-01-01', '2026-01-01') RETURNING id`, name, folder, ids[env], draft)
				w.Exec(`INSERT INTO notebook_versions (notebook_id, version, content, note, created_at, created_by) VALUES ($1, 1,
					'{"source": "import marimo\\napp = marimo.App()\\n"}', 'created', '2026-01-01', $2)`, ids[key], author)
			}
			nb("board", "nr-board", "env", w.People["writer"], nil)
			nb("draft", "nr-draft", "env", w.People["writer"], "import marimo # draft\n")
			nb("noauthor", "nr-noauthor", "env", nil, nil)
			nb("inactive", "nr-inactive", "env", w.People["inactive"], nil)
			nb("nomarimo", "nr-nomarimo", "nomarimo", w.People["writer"], nil)
			nb("building", "nr-building", "building", w.People["writer"], nil)
			nb("off", "nr-off", "off", w.People["writer"], nil)
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
			job := map[string]any{"metadata": map[string]any{"name": "the-job", "uid": "u-1"}, "status": map[string]any{"active": 1}}
			readyPod := map[string]any{"items": []any{map[string]any{"metadata": map[string]any{"name": "p1", "creationTimestamp": "2026-01-01T00:00:00Z"},
				"status": map[string]any{"podIP": "siem.test", "conditions": []any{map[string]any{"type": "Ready", "status": "True"}}}}}}
			k8s := func(more ...EngineRule) []EngineRule {
				return append(more,
					EngineRule{Method: "POST", Path: "^/apis/batch/v1/namespaces/scripts/jobs$", Body: job},
					EngineRule{Method: "GET", Path: "^/apis/batch/v1/namespaces/scripts/jobs/", Body: job},
					EngineRule{Method: "GET", Path: "^/api/v1/namespaces/scripts/pods$", Body: readyPod},
					EngineRule{Method: "GET", Path: "^/notebook.py$", Text: "import marimo # edited\n", Type: "text/plain"},
					EngineRule{Path: "^/notebooks/", Text: "<html>marimo</html>", Type: "text/html",
						Headers: map[string]string{"Content-Security-Policy": "default-src 'self'; frame-ancestors 'none'", "Set-Cookie": "m=1",
							"X-Frame-Options": "DENY", "X-Marimo": "yes"}},
				)
			}
			step := func(name, method, path, p string, body any, rules []EngineRule) {
				s = append(s, Step{Name: name, Method: method, Path: path, Auth: as(p), JSON: body, Engine: rules, EngineScrub: random})
			}
			sessions := func(k string) string { return fmt.Sprintf("/notebooks/%d/sessions", id(k)) }
			for _, p := range []string{"writer", "editor", "obs_flag", "nobody"} {
				step("start the app · "+p, "POST", sessions("board"), p, map[string]any{"mode": "app"}, k8s())
			}
			step("the sessions, the app ready", "GET", sessions("board"), "writer", nil, k8s())
			step("start the app again", "POST", sessions("board"), "editor", map[string]any{"mode": "app"}, k8s())
			step("start the editor, not allowed", "POST", sessions("board"), "editor", map[string]any{"mode": "edit"}, k8s())
			step("start the editor", "POST", sessions("draft"), "writer", map[string]any{"mode": "edit"}, k8s())
			step("start the editor, someone else's", "POST", sessions("draft"), "admin", map[string]any{"mode": "edit"}, k8s())
			step("the sessions, the editor ready", "GET", sessions("draft"), "writer", nil, k8s())
			step("save from the editor", "POST", fmt.Sprintf("/notebooks/%d/edit/save", id("draft")), "writer", map[string]any{"note": " my note "}, k8s())
			step("save again, the same", "POST", fmt.Sprintf("/notebooks/%d/edit/save", id("draft")), "writer", map[string]any{}, k8s())
			step("save, not the editor", "POST", fmt.Sprintf("/notebooks/%d/edit/save", id("draft")), "admin", map[string]any{}, k8s())
			step("save, a note too long", "POST", fmt.Sprintf("/notebooks/%d/edit/save", id("draft")), "writer",
				map[string]any{"note": strings.Repeat("n", 501)}, k8s())
			step("save, the app's notebook", "POST", fmt.Sprintf("/notebooks/%d/edit/save", id("board")), "writer", map[string]any{}, k8s())
			step("start, no author", "POST", sessions("noauthor"), "writer", map[string]any{"mode": "app"}, k8s())
			step("start, an inactive author", "POST", sessions("inactive"), "writer", map[string]any{"mode": "app"}, k8s())
			step("start, no marimo", "POST", sessions("nomarimo"), "writer", map[string]any{"mode": "app"}, k8s())
			step("start, no image yet", "POST", sessions("building"), "writer", map[string]any{"mode": "app"}, k8s())
			step("start, an environment off", "POST", sessions("off"), "admin", map[string]any{"mode": "app"}, k8s())
			step("start, the cluster refuses", "POST", sessions("noauthor"), "writer", map[string]any{"mode": "edit"},
				[]EngineRule{{Method: "POST", Path: "^/apis/batch/v1/namespaces/scripts/jobs$", Status: 403, Body: map[string]any{"message": "quota"}}})
			step("the sessions, one failed", "GET", sessions("noauthor"), "writer", nil, k8s())
			step("start, a bad mode", "POST", sessions("board"), "writer", map[string]any{"mode": "run"}, k8s())
			step("start, a word", "POST", "/notebooks/x/sessions", "writer", map[string]any{"mode": "app"}, k8s())

			// the proxy, on the notebooks' own address with a ticket
			ticket := Step{Name: "proxy: a ticket", Method: "POST", Path: fmt.Sprintf("/notebooks/%d/ticket", id("board")), Auth: as("writer"),
				JSON: map[string]any{"mode": "app"}, Mask: []string{"url"}, Header: map[string]string{"Origin": "https://app.test", "Host": "app.test"},
				Capture: func(name string, res *http.Response) {
					var out struct {
						URL string `json:"url"`
					}
					raw, _ := io.ReadAll(res.Body)
					res.Body = io.NopCloser(bytes.NewReader(raw))
					_ = json.Unmarshal(raw, &out)
					if u, err := url.Parse(out.URL); err == nil {
						sides[name] = &side{ticket: u.Query().Get("ticket")}
					}
				}}
			s = append(s, ticket)
			scrubCookie := []string{`tab_notebook=[A-Za-z0-9_.-]+`}
			frame := func(name, method, path string, prepare func(string, *http.Request), header map[string]string, rules []EngineRule) {
				h := map[string]string{"Host": "nb.test"}
				for k, v := range header {
					h[k] = v
				}
				s = append(s, Step{Name: "proxy: " + name, Method: method, Path: path, Header: h, Prepare: prepare, Engine: rules,
					HeaderScrub: scrubCookie, EngineScrub: random})
			}
			app := fmt.Sprintf("/notebooks/%d/app/", id("board"))
			frame("the ticket used", "GET", app+"?a=1&ticket=x&b=2", func(name string, r *http.Request) {
				if sd := sides[name]; sd != nil {
					r.URL.RawQuery = "a=1&ticket=" + url.QueryEscape(sd.ticket) + "&b=2"
				}
			}, nil, k8s())
			s[len(s)-1].Capture = func(name string, res *http.Response) {
				for _, c := range res.Header.Values("Set-Cookie") {
					if strings.HasPrefix(c, "tab_notebook=") {
						v, _, _ := strings.Cut(strings.TrimPrefix(c, "tab_notebook="), ";")
						if sides[name] != nil {
							sides[name].cookie = v
						}
					}
				}
			}
			withCookie := func(name string, r *http.Request) {
				if sd := sides[name]; sd != nil && sd.cookie != "" {
					r.AddCookie(&http.Cookie{Name: "tab_notebook", Value: sd.cookie})
				}
			}
			frame("the ticket again", "GET", app+"?ticket=x", func(name string, r *http.Request) {
				if sd := sides[name]; sd != nil {
					r.URL.RawQuery = "ticket=" + url.QueryEscape(sd.ticket)
				}
				withCookie(name, r)
			}, nil, k8s())
			frame("a ticket unknown", "GET", app+"?ticket=nope", nil, nil, k8s())
			frame("the app's root", "GET", app, withCookie, nil, k8s())
			frame("a file of marimo", "GET", app+"assets/x.js?v=2", withCookie, map[string]string{"Accept": "text/javascript"}, k8s())
			frame("a POST from its page", "POST", app+"api/kernel/run", withCookie, map[string]string{"Origin": "https://nb.test"}, k8s())
			frame("a POST from elsewhere", "POST", app+"api/kernel/run", withCookie, map[string]string{"Origin": "https://evil.test"}, k8s())
			frame("dot segments", "GET", app+"a/%2E%2E/sse", withCookie, nil, k8s())
			frame("the app's event stream", "GET", app+"sse", withCookie, nil, k8s())
			frame("without a cookie", "GET", app, nil, nil, k8s())
			frame("without a cookie, a file", "GET", app+"x.js", nil, nil, k8s())
			frame("without the slash", "GET", fmt.Sprintf("/notebooks/%d/app?x=1", id("board")), nil, nil, k8s())
			frame("the editor, not running", "GET", fmt.Sprintf("/notebooks/%d/edit/", id("board")), withCookie, nil, k8s())
			frame("the rest of the API is not there", "GET", "/auth/me", nil, nil, k8s())
			frame("nor its health", "GET", "/health", nil, nil, k8s())
			frame("nor save", "POST", fmt.Sprintf("/notebooks/%d/edit/save", id("draft")), nil, nil, k8s())
			frame("the pod not answering", "GET", app+"x.js", withCookie, nil,
				[]EngineRule{{Method: "GET", Path: "^/apis/batch/v1/namespaces/scripts/jobs/", Status: 404}})
			s = append(s, Step{Name: "proxy: the interface's address", Method: "GET", Path: app, Auth: as("writer"), Header: map[string]string{"Host": "app.test"}},
				Step{Name: "proxy: a WebSocket refused there", Method: "GET", Path: app + "ws", Header: map[string]string{"Host": "app.test", "Upgrade": "websocket",
					"Connection": "Upgrade", "Sec-WebSocket-Key": "dGhlIHNhbXBsZSBub25jZQ==", "Sec-WebSocket-Version": "13"}},
				Step{Name: "proxy: a WebSocket without its page", Method: "GET", Path: app + "ws", Header: map[string]string{"Host": "nb.test", "Upgrade": "websocket",
					"Connection": "Upgrade", "Sec-WebSocket-Key": "dGhlIHNhbXBsZSBub25jZQ==", "Sec-WebSocket-Version": "13"}, Prepare: withCookie},
			)
			// stopping
			step("stop the app, a viewer", "DELETE", sessions("board")+"/app", "editor", nil, k8s())
			step("stop the editor, its editor", "DELETE", sessions("draft")+"/edit", "writer", nil, k8s())
			step("stop the app", "DELETE", sessions("board")+"/app", "writer", nil, k8s())
			step("stop what is not running", "DELETE", sessions("board")+"/app", "writer", nil, k8s())
			step("stop, a bad mode", "DELETE", sessions("board")+"/run", "writer", nil, k8s())
			step("stop, nobody", "DELETE", sessions("board")+"/app", "nobody", nil, k8s())
			s = append(s, Step{Name: "pause", Method: "GET", Path: "/auth/sso/config", Before: func(*World) { time.Sleep(time.Second) }})
			return s
		},
	})
}
