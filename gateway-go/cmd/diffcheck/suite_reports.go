package main

import (
	"fmt"
	"net/http"
	"time"
)

// routes/reports.py: the reports, their versions, the renders and what the editor asks.

// rep holds the ids the reports' seed made (both databases have the same).
var rep struct {
	root, sales, edit, other, empty, conns                int64
	monthly, quarterly, secret, broken, missing, sent, mc int64
	listy, group                                          int64
}

// repContent is a definition as json.dumps wrote it (ASCII, «, » and «: »).
const repContent = `{"v": 1, "page": {"size": "A4", "orientation": "portrait", "margins": [15, 12, 15, 12.5]}, "locale": "it", ` +
	`"timezone": "Europe/Rome", "theme": {"font": "IBM Plex Sans", "size": 9.0, "color": "#1f2933", "accent": "#3b5bdb"}, ` +
	`"parameters": [{"name": "region", "label": "Regione é", "type": "text", "default": "Nord"}], ` +
	`"datasets": [{"name": "orders", "source": {"kind": "datasource", "datasource_id": 1}}, ` +
	`{"name": "big", "source": {"kind": "sql", "sql": "SELECT 1", "connection_id": 3}}], ` +
	`"sections": {"page_header": {"height": 18, "on_first_page": true, "items": []}, "body": {"items": [` +
	`{"id": "t1", "type": "text", "x": 0, "y": 0, "w": 50.5, "h": 10, "value": "1e+20 — caffè", "big": 123456789012345678901234567890, "f": 1e+16, "g": 1e-05}]}, ` +
	`"page_footer": {"height": 10, "on_last_page": true, "items": []}}}`

