package main

// The rounds of engine_settings_loop, siem_loop and housekeeping_loop.

func encrypted(plain string) string {
	if fernet == nil {
		fail("DIFF_FERNET is not set")
	}
	t, err := fernet.Encrypt(plain)
	if err != nil {
		fail("%v", err)
	}
	return t
}

func init() {
	// services/engine_panel_settings.py one_round: the saved settings sent again
	register(Suite{
		Module: "jobs_engine_settings",
		Seed: func(w *World) {
			w.Exec(`DELETE FROM engine_settings`)
			w.Exec(`INSERT INTO engine_settings (key, value, value_json, updated_at, updated_by) VALUES
				('preview.max_rows', 5000, NULL, '2026-01-01', 'admin@x.test'),
				('celery.task_time_limit', 7, '6.5', '2026-01-01', NULL),
				('duckdb.threads', 2, '2.4', '2026-01-01', NULL),
				('export.enabled', 1, 'true', '2026-01-01', NULL),
				('clickhouse.host', 0, '"ch.internal"', '2026-01-01', NULL),
				('upload.max_bytes', 4194304, NULL, '2026-01-01', NULL)`)
			w.Exec(`DELETE FROM gateway_settings WHERE key = 'upload.max_bytes'`)
		},
		Steps: func(w *World) []Step {
			put := func(status int, body any) []EngineRule {
				return []EngineRule{{Method: "PUT", Path: "^/settings$", Status: status, Body: body}}
			}
			tick := func(name string, engine []EngineRule, before func(*World), env map[string]string) Step {
				return Step{Name: name, Method: "TICK", Path: "engine_settings", Tick: "engine_settings", Now: "2026-02-01T00:00:00Z",
					Engine: engine, Before: before, Env: env}
			}
			return []Step{
				tick("the engine takes them", put(200, map[string]any{"ok": true}), nil, nil),
				tick("with the gateway's upload limit from its panel", put(200, map[string]any{}), func(w *World) {
					w.Exec(`INSERT INTO gateway_settings (key, value, updated_at) VALUES ('upload.max_bytes', '2097152', now())`)
				}, nil),
				tick("an upload limit the panel would refuse is not sent", put(200, map[string]any{}), func(w *World) {
					w.Exec(`UPDATE gateway_settings SET value = '12' WHERE key = 'upload.max_bytes'`)
				}, nil),
				tick("the engine refuses one key, then still", put(422, map[string]any{"detail": map[string]any{"errors": []any{
					map[string]any{"key": "duckdb.threads", "code": "range"}}}}), nil, nil),
				tick("the engine refuses the gateway's limit", put(422, map[string]any{"detail": map[string]any{"errors": []any{
					map[string]any{"key": "upload.max_bytes"}}}}), func(w *World) {
					w.Exec(`UPDATE gateway_settings SET value = '2097152' WHERE key = 'upload.max_bytes'`)
				}, nil),
				tick("a refusal without errors", put(422, map[string]any{"detail": "no"}), nil, nil),
				tick("a refusal that is not a dict", put(422, []any{1}), nil, nil),
				tick("the engine fails", put(500, map[string]any{"detail": "down"}), nil, nil),
				tick("someone is saving: the round skips", put(200, map[string]any{}), func(w *World) {
					holdLock(w, 7402118)
				}, nil),
				tick("the lock is free again", put(200, map[string]any{}), func(w *World) { releaseLocks(w) }, nil),
				tick("the panel off: nothing of it is sent", put(200, map[string]any{}), nil,
					map[string]string{"ENGINE__SETTINGS_PANEL": "false"}),
				tick("a JSON value that is NaN", put(200, map[string]any{}), func(w *World) {
					w.Exec(`UPDATE engine_settings SET value_json = 'NaN' WHERE key = 'duckdb.threads'`)
				}, nil),
				tick("no settings saved", put(200, map[string]any{}), func(w *World) { w.Exec(`DELETE FROM engine_settings`) }, nil),
			}
		},
	})

	// services/siem.py one_round: the audit log to Splunk, a batch at a time
	register(Suite{
		Module: "jobs_siem",
		Seed: func(w *World) {
			w.Exec(`DELETE FROM siem_destinations`)
			w.Exec(`INSERT INTO siem_destinations (id, kind, enabled, url, token_enc, index, source, sourcetype, host, verify_tls,
				cursor_id, sent_total, failures, updated_at) SELECT 1, 'splunk_hec', false, 'http://siem.test:8000', $1, NULL, NULL,
				'custom:audit', '', true, coalesce(max(id), 0), 3, 0, '2026-01-01' FROM audit_logs`, encrypted("hec-token-1"))
			ev := func(ts string, actor any, detail any, ip any) {
				w.Exec(`INSERT INTO audit_logs (ts, actor_id, actor_label, action, outcome, target_type, target_id, target_label, detail,
					ip, user_agent) VALUES ($1, $2, 'siem@x.test', 'siem.event', 'success', 'flow', 7, 'Vendite è', $3, $4, NULL)`,
					ts, actor, detail, ip)
			}
			ev("2025-12-01 10:00:00", w.People["editor"], `{"b": 1, "a": [1, 2.5, "ü"], "n": null}`, "10.0.0.1")
			ev("2025-12-01 10:00:00.123456", nil, `not json`, nil)
			ev("2025-12-01 10:00:01.9996", nil, `null`, nil)
			ev("2025-12-01 10:00:02", nil, ``, nil)
			ev("2025-12-31 23:59:55", nil, `{"late": true}`, nil) // within the 10 s delay at the first moment
		},
		Steps: func(w *World) []Step {
			hec := func(status int, body any) []EngineRule {
				return []EngineRule{{Method: "POST", Path: "^/services/collector/event$", Status: status, Body: body}}
			}
			ok := hec(200, map[string]any{"text": "Success", "code": 0})
			tick := func(name, now string, engine []EngineRule, before func(*World)) Step {
				return Step{Name: name, Method: "TICK", Path: "siem", Tick: "siem", Now: now, Engine: engine, Before: before}
			}
			return []Step{
				tick("off: nothing goes", "2026-01-01T00:00:00Z", ok, nil),
				tick("on: the old items go, the last 10 seconds wait", "2026-01-01T00:00:00Z", ok, func(w *World) {
					w.Exec(`UPDATE siem_destinations SET enabled = true`)
				}),
				tick("Splunk refuses the token", "2026-01-01T00:01:00Z", hec(403, map[string]any{"text": "Invalid token", "code": 4}), nil),
				tick("the wait is not over", "2026-01-01T00:01:03Z", ok, nil),
				tick("refused again, a longer wait", "2026-01-01T00:01:06Z", hec(503, "busy"), nil),
				tick("a 200 whose code is not 0", "2026-01-01T00:02:00Z", hec(200, map[string]any{"text": "No data", "code": 5}), nil),
				tick("an answer that is not JSON", "2026-01-01T00:03:00Z", []EngineRule{{Method: "POST",
					Path: "^/services/collector/event$", Text: "fine", Type: "text/plain"}}, nil),
				tick("many items: several batches, with an index", "2026-01-01T00:04:00Z", ok, func(w *World) {
					w.Exec(`UPDATE siem_destinations SET index = 'tab', host = 'h1', source = NULL`)
					w.Exec(`INSERT INTO audit_logs (ts, actor_label, action, outcome, detail)
						SELECT timestamp '2025-12-02' + g * interval '1 second', 'bulk@x.test', 'siem.bulk', 'success',
						'{"i": ' || g || '}' FROM generate_series(1, 1100) g`)
				}),
				tick("another process holds the lock", "2026-01-01T00:05:00Z", ok, func(w *World) {
					w.Exec(`INSERT INTO audit_logs (ts, actor_label, action, outcome) VALUES ('2025-12-03', 'x', 'siem.more', 'success')`)
					holdLock(w, 7402122)
				}),
				tick("the lock free again", "2026-01-01T00:05:00Z", ok, func(w *World) { releaseLocks(w) }),
				tick("no token", "2026-01-01T00:06:00Z", ok, func(w *World) {
					w.Exec(`INSERT INTO audit_logs (ts, actor_label, action, outcome) VALUES ('2025-12-03', 'x', 'siem.more', 'success')`)
					w.Exec(`UPDATE siem_destinations SET token_enc = NULL`)
				}),
				tick("an address that does not resolve", "2026-01-01T00:07:00Z", ok, func(w *World) {
					w.Exec(`UPDATE siem_destinations SET token_enc = $1, url = 'http://nowhere.invalid'`, encrypted("hec-token-2"))
				}),
				tick("a loopback address", "2026-01-01T00:20:00Z", ok, func(w *World) {
					w.Exec(`UPDATE siem_destinations SET url = 'http://127.0.0.1:9/services/collector', next_attempt_at = NULL`)
				}),
				tick("off again", "2026-01-01T01:00:00Z", ok, func(w *World) {
					w.Exec(`UPDATE siem_destinations SET enabled = false, url = 'http://siem.test:8000', next_attempt_at = NULL`)
				}),
			}
		},
	})

	// services/housekeeping.py one_pass: the history trimmed
	register(Suite{
		Module: "jobs_housekeeping",
		Seed: func(w *World) {
			p := w.Insert(`INSERT INTO projects (name, description, created_at) VALUES ('hk folder', '', '2026-01-01') RETURNING id`)
			flow := w.Insert(`INSERT INTO flows (name, description, project_id, definition, engine, created_at, updated_at)
				VALUES ('hk flow', '', $1, '{}', 'duckdb', '2026-01-01', '2026-01-01') RETURNING id`, p)
			for v := 1; v <= 5; v++ {
				w.Exec(`INSERT INTO flow_versions (flow_id, version, definition, note, created_at) VALUES ($1, $2, '{}', '', '2026-01-01')`, flow, v)
			}
			sql := w.Insert(`INSERT INTO sql_scripts (name, description, project_id, current_version, created_at, updated_at)
				VALUES ('hk sql', '', $1, 2, '2026-01-01', '2026-01-01') RETURNING id`, p)
			rep := w.Insert(`INSERT INTO reports (name, description, project_id, current_version, created_at, updated_at)
				VALUES ('hk report', '', $1, 1, '2026-01-01', '2026-01-01') RETURNING id`, p)
			dash := w.Insert(`INSERT INTO dashboards (name, description, project_id, current_version, created_at, updated_at)
				VALUES ('hk dash', '', $1, 4, '2026-01-01', '2026-01-01') RETURNING id`, p)
			for v := 1; v <= 4; v++ {
				for _, t := range [][2]any{{"sql_script_versions", "script_id"}, {"report_versions", "report_id"}, {"dashboard_versions", "dashboard_id"}} {
					id := map[string]int64{"sql_script_versions": sql, "report_versions": rep, "dashboard_versions": dash}[t[0].(string)]
					w.Exec(`INSERT INTO `+t[0].(string)+` (`+t[1].(string)+`, version, content, note, created_at)
						VALUES ($1, $2, '', '', '2026-01-01')`, id, v)
				}
			}
			for _, day := range []string{"2026-01-01", "2026-01-31", "2026-02-01", "2026-02-28"} {
				w.Exec(`INSERT INTO report_views (report_id, user_id, day, at) VALUES ($1, $2, $3, $4)`, rep, w.People["editor"], day, day)
				w.Exec(`INSERT INTO dashboard_views (dashboard_id, user_id, day, at) VALUES ($1, $2, $3, $4)`, dash, w.People["editor"], day, day)
			}
			for i, notify := range []any{nil, "sent", "pending", "sending", nil, nil} {
				w.Exec(`INSERT INTO data_contract_results (datasource_id, contract_version, trigger, snapshot_key, outcome, blocked, errors,
					warnings, report, notify, evaluated_at) VALUES (987654, 1, 'refresh', 'k', 'ok', false, 0, 0, '{}', $1, $2)`,
					notify, "2026-01-0"+string(rune('1'+i)))
			}
			chat := func(updated string) {
				c := w.Insert(`INSERT INTO ai_chats (user_id, title, model_id, created_at, updated_at) VALUES ($1, 'hk', 'm', $2, $2)
					RETURNING id`, w.People["editor"], updated)
				w.Exec(`INSERT INTO ai_chat_turns (chat_id, seq, question, messages, model_id, input_tokens, output_tokens, requests,
					created_at) VALUES ($1, 1, 'q', '[]', 'm', 1, 1, 1, $2)`, c, updated)
			}
			chat("2025-10-01")
			chat("2026-02-20")
			for _, u := range []string{"2026-02-27 23:00:00", "2026-02-28 01:00:00"} {
				w.Exec(`INSERT INTO agent_drafts (user_id, origin, base_hash, name, engine, definition, counters, created_at, updated_at)
					VALUES ($1, 'chat', '', 'hk', 'duckdb', '{}', '{}', $2, $2)`, w.People["editor"], u)
			}
			plan := func(status, created, expires string) {
				w.Exec(`INSERT INTO agent_plans (user_id, origin, actions, summary, status, created_at, expires_at)
					VALUES ($1, 'chat', '[]', 'hk', $2, $3, $4)`, w.People["editor"], status, created, expires)
			}
			plan("pending", "2026-02-28", "2026-02-28 23:00:00")
			plan("pending", "2026-02-28", "2026-03-02")
			plan("applying", "2026-02-28", "2026-02-28 22:00:00")
			plan("applying", "2026-02-28", "2026-02-28 23:30:00")
			plan("applied", "2025-10-01", "2025-10-01")
			plan("cancelled", "2026-02-20", "2026-02-20")
			w.Exec(`INSERT INTO ai_comments (text, key, state, model, reason, written_at, expires_at) VALUES
				('t', 'hk-1', 'done', 'm', '', '2026-02-01', '2026-02-28'), ('t', 'hk-2', 'done', 'm', '', '2026-02-01', '2026-03-05')`)
			w.Exec(`INSERT INTO ai_comment_threads (thread, called_at, expires_at) VALUES
				('hk-a', '2026-02-01', '2026-03-01'), ('hk-b', '2026-02-01', '2026-03-01 00:00:01')`)
			run := func(kind, finished, detail, script string) {
				w.Exec(`INSERT INTO runs (kind, task_id, status, trigger_type, input_key, output_bucket, output_key, publish_description,
					publish_overwrite, publish_sort_keys, started_at, finished_at, error_detail, script, flow_id)
					VALUES ($1, '', 'SUCCESS', 'manual', '', '', '', '', false, '', '2025-12-01', $2, NULLIF($3, ''), NULLIF($4, ''), $5)`,
					kind, finished, detail, script, flow)
			}
			run("flow", "2025-12-01", "Traceback …", "")
			run("flow", "2026-02-27", "Traceback new", "")
			run("script", "2025-12-01", "", `{"log": "printed é\nmore", "exit_code": 0}`)
			run("container", "2025-12-01", "", `{"exit_code": 1, "log": "x", "log_expired": false}`)
			run("script", "2025-12-01", "", `{"log": "", "exit_code": 0}`)
			run("script", "2025-12-02", "", `{"log": "y", "attempts": [1, 2], "z": {"log": "inner"}}`)
		},
		Steps: func(w *World) []Step {
			tick := func(name, now string, env map[string]string, before func(*World)) Step {
				return Step{Name: name, Method: "TICK", Path: "housekeeping", Tick: "housekeeping", Now: now, Env: env, Before: before}
			}
			few := map[string]string{"ORCHESTRATOR__VERSIONS_KEPT": "2", "ORCHESTRATOR__CONTRACT_RESULTS_KEPT": "2",
				"ORCHESTRATOR__LOG_RETENTION_DAYS": "30", "ORCHESTRATOR__AI_CHAT_RETENTION_DAYS": "90"}
			return []Step{
				tick("everything kept (zeros)", "2026-03-01T00:00:00Z", map[string]string{"ORCHESTRATOR__VERSIONS_KEPT": "0",
					"ORCHESTRATOR__CONTRACT_RESULTS_KEPT": "0", "ORCHESTRATOR__LOG_RETENTION_DAYS": "0",
					"ORCHESTRATOR__AI_CHAT_RETENTION_DAYS": "0"}, nil),
				tick("a pass with few kept", "2026-03-01T00:00:00Z", few, nil),
				tick("again: nothing more", "2026-03-01T00:00:00Z", few, nil),
				tick("the views' window in Rome", "2026-03-01T23:30:00Z", few, func(w *World) {
					w.Exec(`INSERT INTO gateway_settings (key, value, updated_at) VALUES ('app.timezone', '"Europe/Rome"', now())`)
				}),
				tick("a run's script that is a list fails that kind only", "2026-03-02T00:00:00Z", few, func(w *World) {
					w.Exec(`DELETE FROM gateway_settings WHERE key = 'app.timezone'`)
					w.Exec(`INSERT INTO runs (kind, task_id, status, trigger_type, input_key, output_bucket, output_key, publish_description,
						publish_overwrite, publish_sort_keys, started_at, finished_at, script)
						VALUES ('script', '', 'SUCCESS', 'manual', '', '', '', '', false, '', '2025-12-01', '2025-12-01', '["log": "q"]'),
						('script', '', 'SUCCESS', 'manual', '', '', '', '', false, '', '2025-12-01', '2025-12-01', '[{"log": "q"}]')`)
				}),
			}
		},
	})
}
