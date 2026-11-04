package main

import (
	"fmt"
	"time"
)

// routes/gateway_settings.py: the gateway's settings in Administration → Settings, and the
// rollout of the ones read at start (Docker: «manual»).
func init() {
	register(Suite{
		Module: "gateway_settings",
		Seed:   seedEngineRows, // the engine's saved settings: sent again when the upload limit changes
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
			return append(s, gatewaySettingsMore(w)...)
		},
	})
}

// gatewaySettingsMore: the Go port's own steps (routes/gateway_settings.py read and write):
// rows the panel would not have written, the constraints between two settings and the saved
// ones they drop, the engine's hard limit, the schedules read again in a new zone, the upload
// limit sent to the engine.
func gatewaySettingsMore(w *World) []Step {
	var s []Step
	put := func(name string, values any, rules []EngineRule, before func(*World)) {
		s = append(s, Step{Name: "change: " + name, Method: "PUT", Path: "/admin/gateway-settings", Auth: w.As("admin"),
			JSON: map[string]any{"values": values}, Engine: rules, Before: before})
	}
	catalog := func(item map[string]any) []EngineRule {
		return []EngineRule{{Method: "GET", Path: "^/settings$", Body: map[string]any{"items": []any{map[string]any{"key": "other"}, item}}}}
	}
	odd := func(w *World) {
		w.Exec(`DELETE FROM gateway_settings`)
		w.Exec(`INSERT INTO gateway_settings (key, value, updated_at, updated_by) VALUES
			('x.unknown', '1', '2026-03-01 10:00:00', 'alice.rossi@example.test'),
			('auth.api_token_max_days', 'not json', '2026-03-01', NULL),
			('ai.max_requests', '"12"', '2026-03-02', '@x.test'),
			('ai.comment_model', '{"a": [1, 2.5, 99999999999999999999, null]}', '2026-03-02', 'é@x.test'),
			('orchestrator.heartbeat_seconds', '50', '2026-03-03', ' '),
			('monitoring.cache_seconds', 'null', '2026-03-03', 'nobody'),
			('preview.default_sample_rows', '1000', '2026-03-03', 'a@x.test')`)
	}
	s = append(s, Step{Name: "the settings with odd rows · admin", Method: "GET", Path: "/admin/gateway-settings", Auth: w.As("admin"), Before: odd})
	for _, p := range everyone(w)[1:] {
		s = append(s, Step{Name: "the settings with odd rows · " + p.name, Method: "GET", Path: "/admin/gateway-settings", Auth: p.auth})
	}
	put("one, with odd rows saved", map[string]any{"ai.max_requests": 13}, nil, nil)
	put("dead after raised", map[string]any{"orchestrator.dead_after_seconds": 200}, nil, nil)
	put("the heartbeat within it", map[string]any{"orchestrator.heartbeat_seconds": 50}, nil, nil)
	put("dead after back: the saved heartbeat would break", map[string]any{"orchestrator.dead_after_seconds": nil}, nil, nil)
	put("both together", map[string]any{"orchestrator.dead_after_seconds": nil, "orchestrator.heartbeat_seconds": 10.5}, nil, nil)
	put("an unknown one among good ones", map[string]any{"ai.max_requests": 14, "nope": 1, "scripts.build_cpu": "2"}, nil, nil)
	many := map[string]any{}
	for i := 0; i < 81; i++ {
		many[fmt.Sprintf("k%d", i)] = i
	}
	put("more than 80", many, nil, nil)
	s = append(s, Step{Name: "change: big numbers", Method: "PUT", Path: "/admin/gateway-settings", Auth: w.As("admin"),
		ContentType: "application/json", Raw: []byte(`{"values": {"ai.max_requests": 1000000000000000000000000000000,
		"ai.max_cost_per_turn_usd": -1e400, "upload.max_bytes": 2.0, "ai.max_cost_per_day_usd": 1` + repeat("0", 400) + `}}`)})
	put("floats for floats", map[string]any{"ai.max_cost_per_day_usd": 3, "webhooks.timeout_seconds": 59.5, "monitoring.cache_seconds": 0}, nil, nil)
	put("texts", map[string]any{"ai.comment_model": "  é model  ", "ai.default_model": ""}, nil, nil)
	put("visitors (not available here)", map[string]any{"notebooks.max_visitors_per_app": 60}, nil, nil)
	put("the pool's executions", map[string]any{"db.pool_size": 3, "db.max_overflow": 0, "orchestrator.max_concurrent": 4}, nil, nil)
	put("the pool's executions, the threads", map[string]any{"db.pool_size": nil, "app.gateway_threads": 4, "db.max_overflow": 0,
		"orchestrator.max_concurrent": 5}, nil, nil)
	put("the pool back", map[string]any{"app.gateway_threads": nil, "db.max_overflow": nil, "orchestrator.max_concurrent": nil}, nil, nil)
	// the engine's hard limit
	put("stale: the engine down", map[string]any{"engine.run_stale_timeout_seconds": 1000},
		[]EngineRule{{Method: "GET", Path: "^/settings$", Status: 500, Text: "down"}}, nil)
	put("stale: a catalog that is a list", map[string]any{"engine.run_stale_timeout_seconds": 1000},
		[]EngineRule{{Method: "GET", Path: "^/settings$", Body: []any{1}}}, nil)
	put("stale: not JSON", map[string]any{"engine.run_stale_timeout_seconds": 1000},
		[]EngineRule{{Method: "GET", Path: "^/settings$", Text: "ok", Type: "text/plain"}}, nil)
	put("stale: the environment's limit", map[string]any{"engine.run_stale_timeout_seconds": 1000},
		catalog(map[string]any{"key": "celery.task_time_limit", "value": nil, "env_value": 900}), nil)
	put("stale: a decimal limit", map[string]any{"engine.run_stale_timeout_seconds": 1000},
		catalog(map[string]any{"key": "celery.task_time_limit", "value": 800.5, "env_value": 900}), nil)
	put("stale: a limit in words", map[string]any{"engine.run_stale_timeout_seconds": 1000},
		catalog(map[string]any{"key": "celery.task_time_limit", "value": "900"}), nil)
	put("stale: an item that is not a dict first", map[string]any{"engine.run_stale_timeout_seconds": 1000},
		[]EngineRule{{Method: "GET", Path: "^/settings$", Body: map[string]any{"items": []any{1}}}}, nil)
	put("stale: no items", map[string]any{"engine.run_stale_timeout_seconds": 1000},
		[]EngineRule{{Method: "GET", Path: "^/settings$", Body: map[string]any{"items": map[string]any{}}}}, nil)
	put("stale: back to the environment", map[string]any{"engine.run_stale_timeout_seconds": nil},
		catalog(map[string]any{"key": "celery.task_time_limit", "value": 600}), nil)
	put("stale: the environment's under the limit", map[string]any{"engine.run_stale_timeout_seconds": nil},
		catalog(map[string]any{"key": "celery.task_time_limit", "value": 7200}), nil)
	// the schedules read again in another zone
	schedules := func(w *World) {
		w.Exec(`UPDATE flows SET run_schedule = '*/7 3 * * 1' WHERE id = $1`, Data["flow:duck"])
		w.Exec(`UPDATE flows SET run_schedule = 'nope' WHERE id = $1`, Data["flow:polars-chdb"])
	}
	put("a zone, with schedules", map[string]any{"app.timezone": "America/New_York"}, nil, schedules)
	put("the zone again", map[string]any{"app.timezone": " America/New_York "}, nil, nil)
	put("the zone back", map[string]any{"app.timezone": nil}, nil, nil)
	// the upload limit goes to the engine too
	pushed := []EngineRule{{Method: "PUT", Path: "^/settings$", Body: map[string]any{"items": []any{}}}}
	put("the upload limit", map[string]any{"upload.max_bytes": 2 << 20}, pushed, nil)
	put("the upload limit, the engine refusing", map[string]any{"upload.max_bytes": 3 << 20},
		[]EngineRule{{Method: "PUT", Path: "^/settings$", Status: 422, Body: map[string]any{"detail": map[string]any{
			"errors": []any{map[string]any{"key": "upload.max_bytes"}}}}}}, nil)
	put("the upload limit back, the engine down", map[string]any{"upload.max_bytes": nil},
		[]EngineRule{{Method: "PUT", Path: "^/settings$", Status: 503, Text: "down"}}, nil)
	s = append(s, Step{Name: "the settings after all that · obs_flag", Method: "GET", Path: "/admin/gateway-settings", Auth: w.As("obs_flag")})
	// back to the environment for the suites after this one: both gateways read it again
	s = append(s, Step{Name: "the settings, the panel emptied", Method: "GET", Path: "/admin/gateway-settings", Auth: w.As("admin"),
		Before: func(w *World) {
			w.Exec(`DELETE FROM gateway_settings`)
			w.Exec(`UPDATE flows SET run_schedule = NULL, next_run_at = NULL WHERE id IN ($1, $2)`, Data["flow:duck"], Data["flow:polars-chdb"])
			w.inForce = nil
			time.Sleep(11 * time.Second)
		}})
	return s
}
