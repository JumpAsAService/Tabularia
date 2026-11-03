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
			s = append(s, Step{Name: "the settings, an observer: emails masked", Method: "GET", Path: "/admin/engine-settings", Auth: w.As("obs_group")})
			return s
		},
	})
}
