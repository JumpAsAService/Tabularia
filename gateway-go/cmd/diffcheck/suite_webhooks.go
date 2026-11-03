package main

import (
	"net/http"
	"strconv"
	"time"
)

// the seed's ids: the first folder, the first webhook, the first delivery
var whFolder, whHook, whDelivery int64

// routes/webhooks.py: the subscriptions, their secrets, the pings and the deliveries.
func init() {
	maskColumns("webhooks", "secret_enc") // Fernet: random on each side
	// a delivery's body carries a random id and the moment it was queued
	payloadScrub := []string{`"id":"[0-9a-f-]{36}"`, `"created_at":"[^"]*"`}
	scrubColumn("webhook_deliveries", "payload", payloadScrub...)
	register(Suite{
		Module: "webhooks",
		Seed: func(w *World) {
			folder := w.Insert(`INSERT INTO projects (name, description, created_at) VALUES ('wh-Sales', '', '2026-01-01') RETURNING id`)
			whFolder = folder
			w.Token("admin", "tab_wh_admin_write_00000000", "write", time.Hour)
			w.Exec(`INSERT INTO projects (name, description, parent_id, created_at) VALUES ('wh-Sub', '', $1, '2026-01-01')`, folder)
			hook := w.Insert(`INSERT INTO webhooks (name, url, secret_enc, events, project_id, active, created_by, created_at)
				VALUES ('wh-Airflow', 'http://airflow.internal:8080/api/hook?k=1', 'x', '["flow.run.failed"]', $1, true, $2,
				'2026-02-01 10:00:00') RETURNING id`, folder, w.People["admin"])
			whHook = hook
			w.Exec(`INSERT INTO webhooks (name, url, secret_enc, events, project_id, active, created_by, created_at)
				VALUES ('wh-broken events', 'https://user:pw@Hooks.Example.com/x', 'x', 'not json', NULL, false, NULL, '2026-02-02')`)
			w.Exec(`INSERT INTO webhooks (name, url, secret_enc, events, project_id, active, created_by, created_at)
				VALUES ('wh-odd events', 'https://[::1]:9/x', 'x', '["flow.run.failed", 3, null, "x"]', NULL, true, NULL, '2026-02-03')`)
			for i, st := range []string{"delivered", "failed", "pending"} {
				var delivered any
				if st == "delivered" {
					delivered = "2026-02-05 12:00:00.123456"
				}
				id := w.Insert(`INSERT INTO webhook_deliveries (webhook_id, event, payload, status, attempts, next_attempt_at, created_at,
					delivered_at, last_status_code, last_error) VALUES ($1, 'flow.run.failed', $2, $3, $4, '2026-02-05', $5, $6, $7, $8)
					RETURNING id`,
					hook, `{"id":"0f0e0d0c-0b0a-4908-8706-050403020100","event":"flow.run.failed","created_at":"2026-02-05T12:00:00Z","data":{"run":{"id":`+strconv.Itoa(i)+`,"error":"boom é","duration_seconds":1.0}}}`,
					st, i+1, time.Date(2026, 2, 5, 12, i, 0, 0, time.UTC), delivered, 500+i, "response 500: nope from 10.0.0.9")
				if i == 0 {
					whDelivery = id
				}
			}
			w.Exec(`INSERT INTO webhook_deliveries (webhook_id, event, payload, status, attempts, next_attempt_at, created_at)
				VALUES ($1, 'ping', '[1, 2]', 'failed', 6, '2026-02-06', '2026-02-06')`, hook)
			w.Exec(`INSERT INTO webhook_deliveries (webhook_id, event, payload, status, attempts, next_attempt_at, created_at)
				VALUES (999999, 'ping', 'not json', 'failed', 6, '2026-02-06', '2026-02-06')`)
		},
		Steps: func(w *World) []Step {
			var s []Step
			admin := w.As("admin")
			id := func(base, plus int64) string { return strconv.FormatInt(base+plus, 10) }
			hook, created, idna := id(whHook, 0), id(whHook, 3), id(whHook, 5)
			folder, sub := id(whFolder, 0), id(whFolder, 1)
			_, _, _ = created, folder, sub
			add := func(name, method, path string, auth func(*http.Request), body any) {
				st := Step{Name: name, Method: method, Path: path, Auth: auth, JSON: body,
					Mask: []string{"secret"}, Scrub: payloadScrub}
				s = append(s, st)
			}
			for _, p := range []string{"admin", "obs_flag", "editor"} {
				add("the events · "+p, "GET", "/webhooks/events", w.As(p), nil)
				add("the webhooks · "+p, "GET", "/webhooks", w.As(p), nil)
			}
			add("the webhooks · nobody", "GET", "/webhooks", nil, nil)
			s = append(s,
				Step{Name: "a broken JSON from nobody", Method: "POST", Path: "/webhooks", Raw: []byte(`{"name"`), ContentType: "application/json"},
				Step{Name: "no body", Method: "POST", Path: "/webhooks", Auth: admin},
				Step{Name: "PATCH, a broken JSON from nobody", Method: "PATCH", Path: "/webhooks/" + hook, Raw: []byte(`[`), ContentType: "application/json"},
			)
			good := map[string]any{"name": " wh-New ", "url": " https://Receiver.Example.com:8443/in ", "events": []string{"flow.run.succeeded", "flow.run.failed", "flow.run.succeeded"}}
			add("create · nobody", "POST", "/webhooks", nil, good)
			add("create · an observer", "POST", "/webhooks", w.As("obs_flag"), good)
			add("create · editor", "POST", "/webhooks", w.As("editor"), good)
			add("create · bad types", "POST", "/webhooks", admin, map[string]any{"name": 1, "events": "flow.run.failed", "active": "maybe"})
			add("create · missing fields", "POST", "/webhooks", admin, map[string]any{})
			for _, c := range []map[string]any{
				{"name": "  ", "url": "ftp://x", "events": []string{"nope"}},
				{"name": repeat("n", 121), "url": "https://x.test", "events": []string{"ping"}},
				{"name": "x", "url": "ftp://x", "events": []string{"nope"}, "project_id": 999999},
				{"name": "x", "url": "https://x.test", "events": []string{"nope"}, "project_id": 999999},
				{"name": "x", "url": "https://x.test", "events": []string{}},
				{"name": "x", "url": "https://x.test", "events": []string{"flow.run.failed", "ping"}},
				{"name": "x", "url": "https://x.test", "events": []string{"flow.run.failed"}, "project_id": 999999},
				{"name": "x", "url": "https://x.test", "events": []string{"flow.run.failed"}, "project_id": 99999999999},
				{"name": "x", "url": "http://a..b/", "events": []string{"flow.run.failed"}},
				{"name": "x", "url": "https://[::1", "events": []string{"flow.run.failed"}},
				{"name": "x", "url": "http://x.test:0/", "events": []string{"flow.run.failed"}},
			} {
				add("create · refused", "POST", "/webhooks", admin, c)
			}
			add("create", "POST", "/webhooks", admin, good)
			add("create · in a folder, off", "POST", "/webhooks", admin, map[string]any{"name": "wh-Folder", "url": "http://10.1.2.3/hook",
				"events": []string{"datasource.refresh.failed"}, "project_id": whFolder, "active": false})
			add("create · a host only Python judges", "POST", "/webhooks", admin, map[string]any{"name": "wh-IDNA", "url": "https://bücher.example/h",
				"events": []string{"flow.run.failed"}})
			add("the webhooks after · admin", "GET", "/webhooks", admin, nil)
			add("the webhooks after · observer (an IDNA host: Python)", "GET", "/webhooks", w.As("obs_flag"), nil)
			add("PATCH the IDNA one away", "PATCH", "/webhooks/"+idna, admin, map[string]any{"url": "https://books.example/h"})
			add("the webhooks after · observer", "GET", "/webhooks", w.As("obs_flag"), nil)
			patch := func(name, id string, auth func(*http.Request), body any) {
				add("patch · "+name, "PATCH", "/webhooks/"+id, auth, body)
			}
			patch("nobody", hook, nil, map[string]any{"active": false})
			patch("observer", hook, w.As("obs_flag"), map[string]any{"active": false})
			patch("not a number, and a bad body", "x", admin, map[string]any{"active": "maybe"})
			patch("unknown", "999", admin, map[string]any{"active": false})
			patch("past the column", "99999999999", admin, map[string]any{})
			patch("nothing", hook, admin, map[string]any{})
			patch("name", hook, admin, map[string]any{"name": " wh-Airflow 2 "})
			patch("a bad name", hook, admin, map[string]any{"name": " "})
			patch("a bad url after a good name: nothing saved", hook, admin, map[string]any{"name": "wh-Z", "url": "nope"})
			patch("events", hook, admin, map[string]any{"events": []string{"datasource.refresh.succeeded", "flow.run.failed"}})
			patch("bad events", hook, admin, map[string]any{"events": []string{"x"}})
			patch("a folder that is not there", hook, admin, map[string]any{"project_id": 4242})
			patch("the sub-folder", hook, admin, map[string]any{"project_id": whFolder + 1})
			patch("clear the folder (wins over project_id)", hook, admin, map[string]any{"project_id": whFolder, "clear_project": true})
			patch("off, new address", hook, admin, map[string]any{"active": false, "url": "http://airflow.internal/hook2"})
			patch("on again", hook, Bearer("tab_wh_admin_write_00000000"), map[string]any{"active": true})
			for _, p := range []string{"admin", "obs_flag"} {
				add("deliveries · "+p, "GET", "/webhooks/"+hook+"/deliveries", w.As(p), nil)
			}
			for _, q := range []string{"?limit=2", "?limit=0", "?limit=201", "?limit=x"} {
				add("deliveries "+q, "GET", "/webhooks/"+hook+"/deliveries"+q, admin, nil)
			}
			add("deliveries · unknown", "GET", "/webhooks/77/deliveries", admin, nil)
			add("deliveries · not a number, bad limit", "GET", "/webhooks/x/deliveries?limit=0", admin, nil)
			add("deliveries · editor", "GET", "/webhooks/"+hook+"/deliveries", w.As("editor"), nil)
			add("ping · observer", "POST", "/webhooks/"+hook+"/test", w.As("obs_flag"), nil)
			add("ping · unknown", "POST", "/webhooks/404/test", admin, nil)
			add("ping · x", "POST", "/webhooks/x/test", admin, nil)
			add("ping", "POST", "/webhooks/"+hook+"/test", admin, nil)
			add("redeliver · observer", "POST", "/webhooks/deliveries/"+id(whDelivery, 0)+"/redeliver", w.As("obs_flag"), nil)
			add("redeliver · unknown", "POST", "/webhooks/deliveries/4040/redeliver", admin, nil)
			add("redeliver · x", "POST", "/webhooks/deliveries/x/redeliver", admin, nil)
			add("redeliver", "POST", "/webhooks/deliveries/"+id(whDelivery, 0)+"/redeliver", admin, nil)
			add("redeliver · a payload that is a list", "POST", "/webhooks/deliveries/"+id(whDelivery, 3)+"/redeliver", admin, nil)
			add("redeliver · a payload that is not JSON, of a webhook gone", "POST", "/webhooks/deliveries/"+id(whDelivery, 4)+"/redeliver", admin, nil)
			add("deliveries after", "GET", "/webhooks/"+hook+"/deliveries", admin, nil)
			add("the webhooks: the last delivery", "GET", "/webhooks", admin, nil)
			add("rotate · observer", "POST", "/webhooks/"+hook+"/rotate-secret", w.As("obs_flag"), nil)
			add("rotate · unknown", "POST", "/webhooks/12/rotate-secret", admin, nil)
			add("rotate", "POST", "/webhooks/"+hook+"/rotate-secret", admin, nil)
			add("delete · observer", "DELETE", "/webhooks/"+hook, w.As("obs_flag"), nil)
			add("delete · unknown", "DELETE", "/webhooks/12", admin, nil)
			add("delete · x", "DELETE", "/webhooks/x", admin, nil)
			add("delete", "DELETE", "/webhooks/"+hook, admin, nil)
			add("delete again", "DELETE", "/webhooks/"+hook, admin, nil)
			add("the webhooks at the end", "GET", "/webhooks", admin, nil)
			add("a PATCH on /webhooks/events: Python's", "PATCH", "/webhooks/events", admin, map[string]any{})
			return s
		},
	})
}
