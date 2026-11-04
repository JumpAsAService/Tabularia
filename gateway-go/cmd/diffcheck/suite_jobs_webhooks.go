package main

import "fmt"

// webhooks_loop (services/webhooks.py deliver_due): the deliveries taken, sent to the receiver
// (the fake engine, as siem.test), their outcome written, retried, failed.
func init() {
	register(Suite{
		Module: "jobs_webhooks",
		Seed: func(w *World) {
			// the queue is this suite's: what other suites left waiting goes
			w.Exec(`DELETE FROM webhook_deliveries WHERE status IN ('pending', 'sending')`)
			hook := func(name, url string, active bool) int64 {
				return w.Insert(`INSERT INTO webhooks (name, url, secret_enc, events, active, created_at)
					VALUES ($1, $2, $3, '["flow.run.succeeded"]', $4, '2026-01-01') RETURNING id`, name, url, encrypted("whsec_"+name), active)
			}
			ok := hook("wh-ok", "http://siem.test:8000/hook/ok", true)
			bad := hook("wh-bad", "http://siem.test:8000/hook/bad", true)
			off := hook("wh-off", "http://siem.test:8000/hook/off", false)
			loop := hook("wh-loop", "http://127.0.0.1:9/hook", true)
			lost := hook("wh-lost", "http://nowhere.invalid/hook", true)
			delivery := func(hook int64, id, next string, attempts int, status, claimed string) {
				w.Exec(`INSERT INTO webhook_deliveries (webhook_id, event, payload, status, attempts, next_attempt_at, created_at, claimed_at)
					VALUES ($1, 'flow.run.succeeded', $2, $3, $4, $5, '2026-01-01', NULLIF($6, '')::timestamp)`, hook,
					fmt.Sprintf(`{"id": "%s", "event": "flow.run.succeeded", "data": {"run": 1, "name": "Vendite è"}}`, id),
					status, attempts, next, claimed)
			}
			delivery(ok, "d-ok-1", "2026-02-01 00:00:00", 0, "pending", "")
			delivery(ok, "d-ok-2", "2026-02-01 00:00:00", 0, "pending", "")
			delivery(ok, "d-ok-3", "2026-01-31 23:59:00", 0, "pending", "")
			delivery(ok, "d-later", "2026-02-01 00:10:00", 0, "pending", "")
			delivery(bad, "d-bad", "2026-02-01 00:00:00", 0, "pending", "")
			delivery(bad, "d-last", "2026-02-01 00:00:00", 5, "pending", "")
			delivery(off, "d-off", "2026-02-01 00:00:00", 0, "pending", "")
			delivery(loop, "d-loop", "2026-02-01 00:00:00", 0, "pending", "")
			delivery(lost, "d-lost", "2026-02-01 00:00:00", 0, "pending", "")
			delivery(ok, "d-stuck", "2026-01-31 23:00:00", 1, "sending", "2026-01-31 23:50:00")
			delivery(ok, "d-taken", "2026-01-31 23:00:00", 1, "sending", "2026-01-31 23:58:00")
			w.Exec(`INSERT INTO webhook_deliveries (webhook_id, event, payload, status, attempts, next_attempt_at, created_at)
				VALUES ($1, 'ping', 'not json', 'pending', 0, '2026-02-01', '2026-01-01'),
				($1, 'ping', '{"no": "id"}', 'pending', 0, '2026-02-01', '2026-01-01')`, ok)
		},
		Steps: func(w *World) []Step {
			receivers := func(ok, bad int, badBody string) []EngineRule {
				return []EngineRule{
					{Method: "POST", Path: "^/hook/ok$", Status: ok, Body: map[string]any{"thanks": true}},
					{Method: "POST", Path: "^/hook/bad$", Status: bad, Text: badBody, Type: "text/plain"},
				}
			}
			tick := func(name, now string, engine []EngineRule, before func(*World)) Step {
				return Step{Name: name, Method: "TICK", Path: "webhooks", Tick: "webhooks", Now: now, Engine: engine, Before: before,
					EngineUnordered: true}
			}
			long := ""
			for range 40 {
				long += "0123456789"
			}
			return []Step{
				tick("the due ones sent, two per receiver at a time", "2026-02-01T00:05:00Z", receivers(200, 500, "down"), nil),
				tick("nothing due yet", "2026-02-01T00:05:10Z", receivers(200, 500, "down"), nil),
				tick("the retries after 30 s, and a later one", "2026-02-01T00:10:00Z", receivers(204, 404, long), nil),
				tick("a 3xx is a failure too", "2026-02-01T00:13:00Z", receivers(200, 302, ""), nil),
				tick("the receiver back", "2026-02-01T00:30:00Z", receivers(200, 200, "ok"), nil),
				tick("a webhook removed meanwhile", "2026-02-01T01:00:00Z", receivers(200, 200, "ok"), func(w *World) {
					w.Exec(`UPDATE webhook_deliveries SET status = 'pending', next_attempt_at = '2026-02-01 00:59:00'
						WHERE payload LIKE '%d-ok-1%'`)
					w.Exec(`UPDATE webhooks SET url = 'http://siem.test:8000/hook/ok?x=1' WHERE name = 'wh-ok'`)
					w.Exec(`INSERT INTO webhook_deliveries (webhook_id, event, payload, status, attempts, next_attempt_at, created_at)
						VALUES (999999, 'ping', '{"id": "d-orphan"}', 'pending', 0, '2026-02-01', '2026-01-01')`)
				}),
			}
		},
	})
}
