package main

import "strings"

// routes/queue.py: the run queue, proxied to the engine.
func init() {
	overview := map[string]any{"workers": []any{map[string]any{"name": "w1", "active": 1}}, "active": []any{},
		"reserved": []any{}, "scheduled": []any{}, "ratio": 1.0, "big": 12345678901234567}
	register(Suite{
		Module: "queue",
		Seed:   seedData,
		Engine: []EngineRule{
			{Method: "GET", Path: "^/tasks/queue$", Body: overview},
			{Method: "DELETE", Path: "^/tasks/", Body: map[string]any{"revoked": true, "è": "ü"}},
		},
		Steps: func(w *World) []Step {
			s := forEveryone(w, "the overview", "GET", "/queue", nil)
			s = append(s, forEveryone(w, "stop a job", "POST", "/queue/jobs/abc-123/stop", nil)...)
			admin := w.As("admin")
			fail := func(name, method, path string, rule EngineRule) {
				s = append(s, Step{Name: name, Method: method, Path: path, Auth: admin, Engine: []EngineRule{rule}})
			}
			fail("the engine refuses with JSON", "GET", "/queue", EngineRule{Path: ".", Status: 404, Body: map[string]any{"detail": "Not Found"}})
			fail("the engine fails with text", "GET", "/queue", EngineRule{Path: ".", Status: 503, Text: "Service Unavailable", Type: "text/plain"})
			fail("the engine fails with a long text", "GET", "/queue", EngineRule{Path: ".", Status: 500, Text: strings.Repeat("è", 600), Type: "text/plain; charset=utf-8"})
			fail("the engine answers 418", "GET", "/queue", EngineRule{Path: ".", Status: 418, Text: "teapot", Type: "text/plain"})
			fail("the engine answers 200 not JSON", "GET", "/queue", EngineRule{Path: ".", Status: 200, Text: "<html>", Type: "text/html"})
			fail("the engine answers 204", "GET", "/queue", EngineRule{Path: ".", Status: 204})
			fail("the engine answers 302", "GET", "/queue", EngineRule{Path: ".", Status: 302, Text: "moved"})
			fail("the engine answers a list", "GET", "/queue", EngineRule{Path: ".", Body: []any{1, "a", nil}})
			fail("the engine answers NaN", "GET", "/queue", EngineRule{Path: ".", Text: `{"a": NaN}`})
			fail("stop: the engine refuses", "POST", "/queue/jobs/abc/stop", EngineRule{Path: ".", Status: 404, Body: map[string]any{"detail": "no such task"}})
			fail("stop: the engine answers not JSON", "POST", "/queue/jobs/abc/stop", EngineRule{Path: ".", Status: 200, Text: "ok", Type: "text/plain"})
			for _, id := range []string{"a%20b", "a%3Fb%3Dc", "a%23b", "a%2Fb", "%C3%A8", "a+b", "..", "a%25b", "x;y", "a%2541", "a%25", "a%01b", "a%22b%3C%3E%60", "a/../b"} {
				s = append(s, Step{Name: "stop an odd id " + id, Method: "POST", Path: "/queue/jobs/" + id + "/stop", Auth: admin})
			}
			s = append(s,
				Step{Name: "stop with a body", Method: "POST", Path: "/queue/jobs/j1/stop", JSON: map[string]any{"x": 1}, Auth: admin},
				Step{Name: "stop with a broken body", Method: "POST", Path: "/queue/jobs/j1/stop", Raw: []byte("{"), ContentType: "application/json", Auth: admin},
				Step{Name: "GET on stop", Method: "GET", Path: "/queue/jobs/j1/stop", Auth: admin},
				Step{Name: "the overview with a query", Method: "GET", Path: "/queue?x=1", Auth: admin},
			)
			return s
		},
	})
}
