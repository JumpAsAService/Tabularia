package main

import (
	"fmt"
	"net/http"
	"time"
)

// routes/groups.py: the groups, their two roles, and what deleting one takes with it.
func init() {
	register(Suite{
		Module: "groups",
		Seed: func(w *World) {
			w.Token("admin", "tab_gr_admin_write_000000000", "write", time.Hour)
			group := func(name string, admin, observer bool, members ...string) int64 {
				Groups[name] = w.Insert(`INSERT INTO groups (name, description, is_admin, is_observer, created_at)
					VALUES ($1, $2, $3, $4, '2026-02-03 04:05:06.789') RETURNING id`, name, "about "+name, admin, observer)
				for _, m := range members {
					w.Exec(`INSERT INTO user_groups (user_id, group_id) VALUES ($1, $2)`, w.People[m], Groups[name])
				}
				return Groups[name]
			}
			plain := group("gr-Plain", false, false, "writer", "sso")
			group("gr-Admins", true, false, "writer")
			group("gr-Watch", false, true, "sso")
			group("gr-Empty", false, false)
			group("gr-Both", true, true)
			folder := w.Insert(`INSERT INTO projects (name, description, created_at) VALUES ('gr-Folder', '', '2026-01-01') RETURNING id`)
			w.Exec(`INSERT INTO permissions (project_id, group_id, capability) VALUES ($1, $2, 'view')`, folder, plain)
			env := w.Insert(`INSERT INTO script_environments (name, language, enabled, cpu_request, cpu_limit, memory_request,
				memory_limit, max_timeout_seconds, created_at, updated_at)
				VALUES ('gr-env', 'python', true, '1', '1', '1Gi', '1Gi', 60, '2026-01-01', '2026-01-01') RETURNING id`)
			w.Exec(`INSERT INTO script_environment_groups (environment_id, group_id) VALUES ($1, $2)`, env, plain)
			reg := w.Insert(`INSERT INTO container_registries (name, host, protocol, tls, username, enabled, created_at, updated_at)
				VALUES ('gr-reg', 'r.x', 'https', 'verify', '', true, '2026-01-01', '2026-01-01') RETURNING id`)
			w.Exec(`INSERT INTO container_registry_access (registry_id, group_id, created_at) VALUES ($1, $2, '2026-01-01')`, reg, plain)
		},
		Steps: func(w *World) []Step {
			gid := func(n string) string { return fmt.Sprint(Groups[n]) }
			var s []Step
			add := func(name, method, path string, auth func(*http.Request), body any) {
				s = append(s, Step{Name: name, Method: method, Path: path, Auth: auth, JSON: body})
			}
			for _, p := range []string{"admin", "editor", "obs_flag", "service", "writer"} {
				add(p+" lists the groups", "GET", "/groups", w.As(p), nil)
			}
			add("nobody lists", "GET", "/groups", nil, nil)

			// creations and changes (forwarded until the bodies' validation is ported)
			add("create", "POST", "/groups", w.As("admin"), map[string]any{"name": "gr-New", "description": "d"})
			add("a name taken", "POST", "/groups", w.As("admin"), map[string]any{"name": "gr-New"})
			add("no name", "POST", "/groups", w.As("admin"), map[string]any{"description": "d"})
			add("not an administrator", "POST", "/groups", w.As("editor"), map[string]any{"name": "gr-No"})
			add("an observer", "POST", "/groups", w.As("obs_flag"), map[string]any{"name": "gr-No"})
			add("nobody", "POST", "/groups", nil, map[string]any{"name": "gr-No"})
			add("an administrator by group", "POST", "/groups", w.As("admin_group"), map[string]any{"name": "gr-New2"})
			add("a description", "PATCH", "/groups/"+gid("gr-Plain"), w.As("admin"), map[string]any{"description": "changed"})
			add("the same description", "PATCH", "/groups/"+gid("gr-Plain"), w.As("admin"), map[string]any{"description": "changed"})
			add("make it administrators'", "PATCH", "/groups/"+gid("gr-Empty"), w.As("admin"), map[string]any{"is_admin": true})
			add("make it observers', with a token", "PATCH", "/groups/"+gid("gr-Plain"), Bearer("tab_gr_admin_write_000000000"), map[string]any{"is_observer": true})
			add("make it observers'", "PATCH", "/groups/"+gid("gr-Plain"), w.As("admin"), map[string]any{"is_observer": true, "is_admin": false})
			add("unmake it", "PATCH", "/groups/"+gid("gr-Plain"), w.As("admin"), map[string]any{"is_observer": false})
			add("the last way one is an administrator", "PATCH", "/groups/"+gid("Admins"), w.As("admin_group"), map[string]any{"is_admin": false})
			add("an unknown group", "PATCH", "/groups/999999", w.As("admin"), map[string]any{"description": "x"})
			add("a bad path", "PATCH", "/groups/x", w.As("admin"), map[string]any{"description": "x"})
			add("not an administrator", "PATCH", "/groups/"+gid("gr-Plain"), w.As("editor"), map[string]any{"description": "x"})
			add("a flag that is text", "PATCH", "/groups/"+gid("gr-Plain"), w.As("admin"), map[string]any{"is_admin": "maybe"})

			// deletions
			add("not an administrator", "DELETE", "/groups/"+gid("gr-Plain"), w.As("editor"), nil)
			add("an observer", "DELETE", "/groups/"+gid("gr-Plain"), w.As("obs_flag"), nil)
			add("nobody", "DELETE", "/groups/"+gid("gr-Plain"), nil, nil)
			add("not an administrator, a bad path", "DELETE", "/groups/x", w.As("editor"), nil)
			add("a bad path", "DELETE", "/groups/x", w.As("admin"), nil)
			add("an unknown group", "DELETE", "/groups/999999", w.As("admin"), nil)
			add("one's only administrators' group", "DELETE", "/groups/"+gid("Admins"), w.As("admin_group"), nil)
			add("a group with grants, environments, registries", "DELETE", "/groups/"+gid("gr-Plain"), w.As("admin"), nil)
			add("an administrators' group", "DELETE", "/groups/"+gid("gr-Admins"), w.As("admin_group"), nil)
			add("an observers' group", "DELETE", "/groups/"+gid("gr-Watch"), Bearer("tab_gr_admin_write_000000000"), nil)
			add("both roles, no members", "DELETE", "/groups/"+gid("gr-Both"), w.As("admin"), nil)
			add("again: gone", "DELETE", "/groups/"+gid("gr-Both"), w.As("admin"), nil)
			add("the groups now", "GET", "/groups", w.As("admin"), nil)
			return s
		},
	})
}
