package main

import (
	"fmt"
	"net/http"
	"time"
)

// routes/reports.py: the reports, their versions, the renders and what the editor asks.

// rep holds the ids the reports' seed made (both databases have the same).
var rep struct {
	root, sales, edit, other, empty, conns                  int64
	monthly, quarterly, secret, broken, missing, sent, mc   int64
	listy, group                                            int64
	ds, dsSecret, view, connPg, connSftp, script, scriptCat int64
	r1, r2, r3, r4                                          int64
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
			view := func(report int64, person string, daysAgo int) {
				w.Exec(`INSERT INTO report_views (report_id, user_id, day, at) VALUES ($1, $2, (now() at time zone 'UTC')::date - $3::int,
					'2026-02-01')`, report, w.People[person], daysAgo)
			}
			view(rep.monthly, "editor", 1)
			view(rep.monthly, "editor", 2)
			view(rep.monthly, "admin", 3)
			view(rep.secret, "writer", 0)
			view(rep.secret, "editor", 29)
			view(rep.secret, "admin", 30)
			view(rep.mc, "admin", 40)
			w.Exec(`UPDATE users SET full_name = '' WHERE id = $1`, w.People["sso"])
			datasource := func(name string, project int64, columns string) int64 {
				return w.Insert(`INSERT INTO datasources (name, description, project_id, bucket, key, columns, sort_keys, column_descriptions,
					kind, created_at, updated_at) VALUES ($1::text, '', $2, 'data-prep', 'datasets/rep/' || $1::text || '.parquet', $3, '[]', '{}',
					'upload', '2026-01-01', '2026-01-01') RETURNING id`, name, project, columns)
			}
			rep.ds = datasource("rep_orders", rep.sales, `[{"name": "region", "dtype": "String"}, {"name": "n", "dtype": "Int64"}, {"name": "d", "dtype": "Date"}]`)
			rep.dsSecret = datasource("rep_secret", rep.other, `[]`)
			rep.view = w.Insert(`INSERT INTO saved_views (name, description, project_id, datasource_id, spec, created_at, updated_at)
				VALUES ('rep view', '', $1, $2, '{"filters": [{"column": "region", "operator": "=", "value": "Nord"}]}', '2026-01-01', '2026-01-01')
				RETURNING id`, rep.sales, rep.ds)
			connection := func(name, dbType string) int64 {
				return w.Insert(`INSERT INTO connections (name, description, project_id, db_type, host, port, username, password_encrypted,
					database, db_schema, extra, created_at, updated_at, private_key_encrypted, host_key_type, host_key_fingerprint)
					VALUES ($1, '', $2, $3, 'db.example', 5432, 'u', '', 'shop', '', '{}', '2026-01-01', '2026-01-01', '', '', '')
					RETURNING id`, name, rep.conns, dbType)
			}
			rep.connPg = connection("rep pg", "postgresql")
			rep.connSftp = connection("rep sftp", "sftp")
			script := func(name, content string) int64 {
				id := w.Insert(`INSERT INTO sql_scripts (name, description, project_id, current_version, created_at, updated_at)
					VALUES ($1, '', $2, 1, '2026-01-01', '2026-01-01') RETURNING id`, name, rep.sales)
				w.Exec(`INSERT INTO sql_script_versions (script_id, version, content, note, created_at) VALUES ($1, 1, $2, 'created', '2026-01-01')`,
					id, content)
				return id
			}
			rep.script = script("rep on pg", fmt.Sprintf(`{"sql": "SELECT * FROM orders WHERE region = {region}", "connection_id": %d}`, rep.connPg))
			rep.scriptCat = script("rep on catalog", `{"sql": "SELECT * FROM rep_orders"}`)
			w.Insert(`INSERT INTO flow_groups (name, description, project_id, owner_id, definition, run_schedule, run_scheduled_by, next_run_at,
				created_at, updated_at) VALUES ('Rep scheduled', '', $1, $2, $3, '0 * * * *', $2, '2026-12-01', '2026-01-01', '2026-01-01')
				RETURNING id`, rep.edit, w.People["admin"],
				fmt.Sprintf(`{"steps": [[{"kind": "pushmail", "id": 2, "pushmail": {"reports": [{"reportId": %d, "params": {"region": "", "x": 1}}]}}]]}`, rep.monthly))
			renderable := func(name, content string) int64 {
				id := report(name, rep.edit, "admin", 1, 0, nil)
				version(id, 1, content, "created", "admin")
				return id
			}
			rep.r1 = renderable("Render: ds/view?", fmt.Sprintf(`{"v": 1, "locale": "it", "timezone": "Europe/Rome", "parameters": [`+
				`{"name": "region", "type": "text", "default": "Nord"}, {"name": "since", "type": "date", "default": "{today-30}"}, `+
				`{"name": "n", "type": "number", "multi": true, "default": [1, 2.5]}, {"name": "flag", "type": "boolean"}], `+
				`"datasets": [{"name": "orders", "source": {"kind": "datasource", "datasource_id": %d, "filters": [`+
				`{"column": "region", "op": "=", "value": "{region}"}, {"column": "n", "op": "in", "value": "{n}"}, `+
				`{"column": "d", "op": ">=", "value": "{today-7}"}, {"column": "x", "op": "=", "value": 5}]}}, `+
				`{"name": "v", "source": {"kind": "view", "view_id": %d}}, `+
				`{"name": "q", "source": {"kind": "sql", "sql": "SELECT * FROM t WHERE r = {region} AND d > {since} AND f = {flag} AND n IN {n} AND x < {now}", "connection_id": %d}}], `+
				`"sections": {"body": {"items": [{"id": "logo", "type": "image", "x": 0, "y": 0, "w": 10, "h": 10, "asset": "data:image/png;base64,iVBORw0KGgo="}]}}}`,
				rep.ds, rep.view, rep.connPg))
			rep.r2 = renderable("Render choices", fmt.Sprintf(`{"v": 1, "parameters": [`+
				`{"name": "region", "type": "text", "choices": {"dataset": "regions", "value": "[region]"}}, `+
				`{"name": "agent", "type": "text", "multi": true, "choices": {"dataset": "regions", "value": "[agent]", "label": "[name]"}}, `+
				`{"name": "fixed", "type": "number", "choices": {"values": [1, 2]}}], `+
				`"datasets": [{"name": "regions", "source": {"kind": "datasource", "datasource_id": %d, "filters": [{"column": "region", "op": "=", "value": "{region}"}]}}]}`, rep.ds))
			rep.r3 = renderable("Render catalog", `{"v": 1, "datasets": [{"name": "q", "source": {"kind": "sql", "sql": "SELECT * FROM rep_orders"}}]}`)
			rep.r4 = renderable("Render secret", fmt.Sprintf(`{"v": 1, "datasets": [{"name": "s", "source": {"kind": "datasource", "datasource_id": %d}}]}`, rep.dsSecret))
			w.Exec(`UPDATE datasources SET key = '' WHERE id = $1`, rep.dsSecret)
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
			for _, p := range []string{"admin", "editor", "writer", "obs_flag", "obs_group"} {
				for _, q := range []string{"", "?sort=name", "?sort=updated", "?q=sales", "?q=%20%20", "?q=rep%20sales&sort=name",
					"?q=MONTH", "?q=_", "?q=%25", "?q=about%20sent", "?limit=2&offset=1", "?limit=2&offset=1&sort=name", "?offset=50"} {
					add(p+" searches "+q, "GET", "/reports/search"+q, w.As(p), nil)
				}
			}
			add("search signed out", "GET", "/reports/search", nil, nil)
			add("search, bad sort and limit", "GET", "/reports/search?sort=x&limit=0", w.As("admin"), nil)
			add("search, limit past 200", "GET", "/reports/search?limit=201&offset=-1", w.As("admin"), nil)
			add("search, limit no number", "GET", "/reports/search?limit=a&limit=3", w.As("admin"), nil)
			add("search, limit no number last", "GET", "/reports/search?limit=3&limit=a", w.As("admin"), nil)
			add("search, bad sort signed out", "GET", "/reports/search?sort=x", nil, nil)

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
			s = append(s, repWrites(w)...)
			s = append(s, repRenders(w)...)
			return s
		},
	})
}

