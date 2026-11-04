package main

// routes/engine_settings.py: the engine's settings in Administration → Settings.
func init() {
	item := func(key, kind string, extra map[string]any) map[string]any {
		m := map[string]any{"key": key, "category": "c", "unit": "rows", "min": 1, "max": 100.5, "env_var": "X__Y", "env_value": 3, "value": 4}
		if kind != "" {
			m["kind"] = kind
		}
		for k, v := range extra {
			m[k] = v
		}
		return m
	}
	catalog := map[string]any{"items": []any{
		item("workers.run_concurrency", "", nil),
		item("ingest.parquet_row_group_rows", "int", map[string]any{"rollout": []any{"worker"}, "choices": []any{}}),
		item("preview.cache", "bool", map[string]any{"env_value": true, "value": nil}),
		item("cache.ratio", "float", map[string]any{"optional": true, "format": "x"}),
		item("clickhouse_external.password", "secret", map[string]any{"env_value": "", "value": "tok"}),
		item("clickhouse_external.ai_password", "secret", nil),
		item("upload.max_bytes", "int", nil),
		item("bad.json", "choice", map[string]any{"choices": []any{"a", "b"}, "env_value": map[string]any{"z": 1, "a": []any{1.0}}}),
		item("never.saved", "str", nil),
	}, "unavailable": []any{"bigquery"}, "refresh_seconds": 5}
	register(Suite{
		Module: "engine_settings",
		Seed:   seedEngineRows,
		Engine: []EngineRule{{Method: "GET", Path: "^/settings$", Body: catalog}},
		Steps: func(w *World) []Step {
			s := forEveryone(w, "the settings", "GET", "/admin/engine-settings", nil)
			cat := func(name string, rule EngineRule) {
				s = append(s, Step{Name: "the settings: " + name, Method: "GET", Path: "/admin/engine-settings", Auth: w.As("admin"),
					Engine: []EngineRule{rule}})
			}
			body := func(b any) EngineRule { return EngineRule{Path: "^/settings$", Body: b} }
			cat("an old engine", EngineRule{Path: ".", Status: 404})
			cat("the engine fails", EngineRule{Path: ".", Status: 502, Text: "x"})
			cat("not JSON", EngineRule{Path: ".", Text: "x"})
			cat("a list", body([]any{}))
			cat("no items", body(map[string]any{}))
			cat("items empty, refresh 0", body(map[string]any{"items": []any{}, "refresh_seconds": 0, "unavailable": []any{}}))
			cat("an item without a category", body(map[string]any{"items": []any{map[string]any{"key": "a"}}}))
			cat("only the gateway's", body(map[string]any{"items": []any{map[string]any{"key": "upload.max_bytes"}}}))
			cat("a kind null", body(map[string]any{"items": []any{item("a", "", map[string]any{"kind": nil})}}))
			cat("a min not a number", body(map[string]any{"items": []any{item("a", "", map[string]any{"min": "x"})}}))
			cat("a min a numeric text", body(map[string]any{"items": []any{item("a", "", map[string]any{"min": " 2 ", "max": true})}}))
			cat("unavailable not texts", body(map[string]any{"items": []any{}, "unavailable": []any{1}}))
			cat("refresh a text", body(map[string]any{"items": []any{}, "refresh_seconds": "1e1"}))
			// writing
			get := EngineRule{Method: "GET", Path: "^/settings$", Body: catalog}
			okPut := EngineRule{Method: "PUT", Path: "^/settings$", Body: map[string]any{"items": []any{
				map[string]any{"key": "workers.run_concurrency", "rollout": []any{"worker", "nobody"}},
				map[string]any{"key": "cache.ratio", "rollout": "engine"}}}}
			put := func(name string, values any, rules ...EngineRule) {
				if rules == nil {
					rules = []EngineRule{get, okPut}
				}
				s = append(s, Step{Name: "change: " + name, Method: "PUT", Path: "/admin/engine-settings", Auth: w.As("admin"),
					JSON: map[string]any{"values": values}, Engine: rules})
			}
			s = append(s, forEveryone(w, "change nothing", "PUT", "/admin/engine-settings", map[string]any{"values": map[string]any{}})...)
			put("an unknown key", map[string]any{"x.y": 1, "upload.max_bytes": 5})
			put("a secret not text", map[string]any{"clickhouse_external.password": 5})
			put("a secret too long", map[string]any{"clickhouse_external.password": repeat("p", 1025)})
			put("a list value", map[string]any{"cache.ratio": []any{1}})
			put("the same value", map[string]any{"workers.run_concurrency": 6.0, "preview.cache": 1})
			put("a new value", map[string]any{"workers.run_concurrency": 8, "cache.ratio": 0.5})
			put("a password", map[string]any{"clickhouse_external.password": "pässword"})
			put("a text and a choice", map[string]any{"never.saved": "x", "bad.json": "b"})
			put("the engine refuses one", map[string]any{"workers.run_concurrency": 9}, get,
				EngineRule{Method: "PUT", Path: "^/settings$", Status: 422, Body: map[string]any{"detail": map[string]any{"errors": []any{
					map[string]any{"key": "workers.run_concurrency", "code": "max"}}}}})
			put("the engine refuses only the upload limit", map[string]any{"workers.run_concurrency": 9}, get,
				EngineRule{Method: "PUT", Path: "^/settings$", Status: 422, Body: map[string]any{"detail": map[string]any{"errors": []any{
					map[string]any{"key": "upload.max_bytes"}}}}})
			put("the engine fails", map[string]any{"workers.run_concurrency": 10}, get, EngineRule{Method: "PUT", Path: "^/settings$", Status: 500})
			put("the engine is old", map[string]any{"workers.run_concurrency": 10}, get, EngineRule{Method: "PUT", Path: "^/settings$", Status: 404})
			put("the engine answers not JSON", map[string]any{"workers.run_concurrency": 10}, get, EngineRule{Method: "PUT", Path: "^/settings$", Text: "ok"})
			put("the engine refuses with a list", map[string]any{"workers.run_concurrency": 10}, get,
				EngineRule{Method: "PUT", Path: "^/settings$", Status: 422, Body: []any{1}})
			put("the catalog is old", map[string]any{"workers.run_concurrency": 10}, EngineRule{Method: "GET", Path: "^/settings$", Status: 404})
			put("back to the environment", map[string]any{"workers.run_concurrency": nil, "cache.ratio": nil, "clickhouse_external.password": nil,
				"never.saved": nil, "bad.json": nil})
			s = append(s,
				Step{Name: "change with an administrator's token", Method: "PUT", Path: "/admin/engine-settings", JSON: map[string]any{"values": 1},
					Auth: Bearer("tab_data_admin_write_000000"), Header: map[string]string{"Accept-Language": "es"}},
				Step{Name: "change with a broken JSON", Method: "PUT", Path: "/admin/engine-settings", Raw: []byte("{"), ContentType: "application/json"},
				Step{Name: "change with no body", Method: "PUT", Path: "/admin/engine-settings", Auth: w.As("admin")},
			)
			check := func(name string, values any, rule EngineRule) {
				s = append(s, Step{Name: "check ClickHouse: " + name, Method: "POST", Path: "/admin/engine-settings/clickhouse-check",
					Auth: w.As("admin"), JSON: map[string]any{"values": values}, Engine: []EngineRule{rule}})
			}
			chOK := EngineRule{Method: "POST", Path: "^/settings/clickhouse-check$", Body: map[string]any{"steps": []any{map[string]any{"ok": true}}}}
			check("good", map[string]any{"clickhouse_external.host": "h", "clickhouse_external.password": "pw", "clickhouse_external.port": 9000, "x": nil}, chOK)
			check("the password not text", map[string]any{"clickhouse_external.ai_password": true}, chOK)
			check("a float", map[string]any{"a": 1.5}, chOK)
			check("refused", map[string]any{}, EngineRule{Path: ".", Status: 422, Body: map[string]any{"detail": "no"}})
			check("an old engine", map[string]any{}, EngineRule{Path: ".", Status: 404})
			check("the engine fails", map[string]any{}, EngineRule{Path: ".", Status: 500})
			check("not JSON", map[string]any{}, EngineRule{Path: ".", Text: "x"})
			s = append(s, forEveryone(w, "check ClickHouse", "POST", "/admin/engine-settings/clickhouse-check", map[string]any{})...)
			s = append(s, Step{Name: "the settings, an observer: emails masked", Method: "GET", Path: "/admin/engine-settings", Auth: w.As("obs_group")})
			return s
		},
	})
}
