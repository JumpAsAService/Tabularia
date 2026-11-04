package main

import (
	"net/http"
	"strconv"
	"time"
)

// the seed's first plan
var apFirst int64

// routes/agent_plans.py: a plan read and cancelled by its owner. Confirming stays with the
// Python AI service: those steps go through the Go gateway to Python.
func init() {
	register(Suite{
		Module: "agent_plans",
		Seed: func(w *World) {
			w.Token("writer", "tab_ap_writer_write_0000000", "write", time.Hour)
			w.Token("writer", "tab_ap_writer_read_00000000", "read", time.Hour)
			now := time.Now().UTC()
			plan := func(user, origin, status, actions string, result *string, expires time.Time) int64 {
				return w.Insert(`INSERT INTO agent_plans (user_id, origin, chat_id, token_name, actions, summary, status, result,
					created_at, expires_at, applied_at) VALUES ($1, $2, NULL, $3, $4, '1. Save the flow «Sales»', $5, $6,
					'2026-03-01 10:00:00', $7, NULL) RETURNING id`, w.People[user], origin, map[bool]any{true: "script", false: nil}[origin == "mcp"],
					actions, status, result, expires)
			}
			actions := `[{"kind": "save_flow", "words": "Save the flow «Sales»", "name": "Sales", "definition": {"nodes": []},
				"base_hash": "abc", "key": "k1", "diff": ["+x"], "diff_lines": 3, "n": 99, "ratio": 1.5},
				{"kind": "run_flow", "words": "Run it", "planned_action": 1}]`
			apFirst = plan("writer", "chat", "pending", actions, nil, now.Add(20*time.Minute))
			plan("writer", "mcp", "pending", actions, nil, now.Add(-time.Minute))
			plan("writer", "mcp", "applied", actions, ptr(`[{"ok": true, "link": "/flows/3", "object": {"id": 3}}, {"ok": false, "error": "x"}]`), now.Add(time.Hour))
			plan("editor", "chat", "pending", actions, nil, now.Add(time.Hour))
			plan("writer", "mcp", "failed", `not json`, ptr(`not json`), now.Add(time.Hour))
			plan("writer", "mcp", "cancelled", `{"a": 1}`, ptr(`null`), now.Add(time.Hour))
			plan("writer", "mcp", "pending", `[]`, nil, now.Add(time.Hour))
			plan("writer", "mcp", "pending", `[1]`, nil, now.Add(time.Hour))
			plan("writer", "mcp", "applied", `[]`, ptr(`{"ok": true}`), now.Add(time.Hour))
			plan("writer", "mcp", "pending", `[{"kind": "save_flow", "words": "w", "diff_lines": ["+a", "-b"], "key": "k"}]`, nil, now.Add(time.Hour))
		},
		Steps: func(w *World) []Step {
			var s []Step
			id := func(n int64) string { return strconv.FormatInt(apFirst+n, 10) }
			add := func(name, method, path string, auth func(*http.Request), body any) {
				s = append(s, Step{Name: name, Method: method, Path: path, Auth: auth, JSON: body})
			}
			writer := w.As("writer")
			for i := int64(0); i < 9; i++ {
				add("read plan +"+strconv.FormatInt(i, 10), "GET", "/agent/plans/"+id(i), writer, nil)
			}
			add("someone else's plan", "GET", "/agent/plans/"+id(3), writer, nil)
			add("an administrator reading it", "GET", "/agent/plans/"+id(0), w.As("admin"), nil)
			add("nobody", "GET", "/agent/plans/"+id(0), nil, nil)
			add("not a number", "GET", "/agent/plans/x", writer, nil)
			add("unknown", "GET", "/agent/plans/999999", writer, nil)
			add("past the column", "GET", "/agent/plans/99999999999", writer, nil)
			add("with a read token", "GET", "/agent/plans/"+id(0), Bearer("tab_ap_writer_read_00000000"), nil)
			add("cancel · nobody", "POST", "/agent/plans/"+id(0)+"/cancel", nil, nil)
			add("cancel · a read token", "POST", "/agent/plans/"+id(0)+"/cancel", Bearer("tab_ap_writer_read_00000000"), nil)
			add("cancel · someone else's", "POST", "/agent/plans/"+id(3)+"/cancel", writer, nil)
			add("cancel · x", "POST", "/agent/plans/x/cancel", writer, nil)
			add("cancel · expired", "POST", "/agent/plans/"+id(1)+"/cancel", writer, nil)
			add("cancel · applied", "POST", "/agent/plans/"+id(2)+"/cancel", writer, nil)
			add("cancel", "POST", "/agent/plans/"+id(0)+"/cancel", writer, nil)
			add("cancel · a plan whose changes are listed", "POST", "/agent/plans/"+id(9)+"/cancel", writer, nil)
			add("cancel · again", "POST", "/agent/plans/"+id(0)+"/cancel", writer, nil)
			add("cancel · with a write token", "POST", "/agent/plans/"+id(7)+"/cancel", Bearer("tab_ap_writer_write_0000000"), nil)
			// confirming: Python's, through the Go gateway
			add("confirm · a chat's plan with a token", "POST", "/agent/plans/"+id(3)+"/confirm", Bearer("tab_ap_writer_write_0000000"), nil)
			add("confirm · cancelled", "POST", "/agent/plans/"+id(0)+"/confirm", writer, map[string]any{})
			add("confirm · a stale revision", "POST", "/agent/plans/"+id(6)+"/confirm", writer, map[string]any{"revision": "0000"})
			add("confirm · an empty plan", "POST", "/agent/plans/"+id(6)+"/confirm", writer, nil)
			add("read it after", "GET", "/agent/plans/"+id(6), writer, nil)
			return s
		},
	})
}
