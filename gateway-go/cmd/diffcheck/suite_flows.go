package main

import (
	"fmt"
	"net/http"
)

// routes/flows.py: the saved flows.
func init() {
	register(Suite{
		Module: "flows",
		Seed:   func(w *World) { w.seedFlows() },
		Steps: func(w *World) []Step {
			var s []Step
			add := func(name, method, path string, auth func(*http.Request), body any) {
				s = append(s, Step{Name: name, Method: method, Path: path, Auth: auth, JSON: body})
			}
			people := []string{"admin", "editor", "writer", "obs_flag", "admin_group", "service"}
			f := func(format string, args ...any) string { return fmt.Sprintf(format, args...) }

			// ── lists ──
			for _, p := range people {
				add("all flows · "+p, "GET", "/flows", w.As(p), nil)
			}
			add("all flows · nobody", "GET", "/flows", nil, nil)
			for _, p := range []string{"admin", "editor", "writer", "obs_flag"} {
				add("folder's flows · "+p, "GET", f("/projects/%d/flows", FL.Sales), w.As(p), nil)
			}
			add("folder's flows · child, inherited", "GET", f("/projects/%d/flows", FL.Child), w.As("editor"), nil)
			add("folder's flows · writer's folder", "GET", f("/projects/%d/flows", FL.Other), w.As("writer"), nil)
			add("folder's flows · no such folder", "GET", "/projects/999999/flows", w.As("admin"), nil)
			add("folder's flows · not a number", "GET", "/projects/abc/flows", w.As("admin"), nil)
			add("folder's flows · not a number, nobody", "GET", "/projects/abc/flows", nil, nil)
			add("folder's flows · 1.0", "GET", f("/projects/%d.0/flows", FL.Sales), w.As("admin"), nil)
			add("folder's flows · nobody", "GET", f("/projects/%d/flows", FL.Sales), nil, nil)

			// ── one flow ──
			for _, p := range people {
				add("Alpha · "+p, "GET", f("/flows/%d", FL.Alpha), w.As(p), nil)
				add("Boxed (a registry) · "+p, "GET", f("/flows/%d", FL.Boxed), w.As(p), nil)
				add("Scripted (an environment, a script) · "+p, "GET", f("/flows/%d", FL.Scripted), w.As(p), nil)
			}
			add("Secret · editor", "GET", f("/flows/%d", FL.Secret), w.As("editor"), nil)
			add("no such flow", "GET", "/flows/999999", w.As("admin"), nil)
			add("no such flow · nobody", "GET", "/flows/999999", nil, nil)
			add("not a number", "GET", "/flows/abc", w.As("admin"), nil)
			add("1_0 is 10", "GET", "/flows/1_0", w.As("admin"), nil)
			add("past an int32", "GET", "/flows/3000000000", w.As("admin"), nil)
			add("past an int64", "GET", "/flows/99999999999999999999", w.As("admin"), nil)
			add("a negative id", "GET", "/flows/-1", w.As("admin"), nil)
			add("a read token reads", "GET", f("/flows/%d", FL.Alpha), Bearer("tab_writer_read_00000000000"), nil)

			// ── versions, stats ──
			for _, p := range []string{"admin", "editor", "writer"} {
				add("versions of Alpha · "+p, "GET", f("/flows/%d/versions", FL.Alpha), w.As(p), nil)
				add("stats of Alpha · "+p, "GET", f("/flows/%d/stats", FL.Alpha), w.As(p), nil)
			}
			add("versions of Boxed · editor (locked)", "GET", f("/flows/%d/versions", FL.Boxed), w.As("editor"), nil)
			add("stats of Boxed · editor (not locked)", "GET", f("/flows/%d/stats", FL.Boxed), w.As("editor"), nil)
			add("versions of Beta (none)", "GET", f("/flows/%d/versions", FL.Beta), w.As("admin"), nil)
			add("stats of Beta (no runs)", "GET", f("/flows/%d/stats", FL.Beta), w.As("admin"), nil)
			add("versions · no such flow", "GET", "/flows/999999/versions", w.As("admin"), nil)
			add("stats · not a number", "GET", "/flows/x/stats", w.As("admin"), nil)

			// ── presence: leaving ──
			add("leave · editor", "DELETE", f("/flows/%d/presence/tab-1", FL.Alpha), w.As("editor"), nil)
			add("leave · writer, no view", "DELETE", f("/flows/%d/presence/tab-2", FL.Alpha), w.As("writer"), nil)
			add("leave · no such flow", "DELETE", "/flows/999999/presence/tab-1", w.As("editor"), nil)
			add("leave · nobody", "DELETE", f("/flows/%d/presence/tab-1", FL.Alpha), nil, nil)

			// ── duplicate ──
			add("duplicate Alpha · editor", "POST", f("/flows/%d/duplicate", FL.Alpha), w.As("editor"), nil)
			add("duplicate Alpha again · editor", "POST", f("/flows/%d/duplicate", FL.Alpha), w.As("editor"), nil)
			add("duplicate Beta (1) · admin", "POST", f("/flows/%d/duplicate", FL.Beta), w.As("admin"), nil)
			add("duplicate Gamma · writer (view only)", "POST", f("/flows/%d/duplicate", FL.Gamma), w.As("writer"), nil)
			add("duplicate Keys · editor (a hidden datasource)", "POST", f("/flows/%d/duplicate", FL.Keys), w.As("editor"), nil)
			add("duplicate Keys · admin", "POST", f("/flows/%d/duplicate", FL.Keys), w.As("admin"), nil)
			add("duplicate Broken · editor (not JSON)", "POST", f("/flows/%d/duplicate", FL.Broken), w.As("editor"), nil)
			add("duplicate Boxed · editor (locked)", "POST", f("/flows/%d/duplicate", FL.Boxed), w.As("editor"), nil)
			add("duplicate Boxed · admin", "POST", f("/flows/%d/duplicate", FL.Boxed), w.As("admin"), nil)
			add("duplicate Spark · admin (unknown engine)", "POST", f("/flows/%d/duplicate", FL.Spark), w.As("admin"), nil)
			add("duplicate · no such flow", "POST", "/flows/999999/duplicate", w.As("editor"), nil)
			add("duplicate · nobody", "POST", f("/flows/%d/duplicate", FL.Alpha), nil, nil)
			s = append(s,
				Step{Name: "duplicate · no CSRF header", Method: "POST", Path: f("/flows/%d/duplicate", FL.Alpha),
					Auth: Cookie(w.JWT("editor", "s-editor"))},
				Step{Name: "duplicate · a read token", Method: "POST", Path: f("/flows/%d/duplicate", FL.Alpha),
					Auth: Bearer("tab_writer_read_00000000000")},
				Step{Name: "duplicate · a write token without the grant", Method: "POST", Path: f("/flows/%d/duplicate", FL.Alpha),
					Auth: Bearer("tab_writer_write_0000000000")},
			)
			// the administrator disables DuckDB
			s = append(s, Step{Name: "duplicate Duck · DuckDB disabled", Method: "POST", Path: f("/flows/%d/duplicate", FL.Duck),
				Auth: w.As("admin"), Before: func(w *World) {
					w.Exec(`INSERT INTO disabled_engines (engine_id, disabled_at) VALUES ('duckdb', '2026-01-01')
						ON CONFLICT DO NOTHING`)
				}})
			add("duplicate Beta · DuckDB disabled", "POST", f("/flows/%d/duplicate", FL.Beta), w.As("admin"), nil)
			s = append(s, Step{Name: "DuckDB enabled again", Method: "GET", Path: "/flows/999999", Auth: w.As("admin"),
				Before: func(w *World) { w.Exec(`DELETE FROM disabled_engines`) }})

			// ── promote ──
			add("promote Alpha v1 · editor", "POST", f("/flows/%d/versions/1/promote", FL.Alpha), w.As("editor"), nil)
			add("promote Alpha v1 again · editor (same definition: no new version)", "POST",
				f("/flows/%d/versions/3/promote", FL.Alpha), w.As("editor"), nil)
			add("promote Alpha v99", "POST", f("/flows/%d/versions/99/promote", FL.Alpha), w.As("editor"), nil)
			add("promote Alpha vx", "POST", f("/flows/%d/versions/x/promote", FL.Alpha), w.As("editor"), nil)
			add("promote · both not numbers", "POST", "/flows/y/versions/x/promote", w.As("editor"), nil)
			add("promote Alpha · writer (no edit)", "POST", f("/flows/%d/versions/1/promote", FL.Alpha), w.As("writer"), nil)
			add("promote Sched v1 · editor (widens the admin's schedule: off)", "POST",
				f("/flows/%d/versions/1/promote", FL.Sched), w.As("editor"), nil)
			add("promote Sched v2 · admin (its author)", "POST", f("/flows/%d/versions/2/promote", FL.Sched), w.As("admin"), nil)
			add("promote · no such flow", "POST", "/flows/999999/versions/1/promote", w.As("editor"), nil)
			add("promote Keys v1 · editor (ok: v1 reads nothing hidden)", "POST", f("/flows/%d/versions/1/promote", FL.Keys),
				w.As("editor"), nil)

			// ── delete ──
			add("delete Doomed · writer", "DELETE", f("/flows/%d", FL.Doomed), w.As("writer"), nil)
			add("delete Doomed · editor", "DELETE", f("/flows/%d", FL.Doomed), w.As("editor"), nil)
			add("delete Doomed again", "DELETE", f("/flows/%d", FL.Doomed), w.As("editor"), nil)
			add("delete · not a number", "DELETE", "/flows/abc", w.As("editor"), nil)
			add("delete · nobody", "DELETE", f("/flows/%d", FL.Alpha), nil, nil)
			add("all flows at the end · admin", "GET", "/flows", w.As("admin"), nil)
			return append(s, flowBodySteps(w)...)
		},
	})
}