func init() {
	register(Suite{
		Module: "reports",
		Seed: func(w *World) {
			project := func(name string, parent *int64) int64 {
				return w.Insert(`INSERT INTO projects (name, description, parent_id, created_at) VALUES ($1, '', $2, '2026-01-01')
					RETURNING id`, name, parent)
			}
			grant := func(project int64, person, group, capability string) {
				var user, grp *int64
				if person != "" {
					user = ptr(w.People[person])
				}
				if group != "" {
					grp = ptr(Groups[group])
				}
				w.Exec(`INSERT INTO permissions (project_id, user_id, group_id, capability) VALUES ($1, $2, $3, $4)`,
					project, user, grp, capability)
			}
			rep.root = project("Rep Root", nil)
			rep.sales = project("Rep Sales", &rep.root)
			rep.edit = project("Rep Editing", &rep.sales)
			rep.other = project("Rep Other", nil)
			rep.empty = project("Rep Empty", &rep.sales)
			rep.conns = project("Rep Connections", nil)
			grant(rep.sales, "", "Sales", "view")
			grant(rep.edit, "editor", "", "edit")
			grant(rep.other, "writer", "", "edit")
			grant(rep.conns, "editor", "", "connect")
			grant(rep.edit, "service", "", "view")

			report := func(name string, project int64, owner string, current int, views int, lastViewed *string) int64 {
				return w.Insert(`INSERT INTO reports (name, description, project_id, owner_id, current_version, created_at, updated_at,
					views_total, last_viewed_at) VALUES ($1::text, 'about ' || $1::text, $2, $3, $4, '2026-02-01 10:00:00', '2026-02-03 11:30:00.250000',
					$5, $6) RETURNING id`, name, project, w.People[owner], current, views, lastViewed)
			}
			version := func(report int64, v int, content, note, by string) {
				var who *int64
				if by != "" {
					who = ptr(w.People[by])
				}
				w.Exec(`INSERT INTO report_versions (report_id, version, content, note, created_at, created_by)
					VALUES ($1, $2, $3, $4, '2026-02-0`+fmt.Sprint(v)+` 09:00:00', $5)`, report, v, content, note, who)
			}
			rep.monthly = report("Monthly sales", rep.edit, "admin", 2, 0, nil)
			version(rep.monthly, 1, `{"v": 1}`, "created", "admin")
			version(rep.monthly, 2, repContent, "edited", "editor")
			rep.quarterly = report("Quarterly", rep.sales, "admin", 1, 3, ptr("2026-02-10 08:00:00"))
			version(rep.quarterly, 1, `{"v": 1, "datasets": [], "parameters": [{"name": "a"}, {"name": "b"}]}`, "created", "")
			rep.secret = report("Secret", rep.other, "writer", 1, 0, nil)
			version(rep.secret, 1, `{"v": 1, "datasets": {"a": 1}}`, "created", "writer")
			rep.broken = report("Broken", rep.edit, "editor", 1, 0, nil)
			version(rep.broken, 1, `not json`, "created", "editor")
			rep.missing = report("Missing version", rep.edit, "editor", 4, 0, nil)
			rep.sent = report("Sent", rep.edit, "admin", 1, 0, nil)
			version(rep.sent, 1, `{"v": 1}`, "created", "admin")
			rep.mc = report("Monthly sales (1)", rep.edit, "admin", 1, 0, nil)
			version(rep.mc, 1, `{"v": 1, "datasets": "abc"}`, "created", "sso")
			rep.listy = report("Listy", rep.other, "writer", 1, 0, nil)
			version(rep.listy, 1, `[1, 2]`, "created", "writer")
			rep.group = w.Insert(`INSERT INTO flow_groups (name, description, project_id, owner_id, definition, created_at, updated_at)
				VALUES ('Rep weekly mail', '', $1, $2, $3, '2026-01-01', '2026-01-01') RETURNING id`, rep.edit, w.People["admin"],
				fmt.Sprintf(`{"steps": [[{"kind": "pushmail", "id": 1, "pushmail": {"reports": [{"reportId": %d, "params": {"region": "Nord"}}]}}, null]]}`, rep.sent))
			w.Insert(`INSERT INTO flow_groups (name, description, project_id, owner_id, definition, created_at, updated_at)
				VALUES ('Rep unreadable', '', $1, $2, $3, '2026-01-01', '2026-01-01') RETURNING id`, rep.edit, w.People["admin"],
				fmt.Sprintf(`{"steps": "no", "reportId": %d}`, rep.monthly))
			w.Token("writer", "tab_rep_writer_rw_000000000", "write", time.Hour)
			w.Token("writer", "tab_rep_writer_ro_000000000", "read", time.Hour)
			w.Token("editor", "tab_rep_editor_rw_000000000", "write", time.Hour)
		},
		Steps: func(w *World) []Step {
			var s []Step
			add := func(name, method, path string, auth func(*http.Request), body any) {
				s = append(s, Step{Name: name, Method: method, Path: path, Auth: auth, JSON: body})
			}
			id := func(n int64) string { return fmt.Sprint(n) }
			people := []string{"admin", "editor", "writer", "obs_flag", "obs_group", "admin_group", "service", "sso"}

			// lists
			for _, p := range people {
				add("list as "+p, "GET", "/reports", w.As(p), nil)
			}
			add("list without a session", "GET", "/reports", nil, nil)
			add("list with a read token", "GET", "/reports", Bearer("tab_rep_writer_ro_000000000"), nil)
			for _, p := range []string{"admin", "editor", "writer", "obs_flag"} {
				for _, f := range []int64{rep.sales, rep.edit, rep.other, rep.empty} {
					add(p+" lists folder "+id(f), "GET", "/projects/"+id(f)+"/reports", w.As(p), nil)
				}
			}
			add("an unknown folder", "GET", "/projects/999999/reports", w.As("editor"), nil)
			add("a folder that is no number", "GET", "/projects/abc/reports", w.As("editor"), nil)
			add("a folder that is no number, signed out", "GET", "/projects/abc/reports", nil, nil)
			add("a folder past int64", "GET", "/projects/99999999999999999999/reports", w.As("admin"), nil)

			// one report
			for _, p := range people {
				for _, r := range []int64{rep.monthly, rep.quarterly, rep.secret, rep.broken, rep.missing, rep.mc} {
					add(p+" opens report "+id(r), "GET", "/reports/"+id(r), w.As(p), nil)
				}
			}
			add("a list as its definition", "GET", "/reports/"+id(rep.listy), w.As("admin"), nil)
			add("unknown report", "GET", "/reports/999999", w.As("admin"), nil)
			for _, raw := range []string{"abc", "1.0", "%201%20", "+1", "1_0", "1e3", "-1", "99999999999999999999"} {
				add("report id «"+raw+"»", "GET", "/reports/"+raw, w.As("admin"), nil)
			}
			add("signed out", "GET", "/reports/"+id(rep.monthly), nil, nil)
			add("a read token reads it", "GET", "/reports/"+id(rep.secret), Bearer("tab_rep_writer_ro_000000000"), nil)
			add("search is not a report", "GET", "/reports/search", w.As("admin"), nil)
			add("search, bad sort", "GET", "/reports/search?sort=x&limit=0", w.As("admin"), nil)

			// versions
			for _, p := range []string{"admin", "editor", "writer", "obs_group"} {
				add(p+" lists the versions", "GET", "/reports/"+id(rep.monthly)+"/versions", w.As(p), nil)
				add(p+" reads version 1", "GET", "/reports/"+id(rep.monthly)+"/versions/1", w.As(p), nil)
				add(p+" reads version 2", "GET", "/reports/"+id(rep.monthly)+"/versions/2", w.As(p), nil)
			}
			add("versions of one without any", "GET", "/reports/"+id(rep.missing)+"/versions", w.As("editor"), nil)
			add("versions by someone gone", "GET", "/reports/"+id(rep.quarterly)+"/versions", w.As("admin"), nil)
			add("a version that is not", "GET", "/reports/"+id(rep.monthly)+"/versions/3", w.As("editor"), nil)
			add("an unreadable version", "GET", "/reports/"+id(rep.broken)+"/versions/1", w.As("editor"), nil)
			add("a list version", "GET", "/reports/"+id(rep.listy)+"/versions/1", w.As("admin"), nil)
			add("both ids wrong", "GET", "/reports/x/versions/y", w.As("editor"), nil)
			add("the version wrong", "GET", "/reports/"+id(rep.monthly)+"/versions/1.5", w.As("editor"), nil)
			add("the version past int64", "GET", "/reports/"+id(rep.monthly)+"/versions/99999999999999999999", w.As("editor"), nil)
			add("versions signed out", "GET", "/reports/"+id(rep.monthly)+"/versions", nil, nil)

			// openings
			add("editor opens it", "POST", "/reports/"+id(rep.quarterly)+"/views", w.As("editor"), nil)
			add("editor opens it again", "POST", "/reports/"+id(rep.quarterly)+"/views", w.As("editor"), nil)
			add("admin opens it", "POST", "/reports/"+id(rep.quarterly)+"/views", w.As("admin"), nil)
			add("a token opens it", "POST", "/reports/"+id(rep.secret)+"/views", Bearer("tab_rep_writer_rw_000000000"), nil)
			add("a read token opens it", "POST", "/reports/"+id(rep.secret)+"/views", Bearer("tab_rep_writer_ro_000000000"), nil)
			add("an observer cannot see it", "POST", "/reports/"+id(rep.quarterly)+"/views", w.As("obs_flag"), nil)
			add("opening without the CSRF header", "POST", "/reports/"+id(rep.quarterly)+"/views", Cookie(w.JWT("editor", "s-editor")), nil)
			add("opening an unknown one", "POST", "/reports/abc/views", w.As("editor"), nil)

			// copies
			add("editor copies Monthly sales", "POST", "/reports/"+id(rep.monthly)+"/duplicate", w.As("editor"), nil)
			add("editor copies its copy", "POST", "/reports/"+id(rep.mc)+"/duplicate", w.As("editor"), nil)
			add("editor copies the broken one", "POST", "/reports/"+id(rep.broken)+"/duplicate", w.As("editor"), nil)
			add("editor copies the one without a version", "POST", "/reports/"+id(rep.missing)+"/duplicate", w.As("editor"), nil)
			add("editor cannot copy in a folder it reads", "POST", "/reports/"+id(rep.quarterly)+"/duplicate", w.As("editor"), nil)
			add("editor cannot see the secret one", "POST", "/reports/"+id(rep.secret)+"/duplicate", w.As("editor"), nil)
			add("admin copies the secret one", "POST", "/reports/"+id(rep.secret)+"/duplicate", w.As("admin"), nil)
			add("a write token copies", "POST", "/reports/"+id(rep.secret)+"/duplicate", Bearer("tab_rep_writer_rw_000000000"), nil)
			add("a read token cannot copy", "POST", "/reports/"+id(rep.secret)+"/duplicate", Bearer("tab_rep_writer_ro_000000000"), nil)
			add("a list cannot be copied", "POST", "/reports/"+id(rep.listy)+"/duplicate", w.As("admin"), nil)
			add("lists after the copies", "GET", "/projects/"+id(rep.edit)+"/reports", w.As("editor"), nil)

			// deletions
			add("a report sent by a group", "DELETE", "/reports/"+id(rep.sent), w.As("editor"), nil)
			add("editor cannot delete in a folder it reads", "DELETE", "/reports/"+id(rep.quarterly), w.As("editor"), nil)
			add("editor cannot see the secret one", "DELETE", "/reports/"+id(rep.secret), w.As("editor"), nil)
			add("a read token cannot delete", "DELETE", "/reports/"+id(rep.secret), Bearer("tab_rep_writer_ro_000000000"), nil)
			add("editor deletes the broken one", "DELETE", "/reports/"+id(rep.broken), w.As("editor"), nil)
			add("again: gone", "DELETE", "/reports/"+id(rep.broken), w.As("editor"), nil)
			add("admin deletes a read one", "DELETE", "/reports/"+id(rep.quarterly), w.As("admin"), nil)
			add("deleting without an id", "DELETE", "/reports/x", w.As("admin"), nil)
			add("lists after the deletions", "GET", "/reports", w.As("admin"), nil)
			return s
		},
	})
}
