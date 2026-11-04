package main

import (
	"fmt"
	"net/http"
	"strings"
	"time"
)

// routes/users.py: the accounts, their roles and groups, one's own language and password.
func init() {
	register(Suite{
		Module: "users",
		Seed: func(w *World) {
			maskColumns("users", "hashed_password")
			w.Person("us-pw", "us-pw@x.test", "the old password 1", false, false, false)
			w.Session("us-pw", "s-us-pw-2", time.Minute, time.Hour)
			w.Token("us-pw", "tab_us_pw_write_000000000000", "write", time.Hour)
			w.Person("us-brake", "us-brake@x.test", "the old password 1", false, false, false)
			w.Exec(`INSERT INTO login_attempts (ip, email, failures, last_at) VALUES ('?', 'password-change:'||$1::text, 6, now() at time zone 'utc' - interval '20.5 seconds')`,
				fmt.Sprint(w.People["us-brake"]))
			w.Person("us-target", "Us-Target@x.test", "the old password 1", false, false, false)
			w.Session("us-target", "s-us-target-2", time.Minute, time.Hour)
			w.Token("us-target", "tab_us_target_000000000000", "write", time.Hour)
			w.Person("us-svc", "us-svc@service.invalid", "", false, false, true)
			w.Token("us-svc", "tab_us_svc_0000000000000000", "write", time.Hour)
			w.Person("us-gone", "us-gone@x.test", "the old password 1", false, false, false)
			w.Token("us-gone", "tab_us_gone_000000000000000", "write", time.Hour)
			w.Person("us-full", "us-full@x.test", "the old password 1", false, false, false)
			w.Person("us-boss", "us-boss@x.test", "the old password 1", true, false, false)
			w.Token("admin", "tab_us_admin_write_000000000", "write", time.Hour)
			// us-gone's personal folder, empty; us-full's, with a flow in it
			for _, who := range []string{"us-gone", "us-full"} {
				f := w.Insert(`INSERT INTO projects (name, description, owner_id, created_at) VALUES ($1, '', $2, '2026-01-01') RETURNING id`, who, w.People[who])
				w.Exec(`INSERT INTO permissions (project_id, user_id, capability) VALUES ($1, $2, 'edit')`, f, w.People[who])
				Folders[who] = f
			}
			w.Exec(`INSERT INTO flows (name, description, project_id, definition, engine, created_at, updated_at, owner_id)
				VALUES ('us-flow', '', $1, '{}', 'polars', '2026-01-01', '2026-01-01', $2)`, Folders["us-full"], w.People["us-full"])
			w.Exec(`INSERT INTO audit_logs (ts, actor_id, actor_label, action, outcome, ip, user_agent)
				VALUES ('2026-01-01', $1, 'us-gone@x.test', 'auth.login', 'success', '1.2.3.4', 'ua'),
				       ('2026-01-01', NULL, 'US-GONE@x.test', 'auth.login_failed', 'failure', '1.2.3.4', 'ua')`, w.People["us-gone"])
			w.Exec(`INSERT INTO login_attempts (ip, email, failures, last_at) VALUES ('1.2.3.4', 'us-gone@x.test', 2, '2026-01-01')`)
			w.Insert(`INSERT INTO projects (name, description, created_at) VALUES ('us-coll', '', '2026-01-01') RETURNING id`)
			w.Insert(`INSERT INTO projects (name, description, created_at) VALUES ('us-coll (1)', '', '2026-01-01') RETURNING id`)
			Groups["us-Admins"] = w.Insert(`INSERT INTO groups (name, description, is_admin, is_observer, created_at) VALUES ('us-Admins', '', true, false, '2026-01-01') RETURNING id`)
			Groups["us-Watch"] = w.Insert(`INSERT INTO groups (name, description, is_admin, is_observer, created_at) VALUES ('us-Watch', '', false, true, '2026-01-01') RETURNING id`)
			Groups["us-Plain"] = w.Insert(`INSERT INTO groups (name, description, is_admin, is_observer, created_at) VALUES ('us-Plain', '', false, false, '2026-01-01') RETURNING id`)
			w.Exec(`INSERT INTO user_groups (user_id, group_id) VALUES ($1, $2)`, w.People["us-target"], Groups["us-Plain"])
		},
		Steps: func(w *World) []Step {
			u := func(k string) string { return fmt.Sprint(w.People[k]) }
			g := func(k string) string { return fmt.Sprint(Groups[k]) }
			var s []Step
			add := func(name, method, path string, auth func(*http.Request), body any) {
				s = append(s, Step{Name: name, Method: method, Path: path, Auth: auth, JSON: body})
			}
			for _, p := range []string{"admin", "admin_group", "obs_flag", "obs_group", "editor", "writer", "service"} {
				add(p+" lists the accounts", "GET", "/users", w.As(p), nil)
				// list_users reads `select(User)` without an order: the table's physical order, which
				// the two databases need not share once their sessions wrote last_seen_at at
				// different moments (a Python bug, noted)
				s[len(s)-1].Unordered = []string{""}
			}
			add("nobody lists", "GET", "/users", nil, nil)
			add("the password rule", "GET", "/users/password-rules", w.As("writer"), nil)
			add("the password rule, nobody", "GET", "/users/password-rules", nil, nil)

			// creation
			strong := "a strong password 7"
			add("create", "POST", "/users", w.As("admin"), map[string]any{"email": "us-new@x.test", "password": strong, "full_name": "New"})
			add("an email taken", "POST", "/users", w.As("admin"), map[string]any{"email": "us-new@x.test", "password": strong})
			add("an administrator and observer", "POST", "/users", w.As("admin_group"), map[string]any{"email": "us-boss2@x.test", "password": strong, "is_superuser": true, "is_observer": true})
			add("an observer: no personal folder", "POST", "/users", w.As("admin"), map[string]any{"email": "us-look@x.test", "password": strong, "is_observer": true})
			add("a folder name taken twice", "POST", "/users", w.As("admin"), map[string]any{"email": "us-coll@y.test", "password": strong})
			add("with a token", "POST", "/users", Bearer("tab_us_admin_write_000000000"), map[string]any{"email": "us-tok@x.test", "password": strong})
			add("a published password", "POST", "/users", w.As("admin"), map[string]any{"email": "us-weak@x.test", "password": " Password "})
			add("a short password", "POST", "/users", w.As("admin"), map[string]any{"email": "us-weak@x.test", "password": "short"})
			add("a long password", "POST", "/users", w.As("admin"), map[string]any{"email": "us-weak@x.test", "password": strings.Repeat("è", 40)})
			add("no email", "POST", "/users", w.As("admin"), map[string]any{"password": strong})
			add("not an administrator", "POST", "/users", w.As("editor"), map[string]any{"email": "us-no@x.test", "password": strong})
			add("an observer", "POST", "/users", w.As("obs_flag"), map[string]any{"email": "us-no@x.test", "password": strong})
			s = append(s, bodyEdges("POST", "/users", w.As("admin"))...)

			// one's language
			add("a language", "PUT", "/users/me/locale", w.As("writer"), map[string]any{"locale": "fr"})
			add("the same language", "PUT", "/users/me/locale", w.As("writer"), map[string]any{"locale": "fr"})
			add("an unknown language", "PUT", "/users/me/locale", w.As("writer"), map[string]any{"locale": "xx"})
			add("a language with a token", "PUT", "/users/me/locale", Bearer("tab_us_pw_write_000000000000"), map[string]any{"locale": "de"})
			add("nobody's language", "PUT", "/users/me/locale", nil, map[string]any{"locale": "it"})
			s = append(s, bodyEdges("PUT", "/users/me/locale", w.As("writer"))...)

			// one's password
			change := map[string]any{"current_password": "the old password 1", "new_password": "the new password 22"}
			add("an identity provider's account", "POST", "/users/me/password", w.As("sso"), change)
			add("an observer", "POST", "/users/me/password", w.As("obs_flag"), change)
			add("with a token", "POST", "/users/me/password", Bearer("tab_us_pw_write_000000000000"), change)
			add("a weak new password", "POST", "/users/me/password", w.As("us-pw"), map[string]any{"current_password": "x", "new_password": "admin"})
			add("braked", "POST", "/users/me/password", w.As("us-brake"), change)
			// the seconds left are counted by each side a moment apart, across a second's edge
			s[len(s)-1].Scrub = []string{`^\d+$`, `in \d+ seconds`}
			for i := 1; i <= 6; i++ {
				add(fmt.Sprintf("a wrong current password (%d)", i), "POST", "/users/me/password", w.As("us-pw"),
					map[string]any{"current_password": "wrong", "new_password": "the new password 22"})
				s[len(s)-1].Scrub = []string{`^\d+$`, `in \d+ seconds`} // the last one is braked
			}
			add("now braked", "POST", "/users/me/password", w.As("us-pw"), change)
			// the seconds left are counted by each side a moment apart, across a second's edge
			s[len(s)-1].Scrub = []string{`^\d+$`, `in \d+ seconds`}
			add("changed", "POST", "/users/me/password", w.As("us-target"), change)
			add("the session it was changed from holds", "GET", "/users/password-rules", w.As("us-target"), nil)
			add("the other session is closed", "GET", "/users/password-rules", Cookie(w.JWT("us-target", "s-us-target-2")), nil)
			add("nobody", "POST", "/users/me/password", nil, change)
			s = append(s, bodyEdges("POST", "/users/me/password", w.As("us-pw"))...)

			// changes
			add("rename", "PATCH", "/users/"+u("us-target"), w.As("admin"), map[string]any{"full_name": "Renamed"})
			add("the same name", "PATCH", "/users/"+u("us-target"), w.As("admin"), map[string]any{"full_name": "Renamed"})
			add("a password, with a token", "PATCH", "/users/"+u("us-target"), Bearer("tab_us_admin_write_000000000"), map[string]any{"password": strong})
			add("a weak password", "PATCH", "/users/"+u("us-target"), w.As("admin"), map[string]any{"password": "tabularia"})
			add("a service account's password", "PATCH", "/users/"+u("us-svc"), w.As("admin"), map[string]any{"password": strong})
			add("deactivate a service account", "PATCH", "/users/"+u("us-svc"), Bearer("tab_us_admin_write_000000000"), map[string]any{"is_active": false})
			add("reactivate, with a token", "PATCH", "/users/"+u("us-svc"), Bearer("tab_us_admin_write_000000000"), map[string]any{"is_active": true})
			add("reactivate", "PATCH", "/users/"+u("us-svc"), w.As("admin"), map[string]any{"is_active": true})
			add("promote, with a token", "PATCH", "/users/"+u("us-target"), Bearer("tab_us_admin_write_000000000"), map[string]any{"is_superuser": true})
			add("promote", "PATCH", "/users/"+u("us-target"), w.As("admin"), map[string]any{"is_superuser": true, "full_name": "Boss"})
			add("demote", "PATCH", "/users/"+u("us-target"), w.As("admin"), map[string]any{"is_superuser": false})
			add("an observer, with a token", "PATCH", "/users/"+u("us-target"), Bearer("tab_us_admin_write_000000000"), map[string]any{"is_observer": true})
			add("an observer: the tokens go", "PATCH", "/users/"+u("us-target"), w.As("admin"), map[string]any{"is_observer": true})
			add("no longer an observer", "PATCH", "/users/"+u("us-target"), w.As("admin"), map[string]any{"is_observer": false})
			add("a new password", "PATCH", "/users/"+u("us-target"), w.As("admin"), map[string]any{"password": strong})
			add("deactivate", "PATCH", "/users/"+u("us-target"), w.As("admin"), map[string]any{"is_active": false})
			add("one's own flag, the last way", "PATCH", "/users/"+u("us-boss"), w.As("us-boss"), map[string]any{"is_superuser": false})
			add("deactivate oneself", "PATCH", "/users/"+u("us-boss"), w.As("us-boss"), map[string]any{"is_active": false})
			add("one's own password", "PATCH", "/users/"+u("us-boss"), w.As("us-boss"), map[string]any{"password": strong})
			add("one's own flag, admin by group too", "PATCH", "/users/"+u("admin_group"), w.As("admin_group"), map[string]any{"is_superuser": false, "full_name": "Still"})
			add("an unknown account", "PATCH", "/users/999999", w.As("admin"), map[string]any{"full_name": "x"})
			add("a bad path", "PATCH", "/users/x", w.As("admin"), map[string]any{"full_name": 5})
			add("not an administrator", "PATCH", "/users/"+u("us-target"), w.As("editor"), map[string]any{"full_name": "x"})
			add("nobody", "PATCH", "/users/"+u("us-target"), nil, map[string]any{"full_name": "x"})
			s = append(s, bodyEdges("PATCH", "/users/"+u("us-target"), w.As("admin"))...)

			// groups
			add("join an administrators' group, with a token", "PUT", "/users/"+u("writer")+"/groups/"+g("us-Admins"), Bearer("tab_us_admin_write_000000000"), nil)
			add("join an administrators' group", "PUT", "/users/"+u("writer")+"/groups/"+g("us-Admins"), w.As("admin"), nil)
			add("again", "PUT", "/users/"+u("writer")+"/groups/"+g("us-Admins"), Bearer("tab_us_admin_write_000000000"), nil)
			add("join an observers' group: the tokens go", "PUT", "/users/"+u("us-pw")+"/groups/"+g("us-Watch"), w.As("admin"), nil)
			add("a plain group, with a token", "PUT", "/users/"+u("us-svc")+"/groups/"+g("us-Plain"), Bearer("tab_us_admin_write_000000000"), nil)
			add("an unknown account", "PUT", "/users/999999/groups/"+g("us-Plain"), w.As("admin"), nil)
			add("an unknown group", "PUT", "/users/"+u("writer")+"/groups/999999", w.As("admin"), nil)
			add("bad paths", "PUT", "/users/x/groups/y", w.As("admin"), nil)
			add("not an administrator", "PUT", "/users/"+u("writer")+"/groups/"+g("us-Plain"), w.As("editor"), nil)
			add("leave", "DELETE", "/users/"+u("writer")+"/groups/"+g("us-Admins"), w.As("admin"), nil)
			add("leave again", "DELETE", "/users/"+u("writer")+"/groups/"+g("us-Admins"), w.As("admin"), nil)
			add("leave a plain group", "DELETE", "/users/"+u("us-svc")+"/groups/"+g("us-Plain"), w.As("admin"), nil)
			add("leave one's only administrators' group", "DELETE", "/users/"+u("admin_group")+"/groups/"+fmt.Sprint(Groups["Admins"]), w.As("admin_group"), nil)
			add("an unknown pair", "DELETE", "/users/999999/groups/999999", w.As("admin"), nil)
			add("bad paths", "DELETE", "/users/x/groups/1", w.As("admin"), nil)
			add("nobody", "DELETE", "/users/x/groups/1", nil, nil)

			// deletion
			add("oneself", "DELETE", "/users/"+u("admin"), w.As("admin"), nil)
			add("an account past the integers", "DELETE", "/users/3000000000", w.As("admin"), nil)
			add("an account past the integers, changed", "PATCH", "/users/3000000000", w.As("admin"), map[string]any{"full_name": "x"})
			add("a membership past the integers", "PUT", "/users/3000000000/groups/3000000000", w.As("admin"), nil)
			add("a membership past the integers, left", "DELETE", "/users/3000000000/groups/3000000000", w.As("admin"), nil)
			add("an unknown account", "DELETE", "/users/999999", w.As("admin"), nil)
			add("a bad path", "DELETE", "/users/x", w.As("admin"), nil)
			add("not an administrator", "DELETE", "/users/"+u("us-gone"), w.As("editor"), nil)
			add("with an empty personal folder", "DELETE", "/users/"+u("us-gone"), w.As("admin"), nil)
			add("with a personal folder holding a flow", "DELETE", "/users/"+u("us-full"), Bearer("tab_us_admin_write_000000000"), nil)
			add("a service account", "DELETE", "/users/"+u("us-svc"), w.As("admin_group"), nil)
			add("the accounts now", "GET", "/users", w.As("admin"), nil)
			s[len(s)-1].Unordered = []string{""}
			return s
		},
	})
}
