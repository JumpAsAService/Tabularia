package main

// routes/workers.py: Administration → Workers, off Kubernetes (KUBERNETES__SCALING off).
func init() {
	catalog := map[string]any{"items": []any{
		map[string]any{"key": "workers.run_concurrency", "min": 1, "max": 16, "value": 4, "category": "workers"},
		map[string]any{"key": "workers.preview_concurrency", "value": nil},
		map[string]any{"key": "other", "min": "x"},
	}}
	live := map[string]any{
		"workers": []any{
			map[string]any{"name": "w1", "kind": "run", "queues": []any{"celery"}, "processes": 4, "busy": "2", "memory_share": 1,
				"autoscale": true, "started_at": 1700000000, "seen_at": 1700000001.5, "extra": "ignored"},
			map[string]any{"name": "p1", "kind": "preview", "memory_used": 2.0},
			map[string]any{"name": "x", "kind": "other"},
		},
		"queued": map[string]any{"celery": 3, "preview": "0"}, "tick_seconds": 5,
	}
	register(Suite{
		Module: "workers",
		Seed:   seedEngineRows,
		Engine: []EngineRule{{Method: "GET", Path: "^/settings$", Body: catalog}, {Method: "GET", Path: "^/workers$", Body: live}},
		Steps: func(w *World) []Step {
			s := forEveryone(w, "the workers", "GET", "/admin/workers", nil)
			s = append(s, forEveryone(w, "scale", "PUT", "/admin/workers/run/scaling", map[string]any{"min_replicas": 1, "max_replicas": 3})...)
			s = append(s, forEveryone(w, "give back", "DELETE", "/admin/workers/preview/scaling", nil)...)
			as := func(name, method, path string, body any, rules []EngineRule) {
				s = append(s, Step{Name: name, Method: method, Path: path, JSON: body, Auth: w.As("admin"), Engine: rules})
			}
			engine := func(name string, cat, workers EngineRule) {
				as("the workers: "+name, "GET", "/admin/workers", nil, []EngineRule{cat, workers})
			}
			ok := EngineRule{Method: "GET", Path: "^/workers$", Body: live}
			cat := EngineRule{Method: "GET", Path: "^/settings$", Body: catalog}
			engine("an old engine", EngineRule{Path: "^/settings$", Status: 404}, ok)
			engine("the catalog fails", EngineRule{Path: "^/settings$", Status: 500}, ok)
			engine("the catalog redirects", EngineRule{Path: "^/settings$", Status: 302}, ok)
			engine("the catalog not JSON", EngineRule{Path: "^/settings$", Text: "x"}, ok)
			engine("the workers 404", cat, EngineRule{Path: "^/workers$", Status: 404})
			engine("the workers 503", cat, EngineRule{Path: "^/workers$", Status: 503})
			engine("no workers known", cat, EngineRule{Path: "^/workers$", Body: map[string]any{"queued": nil}})
			engine("workers null, tick 0", cat, EngineRule{Path: "^/workers$", Body: map[string]any{"workers": nil, "tick_seconds": 0}})
			engine("a worker without a name", cat, EngineRule{Path: "^/workers$", Body: map[string]any{"workers": []any{map[string]any{"kind": "run"}}}})
			engine("a busy that is not a number", cat, EngineRule{Path: "^/workers$", Body: map[string]any{"workers": []any{map[string]any{"kind": "run", "name": "a", "busy": "x"}}}})
			engine("queued not a dict", cat, EngineRule{Path: "^/workers$", Body: map[string]any{"queued": []any{1}}})
			engine("queued a float", cat, EngineRule{Path: "^/workers$", Body: map[string]any{"queued": map[string]any{"celery": 2.5}}})
			engine("the catalog a list", EngineRule{Path: "^/settings$", Body: []any{}}, ok)
			engine("no items", EngineRule{Path: "^/settings$", Body: map[string]any{}}, ok)
			engine("items null", EngineRule{Path: "^/settings$", Body: map[string]any{"items": nil}}, ok)
			engine("an item without a key", EngineRule{Path: "^/settings$", Body: map[string]any{"items": []any{map[string]any{"min": 1}}}}, ok)
			engine("a min null", EngineRule{Path: "^/settings$", Body: map[string]any{"items": []any{map[string]any{"key": "workers.run_concurrency", "min": nil}}}}, ok)
			engine("tick a text", cat, EngineRule{Path: "^/workers$", Body: map[string]any{"tick_seconds": "2.5"}})
			s = append(s, Step{Name: "the workers, an observer sees masked emails", Method: "GET", Path: "/admin/workers", Auth: w.As("obs_flag")})
			for _, c := range []struct {
				name, method, path string
				body               any
			}{
				{"scale an unknown kind", "PUT", "/admin/workers/big/scaling", map[string]any{"min_replicas": 1, "max_replicas": 3}},
				{"scale with bad bounds", "PUT", "/admin/workers/run/scaling", map[string]any{"min_replicas": -1, "max_replicas": "3"}},
				{"scale with nothing", "PUT", "/admin/workers/run/scaling", nil},
				{"scale with an unknown kind and nothing", "PUT", "/admin/workers/x/scaling", nil},
				{"give back an unknown kind", "DELETE", "/admin/workers/x/scaling", nil},
				{"give back run", "DELETE", "/admin/workers/run/scaling", nil},
			} {
				as(c.name, c.method, c.path, c.body, nil)
			}
			s = append(s,
				Step{Name: "scale with a broken JSON, nobody", Method: "PUT", Path: "/admin/workers/run/scaling", Raw: []byte("{"), ContentType: "application/json"},
				Step{Name: "scale with a bad body, a token", Method: "PUT", Path: "/admin/workers/x/scaling", JSON: map[string]any{}, Auth: Bearer("tab_data_service_write_0000")},
				Step{Name: "scale with an administrator's token", Method: "PUT", Path: "/admin/workers/x/scaling", JSON: map[string]any{},
					Auth: Bearer("tab_data_admin_write_000000")},
				Step{Name: "give back with an administrator's token, in French", Method: "DELETE", Path: "/admin/workers/run/scaling",
					Auth: Bearer("tab_data_admin_write_000000"), Header: map[string]string{"Accept-Language": "fr-CH, fr;q=0.9"}},
				Step{Name: "the workers with an administrator's token", Method: "GET", Path: "/admin/workers", Auth: Bearer("tab_data_admin_write_000000")},
				Step{Name: "scale with a token, in Italian", Method: "PUT", Path: "/admin/workers/run/scaling", JSON: map[string]any{}, Auth: Bearer("tab_data_service_write_0000"),
					Header: map[string]string{"Accept-Language": "it"}},
			)
			return s
		},
	})
}
