package main

import (
	"fmt"
	"net/http"
	"net/url"
)

// routes/search.py: one search by name over every kind, each with its own permission.
func init() {
	register(Suite{
		Module: "search",
		Seed: func(w *World) {
			stamp := `'2026-01-01', '2026-01-01'`
			folder := func(name string, parent *int64) int64 {
				return w.Insert(`INSERT INTO projects (name, description, parent_id, created_at) VALUES ($1, '', $2, '2026-01-01') RETURNING id`, name, parent)
			}
			grant := func(f int64, person, capability string) {
				w.Exec(`INSERT INTO permissions (project_id, user_id, capability) VALUES ($1, $2, $3)`, f, w.People[person], capability)
			}
			top := folder("se-Alpha top", nil)
			mid := folder("se-alpha mid", &top)
			low := folder("Zeta se-alpha low", &mid)
			other := folder("se-Other", nil)
			secret := folder("se-alpha secret", nil)
			grant(mid, "writer", "view")
			grant(other, "writer", "connect")
			grant(secret, "sso", "view")
			grant(top, "service", "view")
			flow := func(name, def string, f int64) {
				w.Exec(`INSERT INTO flows (name, description, project_id, definition, engine, created_at, updated_at)
					VALUES ($1, '', $2, $3, 'duckdb', `+stamp+`)`, name, f, def)
			}
			flow("se-alpha flow", `{"nodes": []}`, mid)
			flow("SE-ALPHA flow two", `{}`, low)
			flow("se_100% flow", `{}`, mid)
			flow("se-alpha scripted", `{"nodes": [{"type": "script", "data": {"environmentId": 1}}]}`, secret)
			// flow_access.blocked: a registry the reader may not use, an environment they may not use
			registry := w.Insert(`INSERT INTO container_registries (name, host, protocol, tls, username, repositories, enabled, created_at,
				updated_at) VALUES ('se-registry', 'ghcr.io', 'https', 'verify', '', '*', true, '2026-01-01', '2026-01-01') RETURNING id`)
			env := w.Insert(`INSERT INTO script_environments (name, description, language, enabled, cpu_request, cpu_limit, memory_request,
				memory_limit, max_timeout_seconds, created_at, updated_at) VALUES ('se-env', '', 'python', true, '1', '1', '1Gi', '1Gi', 600,
				'2026-01-01', '2026-01-01') RETURNING id`)
			flow("se-boxed flow", fmt.Sprintf(`{"nodes": [{"type": "container", "data": {"registryId": %d, "repository": "a/b"}}]}`, registry), mid)
			flow("se-boxed scripted", fmt.Sprintf(`{"nodes": [{"type": "script", "data": {"environmentId": %d}}]}`, env), mid)
			w.Exec(`INSERT INTO flow_groups (name, description, project_id, definition, created_at, updated_at) VALUES ('se-alpha group', '', $1, '{}', `+stamp+`)`, mid)
			ds := w.Insert(`INSERT INTO datasources (name, description, project_id, bucket, key, columns, sort_keys, column_descriptions, kind,
				created_at, updated_at, rows) VALUES ('se-alpha ds', '', $1, 'b', 'datasets/se.parquet', '[]', '[]', '{}', 'file', `+stamp+`, 42) RETURNING id`, low)
			w.Exec(`INSERT INTO datasources (name, description, project_id, bucket, key, columns, sort_keys, column_descriptions, kind,
				created_at, updated_at) VALUES ('a se-alpha ds', '', $1, 'b', 'datasets/se2.parquet', '[]', '[]', '{}', 'file', `+stamp+`)`, mid)
			w.Exec(`INSERT INTO saved_views (name, description, project_id, datasource_id, spec, created_at, updated_at) VALUES ('se-alpha view', '', $1, $2, '{}', `+stamp+`)`, mid, ds)
			w.Exec(`INSERT INTO sql_scripts (name, description, project_id, current_version, created_at, updated_at) VALUES ('se-alpha sql', '', $1, 1, `+stamp+`)`, mid)
			w.Exec(`INSERT INTO reports (name, description, project_id, current_version, created_at, updated_at) VALUES ('se-alpha report', '', $1, 1, `+stamp+`)`, low)
			w.Exec(`INSERT INTO dashboards (name, description, project_id, current_version, created_at, updated_at) VALUES ('Se-Alpha dash', '', $1, 1, `+stamp+`)`, mid)
			w.Exec(`INSERT INTO connections (name, description, project_id, db_type, host, username, password_encrypted, database, db_schema,
				extra, created_at, updated_at) VALUES ('se-alpha conn', '', $1, 'postgres', 'h', 'u', '', 'd', '', '{}', `+stamp+`)`, other)
			w.Exec(`INSERT INTO connections (name, description, project_id, db_type, host, username, password_encrypted, database, db_schema,
				extra, created_at, updated_at) VALUES ('se-alpha conn hidden', '', $1, 'mysql', 'h', 'u', '', 'd', '', '{}', `+stamp+`)`, mid)
		},
		Steps: func(w *World) []Step {
			var s []Step
			find := func(name, query string, auth func(*http.Request)) {
				s = append(s, Step{Name: name, Method: "GET", Path: "/search" + query, Auth: auth})
			}
			q := func(v string) string { return "?q=" + url.QueryEscape(v) }
			find("nothing asked", "", w.As("writer"))
			find("blanks", q("   "), w.As("writer"))
			for _, p := range []string{"admin", "writer", "obs_flag", "service", "editor"} {
				find(p+" searches", q("se-alpha"), w.As(p))
			}
			find("a person who reads a flow with scripts", q("se-alpha"), w.As("sso"))
			find("flows the reader may not open", q("se-boxed"), w.As("writer"))
			find("flows the administrator opens", q("se-boxed"), w.As("admin"))
			find("in capitals, with blanks", q("  SE-ALPHA "), w.As("writer"))
			find("a percent sign", q("100%"), w.As("admin"))
			find("an underscore", q("se_"), w.As("admin"))
			find("a backslash", q(`\`), w.As("admin"))
			find("one kind", q("se-alpha")+"&kind=datasource", w.As("admin"))
			find("an unknown kind", q("se-alpha")+"&kind=nope", w.As("admin"))
			find("an empty kind", q("se-alpha")+"&kind=", w.As("admin"))
			find("a page", q("se")+"&limit=2&offset=1", w.As("admin"))
			find("past the end", q("se")+"&offset=500", w.As("admin"))
			find("an offset beyond any integer", q("se")+"&offset=99999999999999999999999", w.As("admin"))
			find("the last value wins", q("zzz")+"&q=se-alpha", w.As("admin"))
			find("limit 0", q("se")+"&limit=0", w.As("admin"))
			find("limit 201", q("se")+"&limit=201", w.As("admin"))
			find("limit text, offset negative", q("se")+"&limit=x&offset=-1", w.As("admin"))
			find("nobody", q("se"), nil)
			find("nobody, a bad limit", q("se")+"&limit=0", nil)
			return s
		},
	})
}
