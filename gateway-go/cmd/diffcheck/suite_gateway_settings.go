package main

// routes/gateway_settings.py: the gateway's settings in Administration → Settings, and the
// rollout of the ones read at start (Docker: «manual»).
func init() {
	register(Suite{
		Module: "gateway_settings",
		Seed:   seedData,
		Engine: []EngineRule{{Method: "GET", Path: "^/settings$", Body: map[string]any{"items": []any{
			map[string]any{"key": "celery.task_time_limit", "value": nil, "env_value": 3600}}}}},
		Steps: func(w *World) []Step {
			s := forEveryone(w, "the settings", "GET", "/admin/gateway-settings", nil)
			s = append(s, forEveryone(w, "the rollout", "GET", "/admin/settings-rollout", nil)...)
			put := func(name string, values any) {
				s = append(s, Step{Name: "change: " + name, Method: "PUT", Path: "/admin/gateway-settings", Auth: w.As("admin"),
					JSON: map[string]any{"values": values}})
			}
			s = append(s, forEveryone(w, "change nothing", "PUT", "/admin/gateway-settings", map[string]any{"values": map[string]any{}})...)
			put("an unknown key", map[string]any{"x.y": 1})
			put("an unavailable one (sso)", map[string]any{"oidc.button_label": "x"})
			put("wrong types", map[string]any{"auth.session_idle_minutes": "30", "auth.personal_tokens": 1, "ai.max_cost_per_turn_usd": true,
				"ai.default_model": 5})
			put("out of range", map[string]any{"auth.session_idle_minutes": 1441, "jwt.access_ttl_minutes": 14, "webhooks.timeout_seconds": 0.5})
			put("a float for an int", map[string]any{"auth.api_token_max_days": 30.0})
			put("a bad time zone", map[string]any{"app.timezone": "Mars/Base"})
			put("an empty time zone", map[string]any{"app.timezone": "  "})
			put("a text too long", map[string]any{"ai.default_model": repeat("m", 201)})
			put("heartbeat beyond dead after", map[string]any{"orchestrator.heartbeat_seconds": 30})
			put("more executions than connections", map[string]any{"orchestrator.max_concurrent": 200, "db.pool_size": 5})
			put("stale below the engine's hard limit", map[string]any{"engine.run_stale_timeout_seconds": 1000})
			put("good ones", map[string]any{"auth.session_idle_minutes": 30, "ai.max_cost_per_turn_usd": 2, "app.timezone": "Europe/Rome",
				"ai.default_model": "  gpt  ", "db.pool_size": 50})
			s = append(s, Step{Name: "the settings after a change", Method: "GET", Path: "/admin/gateway-settings", Auth: w.As("obs_flag")})
			put("the same again", map[string]any{"auth.session_idle_minutes": 30})
			put("back to the environment", map[string]any{"auth.session_idle_minutes": nil, "ai.max_cost_per_turn_usd": nil,
				"app.timezone": nil, "ai.default_model": nil, "db.pool_size": nil})
			put("values not a dict", []any{1})
			s = append(s,
				Step{Name: "change with a token", Method: "PUT", Path: "/admin/gateway-settings", JSON: map[string]any{"values": 1},
					Auth: Bearer("tab_data_service_write_0000"), Header: map[string]string{"Accept-Language": "de"}},
				Step{Name: "change with a broken JSON", Method: "PUT", Path: "/admin/gateway-settings", Raw: []byte("{"), ContentType: "application/json"},
				Step{Name: "change with no body", Method: "PUT", Path: "/admin/gateway-settings", Auth: w.As("admin")},
			)
			roll := func(name, method, path string, body any) {
				s = append(s, Step{Name: "rollout: " + name, Method: method, Path: path, JSON: body, Auth: w.As("admin")})
			}
			roll("start", "POST", "/admin/settings-rollout", map[string]any{"components": []any{"gateway", "orchestrator"}})
			roll("the state", "GET", "/admin/settings-rollout", nil)
			roll("an unknown component", "POST", "/admin/settings-rollout", map[string]any{"components": []any{"gateway", "x", "y"}})
			roll("no components", "POST", "/admin/settings-rollout", map[string]any{"components": []any{}})
			roll("cancel", "DELETE", "/admin/settings-rollout/gateway", nil)
			roll("cancel what is not there", "DELETE", "/admin/settings-rollout/nothing", nil)
			s = append(s, Step{Name: "rollout: the state, an observer", Method: "GET", Path: "/admin/settings-rollout", Auth: w.As("obs_group")})
			s = append(s, Step{Name: "rollout: start, an observer", Method: "POST", Path: "/admin/settings-rollout", Auth: w.As("obs_group"),
				JSON: map[string]any{"components": []any{"gateway"}}})
			return s
		},
	})
}

func repeat(s string, n int) string {
	out := ""
	for i := 0; i < n; i++ {
		out += s
	}
	return out
}
