package main

import (
	"bytes"
	"net/http"
)

// routes/proxy.py: the routes forwarded to the engine.
func init() {
	engine := []EngineRule{
		{Method: "GET", Path: "^/tasks/operations$", Body: []any{map[string]any{"type": "filter", "label": "Filtro è"}}},
		{Method: "GET", Path: "^/engines$", Body: []any{
			map[string]any{"id": "polars", "available": true, "label": "Polars"},
			map[string]any{"id": "duckdb", "available": true, "version": 1.5},
			map[string]any{"id": "clickhouse", "available": false, "reason": "not configured"},
		}},
		{Method: "POST", Path: "^/tasks/transform-data$", Body: map[string]any{"task_id": "t-transform"}},
		{Method: "POST", Path: "^/tasks/exports$", Body: map[string]any{"task_id": "0b4c1f0e-0000-4000-8000-000000000001"}},
		{Method: "GET", Path: "^/tasks/exports/.*/file$", Body: map[string]any{"pages": []any{"<svg/>"}}},
		{Method: "GET", Path: "^/tasks/exports/", Body: map[string]any{"status": "SUCCESS"}},
		{Method: "POST", Path: "^/files$", Body: map[string]any{"dataset_id": "d-new", "parquet_key": "datasets/up-new.parquet",
			"raw_key": "raw/up-new.csv", "rows": 3}},
	}
	register(Suite{
		Module: "proxy",
		Seed:   func(w *World) { w.seedFlows() },
		Engine: engine,
		Steps: func(w *World) []Step {
			var s []Step
			post := func(name, path string, auth func(*http.Request), body any) {
				s = append(s, Step{Name: name, Method: "POST", Path: path, Auth: auth, JSON: body})
			}
			raw := func(name, path string, auth func(*http.Request), body string) {
				s = append(s, Step{Name: name, Method: "POST", Path: path, Auth: auth, Raw: []byte(body), ContentType: "application/json"})
			}
			get := func(name, path string, auth func(*http.Request)) {
				s = append(s, Step{Name: name, Method: "GET", Path: path, Auth: auth})
			}
			mine := map[string]any{"bucket": "data-prep", "input_key": "datasets/sales.parquet", "operations": []any{}}
			hidden := map[string]any{"input_key": "datasets/hidden.parquet", "operations": []any{}}
			other := map[string]any{"input_key": "datasets/other.parquet", "operations": []any{map[string]any{"type": "limit"}}}

			// ── metadata and the catalog ──
			get("operations · editor", "/tasks/operations?b=2&a=x+y&c=%C3%A8&d&&e=%zz", w.As("editor"))
			get("operations · nobody", "/tasks/operations", nil)
			get("engines · editor", "/engines", w.As("editor"))
			s = append(s, Step{Name: "engines · DuckDB disabled", Method: "GET", Path: "/engines", Auth: w.As("obs_flag"),
				Before: func(w *World) {
					w.Exec(`INSERT INTO disabled_engines (engine_id, disabled_at) VALUES ('duckdb', '2026-01-01') ON CONFLICT DO NOTHING`)
				}})
			s = append(s, Step{Name: "engines · the engine fails", Method: "GET", Path: "/engines", Auth: w.As("editor"),
				Engine: []EngineRule{{Method: "GET", Path: "^/engines$", Status: 503, Raw: ptr("down"),
					Headers: map[string]string{"Content-Type": "text/plain"}}},
				Before: func(w *World) { w.Exec(`DELETE FROM disabled_engines`) }})

			// ── previews ──
			post("preview · editor, a datasource of Sales", "/tasks/preview?x=1", w.As("editor"), mine)
			post("preview · editor, own upload", "/tasks/preview", w.As("editor"), map[string]any{"input_key": "datasets/up-editor.parquet"})
			post("preview · writer, own upload of someone else", "/tasks/preview", w.As("writer"), map[string]any{"input_key": "datasets/up-editor.parquet"})
			post("preview · editor, hidden datasource", "/tasks/preview", w.As("editor"), hidden)
			post("preview · admin, hidden datasource", "/tasks/preview", w.As("admin"), hidden)
			post("preview · editor, another bucket", "/tasks/preview", w.As("editor"), map[string]any{"bucket": "evil", "input_key": "datasets/sales.parquet"})
			post("preview · editor, outside the managed areas", "/tasks/preview", w.As("editor"), map[string]any{"input_key": "secrets/x"})
			post("preview · admin, outside the managed areas", "/tasks/preview", w.As("admin"), map[string]any{"input_key": "secrets/x"})
			post("preview · a nested source", "/tasks/preview", w.As("editor"), map[string]any{"input_key": "datasets/sales.parquet",
				"operations": []any{map[string]any{"type": "join", "params": map[string]any{"right": map[string]any{"key": "datasets/hidden.parquet"}}}}})
			post("preview · a slot", "/tasks/preview", w.As("editor"), map[string]any{"slot": "panel-1", "input_key": "datasets/sales.parquet", "n": 1.0})
			post("preview · a bad slot", "/tasks/preview", w.As("editor"), map[string]any{"input_key": "datasets/sales.parquet", "slot": "a b"})
			post("preview · a slot that is no text", "/tasks/preview", w.As("editor"), map[string]any{"slot": 3})
			raw("preview · text kept byte for byte without a slot", "/tasks/preview", w.As("editor"), `{"input_key":"datasets/sales.parquet","z":1.50,"é":"è"}`)
			raw("preview · accents, a slot", "/tasks/preview", w.As("editor"), `{"slot":"s1","label":"città ☃ 😀","big":123456789012345678901234567890,"f":1e400}`)
			raw("preview · not JSON", "/tasks/preview", w.As("editor"), `{"input_key":`)
			raw("preview · NaN", "/tasks/preview", w.As("editor"), `{"x": NaN}`)
			raw("preview · empty body", "/tasks/preview", w.As("editor"), ``)
			raw("preview · a list", "/tasks/preview", w.As("editor"), `[1, 2]`)
			raw("preview · a BOM", "/tasks/preview", w.As("editor"), "\xef\xbb\xbf{\"a\": 1}")
			s = append(s, Step{Name: "preview · too large", Method: "POST", Path: "/tasks/preview", Auth: w.As("editor"),
				Raw: bytes.Repeat([]byte(" "), 8*1024*1024+1), ContentType: "application/json"})
			post("preview · nobody", "/tasks/preview", nil, mine)
			post("preview · a read token (it only reads)", "/tasks/preview", Bearer("tab_writer_read_00000000000"), other)
			post("cache-status · a read token", "/tasks/cache-status", Bearer("tab_writer_read_00000000000"), other)
			post("cache-status · editor", "/tasks/cache-status", w.As("editor"), mine)
			post("cache-status · editor, hidden", "/tasks/cache-status", w.As("editor"), hidden)

			// ── a run without a saved flow ──
			tr := func(name string, auth func(*http.Request), body any) {
				s = append(s, Step{Name: name, Method: "POST", Path: "/tasks/transform-data", Auth: auth, JSON: body,
					EngineMask: []string{"*.body.output_key"}})
			}
			tr("transform · editor", w.As("editor"), map[string]any{"input_key": "datasets/sales.parquet", "engine": "duckdb",
				"operations": []any{map[string]any{"type": "limit"}}, "output_key": "datasets/sales.parquet"})
			tr("transform · editor, empty bucket", w.As("editor"), map[string]any{"bucket": "", "input_key": "datasets/sales.parquet"})
			tr("transform · writer, VIEW only", w.As("writer"), other)
			tr("transform · editor (Analysts: RUN on Other)", w.As("editor"), other)
			tr("transform · editor, hidden", w.As("editor"), hidden)
			tr("transform · a destination", w.As("editor"), map[string]any{"input_key": "datasets/sales.parquet", "destination": map[string]any{"x": 1}})
			tr("transform · an empty email is no destination", w.As("editor"), map[string]any{"input_key": "datasets/sales.parquet", "email": map[string]any{}})
			tr("transform · operations a text", w.As("editor"), map[string]any{"input_key": "datasets/sales.parquet", "operations": "abc"})
			tr("transform · operations a number", w.As("admin"), map[string]any{"input_key": "datasets/sales.parquet", "operations": 5})
			tr("transform · a list", w.As("editor"), []any{1})
			tr("transform · nobody", nil, mine)
			tr("transform · a read token", Bearer("tab_writer_read_00000000000"), other)

			// ── exports ──
			post("export · editor", "/tasks/export", w.As("editor"), map[string]any{"input_key": "datasets/sales.parquet", "format": "csv",
				"filename": "vendite è.csv", "engine": "polars", "owner": "user:999"})
			post("export · editor, hidden", "/tasks/export", w.As("editor"), hidden)
			post("export · a list", "/tasks/export", w.As("editor"), []any{})
			post("export · a read token", "/tasks/export", Bearer("tab_writer_read_00000000000"), other)
			post("exports · editor", "/tasks/exports", w.As("editor"), map[string]any{"input_key": "datasets/sales.parquet", "format": "xlsx"})
			post("exports · a write token waits", "/tasks/exports", Bearer("tab_writer_write_0000000000"), other)
			post("exports · writer, hidden", "/tasks/exports", w.As("writer"), hidden)
			get("export's state", "/tasks/exports/0b4c1f0e-0000-4000-8000-000000000001?owner=user:1", w.As("editor"))
			get("export's state · not an id", "/tasks/exports/nope", w.As("editor"))
			get("export's state · nobody", "/tasks/exports/0b4c1f0e-0000-4000-8000-000000000001", nil)
			s = append(s, Step{Name: "export's file · gzip", Method: "GET", Path: "/tasks/exports/0b4c1f0e-0000-4000-8000-000000000001/file",
				Auth: w.As("editor"), Header: map[string]string{"Accept-Encoding": "gzip"}})
			for i, d := range []string{"attachment; filename*=UTF-8''vendite%20citt%C3%A0%20%E2%98%83.xlsx",
				"attachment; filename=\"a.csv\"; filename*=UTF-8''%C3%A8.csv", "inline; filename*=utf-8''%22%E2%98%83%22", "attachment"} {
				s = append(s, Step{Name: "export's file · a disposition " + d, Method: "GET", Auth: w.As("editor"),
					Path: "/tasks/exports/0b4c1f0e-0000-4000-8000-00000000000" + string(rune('2'+i)) + "/file",
					Engine: []EngineRule{{Method: "GET", Path: "/file$", Raw: ptr("a,b\n"),
						Headers: map[string]string{"Content-Type": "text/csv; charset=utf-8", "Content-Disposition": d}}}})
			}
			get("export's file · not an id", "/tasks/exports/0B4C1F0E-0000-4000-8000-000000000001/file", w.As("editor"))
			get("a task's state", "/tasks/abc-123?verbose=1", w.As("writer"))
			get("a task's state · nobody", "/tasks/abc-123", nil)

			// ── uploads ──
			up := func(name string, auth func(*http.Request), body string, rules []EngineRule) {
				s = append(s, Step{Name: name, Method: "POST", Path: "/files?name=a%20b.csv", Auth: auth, Raw: []byte(body),
					ContentType: "text/csv", Engine: rules})
			}
			up("upload · editor", w.As("editor"), "a,b\n1,2\n", nil)
			up("upload · obs_flag, no EDIT anywhere", w.As("obs_flag"), "a\n1\n", nil)
			up("upload · writer, VIEW only", w.As("writer"), "a\n1\n", nil)
			up("upload · nobody", nil, "a\n", nil)
			up("upload · a read token", Bearer("tab_writer_read_00000000000"), "a\n", nil)
			up("upload · the engine refuses", w.As("admin"), "a\n", []EngineRule{{Method: "POST", Path: "^/files$", Status: 400,
				Body: map[string]any{"detail": "bad file"}}})
			up("upload · the engine answers no object", w.As("admin"), "a\n", []EngineRule{{Method: "POST", Path: "^/files$", Body: []any{1}}})
			up("upload · the engine answers nulls", w.As("admin"), "a\n", []EngineRule{{Method: "POST", Path: "^/files$",
				Body: map[string]any{"dataset_id": nil, "parquet_key": 5}}})
			return s
		},
	})
	// the ceiling of the uploads, from the panel
	register(Suite{
		Module: "proxy",
		Seed:   func(w *World) { w.seedFlows() },
		Engine: engine,
		Phases: []Phase{{Name: "panel: uploads up to 1 MB", Panel: map[string]string{"upload.max_bytes": "1048576"}}},
		Steps: func(w *World) []Step {
			return []Step{
				{Name: "upload · over the ceiling, declared", Method: "POST", Path: "/files", Auth: w.As("editor"),
					Raw: bytes.Repeat([]byte("x"), 1048577), ContentType: "text/csv"},
				{Name: "upload · over the ceiling, by someone who cannot upload", Method: "POST", Path: "/files", Auth: w.As("obs_flag"),
					Raw: bytes.Repeat([]byte("x"), 1048577), ContentType: "text/csv"},
				{Name: "upload · at the ceiling", Method: "POST", Path: "/files", Auth: w.As("editor"),
					Raw: bytes.Repeat([]byte("x"), 1048576), ContentType: "text/csv"},
			}
		},
	})
}
