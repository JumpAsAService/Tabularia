package main

import (
	"fmt"
	"net/http"
	"time"
)

// routes/dashboards.py: the dashboards, their versions, their numbers and documents.

var dash struct {
	root, sales, edit, other, conns          int64
	board, quarter, secret, broken, sent, bc int64
	listy, ds, dsSecret, view, connPg        int64
}

const dashContent = `{"v": 1, "locale": "it", "timezone": "Europe/Rome", "refresh_seconds": 300, ` +
	`"parameters": [{"name": "region", "type": "text", "default": "Nord"}], ` +
	`"datasets": [{"name": "orders", "source": {"kind": "datasource", "datasource_id": 1}}], ` +
	`"tiles": [{"id": "k1", "type": "kpi", "grid": {"x": 0, "y": 0, "w": 3, "h": 2}, "dataset": "orders", "value": "SUM([n])", "title": "Totàl"}, ` +
	`{"id": "tx", "type": "text", "grid": {"x": 3, "y": 0, "w": 2, "h": 1}, "value": "1.5e+20", "f": 2.0}]}`

func init() {
	register(Suite{
		Module: "dashboards",
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
			dash.root = project("Dash Root", nil)
			dash.sales = project("Dash Sales", &dash.root)
			dash.edit = project("Dash Editing", &dash.sales)
			dash.other = project("Dash Other", nil)
			dash.conns = project("Dash Connections", nil)
			grant(dash.sales, "", "Sales", "view")
			grant(dash.edit, "editor", "", "edit")
			grant(dash.other, "writer", "", "edit")
			grant(dash.conns, "editor", "", "connect")
			grant(dash.root, "service", "", "manage")

			board := func(name string, project int64, owner string, current, views int, lastViewed *string) int64 {
				return w.Insert(`INSERT INTO dashboards (name, description, project_id, owner_id, current_version, created_at, updated_at,
					views_total, last_viewed_at) VALUES ($1::text, 'about ' || $1::text, $2, $3, $4, '2026-03-01 10:00:00',
					'2026-03-02 11:30:00.5', $5, $6) RETURNING id`, name, project, w.People[owner], current, views, lastViewed)
			}
			version := func(id int64, v int, content, note, by string) {
				var who *int64
				if by != "" {
					who = ptr(w.People[by])
				}
				w.Exec(`INSERT INTO dashboard_versions (dashboard_id, version, content, note, created_at, created_by)
					VALUES ($1, $2, $3, $4, '2026-03-0`+fmt.Sprint(v)+` 09:00:00', $5)`, id, v, content, note, who)
			}
			dash.board = board("Board", dash.edit, "admin", 2, 0, nil)
			version(dash.board, 1, `{"v": 1}`, "created", "admin")
			version(dash.board, 2, dashContent, "edited", "editor")
			dash.quarter = board("Quarter board", dash.sales, "admin", 1, 5, ptr("2026-03-10 08:00:00"))
			version(dash.quarter, 1, `{"v": 1, "tiles": [1, 2, 3], "datasets": [], "parameters": {"a": 1}}`, "created", "")
			dash.secret = board("Secret board", dash.other, "writer", 1, 0, nil)
			version(dash.secret, 1, `{"v": 1}`, "created", "writer")
			dash.broken = board("Broken board", dash.edit, "editor", 1, 0, nil)
			version(dash.broken, 1, `{"v": `, "created", "editor")
			dash.sent = board("Sent board", dash.edit, "admin", 1, 0, nil)
			version(dash.sent, 1, `{"v": 1}`, "created", "admin")
			dash.bc = board("Board (1)", dash.edit, "sso", 1, 0, nil)
			version(dash.bc, 1, `{"v": 1}`, "created", "sso")
			dash.listy = board("Listy board", dash.other, "writer", 1, 0, nil)
			version(dash.listy, 1, `"a text"`, "created", "writer")
			w.Insert(`INSERT INTO flow_groups (name, description, project_id, owner_id, definition, created_at, updated_at)
				VALUES ('Dash weekly mail', '', $1, $2, $3, '2026-01-01', '2026-01-01') RETURNING id`, dash.edit, w.People["admin"],
				fmt.Sprintf(`{"steps": [[{"kind": "pushmail", "id": 1, "pushmail": {"dashboards": [{"dashboardId": %d, "params": {}}]}}]]}`, dash.sent))
			w.Insert(`INSERT INTO flow_groups (name, description, project_id, owner_id, definition, run_schedule, run_scheduled_by, next_run_at,
				created_at, updated_at) VALUES ('Dash scheduled', '', $1, $2, $3, '0 * * * *', $2, '2026-12-01', '2026-01-01', '2026-01-01')
				RETURNING id`, dash.edit, w.People["admin"],
				fmt.Sprintf(`{"steps": [[{"kind": "pushmail", "id": 2, "pushmail": {"dashboards": [{"dashboardId": %d, "params": {"region": "Sud"}}]}}], [null]]}`, dash.board))
			view := func(id int64, person string, daysAgo int) {
				w.Exec(`INSERT INTO dashboard_views (dashboard_id, user_id, day, at) VALUES ($1, $2, (now() at time zone 'UTC')::date - $3::int,
					'2026-02-01')`, id, w.People[person], daysAgo)
			}
			view(dash.board, "editor", 1)
			view(dash.board, "admin", 2)
			view(dash.secret, "writer", 0)
			view(dash.quarter, "editor", 31)
			datasource := func(name string, project int64, columns string) int64 {
				return w.Insert(`INSERT INTO datasources (name, description, project_id, bucket, key, columns, sort_keys, column_descriptions,
					kind, created_at, updated_at) VALUES ($1::text, '', $2, 'data-prep', 'datasets/dash/' || $1::text || '.parquet', $3, '[]', '{}',
					'upload', '2026-01-01', '2026-01-01') RETURNING id`, name, project, columns)
			}
			dash.ds = datasource("dash_orders", dash.sales, `[{"name": "region", "dtype": "String"}, {"name": "n", "dtype": "Float64"}]`)
			dash.dsSecret = datasource("dash_secret", dash.other, `not json`)
			dash.connPg = w.Insert(`INSERT INTO connections (name, description, project_id, db_type, host, port, username, password_encrypted,
				database, db_schema, extra, created_at, updated_at, private_key_encrypted, host_key_type, host_key_fingerprint)
				VALUES ('dash pg', '', $1, 'postgresql', 'db.example', 5432, 'u', '', 'shop', '', '{}', '2026-01-01', '2026-01-01', '', '', '')
				RETURNING id`, dash.conns)
			w.Token("writer", "tab_dash_writer_rw_00000000", "write", time.Hour)
			w.Token("writer", "tab_dash_writer_ro_00000000", "read", time.Hour)
		},
		Steps: func(w *World) []Step {
			var s []Step
			add := func(name, method, path string, auth func(*http.Request), body any) {
				s = append(s, Step{Name: name, Method: method, Path: path, Auth: auth, JSON: body})
			}
			raw := func(name, method, path string, auth func(*http.Request), body, ct string) {
				s = append(s, Step{Name: name, Method: method, Path: path, Auth: auth, Raw: []byte(body), ContentType: ct})
			}
			id := func(n int64) string { return fmt.Sprint(n) }
			people := []string{"admin", "editor", "writer", "obs_flag", "obs_group", "admin_group", "service", "sso"}

			for _, p := range people {
				add("list as "+p, "GET", "/dashboards", w.As(p), nil)
				add("folders as "+p, "GET", "/dashboards/folders", w.As(p), nil)
				for _, q := range []string{"", "?sort=name", "?sort=updated&limit=2", "?q=dash%20sales", "?q=BOARD&offset=1", "?q=about%20sent"} {
					add(p+" searches "+q, "GET", "/dashboards/search"+q, w.As(p), nil)
				}
				for _, b := range []int64{dash.board, dash.quarter, dash.secret, dash.broken} {
					add(p+" opens "+id(b), "GET", "/dashboards/"+id(b), w.As(p), nil)
				}
			}
			add("list signed out", "GET", "/dashboards", nil, nil)
			add("folders signed out", "GET", "/dashboards/folders", nil, nil)
			add("search with bad values", "GET", "/dashboards/search?sort=a&limit=500&offset=x", w.As("admin"), nil)
			for _, f := range []int64{dash.sales, dash.edit, dash.other} {
				add("editor lists folder "+id(f), "GET", "/projects/"+id(f)+"/dashboards", w.As("editor"), nil)
			}
			add("an unknown folder", "GET", "/projects/999999/dashboards", w.As("editor"), nil)
			add("a folder that is no number", "GET", "/projects/x/dashboards", w.As("editor"), nil)
			add("a text as its definition", "GET", "/dashboards/"+id(dash.listy), w.As("admin"), nil)
			add("unknown dashboard", "GET", "/dashboards/999999", w.As("admin"), nil)
			add("dashboard id «abc»", "GET", "/dashboards/abc", w.As("admin"), nil)
			add("a read token reads it", "GET", "/dashboards/"+id(dash.secret), Bearer("tab_dash_writer_ro_00000000"), nil)

			// versions
			for _, p := range []string{"admin", "editor", "writer"} {
				add(p+" lists the versions", "GET", "/dashboards/"+id(dash.board)+"/versions", w.As(p), nil)
				add(p+" reads version 2", "GET", "/dashboards/"+id(dash.board)+"/versions/2", w.As(p), nil)
			}
			add("a version that is not", "GET", "/dashboards/"+id(dash.board)+"/versions/7", w.As("editor"), nil)
			add("a broken version", "GET", "/dashboards/"+id(dash.broken)+"/versions/1", w.As("editor"), nil)
			add("both ids wrong", "GET", "/dashboards/x/versions/y", w.As("editor"), nil)

			// openings: in the audit every time
			add("editor opens it", "POST", "/dashboards/"+id(dash.quarter)+"/views", w.As("editor"), nil)
			add("editor opens it again", "POST", "/dashboards/"+id(dash.quarter)+"/views", w.As("editor"), nil)
			add("obs_group opens it", "POST", "/dashboards/"+id(dash.quarter)+"/views", w.As("obs_group"), nil)
			add("a token opens it", "POST", "/dashboards/"+id(dash.secret)+"/views", Bearer("tab_dash_writer_rw_00000000"), nil)
			add("someone who cannot see it", "POST", "/dashboards/"+id(dash.secret)+"/views", w.As("editor"), nil)

			// copies and deletions
			add("editor copies Board", "POST", "/dashboards/"+id(dash.board)+"/duplicate", w.As("editor"), nil)
			add("editor copies the broken one", "POST", "/dashboards/"+id(dash.broken)+"/duplicate", w.As("editor"), nil)
			add("editor cannot copy where it reads", "POST", "/dashboards/"+id(dash.quarter)+"/duplicate", w.As("editor"), nil)
			add("a text cannot be copied", "POST", "/dashboards/"+id(dash.listy)+"/duplicate", w.As("writer"), nil)
			add("one sent by a group", "DELETE", "/dashboards/"+id(dash.sent), w.As("editor"), nil)
			add("editor deletes the broken one", "DELETE", "/dashboards/"+id(dash.broken), w.As("editor"), nil)
			add("again", "DELETE", "/dashboards/"+id(dash.broken), w.As("editor"), nil)
			add("a read token cannot", "DELETE", "/dashboards/"+id(dash.secret), Bearer("tab_dash_writer_ro_00000000"), nil)

			// creating, changing, restoring, checking
			edit := "/projects/" + id(dash.edit) + "/dashboards"
			tile := func(id, typ string, x, y, wd, h int, extra map[string]any) map[string]any {
				t := map[string]any{"id": id, "type": typ, "grid": map[string]any{"x": x, "y": y, "w": wd, "h": h}}
				for k, v := range extra {
					t[k] = v
				}
				return t
			}
			def := func(tiles ...any) map[string]any {
				return map[string]any{"v": 1, "refresh_seconds": 120,
					"parameters": []any{map[string]any{"name": "region", "type": "text", "default": "Nord"}},
					"datasets":   []any{map[string]any{"name": "orders", "source": map[string]any{"kind": "datasource", "datasource_id": dash.ds}}},
					"tiles":      tiles}
			}
			kpi := tile("k1", "kpi", 0, 0, 3, 2, map[string]any{"dataset": "orders", "value": "SUM([n])", "title": "Total"})
			add("editor creates an empty one", "POST", edit, w.As("editor"), map[string]any{"name": "Dash new"})
			add("the same name", "POST", edit, w.As("editor"), map[string]any{"name": "Dash new "})
			add("a blank name", "POST", edit, w.As("editor"), map[string]any{"name": ""})
			raw("a broken JSON", "POST", edit, nil, `[`, "application/json")
			add("a dashboard with a KPI", "POST", edit, w.As("editor"), map[string]any{"name": "Dash kpi", "content": def(kpi)})
			add("tiles on each other", "POST", edit, w.As("editor"), map[string]any{"name": "Dash overlap", "content": def(kpi,
				tile("k2", "kpi", 1, 1, 2, 2, map[string]any{"dataset": "orders", "value": "1"}))})
			add("a tile on a dataset it lacks", "POST", edit, w.As("editor"), map[string]any{"name": "Dash nods", "content": def(
				tile("c", "chart", 0, 0, 4, 4, map[string]any{"dataset": "x", "series": []any{map[string]any{"value": "1"}}}))})
			add("a dataset it cannot read", "POST", edit, w.As("editor"), map[string]any{"name": "Dash secret", "content": map[string]any{"v": 1,
				"datasets": []any{map[string]any{"name": "s", "source": map[string]any{"kind": "datasource", "datasource_id": dash.dsSecret}}}}})
			add("SQL on the catalog: Python", "POST", edit, w.As("editor"), map[string]any{"name": "Dash cat", "content": map[string]any{"v": 1,
				"datasets": []any{map[string]any{"name": "s", "source": map[string]any{"kind": "sql", "sql": "SELECT 1 FROM dash_orders"}}}}})
			add("SQL on a connection", "POST", edit, w.As("editor"), map[string]any{"name": "Dash pg", "content": map[string]any{"v": 1,
				"datasets": []any{map[string]any{"name": "s", "source": map[string]any{"kind": "sql", "sql": "SELECT {x}", "connection_id": dash.connPg}}}}})
			add("a refresh too frequent", "POST", edit, w.As("editor"), map[string]any{"name": "Dash fast", "content": map[string]any{"v": 1, "refresh_seconds": 5}})
			add("a picture", "POST", edit, w.As("admin"), map[string]any{"name": "Dash picture", "content": map[string]any{"v": 1,
				"tiles": []any{tile("img", "image", 0, 0, 1, 1, map[string]any{"asset": "data:image/png;base64,iVBO\nRw0KGgoA"})}}})
			s = append(s, Step{Name: "the engine finds problems", Method: "POST", Path: edit, Auth: w.As("editor"),
				JSON: map[string]any{"name": "Dash engine", "content": def(kpi)},
				Engine: []EngineRule{{Method: "POST", Path: "^/reports/check$", Body: map[string]any{"problems": []any{
					map[string]any{"item": "k1", "field": "value", "message": "the report has no field [n] in the item"}}}}}})
			b := "/dashboards/" + id(dash.board)
			add("rename", "PATCH", b, w.As("editor"), map[string]any{"name": "Dash board"})
			add("a stale version", "PATCH", b, w.As("editor"), map[string]any{"content": def(kpi), "base_version": 1})
			add("a change: the scheduled group stops", "PATCH", b, w.As("editor"), map[string]any{"content": def(kpi), "base_version": 2,
				"description": "x"})
			add("a new default the group gives", "PATCH", b, w.As("admin"), map[string]any{"content": map[string]any{"v": 1,
				"parameters": []any{map[string]any{"name": "region", "type": "text", "default": "Est"}}}})
			add("a move", "PATCH", b, w.As("admin"), map[string]any{"project_id": dash.other})
			add("back", "PATCH", b, w.As("admin"), map[string]any{"project_id": dash.edit})
			add("a bad body", "PATCH", b, w.As("editor"), map[string]any{"name": 1})
			add("restore version 1", "POST", b+"/versions/1/restore", w.As("editor"), nil)
			add("restore a version that is not", "POST", b+"/versions/99/restore", w.As("editor"), nil)
			add("restore a text", "POST", "/dashboards/"+id(dash.listy)+"/versions/1/restore", w.As("admin"), nil)
			add("check of a saved one", "POST", "/dashboards/check", w.As("editor"), map[string]any{"definition": def(kpi, map[string]any{"id": "bad"}),
				"dashboard_id": dash.board})
			add("check of a new one", "POST", "/dashboards/check", w.As("editor"), map[string]any{"definition": []any{}, "project_id": dash.edit})
			add("check of a new one, a dict", "POST", "/dashboards/check", w.As("editor"), map[string]any{"definition": map[string]any{"v": 1, "x": 2},
				"project_id": dash.edit})
			add("check without a folder", "POST", "/dashboards/check", w.As("editor"), map[string]any{"definition": map[string]any{}})
			add("check where it reads", "POST", "/dashboards/check", w.As("editor"), map[string]any{"definition": map[string]any{}, "dashboard_id": dash.quarter})
			add("versions after", "GET", b+"/versions", w.As("admin"), nil)
			return s
		},
	})
}
