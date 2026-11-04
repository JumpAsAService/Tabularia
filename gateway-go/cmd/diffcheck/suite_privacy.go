package main

import (
	"net/http"
	"strings"
	"time"
)

// routes/privacy.py: the privacy notice, read before signing in, written by an administrator.
func init() {
	register(Suite{
		Module: "privacy",
		Seed: func(w *World) {
			w.Exec(`DELETE FROM privacy_notice`)
			w.Token("admin", "tab_pvad_000000000000000000000", "write", time.Hour)
		},
		Phases: []Phase{{Name: "environment's settings"}},
		Steps: func(w *World) []Step {
			var s []Step
			add := func(name, method string, auth func(*http.Request), body any) {
				s = append(s, Step{Name: name, Method: method, Path: "/privacy-notice", Auth: auth, JSON: body})
			}
			add("the first read writes the starting text", "GET", nil, nil)
			add("read again", "GET", w.As("writer"), nil)
			add("change the summary", "PUT", w.As("admin"), map[string]any{"summary": "Kept {audit_retention}."})
			add("turn it off, with a token", "PUT", Bearer("tab_pvad_000000000000000000000"), map[string]any{"enabled": false, "url": "https://x.test/p"})
			add("nothing given", "PUT", w.As("admin_group"), map[string]any{})
			add("a placeholder unknown, a brace", "PUT", w.As("admin"), map[string]any{"body": "{nope} {audit_retention} {"})
			add("a flag that is text", "PUT", w.As("admin"), map[string]any{"enabled": "maybe"})
			add("a long body", "PUT", w.As("admin"), map[string]any{"body": strings.Repeat("é", 5000)})
			add("not an administrator", "PUT", w.As("obs_flag"), map[string]any{"enabled": true})
			add("nobody", "PUT", nil, map[string]any{"enabled": true})
			s = append(s, bodyEdges("PUT", "/privacy-notice", w.As("admin"))...)
			add("read now", "GET", nil, nil)
			return s
		},
	})
}