// repWrites are the steps of the routes that write a definition: create, change, restore, check.
func repWrites(w *World) []Step {
	var s []Step
	id := func(n int64) string { return fmt.Sprint(n) }
	add := func(name, method, path string, auth func(*http.Request), body any) {
		s = append(s, Step{Name: name, Method: method, Path: path, Auth: auth, JSON: body})
	}
	raw := func(name, method, path string, auth func(*http.Request), body, ct string) {
		s = append(s, Step{Name: name, Method: method, Path: path, Auth: auth, Raw: []byte(body), ContentType: ct})
	}
	edit := "/projects/" + id(rep.edit) + "/reports"
	ds := func(name string, source map[string]any) map[string]any {
		return map[string]any{"name": name, "source": source}
	}
	def := func(datasets ...any) map[string]any {
		return map[string]any{"v": 1, "parameters": []any{map[string]any{"name": "region", "type": "text", "default": "Nord"}},
			"datasets": datasets}
	}
	onDS := ds("orders", map[string]any{"kind": "datasource", "datasource_id": rep.ds,
		"filters": []any{map[string]any{"column": "region", "op": "=", "value": "{region}"}}})

	// creating
	add("editor creates an empty one", "POST", edit, w.As("editor"), map[string]any{"name": " Rep new ", "description": " d "})
	add("the same name again", "POST", edit, w.As("editor"), map[string]any{"name": "Rep new"})
	add("a blank name", "POST", edit, w.As("editor"), map[string]any{"name": "   "})
	add("a name that is no text", "POST", edit, w.As("editor"), map[string]any{"name": 5, "description": []any{}})
	add("no body", "POST", edit, w.As("editor"), nil)
	raw("a broken JSON", "POST", edit, w.As("editor"), `{"name": `, "application/json")
	raw("a broken JSON, signed out", "POST", edit, nil, `{"name": `, "application/json")
	add("signed out", "POST", edit, nil, map[string]any{"name": "x"})
	add("in a folder it only reads", "POST", "/projects/"+id(rep.sales)+"/reports", w.As("editor"), map[string]any{"name": "x"})
	add("in an unknown folder", "POST", "/projects/999999/reports", w.As("editor"), map[string]any{"name": "x"})
	add("in a folder that is no number, with a bad body", "POST", "/projects/abc/reports", w.As("editor"), map[string]any{"name": 1})
	add("a read token creates", "POST", edit, Bearer("tab_rep_editor_rw_000000000"), map[string]any{"name": "Rep by token"})
	add("a definition of version 2", "POST", edit, w.As("editor"), map[string]any{"name": "Rep v2", "content": map[string]any{"v": 2}})
	add("a definition that is a list", "POST", edit, w.As("editor"), map[string]any{"name": "Rep list", "content": []any{1}})
	add("a datasource read", "POST", edit, w.As("editor"), map[string]any{"name": "Rep on ds", "content": def(onDS)})
	add("a filter on a column it lacks", "POST", edit, w.As("editor"), map[string]any{"name": "Rep bad col", "content": def(
		ds("orders", map[string]any{"kind": "datasource", "datasource_id": rep.ds,
			"filters": []any{map[string]any{"column": "nope", "op": "=", "value": 1}}}))})
	add("a datasource it cannot read", "POST", edit, w.As("editor"), map[string]any{"name": "Rep secret ds", "content": def(
		ds("s", map[string]any{"kind": "datasource", "datasource_id": rep.dsSecret}))})
	add("admin reads it", "POST", edit, w.As("admin"), map[string]any{"name": "Rep admin secret ds", "content": def(
		ds("s", map[string]any{"kind": "datasource", "datasource_id": rep.dsSecret}))})
	add("a saved view", "POST", edit, w.As("editor"), map[string]any{"name": "Rep on view", "content": def(
		ds("v", map[string]any{"kind": "view", "view_id": rep.view, "filters": []any{map[string]any{"column": "region", "op": "in", "value": []any{"a"}}}}))})
	add("an unknown view", "POST", edit, w.As("editor"), map[string]any{"name": "Rep no view", "content": def(
		ds("v", map[string]any{"kind": "view", "view_id": 999}))})
	add("SQL on a connection", "POST", edit, w.As("editor"), map[string]any{"name": "Rep on pg", "content": def(
		ds("q", map[string]any{"kind": "sql", "sql": "SELECT * FROM t WHERE r = {region} AND d > {today-7} -- {x}", "connection_id": rep.connPg}))})
	add("a parameter in quotes", "POST", edit, w.As("editor"), map[string]any{"name": "Rep quoted", "content": def(
		ds("q", map[string]any{"kind": "sql", "sql": "SELECT '{region}'", "connection_id": rep.connPg}))})
	add("a brace alone", "POST", edit, w.As("editor"), map[string]any{"name": "Rep brace", "content": def(
		ds("q", map[string]any{"kind": "sql", "sql": "SELECT } FROM t", "connection_id": rep.connPg}))})
	add("an unknown name in braces", "POST", edit, w.As("editor"), map[string]any{"name": "Rep unknown brace", "content": def(
		ds("q", map[string]any{"kind": "sql", "sql": "SELECT {nope}", "connection_id": rep.connPg}))})
	add("a backslash makes it unclear", "POST", edit, w.As("editor"), map[string]any{"name": "Rep unclear", "content": def(
		ds("q", map[string]any{"kind": "sql", "sql": `SELECT 'a\' , {region} -- '`, "connection_id": rep.connPg}))})
	add("a connection that is not a database", "POST", edit, w.As("editor"), map[string]any{"name": "Rep sftp", "content": def(
		ds("q", map[string]any{"kind": "sql", "sql": "SELECT 1", "connection_id": rep.connSftp}))})
	add("a connection without CONNECT", "POST", edit, w.As("writer"), map[string]any{"name": "Rep w pg", "content": def(
		ds("q", map[string]any{"kind": "sql", "sql": "SELECT 1", "connection_id": rep.connPg}))})
	add("SQL on the catalog: Python reads it", "POST", edit, w.As("editor"), map[string]any{"name": "Rep catalog", "content": def(
		ds("q", map[string]any{"kind": "sql", "sql": "SELECT * FROM rep_orders WHERE region = {region}"}))})
	add("a script on a connection", "POST", edit, w.As("editor"), map[string]any{"name": "Rep script", "content": def(
		ds("q", map[string]any{"kind": "sql_script", "script_id": rep.script}))})
	add("a script on the catalog: Python reads it", "POST", edit, w.As("editor"), map[string]any{"name": "Rep script cat", "content": def(
		ds("q", map[string]any{"kind": "sql_script", "script_id": rep.scriptCat}))})
	add("an unknown script", "POST", edit, w.As("editor"), map[string]any{"name": "Rep no script", "content": def(
		ds("q", map[string]any{"kind": "sql_script", "script_id": 9999}))})
	lists := map[string]any{"v": 1, "parameters": []any{
		map[string]any{"name": "a", "type": "text", "choices": map[string]any{"dataset": "x", "value": "[a]"}},
		map[string]any{"name": "b", "type": "text", "choices": map[string]any{"dataset": "y", "value": "[b]"}}},
		"datasets": []any{
			ds("x", map[string]any{"kind": "datasource", "datasource_id": rep.ds, "filters": []any{map[string]any{"column": "region", "op": "=", "value": "{b}"}}}),
			ds("y", map[string]any{"kind": "datasource", "datasource_id": rep.ds, "filters": []any{map[string]any{"column": "region", "op": "=", "value": "{a}"}}})}}
	add("lists that follow each other", "POST", edit, w.As("editor"), map[string]any{"name": "Rep circle", "content": lists})
	s = append(s, Step{Name: "the engine finds problems", Method: "POST", Path: edit, Auth: w.As("editor"),
		JSON: map[string]any{"name": "Rep engine", "content": def(onDS)},
		Engine: []EngineRule{{Method: "POST", Path: "^/reports/check$", Body: map[string]any{"problems": []any{
			map[string]any{"item": "k1", "field": "value", "pos": 3, "length": 2, "message": "there is no field [x]"},
			map[string]any{"where": "datasets[0].source", "message": "bad source", "position": 1, "length": true},
			"not a problem"}}}}})
	s = append(s, Step{Name: "the engine has no check yet", Method: "POST", Path: edit, Auth: w.As("editor"),
		JSON:   map[string]any{"name": "Rep engine 404", "content": def(onDS)},
		Engine: []EngineRule{{Method: "POST", Path: "^/reports/check$", Status: 404, Body: map[string]any{"detail": "Not Found"}}}})
	add("admin with a picture", "POST", edit, w.As("admin"), map[string]any{"name": "Rep picture", "content": map[string]any{"v": 1,
		"sections": map[string]any{"page_header": map[string]any{"height": 20, "items": []any{map[string]any{"id": "logo", "type": "image",
			"x": 0, "y": 0, "w": 30, "h": 10, "asset": "data:image/png;base64,iVBO\nRw0K GgoAAAAA"}}}}}})
	add("a description too long", "POST", edit, w.As("editor"), map[string]any{"name": "x", "description": string(make([]byte, 2001))})

	// changing
	m := "/reports/" + id(rep.monthly)
	add("editor renames it", "PATCH", m, w.As("editor"), map[string]any{"name": "Rep monthly"})
	add("to a name taken", "PATCH", m, w.As("editor"), map[string]any{"name": "Sent"})
	add("nothing changes", "PATCH", m, w.As("editor"), map[string]any{"name": "Rep monthly", "description": "about Monthly sales"})
	add("the description", "PATCH", m, w.As("editor"), map[string]any{"description": "  new words "})
	add("a move without EDIT there", "PATCH", m, w.As("editor"), map[string]any{"project_id": rep.other})
	add("a move to no folder", "PATCH", m, w.As("editor"), map[string]any{"project_id": 99999})
	add("a move past int64", "PATCH", m, w.As("editor"), map[string]any{"project_id": 99999999999999999})
	add("a stale version", "PATCH", m, w.As("editor"), map[string]any{"content": def(onDS), "base_version": 1})
	add("a new dataset: the group scheduled by admin stops", "PATCH", m, w.As("editor"), map[string]any{"content": def(onDS), "base_version": 2})
	add("the same content again", "PATCH", m, w.As("editor"), map[string]any{"content": def(onDS)})
	add("a new default", "PATCH", m, w.As("admin"), map[string]any{"content": map[string]any{"v": 1,
		"parameters": []any{map[string]any{"name": "region", "type": "text", "default": "Sud"}}, "datasets": []any{onDS}}})
	add("a bad definition", "PATCH", m, w.As("editor"), map[string]any{"content": map[string]any{"v": 1, "page": map[string]any{"size": "A9"}}})
	add("SQL on the catalog: Python changes it", "PATCH", m, w.As("editor"), map[string]any{"name": "Rep monthly catalog", "content": def(
		ds("q", map[string]any{"kind": "sql", "sql": "SELECT * FROM rep_orders"}))})
	add("admin moves it", "PATCH", m, w.As("admin"), map[string]any{"project_id": rep.other, "name": "Rep moved"})
	add("writer moves it back and renames", "PATCH", m, w.As("writer"), map[string]any{"project_id": rep.edit})
	add("admin moves it back", "PATCH", m, w.As("admin"), map[string]any{"project_id": rep.edit, "name": "Rep monthly"})
	add("a change by someone who cannot see it", "PATCH", m, w.As("writer"), map[string]any{"name": "x"})
	add("a change of one it reads", "PATCH", "/reports/"+id(rep.quarterly), w.As("editor"), map[string]any{"name": "x"})
	add("a change with a bad body and a bad id", "PATCH", "/reports/x", w.As("editor"), map[string]any{"name": []any{}, "base_version": "a"})

	// restoring
	add("restore version 1", "POST", m+"/versions/1/restore", w.As("editor"), nil)
	add("restore the current one", "POST", m+"/versions/"+id(99)+"/restore", w.As("editor"), nil)
	add("versions after", "GET", m+"/versions", w.As("admin"), nil)
	add("restore a bad old one", "POST", "/reports/"+id(rep.listy)+"/versions/1/restore", w.As("admin"), nil)
	add("restore without EDIT", "POST", "/reports/"+id(rep.mc)+"/versions/1/restore", w.As("obs_group"), nil)
	add("restore with bad ids", "POST", "/reports/a/versions/b/restore", w.As("editor"), nil)
	add("restore as a read token", "POST", m+"/versions/1/restore", Bearer("tab_rep_writer_ro_000000000"), nil)

	// checking
	add("check of a saved one", "POST", "/reports/check", w.As("editor"), map[string]any{"definition": def(onDS), "report_id": rep.monthly})
	add("check of a new one", "POST", "/reports/check", w.As("editor"), map[string]any{"definition": map[string]any{"v": 3}, "project_id": rep.edit})
	add("check without a folder", "POST", "/reports/check", w.As("editor"), map[string]any{"definition": map[string]any{}})
	add("check in a folder it reads", "POST", "/reports/check", w.As("editor"), map[string]any{"definition": map[string]any{}, "project_id": rep.sales})
	add("check of one it cannot see", "POST", "/reports/check", w.As("editor"), map[string]any{"definition": map[string]any{}, "report_id": rep.secret})
	add("check without a definition", "POST", "/reports/check", w.As("editor"), map[string]any{"report_id": "x"})
	add("check of catalog SQL: Python", "POST", "/reports/check", w.As("editor"), map[string]any{"definition": def(
		ds("q", map[string]any{"kind": "sql", "sql": "SELECT * FROM rep_orders"})), "project_id": rep.edit})
	s = append(s, Step{Name: "check with the engine's problems", Method: "POST", Path: "/reports/check", Auth: w.As("editor"),
		JSON: map[string]any{"definition": map[string]any{"v": 1, "x": 1}, "project_id": rep.edit},
		Engine: []EngineRule{{Method: "POST", Path: "^/reports/check$", Body: map[string]any{"errors": []any{
			map[string]any{"item": nil, "field": "", "message": "The report: unknown key «x»"},
			map[string]any{"item": "a", "field": "b", "message": "other"}}}}}})
	return s
}

