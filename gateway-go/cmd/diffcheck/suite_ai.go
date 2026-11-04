package main

import (
	"net/http"
	"strconv"
	"time"
)

// routes/ai.py, the part answered in Go: the assistant's state and the agents' instructions.
// The models' catalogs, the chat and the conversations stay with Python's AI service.
func init() {
	register(Suite{
		Module: "ai",
		Seed: func(w *World) {
			var last int64
			w.a.db.QueryRow(w.ctx, `SELECT coalesce(max(id), 0) FROM ai_providers`).Scan(&last)
			on, off := last+1, last+2
			w.Exec(`INSERT INTO ai_providers (name, kind, base_url, secret_encrypted, active, created_at, updated_at)
				VALUES ('ai-Zeta', 'openai_compatible', 'http://z.test/v1', 'x', true, '2026-01-01', '2026-01-01'),
				('ai-Off', 'anthropic', 'https://api.anthropic.com', 'x', false, '2026-01-01', '2026-01-01'),
				('ai-Alpha', 'openai_compatible', 'http://a.test/v1', 'x', true, '2026-01-01', '2026-01-01')`)
			alpha := strconv.FormatInt(last+3, 10)
			ref := func(id int64, m string) string { return "provider:" + strconv.FormatInt(id, 10) + "/" + m }
			for _, m := range []struct {
				ref string
				on  bool
			}{
				{ref(on, "gpt-x"), true}, {ref(on, "text-embedding-3"), true}, {ref(on, "b-model"), true}, {ref(off, "claude"), true},
				{"provider:" + alpha + "/gpt-x", true}, {"provider:" + alpha + "/a-model", true}, {ref(on, "disabled"), false},
				{"env-model", true}, {"provider:99999/ghost", true},
			} {
				w.Exec(`INSERT INTO ai_models (model_id, enabled, updated_at) VALUES ($1, $2, '2026-01-01')`, m.ref, m.on)
			}
			w.Token("writer", "tab_ai_writer_read_00000000", "read", time.Hour)
		},
		Phases: []Phase{
			{Name: "environment's settings"},
			{Name: "panel: a default model by name, metadata-only for the chat", Panel: map[string]string{
				"ai.default_model": `" gpt-x "`, "ai.metadata_only": "true", "ai.max_result_rows": "77"}},
			{Name: "panel: a comment model that is not enabled, MCP metadata-only", Panel: map[string]string{
				"ai.comment_model": `"nope"`, "mcp.metadata_only": "true", "ai.default_model": `"a-model"`}},
			{Name: "panel: a comment model by reference", Panel: map[string]string{
				"ai.comment_model": `"gpt-x"`, "ai.default_model": `"b-model"`}},
		},
		Steps: func(w *World) []Step {
			var s []Step
			add := func(name, method, path string, auth func(*http.Request), body any) {
				s = append(s, Step{Name: name, Method: method, Path: path, Auth: auth, JSON: body})
			}
			for _, p := range []string{"admin", "editor", "obs_flag", "inactive"} {
				add("status · "+p, "GET", "/ai/status", w.As(p), nil)
			}
			add("status · nobody", "GET", "/ai/status", nil, nil)
			add("status · a read token", "GET", "/ai/status", Bearer("tab_ai_writer_read_00000000"), nil)
			for _, q := range []string{"", "?locale=it", "?locale=IT-it", "?locale=de-CH", "?locale=xx", "?locale=fr&locale=es",
				"?locale=" + "esxxxxxxxxxxxxxxxx", "?locale="} {
				add("prompts "+q, "GET", "/ai/prompts"+q, w.As("admin"), nil)
			}
			add("prompts · observer", "GET", "/ai/prompts?locale=it", w.As("obs_group"), nil)
			add("prompts · editor", "GET", "/ai/prompts", w.As("editor"), nil)
			add("prompts · nobody", "GET", "/ai/prompts", nil, nil)
			s = append(s, Step{Name: "a broken JSON from nobody", Method: "PUT", Path: "/ai/prompts/flow", Raw: []byte("{"), ContentType: "application/json"})
			add("set · observer", "PUT", "/ai/prompts/flow", w.As("obs_flag"), map[string]any{"text": "x"})
			add("set · unknown agent", "PUT", "/ai/prompts/nope", w.As("admin"), map[string]any{"text": "x"})
			add("set · unknown agent and a bad body", "PUT", "/ai/prompts/nope", w.As("admin"), map[string]any{"text": ""})
			add("set · blank", "PUT", "/ai/prompts/flow", w.As("admin"), map[string]any{"text": "   "})
			add("set · too long", "PUT", "/ai/prompts/flow", w.As("admin"), map[string]any{"text": repeat("x", 20001)})
			add("set", "PUT", "/ai/prompts/flow?locale=it", w.As("admin"), map[string]any{"text": "  Be brief, è tutto.  "})
			add("set again", "PUT", "/ai/prompts/comment", w.As("admin_group"), map[string]any{"text": "Say little."})
			add("prompts after · observer", "GET", "/ai/prompts", w.As("obs_flag"), nil)
			add("prompts after · admin", "GET", "/ai/prompts", w.As("admin"), nil)
			add("reset · unknown", "DELETE", "/ai/prompts/nope", w.As("admin"), nil)
			add("reset · observer", "DELETE", "/ai/prompts/flow", w.As("obs_flag"), nil)
			add("reset", "DELETE", "/ai/prompts/flow?locale=de", w.As("admin"), nil)
			add("reset again: nothing to record", "DELETE", "/ai/prompts/flow", w.As("admin"), nil)
			add("reset the other", "DELETE", "/ai/prompts/comment", w.As("admin"), nil)
			// forwarded: the models' catalogs are Python's
			add("models · forwarded", "GET", "/ai/models?provider=env", w.As("admin"), nil)
			return s
		},
	})
}
