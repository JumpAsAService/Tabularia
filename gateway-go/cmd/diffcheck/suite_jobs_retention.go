package main

// retention_loop (services/audit_retention.py, ip_retention.py) one round at a time: the
// rows past AUDIT__RETENTION_MINUTES, the addresses past AUDIT__IP_RETENTION_DAYS, in
// batches of 5000.
func init() {
	register(Suite{
		Module: "jobs_retention",
		Seed: func(w *World) {
			ev := func(ts, ip, agent string) {
				w.Exec(`INSERT INTO audit_logs (ts, actor_label, action, outcome, ip, user_agent)
					VALUES ($1, 'ret@x.test', 'ret.event', 'success', NULLIF($2, ''), NULLIF($3, ''))`, ts, ip, agent)
			}
			ev("2025-09-01 10:00:00", "10.0.0.1", "Firefox")
			ev("2025-09-01 11:00:00", "", "Chrome")
			ev("2025-09-01 12:00:00", "", "")
			ev("2025-12-01 09:00:00", "10.0.0.4", "Edge")
			ev("2026-01-31 22:00:00", "10.0.0.5", "Safari")
			ev("2026-01-31 23:30:00", "10.0.0.6", "curl")
			// more than one batch of old rows with an address
			w.Exec(`INSERT INTO audit_logs (ts, actor_label, action, outcome, ip, user_agent)
				SELECT timestamp '2025-08-01' + g * interval '1 second', 'bulk@x.test', 'ret.bulk', 'success', '10.9.0.1', 'bot'
				FROM generate_series(1, 12000) g`)
			w.Token("editor", "tab_ret_old_0000000000000", "read", 24*365*24*3600e9)
			w.Token("editor", "tab_ret_new_0000000000000", "read", 24*365*24*3600e9)
			w.Exec(`UPDATE api_tokens SET last_used_at = '2025-09-01', last_used_ip = '10.1.0.1' WHERE prefix = 'tab_ret_'
				AND name = 'script' AND token_hash = encode(sha256('tab_ret_old_0000000000000'::bytea), 'hex')`)
			w.Exec(`UPDATE api_tokens SET last_used_at = '2026-01-30', last_used_ip = '10.1.0.2'
				WHERE token_hash = encode(sha256('tab_ret_new_0000000000000'::bytea), 'hex')`)
			w.Exec(`INSERT INTO user_sessions (id, user_id, created_at, last_seen_at, expires_at, ip, user_agent, method) VALUES
				('ret-old', $1, '2025-08-01', '2025-09-01', '2025-09-02', '10.2.0.1', 'Edge', 'password'),
				('ret-old-agent', $1, '2025-08-01', '2025-09-01', '2025-09-02', NULL, 'Edge', 'password'),
				('ret-new', $1, '2026-01-01', '2026-01-30', '2026-03-01', '10.2.0.2', 'Edge', 'password')`, w.People["writer"])
			w.Exec(`UPDATE users SET last_seen_ip = '10.3.0.1' WHERE id = $1`, w.People["editor"])
		},
		Steps: func(w *World) []Step {
			at := "2026-02-01T00:00:00Z"
			tick := func(name, round, now string, env map[string]string, before func(*World)) Step {
				return Step{Name: name, Method: "TICK", Path: round, Tick: round, Now: now, Env: env, Before: before}
			}
			readdress := func(w *World) {
				w.Exec(`UPDATE audit_logs SET ip = '10.9.0.1' WHERE action = 'ret.bulk'`)
				w.Exec(`UPDATE users SET last_seen_ip = '10.3.0.2' WHERE id = $1`, w.People["editor"])
			}
			return []Step{
				tick("nothing configured: the loop has no work", "retention", at, map[string]string{"AUDIT__IP_RETENTION_DAYS": "0"}, nil),
				tick("the addresses past the default 90 days", "retention", at, nil, nil),
				tick("again: nothing left past them", "retention", at, nil, nil),
				tick("a round of the hour's rest: no addresses, nothing to delete", "retention.rows", at, nil, readdress),
				tick("addresses kept for 10 days, from a later moment", "retention", "2026-02-15T12:00:00Z",
					map[string]string{"AUDIT__IP_RETENTION_DAYS": "10"}, readdress),
				tick("rows past 60 minutes, without the addresses", "retention.rows", at,
					map[string]string{"AUDIT__RETENTION_MINUTES": "60", "AUDIT__IP_RETENTION_DAYS": "0"}, readdress),
				tick("rows past a day and addresses past a year, much later", "retention", "2026-08-01T06:30:00Z",
					map[string]string{"AUDIT__RETENTION_MINUTES": "1440", "AUDIT__IP_RETENTION_DAYS": "365"}, readdress),
			}
		},
	})
}
