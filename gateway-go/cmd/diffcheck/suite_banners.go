package main

import (
	"fmt"
	"net/http"
	"strings"
	"time"
)

// routes/banners.py: the warnings shown in the Explore.
func init() {
	var old int64
	register(Suite{
		Module: "banners",
		Seed: func(w *World) {
			w.Token("admin", "tab_bnad_000000000000000000000", "write", time.Hour)
			old = w.Insert(`INSERT INTO banners (message, level, created_at, created_by) VALUES ($1, 'info', '2026-01-01', $2) RETURNING id`,
				strings.Repeat("ò", 90), w.People["admin"])
			w.Insert(`INSERT INTO banners (message, level, created_at) VALUES ('bn-old', 'danger', '2026-01-02 03:04:05.6') RETURNING id`)
		},
		Steps: func(w *World) []Step {
			var s []Step
			add := func(name, method, path string, auth func(*http.Request), body any) {
				s = append(s, Step{Name: name, Method: method, Path: path, Auth: auth, JSON: body})
			}
			add("the banners", "GET", "/banners", w.As("writer"), nil)
			add("nobody", "GET", "/banners", nil, nil)
			add("put one up", "POST", "/banners", w.As("admin"), map[string]any{"message": "  maintenance tonight  "})
			add("a level", "POST", "/banners", Bearer("tab_bnad_000000000000000000000"), map[string]any{"message": strings.Repeat("à", 100), "level": "danger"})
			add("blanks", "POST", "/banners", w.As("admin"), map[string]any{"message": "   "})
			add("empty", "POST", "/banners", w.As("admin"), map[string]any{"message": ""})
			add("too long", "POST", "/banners", w.As("admin"), map[string]any{"message": strings.Repeat("x", 501)})
			add("an unknown level", "POST", "/banners", w.As("admin"), map[string]any{"message": "x", "level": "error"})
			add("not an administrator", "POST", "/banners", w.As("obs_flag"), map[string]any{"message": "x"})
			s = append(s, bodyEdges("POST", "/banners", w.As("admin"))...)
			add("take one down", "DELETE", fmt.Sprintf("/banners/%d", old), w.As("admin_group"), nil)
			add("again", "DELETE", fmt.Sprintf("/banners/%d", old), w.As("admin"), nil)
			add("a bad path", "DELETE", "/banners/x", w.As("admin"), nil)
			add("not an administrator", "DELETE", "/banners/1", w.As("writer"), nil)
			add("nobody", "DELETE", "/banners/1", nil, nil)
			add("the banners now", "GET", "/banners", w.As("writer"), nil)
			return s
		},
	})
}