// repRenderEngine is the fake engine of a render that goes well.
var repRenderEngine = []EngineRule{
	{Method: "GET", Path: "^/engines$", Body: []any{map[string]any{"id": "polars", "available": true},
		map[string]any{"id": "duckdb", "available": true}, map[string]any{"id": "chdb", "available": false}}},
	{Method: "POST", Path: "^/placeholders/preview$", Body: map[string]any{"fields": map[string]any{
		"p0": map[string]any{"value": "2026-10-04"}, "p1": map[string]any{"value": "2026-11-03T10:20"}, "p2": map[string]any{"value": "2026-10-27"}}}},
	{Method: "POST", Path: "^/reports/render$", Body: map[string]any{"task_id": "t-1", "filename": "made.pdf"}},
	{Method: "POST", Path: "^/reports/choices$", Body: map[string]any{"choices": []any{map[string]any{"value": "Nord"},
		map[string]any{"value": "Sud", "label": "South"}, "junk"}, "truncated": false}},
	{Method: "GET", Path: "^/tasks/exports/t-1$", Body: map[string]any{"status": "ready"}},
	{Method: "GET", Path: "^/tasks/exports/t-1/file$", Body: map[string]any{"a": "file"}},
}

// repRenders are the steps of the renders: the viewer's, the editor's preview, the API's.
func repRenders(w *World) []Step {
	var s []Step
	id := func(n int64) string { return fmt.Sprint(n) }
	add := func(name, method, path string, auth func(*http.Request), body any, engine ...EngineRule) {
		rules := repRenderEngine
		if engine != nil {
			rules = append(append([]EngineRule{}, engine...), repRenderEngine...)
		}
		s = append(s, Step{Name: name, Method: method, Path: path, Auth: auth, JSON: body, Engine: rules})
	}
	r1 := "/reports/" + id(rep.r1) + "/render"
	add("editor renders it", "POST", r1, w.As("editor"), map[string]any{})
	add("admin renders it with values", "POST", r1, w.As("admin"), map[string]any{"format": "html",
		"params": map[string]any{"region": "Sud", "since": "2026-01-31", "n": []any{3, "4.5", 3.0}, "flag": "yes"}, "state": map[string]any{"items": map[string]any{"t": map[string]any{}}}})
	add("as svg with a token", "POST", r1, Bearer("tab_rep_editor_rw_000000000"), map[string]any{"format": "svg", "params": map[string]any{"region": nil}})
	add("a value that does not fit", "POST", r1, w.As("editor"), map[string]any{"params": map[string]any{"since": "yesterday"}})
	add("two values for one", "POST", r1, w.As("editor"), map[string]any{"params": map[string]any{"region": []any{"a", "b"}}})
	add("a parameter it lacks", "POST", r1, w.As("editor"), map[string]any{"params": map[string]any{"nope": 1}})
	add("an object as a value", "POST", r1, w.As("editor"), map[string]any{"params": map[string]any{"region": map[string]any{"a": 1}}})
	add("no value said out loud", "POST", r1, w.As("editor"), map[string]any{"params": map[string]any{"region": "{none}", "n": []any{"", " "}}})
	add("a placeholder with a space", "POST", r1, w.As("editor"), map[string]any{"params": map[string]any{"since": "{today 1}"}})
	add("a placeholder given", "POST", r1, w.As("editor"), map[string]any{"params": map[string]any{"since": "{yesterday}"}})
	add("a state that is wrong", "POST", r1, w.As("editor"), map[string]any{"state": map[string]any{"items": []any{1}}})
	add("a format it does not know", "POST", r1, w.As("editor"), map[string]any{"format": "docx"})
	add("someone who cannot see it", "POST", r1, w.As("obs_flag"), map[string]any{})
	add("signed out", "POST", r1, nil, map[string]any{})
	add("the engine refuses", "POST", r1, w.As("editor"), map[string]any{},
		EngineRule{Method: "POST", Path: "^/reports/render$", Status: 422, Body: map[string]any{"detail": "there is no field [x]"}})
	add("the engine is busy", "POST", r1, w.As("editor"), map[string]any{},
		EngineRule{Method: "POST", Path: "^/reports/render$", Status: 429, Body: map[string]any{"detail": "Too many exports"}})
	add("the engine times out", "POST", r1, w.As("editor"), map[string]any{},
		EngineRule{Method: "POST", Path: "^/reports/render$", Status: 504, Body: map[string]any{"detail": "x"}})
	add("the engine fails", "POST", r1, w.As("editor"), map[string]any{},
		EngineRule{Method: "POST", Path: "^/reports/render$", Status: 500, Body: "oops"})
	add("the placeholders cannot be read", "POST", r1, w.As("editor"), map[string]any{},
		EngineRule{Method: "POST", Path: "^/placeholders/preview$", Status: 500, Body: map[string]any{}})
	add("the time zone is wrong", "POST", r1, w.As("editor"), map[string]any{},
		EngineRule{Method: "POST", Path: "^/placeholders/preview$", Body: map[string]any{"error": "unknown zone"}})
	add("a placeholder without a value", "POST", r1, w.As("editor"), map[string]any{},
		EngineRule{Method: "POST", Path: "^/placeholders/preview$", Body: map[string]any{"fields": map[string]any{"p0": map[string]any{"error": "bad offset"}}}})
	add("the engines cannot be read", "POST", r1, w.As("editor"), map[string]any{},
		EngineRule{Method: "GET", Path: "^/engines$", Status: 500, Body: map[string]any{}})
	add("polars is not available", "POST", r1, w.As("editor"), map[string]any{},
		EngineRule{Method: "GET", Path: "^/engines$", Body: []any{map[string]any{"id": "duckdb", "available": true}}})
	add("no engine is available", "POST", r1, w.As("editor"), map[string]any{},
		EngineRule{Method: "GET", Path: "^/engines$", Body: []any{}})
	r2 := "/reports/" + id(rep.r2) + "/render"
	add("a choice from a dataset", "POST", r2, w.As("editor"), map[string]any{"params": map[string]any{"region": "Sud", "agent": []any{"Nord"}}})
	add("a choice not among them", "POST", r2, w.As("editor"), map[string]any{"params": map[string]any{"region": "Est"}})
	add("a fixed choice not among them", "POST", r2, w.As("editor"), map[string]any{"params": map[string]any{"fixed": "3"}})
	add("the choices cannot be read", "POST", r2, w.As("editor"), map[string]any{"params": map[string]any{"region": "Est"}},
		EngineRule{Method: "POST", Path: "^/reports/choices$", Status: 500, Body: map[string]any{"detail": "boom"}})
	add("the choices are cut short", "POST", r2, w.As("editor"), map[string]any{"params": map[string]any{"region": "Est"}},
		EngineRule{Method: "POST", Path: "^/reports/choices$", Body: map[string]any{"choices": []any{}, "truncated": true}})
	add("a catalog SQL: Python renders it", "POST", "/reports/"+id(rep.r3)+"/render", w.As("editor"), map[string]any{})
	add("a datasource without data", "POST", "/reports/"+id(rep.r4)+"/render", w.As("admin"), map[string]any{})
	add("a datasource it cannot read", "POST", "/reports/"+id(rep.r4)+"/render", w.As("editor"), map[string]any{})
	add("a definition that is a list", "POST", "/reports/"+id(rep.listy)+"/render", w.As("writer"), map[string]any{})

	// the editor's preview
	newDef := map[string]any{"v": 1, "parameters": []any{map[string]any{"name": "region", "type": "text"}},
		"datasets": []any{map[string]any{"name": "o", "source": map[string]any{"kind": "datasource", "datasource_id": rep.ds,
			"filters": []any{map[string]any{"column": "region", "op": "=", "value": "{region}"}}}}}}
	add("a preview of a new one", "POST", "/reports/preview", w.As("editor"), map[string]any{"definition": newDef, "project_id": rep.edit,
		"params": map[string]any{"region": "Nord"}})
	add("a preview of a saved one", "POST", "/reports/preview", w.As("editor"), map[string]any{"definition": newDef, "report_id": rep.r1, "format": "xlsx"})
	add("a preview with problems", "POST", "/reports/preview", w.As("editor"), map[string]any{"definition": map[string]any{"v": 1, "page": 1},
		"project_id": rep.edit})
	add("a preview where it reads", "POST", "/reports/preview", w.As("editor"), map[string]any{"definition": newDef, "project_id": rep.sales})
	add("a preview of catalog SQL: Python", "POST", "/reports/preview", w.As("editor"), map[string]any{"definition": map[string]any{"v": 1,
		"datasets": []any{map[string]any{"name": "q", "source": map[string]any{"kind": "sql", "sql": "SELECT 1 FROM rep_orders"}}}}, "project_id": rep.edit})
	add("a preview without a definition", "POST", "/reports/preview", w.As("editor"), map[string]any{"project_id": rep.edit, "format": "x"})

	// the API
	add("the API renders it", "GET", r1+"?region=Sud&n=1&n=2&format=pdf", Bearer("tab_rep_editor_rw_000000000"), nil)
	add("the API with a read token", "GET", r1+"?format=html", Bearer("tab_rep_writer_ro_000000000"), nil)
	add("the API with a bad format", "GET", r1+"?format=svg", w.As("editor"), nil)
	add("the API with a bad value", "GET", r1+"?since=x", w.As("editor"), nil)
	add("the API: the render fails", "GET", r1, w.As("editor"), nil,
		EngineRule{Method: "GET", Path: "^/tasks/exports/t-1$", Body: map[string]any{"status": "failed", "code": 422, "detail": "the data is too large"}})
	add("the API: the engine answers 404", "GET", r1, w.As("editor"), nil,
		EngineRule{Method: "GET", Path: "^/tasks/exports/t-1$", Status: 404, Body: map[string]any{}})
	add("the API: the file is gone", "GET", r1, w.As("editor"), nil,
		EngineRule{Method: "GET", Path: "^/tasks/exports/t-1/file$", Status: 410, Body: map[string]any{"detail": "gone"}})
	add("the API of a catalog SQL: Python", "GET", "/reports/"+id(rep.r3)+"/render", w.As("editor"), nil)

	// the form's parameters
	pr := func(n int64) string { return "/reports/" + id(n) + "/parameters" }
	add("the form of a report", "GET", pr(rep.r1), w.As("editor"), nil)
	add("the form with values given", "GET", pr(rep.r1)+"?region=Sud&since=2026-02-02&format=pdf", w.As("editor"), nil)
	add("the form with a bad value", "GET", pr(rep.r1)+"?since=x", w.As("editor"), nil)
	add("the form with an unknown name", "GET", pr(rep.r1)+"?nope=1", w.As("editor"), nil)
	add("the form of the choices", "GET", pr(rep.r2), w.As("editor"), nil)
	add("the form of the choices, a region chosen", "GET", pr(rep.r2)+"?region=Sud&agent=a&agent=b", w.As("editor"), nil)
	add("the choices fail", "GET", pr(rep.r2), w.As("editor"), nil,
		EngineRule{Method: "POST", Path: "^/reports/choices$", Status: 422, Body: map[string]any{"detail": "no field [region]"}})
	add("the engine has no choices yet", "GET", pr(rep.r2), w.As("editor"), nil,
		EngineRule{Method: "POST", Path: "^/reports/choices$", Status: 404, Body: map[string]any{}})
	add("the form of a catalog SQL: Python", "GET", pr(rep.r3), w.As("editor"), nil)
	add("the form of one it cannot see", "GET", pr(rep.r1), w.As("obs_flag"), nil)
	add("the form's id wrong", "GET", "/reports/x/parameters", w.As("editor"), nil)
	edited := map[string]any{"v": 1, "timezone": "Europe/Rome", "locale": "de", "parameters": []any{
		map[string]any{"name": "region", "type": "text", "choices": map[string]any{"dataset": "o", "value": "[region]"}},
		map[string]any{"name": "other", "type": "text", "choices": map[string]any{"dataset": "missing", "value": "[x]"}},
		map[string]any{"name": "d", "type": "date", "default": "{today}"}},
		"datasets": []any{map[string]any{"name": "o", "source": map[string]any{"kind": "datasource", "datasource_id": rep.ds}},
			map[string]any{"name": "secret", "source": map[string]any{"kind": "datasource", "datasource_id": rep.dsSecret}}},
		"sections": "half done"}
	add("the editor's form", "POST", pr(rep.r1), w.As("editor"), map[string]any{"definition": edited, "params": map[string]any{"region": "Nord"}})
	add("the editor's form with bad parameters", "POST", pr(rep.r1), w.As("editor"), map[string]any{"definition": map[string]any{"parameters": []any{
		map[string]any{"name": "1x", "type": "text"}}}})
	add("the editor's form with an object given", "POST", pr(rep.r1), w.As("editor"), map[string]any{"definition": edited, "params": map[string]any{"region": []any{[]any{}}}})
	add("the editor's form where it reads", "POST", pr(rep.quarterly), w.As("editor"), map[string]any{"definition": edited})
	add("the editor's form of catalog SQL: Python", "POST", pr(rep.r1), w.As("editor"), map[string]any{"definition": map[string]any{"v": 1,
		"datasets": []any{map[string]any{"name": "q", "source": map[string]any{"kind": "sql", "sql": "SELECT 1 FROM rep_orders"}}}}})

	// the fields: without a count (a required parameter without a default), whose request would
	// race the fields' in the Python gateway itself
	fr := "/reports/" + id(rep.r1) + "/fields"
	need := map[string]any{"v": 1, "parameters": []any{map[string]any{"name": "region", "type": "text", "required": true}},
		"datasets": []any{
			map[string]any{"name": "o", "source": map[string]any{"kind": "datasource", "datasource_id": rep.ds,
				"filters": []any{map[string]any{"column": "region", "op": "=", "value": "{region}"}}}},
			map[string]any{"name": "v", "source": map[string]any{"kind": "view", "view_id": rep.view,
				"filters": []any{map[string]any{"column": "region", "op": "=", "value": "{region}"}}}},
			map[string]any{"name": "q", "source": map[string]any{"kind": "sql", "sql": "SELECT * FROM t WHERE r = {region}", "connection_id": rep.connPg}},
			map[string]any{"name": "s", "source": map[string]any{"kind": "datasource", "datasource_id": rep.dsSecret}}}}
	fieldsEngine := EngineRule{Method: "POST", Path: "^/(tasks/preview|db/query)$", Body: map[string]any{"columns": []any{
		map[string]any{"name": "a", "dtype": "Int64"}, map[string]any{"name": "b", "dtype": "Datetime(us)"}, map[string]any{"name": "c"}, 7}}}
	for _, name := range []string{"o", "v", "q", "s", "none"} {
		add("the fields of "+name, "POST", fr, w.As("editor"), map[string]any{"dataset": name, "definition": need}, fieldsEngine)
	}
	add("the fields of the view: the engine refuses", "POST", fr, w.As("editor"), map[string]any{"dataset": "v", "definition": need},
		EngineRule{Method: "POST", Path: "^/tasks/preview$", Status: 400, Body: map[string]any{"detail": "bad column"}})
	add("the fields of the editor's, without EDIT", "POST", fr, w.As("obs_group"), map[string]any{"dataset": "orders", "definition": need})
	add("the fields of a dataset it lacks", "POST", fr, w.As("editor"), map[string]any{"dataset": "zz", "definition": need})
	add("the fields with a bad body", "POST", fr, w.As("editor"), map[string]any{"dataset": 3})

	// AI comments stay in Python (the AI service): no provider configured here
	withComment := map[string]any{"v": 1, "datasets": []any{map[string]any{"name": "o", "source": map[string]any{"kind": "datasource", "datasource_id": rep.ds}}},
		"sections": map[string]any{"body": map[string]any{"items": []any{
			map[string]any{"id": "k1", "type": "kpi", "x": 0, "y": 0, "w": 50, "h": 20, "dataset": "o", "value": "SUM([n])"},
			map[string]any{"id": "ai", "type": "comment", "x": 0, "y": 30, "w": 100, "h": 30, "reads": []any{"k1"}}}}}}
	tiles := EngineRule{Method: "POST", Path: "^/reports/tiles$", Body: map[string]any{"tiles": map[string]any{"k1": map[string]any{"type": "kpi", "text": "3"}}}}
	add("Try it: a comment of the editor", "POST", "/reports/preview/comment", w.As("editor"), map[string]any{"item": "ai", "definition": withComment,
		"project_id": rep.edit}, tiles)
	add("Try it: not a comment", "POST", "/reports/preview/comment", w.As("editor"), map[string]any{"item": "k1", "definition": withComment,
		"project_id": rep.edit}, tiles)
	// a render with a comment is the Python gateway's too, but not compared here: it names the rows its
	// comment reads with a random word (`reading`, a uuid) sent to the engine
	return s
}
