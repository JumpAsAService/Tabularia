package main

import (
	"net/http"
	"time"
)

// routes/auth.py: /auth/me, and the authentication every route shares.
func init() {
	register(Suite{
		Module: "auth",
		Seed: func(w *World) {
			w.Session("editor", "s-expired", time.Minute, -time.Minute)
			w.Session("editor", "s-still", 40*time.Minute, time.Hour)
			w.Token("editor", "tab_editor_write_0000000000", "write", time.Hour)
			w.Token("editor", "tab_editor_read_00000000000", "read", time.Hour)
			w.Token("editor", "tab_editor_old_000000000000", "write", -time.Minute)
			w.Token("service", "tab_service_read_0000000000", "read", time.Hour)
			w.Token("obs_group", "tab_observer_00000000000000", "write", time.Hour)
			w.Token("writer", "tab_writer_write_0000000000", "write", time.Hour)
			w.Token("writer", "tab_writer_read_00000000000", "read", time.Hour)
		},
		Phases: []Phase{
			{Name: "environment's settings"},
			{Name: "panel: idle timeout 30 min, no personal tokens", Panel: map[string]string{
				"auth.session_idle_minutes": "30", "auth.personal_tokens": "false", "auth.api_token_max_days": "90"}},
		},
		Steps: func(w *World) []Step {
			var s []Step
			get := func(name string, auth func(*http.Request), h map[string]string) {
				s = append(s, Step{Name: name, Method: "GET", Path: "/auth/me", Auth: auth, Header: h})
			}
			for _, p := range peopleSpec {
				get(p.key+" · cookie", Cookie(w.JWT(p.key, "s-"+p.key)), nil)
				get(p.key+" · header", Bearer(w.JWT(p.key, "s-"+p.key)), nil)
			}
			get("nobody", nil, nil)
			get("a cookie that is no token", Cookie("not.a.token"), nil)
			get("a header that is no token", Bearer("not.a.token"), nil)
			get("Basic, then the cookie", func(r *http.Request) {
				r.Header.Set("Authorization", "Basic eDp5")
				Cookie(w.JWT("editor", "s-editor"))(r)
			}, nil)
			get("expired session", Cookie(w.JWT("editor", "s-expired")), nil)
			get("session still for 40 min", Cookie(w.JWT("editor", "s-still")), nil)
			get("someone else's session", Cookie(w.JWT("admin", "s-editor")), nil)
			get("closed session", Cookie(w.JWT("editor", "s-gone")), nil)
			get("an observer's write token (editor is in Watchers)", Bearer("tab_editor_write_0000000000"), nil)
			get("an observer's read token", Bearer("tab_editor_read_00000000000"), nil)
			get("a personal write token", Bearer("tab_writer_write_0000000000"), nil)
			get("a personal read token", Bearer("tab_writer_read_00000000000"), nil)
			get("expired token", Bearer("tab_editor_old_000000000000"), nil)
			get("service account's token", Bearer("tab_service_read_0000000000"), nil)
			get("an observer's personal token", Bearer("tab_observer_00000000000000"), nil)
			get("a token in the cookie", Cookie("tab_writer_write_0000000000"), nil)
			get("a personal read token, in Italian", Bearer("tab_writer_read_00000000000"), map[string]string{"Accept-Language": "it-IT,it;q=0.9"})
			s = append(s,
				// not ported: forwarded to Python, which must answer the same through the Go gateway
				Step{Name: "forwarded: POST without the CSRF header", Method: "POST", Path: "/auth/me", Auth: Cookie(w.JWT("editor", "s-editor"))},
				Step{Name: "forwarded: HEAD", Method: "HEAD", Path: "/auth/me", Auth: Cookie(w.JWT("editor", "s-editor"))},
				Step{Name: "forwarded: a read token writing", Method: "POST", Path: "/projects", Auth: Bearer("tab_writer_read_00000000000"),
					Header: map[string]string{"Accept-Language": "it"}},
			)
			return s
		},
	})
}
