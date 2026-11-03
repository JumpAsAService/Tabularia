package main

import (
	"fmt"
	"net/http"
	"time"
)

// routes/permissions.py: the grants on a folder.
func init() {
	grants := map[string]int64{}
	register(Suite{
		Module: "permissions",
		Seed: func(w *World) {
			w.Token("admin", "tab_pm_admin_write_000000000", "write", time.Hour)
			folder := func(name string, parent *int64, owner string) int64 {
				id := w.Insert(`INSERT INTO projects (name, description, parent_id, owner_id, created_at)
					VALUES ($1, '', $2, $3, '2026-01-01') RETURNING id`, name, parent, w.People[owner])
				Folders[name] = id
				return id
			}
			grant := func(key string, folder int64, person, group, capability string) {
				var u, g *int64
				if person != "" {
					u = ptr(w.People[person])
				}
				if group != "" {
					g = ptr(Groups[group])
				}
				grants[key] = w.Insert(`INSERT INTO permissions (project_id, user_id, group_id, capability) VALUES ($1, $2, $3, $4) RETURNING id`,
					folder, u, g, capability)
			}
			a := folder("pm-A", nil, "admin")
			b := folder("pm-B", &a, "admin")
			personal := folder("sso", nil, "sso")
			grant("editor manages A", a, "editor", "", "manage")
			grant("Analysts view B", b, "", "Analysts", "view")
			grant("writer edits B", b, "writer", "", "edit")
			grant("sso's own", personal, "sso", "", "edit")
			grant("sso manages its own", personal, "sso", "", "manage")
			grant("writer connects B", b, "writer", "", "connect")
		},
		Steps: func(w *World) []Step {
			a, b := fmt.Sprint(Folders["pm-A"]), fmt.Sprint(Folders["pm-B"])
			g := func(k string) string { return fmt.Sprint(grants[k]) }
			var s []Step
			add := func(name, method, path string, auth func(*http.Request), body any) {
				s = append(s, Step{Name: name, Method: method, Path: path, Auth: auth, JSON: body})
			}
			for _, p := range []string{"admin", "editor", "writer", "obs_flag", "admin_group"} {
				add(p+" lists A's grants", "GET", "/projects/"+a+"/permissions", w.As(p), nil)
				add(p+" lists B's grants (inherited)", "GET", "/projects/"+b+"/permissions", w.As(p), nil)
			}
			add("nobody lists", "GET", "/projects/"+a+"/permissions", nil, nil)
			add("an unknown folder", "GET", "/projects/999999/permissions", w.As("writer"), nil)
			add("a bad path", "GET", "/projects/x/permissions", w.As("writer"), nil)
			add("a token lists", "GET", "/projects/"+a+"/permissions", Bearer("tab_pm_admin_write_000000000"), nil)

			// grants (forwarded until the bodies' validation is ported)
			add("grant a person", "POST", "/projects/"+b+"/permissions", w.As("editor"), map[string]any{"capability": "run", "user_id": w.People["obs_flag"]})
			add("grant a group", "POST", "/projects/"+b+"/permissions", w.As("editor"), map[string]any{"capability": "edit", "group_id": Groups["Sales"]})
			add("the same grant again", "POST", "/projects/"+b+"/permissions", w.As("editor"), map[string]any{"capability": "edit", "group_id": Groups["Sales"]})
			add("both subjects", "POST", "/projects/"+b+"/permissions", w.As("editor"), map[string]any{"capability": "edit", "group_id": Groups["Sales"], "user_id": w.People["writer"]})
			add("no subject", "POST", "/projects/"+b+"/permissions", w.As("editor"), map[string]any{"capability": "edit"})
			add("an unknown person", "POST", "/projects/"+b+"/permissions", w.As("editor"), map[string]any{"capability": "edit", "user_id": 999999})
			add("an unknown group", "POST", "/projects/"+b+"/permissions", w.As("editor"), map[string]any{"capability": "edit", "group_id": 999999})
			add("an unknown capability", "POST", "/projects/"+b+"/permissions", w.As("editor"), map[string]any{"capability": "own", "user_id": w.People["writer"]})
			add("no capability", "POST", "/projects/"+b+"/permissions", w.As("editor"), map[string]any{"user_id": w.People["writer"]})
			add("without MANAGE", "POST", "/projects/"+b+"/permissions", w.As("writer"), map[string]any{"capability": "edit", "user_id": w.People["writer"]})
			add("an unknown folder", "POST", "/projects/999999/permissions", w.As("writer"), map[string]any{"capability": "edit", "user_id": w.People["writer"]})
			s = append(s, bodyEdges("POST", "/projects/"+b+"/permissions", w.As("editor"))...)
			add("nobody grants", "POST", "/projects/"+b+"/permissions", nil, map[string]any{"capability": "edit", "user_id": w.People["writer"]})
			add("a bad path", "POST", "/projects/x/permissions", w.As("admin"), map[string]any{"capability": "edit", "user_id": w.People["writer"]})

			// revocations
			add("an unknown grant", "DELETE", "/permissions/999999", w.As("writer"), nil)
			add("a bad path", "DELETE", "/permissions/x", w.As("writer"), nil)
			add("nobody revokes", "DELETE", "/permissions/"+g("writer edits B"), nil, nil)
			add("without MANAGE", "DELETE", "/permissions/"+g("writer edits B"), w.As("writer"), nil)
			add("an observer revokes", "DELETE", "/permissions/"+g("writer edits B"), w.As("obs_flag"), nil)
			add("one's own grant on the personal folder", "DELETE", "/permissions/"+g("sso's own"), w.As("sso"), nil)
			add("one's MANAGE on the personal folder", "DELETE", "/permissions/"+g("sso manages its own"), w.As("sso"), nil)
			add("an inherited MANAGE revokes a person's", "DELETE", "/permissions/"+g("writer edits B"), w.As("editor"), nil)
			add("a group's", "DELETE", "/permissions/"+g("Analysts view B"), w.As("editor"), nil)
			add("with a token", "DELETE", "/permissions/"+g("writer connects B"), Bearer("tab_pm_admin_write_000000000"), nil)
			add("one's own MANAGE", "DELETE", "/permissions/"+g("editor manages A"), w.As("editor"), nil)
			add("again: gone", "DELETE", "/permissions/"+g("editor manages A"), w.As("editor"), nil)
			add("an administrator, the personal grant", "DELETE", "/permissions/"+g("sso's own"), w.As("admin"), nil)
			add("A's grants now", "GET", "/projects/"+a+"/permissions", w.As("admin"), nil)
			add("B's grants now", "GET", "/projects/"+b+"/permissions", w.As("admin"), nil)
			return s
		},
	})
}
