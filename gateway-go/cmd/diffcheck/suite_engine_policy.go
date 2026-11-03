package main

// routes/engine_policy.py: the engines allowed in the installation.
func init() {
	register(Suite{
		Module: "engine_policy",
		Seed:   seedData,
		Steps: func(w *World) []Step {
			s := forEveryone(w, "the list", "GET", "/engine-policy", nil)
			s = append(s, forEveryone(w, "allow polars", "PUT", "/engine-policy/polars", map[string]any{"allowed": true})...)
			put := func(name, engine string, body any) {
				s = append(s, Step{Name: name, Method: "PUT", Path: "/engine-policy/" + engine, JSON: body, Auth: w.As("admin")})
			}
			put("disable duckdb", "duckdb", map[string]any{"allowed": false})
			put("disable duckdb again (the row is there)", "duckdb", map[string]any{"allowed": false})
			put("disable, spaces and capitals", "%20ChDB%20", map[string]any{"allowed": false})
			put("an unknown engine", "spark", map[string]any{"allowed": false})
			put("an unknown engine, capitals kept in the message", "Spark", map[string]any{"allowed": true})
			put("an empty engine", "%20", map[string]any{"allowed": true})
			put("a slash inside", "duck%2Fdb", map[string]any{"allowed": true})
			put("the body missing", "duckdb", nil)
			put("allowed missing", "duckdb", map[string]any{})
			put("allowed a string pydantic reads", "bigquery", map[string]any{"allowed": "no"})
			put("allowed a number", "bigquery", map[string]any{"allowed": 1})
			put("allowed not a boolean", "bigquery", map[string]any{"allowed": "maybe"})
			put("allowed null", "bigquery", map[string]any{"allowed": nil})
			put("a list as body", "bigquery", []any{true})
			put("unknown engine and bad body: the body first", "spark", map[string]any{"allowed": "maybe"})
			put("extra fields ignored", "clickhouse", map[string]any{"allowed": false, "x": 1})
			put("the last one cannot go", "bigquery", map[string]any{"allowed": false})
			put("the last one may be allowed again", "bigquery", map[string]any{"allowed": true})
			put("polars back", "polars", map[string]any{"allowed": true})
			put("duckdb back", "duckdb", map[string]any{"allowed": true})
			s = append(s, Step{Name: "the list after the changes", Method: "GET", Path: "/engine-policy", Auth: w.As("admin")})
			s = append(s,
				Step{Name: "a broken JSON, nobody", Method: "PUT", Path: "/engine-policy/duckdb", Raw: []byte(`{"allowed":`), ContentType: "application/json"},
				Step{Name: "a broken JSON, an observer", Method: "PUT", Path: "/engine-policy/duckdb", Raw: []byte(`{"allowed":`), ContentType: "application/json", Auth: w.As("obs_flag")},
				Step{Name: "bad body, an observer: 403 first", Method: "PUT", Path: "/engine-policy/duckdb", JSON: map[string]any{}, Auth: w.As("obs_flag")},
				Step{Name: "form body", Method: "PUT", Path: "/engine-policy/duckdb", Raw: []byte(`allowed=true`), ContentType: "application/x-www-form-urlencoded", Auth: w.As("admin")},
				Step{Name: "JSON without a content type", Method: "PUT", Path: "/engine-policy/chdb", Raw: []byte(`{"allowed": true}`), Auth: w.As("admin")},
				Step{Name: "a query string is ignored", Method: "GET", Path: "/engine-policy?x=1", Auth: w.As("admin")},
				Step{Name: "trailing slash", Method: "GET", Path: "/engine-policy/", Auth: w.As("admin")},
				Step{Name: "POST is not a route", Method: "POST", Path: "/engine-policy", Auth: w.As("admin")},
			)
			return s
		},
	})
}