// the routes with a body or a query
func flowBodySteps(w *World) []Step {
	var s []Step
	f := fmt.Sprintf
	add := func(name, method, path string, auth func(*http.Request), body any) {
		s = append(s, Step{Name: name, Method: method, Path: path, Auth: auth, JSON: body})
	}
	raw := func(name, method, path string, auth func(*http.Request), body, ct string) {
		s = append(s, Step{Name: name, Method: method, Path: path, Auth: auth, Raw: []byte(body), ContentType: ct})
	}
	out := `{"nodes": [{"id": "o1", "type": "output"}]}`

	// ── search ──
	for _, q := range []string{"", "?q=Alpha", "?q=ABOUT", "?q=%25", "?q=fl_", "?limit=1&offset=1", "?limit=2", "?q=&limit=200",
		"?limit=0", "?limit=abc&offset=-1", "?limit=201", "?offset=x&q=a&limit=1.0"} {
		add("search "+q+" · editor", "GET", "/flows/search"+q, w.As("editor"), nil)
	}
	for _, p := range []string{"admin", "writer", "obs_flag"} {
		add("search · "+p, "GET", "/flows/search", w.As(p), nil)
	}
	add("search · nobody", "GET", "/flows/search", nil, nil)
	add("search · nobody, bad limit", "GET", "/flows/search?limit=0", nil, nil)

	// ── create ──
	create := func(name string, project any, auth func(*http.Request), body any) {
		add("create · "+name, "POST", f("/projects/%v/flows", project), auth, body)
	}
	create("editor", FL.Sales, w.As("editor"), map[string]any{"name": "fl New", "definition": out, "description": "d"})
	create("defaults", FL.Sales, w.As("editor"), map[string]any{"name": "fl Defaults"})
	create("engine normalized", FL.Sales, w.As("editor"), map[string]any{"name": "fl Engine", "engine": " DuckDB ", "production_engine": "chdb"})
	create("empty production engine", FL.Sales, w.As("editor"), map[string]any{"name": "fl Prod", "production_engine": "  "})
	create("unknown engine", FL.Sales, w.As("editor"), map[string]any{"name": "fl X", "engine": "spark"})
	create("unknown production engine", FL.Sales, w.As("editor"), map[string]any{"name": "fl X", "production_engine": "Spark"})
	create("a tab in the name", FL.Sales, w.As("editor"), map[string]any{"name": "fl\tTab"})
	create("definition not JSON", FL.Sales, w.As("editor"), map[string]any{"name": "fl X", "definition": "{"})
	create("a hidden datasource", FL.Sales, w.As("editor"), map[string]any{"name": "fl X",
		"definition": f(`{"nodes": [{"data": {"datasourceId": "%d"}}]}`, FL.HiddenDS)})
	create("a hidden key", FL.Sales, w.As("editor"), map[string]any{"name": "fl X", "definition": `{"k": "datasets/hidden.parquet"}`})
	create("writer, VIEW only", FL.Other, w.As("writer"), map[string]any{"name": "fl X"})
	create("no such folder", 999999, w.As("editor"), map[string]any{"name": "fl X"})
	create("no name", FL.Sales, w.As("editor"), map[string]any{"definition": 5})
	create("not a number, no name", "abc", w.As("editor"), map[string]any{})
	create("nobody", FL.Sales, nil, map[string]any{"name": "fl X"})
	create("a read token", FL.Sales, Bearer("tab_writer_read_00000000000"), map[string]any{"name": "fl X"})
	raw("create · broken JSON, nobody", "POST", f("/projects/%d/flows", FL.Sales), nil, `{"name": `, "application/json")
	raw("create · broken JSON", "POST", f("/projects/%d/flows", FL.Sales), w.As("editor"), `{"name": `, "application/json")
	raw("create · text", "POST", f("/projects/%d/flows", FL.Sales), w.As("editor"), `{"name": "fl T"}`, "text/plain")
	raw("create · no body", "POST", f("/projects/%d/flows", FL.Sales), w.As("editor"), ``, "application/json")

	// ── update ──
	patch := func(name string, flow any, auth func(*http.Request), body any) {
		add("patch · "+name, "PATCH", f("/flows/%v", flow), auth, body)
	}
	patch("rename", FL.Alpha, w.As("editor"), map[string]any{"name": "  fl Alpha bis  ", "description": "new"})
	patch("empty name", FL.Alpha, w.As("editor"), map[string]any{"name": "   "})
	patch("a newline", FL.Alpha, w.As("editor"), map[string]any{"name": "fl\nA"})
	patch("engine", FL.Alpha, w.As("editor"), map[string]any{"engine": "CHDB", "production_engine": ""})
	patch("bad engine", FL.Alpha, w.As("editor"), map[string]any{"engine": "spark", "name": "fl never"})
	patch("move to Dest", FL.Gamma, w.As("admin"), map[string]any{"project_id": FL.Dest})
	patch("move to Hidden", FL.Alpha, w.As("editor"), map[string]any{"project_id": FL.Hidden})
	patch("move nowhere", FL.Alpha, w.As("editor"), map[string]any{"project_id": 999999})
	patch("same folder", FL.Alpha, w.As("editor"), map[string]any{"project_id": FL.Sales, "definition": out})
	patch("someone else's schedule widened", FL.Sched2, w.As("editor"), map[string]any{
		"definition": `{"nodes": [{"id": "o1", "type": "output", "data": {"connectionId": 3}}]}`})
	patch("someone else's schedule moved", FL.Sched2, w.As("editor"), map[string]any{"project_id": FL.Dest})
	patch("a hidden datasource", FL.Alpha, w.As("editor"), map[string]any{"definition": f(`{"datasourceId": %d}`, FL.HiddenDS)})
	patch("empty definition", FL.Duck, w.As("admin"), map[string]any{"definition": ""})
	patch("writer", FL.Alpha, w.As("writer"), map[string]any{"name": "x"})
	patch("locked", FL.Boxed, w.As("editor"), map[string]any{"name": "x"})
	patch("bad types", FL.Alpha, w.As("editor"), map[string]any{"name": 5, "project_id": "x"})
	patch("no such flow", 999999, w.As("editor"), map[string]any{})
	patch("nobody", FL.Alpha, nil, map[string]any{})

	// ── presence ──
	beat := func(name string, flow any, auth func(*http.Request), instance any) {
		add("beat · "+name, "POST", f("/flows/%v/presence", flow), auth, map[string]any{"instance": instance})
	}
	beat("editor", FL.Alpha, w.As("editor"), "tab-A")
	beat("editor again", FL.Alpha, w.As("editor"), "tab-A")
	beat("admin", FL.Alpha, w.As("admin"), "tab_B")
	beat("admin on Beta", FL.Beta, w.As("admin"), "tab-C")
	beat("an observer on Beta", FL.Beta, w.As("obs_group"), "tab-D")
	beat("too short", FL.Alpha, w.As("editor"), "ab")
	beat("a bad character", FL.Alpha, w.As("editor"), "tab A")
	beat("a number", FL.Alpha, w.As("editor"), 1234)
	beat("writer", FL.Alpha, w.As("writer"), "tab-W")
	beat("nobody", FL.Alpha, nil, "tab-N")
	add("leave · admin, tab_B", "DELETE", f("/flows/%d/presence/tab_B", FL.Alpha), w.As("admin"), nil)

	// ── schedule ──
	sched := func(name string, flow any, auth func(*http.Request), body any) {
		add("schedule · "+name, "PUT", f("/flows/%v/schedule", flow), auth, body)
	}
	sched("set", FL.Alpha, w.As("editor"), map[string]any{"cron": "0 3 * * *"})
	sched("stripped, production engine", FL.Alpha, w.As("editor"), map[string]any{"cron": "  */15 * * * *  ", "production_engine": "duckdb"})
	sched("seconds and a year", FL.Alpha, w.As("editor"), map[string]any{"cron": "0 0 * * * 30 2099"})
	sched("an alias", FL.Alpha, w.As("editor"), map[string]any{"cron": "@weekly"})
	sched("invalid", FL.Alpha, w.As("editor"), map[string]any{"cron": "61 * * * *"})
	sched("never fires", FL.Alpha, w.As("editor"), map[string]any{"cron": "0 0 31 2 *"})
	sched("nothing to run", FL.Beta, w.As("admin"), map[string]any{"cron": "0 3 * * *"})
	sched("not JSON", FL.Broken, w.As("editor"), map[string]any{"cron": "0 3 * * *"})
	sched("notice", FL.Alpha, w.As("editor"), map[string]any{"notify_emails": "a@x.it; b@Y.IT,, ", "notify_connection_id": FL.Smtp})
	sched("notice outside the domains", FL.Alpha, w.As("editor"), map[string]any{"notify_emails": "c@z.it"})
	sched("notice, bad address", FL.Alpha, w.As("editor"), map[string]any{"notify_emails": "@x.it"})
	sched("notice, open connection", FL.Alpha, w.As("editor"), map[string]any{"notify_emails": "c@z.it", "notify_connection_id": FL.SmtpOpen})
	sched("notice, malformed domains", FL.Alpha, w.As("editor"), map[string]any{"notify_connection_id": FL.SmtpBroken})
	sched("notice, not SMTP", FL.Alpha, w.As("editor"), map[string]any{"notify_connection_id": FL.PG})
	sched("notice, hidden connection", FL.Alpha, w.As("editor"), map[string]any{"notify_connection_id": FL.SmtpHidden})
	sched("notice, no such connection", FL.Alpha, w.As("editor"), map[string]any{"notify_connection_id": 999999})
	sched("notice off", FL.Alpha, w.As("editor"), map[string]any{"notify_connection_id": -1, "notify_emails": ""})
	sched("off", FL.Alpha, w.As("editor"), map[string]any{"cron": "", "production_engine": ""})
	sched("bad production engine", FL.Alpha, w.As("editor"), map[string]any{"production_engine": "x"})
	sched("writer, VIEW", FL.Gamma, w.As("writer"), map[string]any{"cron": "0 3 * * *"})
	sched("locked", FL.Boxed, w.As("editor"), map[string]any{"cron": "0 3 * * *"})
	sched("nobody", FL.Alpha, nil, map[string]any{})
	sched("bad types", FL.Alpha, w.As("editor"), map[string]any{"cron": []any{}, "notify_connection_id": "x"})

	// ── run now ──
	add("run now · editor", "POST", f("/flows/%d/run-now", FL.Alpha), w.As("editor"), nil)
	add("run now · again", "POST", f("/flows/%d/run-now", FL.Alpha), w.As("editor"), nil)
	s = append(s, Step{Name: "run now · again, in Italian", Method: "POST", Path: f("/flows/%d/run-now", FL.Alpha), Auth: w.As("admin"),
		Header: map[string]string{"Accept-Language": "it-IT,it;q=0.9"}})
	add("run now · production", "POST", f("/flows/%d/run-now?mode=production", FL.Duck), w.As("editor"), nil)
	add("run now · bad mode", "POST", f("/flows/%d/run-now?mode=prod", FL.Duck), w.As("editor"), nil)
	add("run now · writer, VIEW", "POST", f("/flows/%d/run-now", FL.Gamma), w.As("writer"), nil)
	add("run now · locked", "POST", f("/flows/%d/run-now", FL.Boxed), w.As("editor"), nil)
	add("run now · no such flow", "POST", "/flows/999999/run-now", w.As("editor"), nil)
	add("run now · nobody", "POST", f("/flows/%d/run-now", FL.Alpha), nil, nil)
	return s
}

func init() {
	maskColumns("flow_presence", "since", "seen")
	// the cron expressions read in another zone
	register(Suite{
		Module: "flows",
		Seed:   func(w *World) { w.seedFlows() },
		Phases: []Phase{{Name: "panel: New York", Panel: map[string]string{"app.timezone": `"America/New_York"`}}},
		Steps: func(w *World) []Step {
			var s []Step
			for _, c := range []string{"0 3 * * *", "30 2 * * *", "0 * * * *", "0 0 L * *", "0 9 * * mon-fri"} {
				s = append(s, Step{Name: "schedule in New York · " + c, Method: "PUT", Path: fmt.Sprintf("/flows/%d/schedule", FL.Sched2),
					Auth: w.As("admin"), JSON: map[string]any{"cron": c}})
			}
			return s
		},
	})
}
