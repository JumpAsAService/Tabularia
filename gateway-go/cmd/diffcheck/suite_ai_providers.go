package main

import (
	"net/http"
	"strconv"
	"time"
)

// the first provider the suite creates
var aipFirst int64

// routes/ai_providers.py: the providers from the panel. «siem.test» (each stack's fake
// engine) stands in for an OpenAI-compatible provider's catalog: no real provider is ever
// called.
func init() {
	maskColumns("ai_providers", "secret_encrypted") // Fernet: random on each side
	models := "^/v1/models$"
	register(Suite{
		Module: "ai_providers",
		Seed: func(w *World) {
			w.Token("admin", "tab_aip_admin_write_0000000", "write", time.Hour)
			var last int64
			w.a.db.QueryRow(w.ctx, `SELECT coalesce(max(id), 0) FROM ai_providers`).Scan(&last)
			aipFirst = last + 1
			w.Exec(`INSERT INTO ai_chats (user_id, title, model_id, kind, created_at, updated_at) VALUES
				($1, 'q', $2, 'assistant', '2026-01-01', '2026-01-01'), ($1, 'q2', $2, 'assistant', '2026-01-01', '2026-01-01')`,
				w.People["editor"], "provider:"+strconv.FormatInt(aipFirst, 10)+"/gpt-x")
			w.Exec(`INSERT INTO ai_models (model_id, enabled, updated_at) VALUES ($1, true, '2026-01-01'), ($2, false, '2026-01-01'),
				('gpt-env', true, '2026-01-01')`, "provider:"+strconv.FormatInt(aipFirst, 10)+"/gpt-x",
				"provider:"+strconv.FormatInt(aipFirst, 10)+"/old")
		},
		Steps: func(w *World) []Step {
			var s []Step
			admin := w.As("admin")
			first := strconv.FormatInt(aipFirst, 10)
			second := strconv.FormatInt(aipFirst+1, 10)
			add := func(name, method, path string, auth func(*http.Request), body any, rules []EngineRule) {
				s = append(s, Step{Name: name, Method: method, Path: path, Auth: auth, JSON: body, Engine: rules})
			}
			for _, p := range []string{"admin", "obs_flag", "editor"} {
				add("the providers · "+p, "GET", "/ai/providers", w.As(p), nil, nil)
			}
			add("the providers · nobody", "GET", "/ai/providers", nil, nil, nil)
			s = append(s, Step{Name: "a broken JSON from nobody", Method: "POST", Path: "/ai/providers", Raw: []byte("{"), ContentType: "application/json"})
			good := map[string]any{"name": " Local LLM ", "kind": "openai_compatible", "base_url": " http://siem.test:8000/v1/ ", "api_key": " sk-1 "}
			add("create · observer", "POST", "/ai/providers", w.As("obs_flag"), good, nil)
			add("create · bad body", "POST", "/ai/providers", admin, map[string]any{"name": "", "kind": "openai"}, nil)
			for _, c := range []map[string]any{
				{"name": "  ", "kind": "openai_compatible", "api_key": "k"},
				{"name": "x", "kind": "openai_compatible", "api_key": "  "},
				{"name": "x", "kind": "openai_compatible", "api_key": "k"},
				{"name": "x", "kind": "openai_compatible", "api_key": "k", "base_url": "ftp://h/v1"},
				{"name": "x", "kind": "openai_compatible", "api_key": "k", "base_url": "https://u:p@h/v1"},
				{"name": "x", "kind": "openai_compatible", "api_key": "k", "base_url": "https://h/v1?x=1"},
				{"name": "x", "kind": "openai_compatible", "api_key": "k", "base_url": "https://h/v1#f"},
				{"name": "x", "kind": "openai_compatible", "api_key": "k", "base_url": "https://[::1/v1"},
				{"name": "x", "kind": "openai_compatible", "api_key": "k", "base_url": "https:///v1"},
			} {
				add("create · refused", "POST", "/ai/providers", admin, c, nil)
			}
			add("create", "POST", "/ai/providers", admin, good, nil)
			add("create · the same name in capitals", "POST", "/ai/providers", admin, map[string]any{"name": "LOCAL llm", "kind": "anthropic", "api_key": "k"}, nil)
			add("create · Anthropic, default address", "POST", "/ai/providers", admin, map[string]any{"name": "Claude", "kind": "anthropic", "api_key": "sk-ant"}, nil)
			add("create · Anthropic, the /v1 its docs show", "POST", "/ai/providers", admin, map[string]any{"name": "Claude 2", "kind": "anthropic", "api_key": "k", "base_url": "https://api.anthropic.com/v1/"}, nil)
			add("create · a host only Python judges", "POST", "/ai/providers", admin, map[string]any{"name": "Ünï", "kind": "openai_compatible", "api_key": "k", "base_url": "https://ünï.example/v1"}, nil)
			add("the providers after · admin", "GET", "/ai/providers", admin, nil, nil)
			add("the providers after · observer", "GET", "/ai/providers", w.As("obs_flag"), nil, nil)
			p1 := "/ai/providers/" + first
			add("patch · observer", "PATCH", p1, w.As("obs_flag"), map[string]any{}, nil)
			add("patch · x and a bad body", "PATCH", "/ai/providers/x", admin, map[string]any{"name": ""}, nil)
			add("patch · unknown", "PATCH", "/ai/providers/9999", admin, map[string]any{}, nil)
			add("patch · nothing", "PATCH", p1, admin, map[string]any{}, nil)
			add("patch · the same name", "PATCH", p1, admin, map[string]any{"name": " Local LLM"}, nil)
			add("patch · a blank name", "PATCH", p1, admin, map[string]any{"name": " "}, nil)
			add("patch · a name taken", "PATCH", p1, admin, map[string]any{"name": "claude"}, nil)
			add("patch · a new address without the key", "PATCH", p1, admin, map[string]any{"base_url": "http://siem.test:8000/v2"}, nil)
			add("patch · the same address", "PATCH", p1, admin, map[string]any{"base_url": "http://siem.test:8000/v1/"}, nil)
			add("patch · a bad address", "PATCH", p1, admin, map[string]any{"base_url": "nope", "api_key": "k"}, nil)
			add("patch · name, key, off", "PATCH", p1, admin, map[string]any{"name": "Local", "api_key": "sk-2", "active": false}, nil)
			add("patch · on again", "PATCH", p1, admin, map[string]any{"active": true}, nil)
			add("patch · a token", "PATCH", p1, Bearer("tab_aip_admin_write_0000000"), map[string]any{"active": true}, nil)
			t := "/ai/providers/test"
			add("test · observer", "POST", t, w.As("obs_flag"), map[string]any{}, nil)
			add("test · bad body", "POST", t, admin, map[string]any{"key": 5, "kind": "x"}, nil)
			add("test · unknown key", "POST", t, admin, map[string]any{"key": "provider:9999"}, nil)
			add("test · the environment's, not set", "POST", t, admin, map[string]any{"key": "env"}, nil)
			add("test · a key that is no key", "POST", t, admin, map[string]any{"key": "nope"}, nil)
			add("test · no kind", "POST", t, admin, map[string]any{}, nil)
			add("test · no API key", "POST", t, admin, map[string]any{"kind": "openai_compatible", "base_url": "http://siem.test:8000/v1"}, nil)
			add("test · a bad address", "POST", t, admin, map[string]any{"kind": "openai_compatible", "base_url": "x"}, nil)
			add("test · saved, a new address, no key", "POST", t, admin, map[string]any{"key": "provider:" + first, "base_url": "http://siem.test:8000/v9"}, nil)
			add("test · saved", "POST", t, admin, map[string]any{"key": "provider:" + first}, []EngineRule{{Path: models,
				Body: map[string]any{"data": []any{map[string]any{"id": "b"}, map[string]any{"id": "a"}, map[string]any{"id": "a"}, map[string]any{"id": ""}, map[string]any{"id": 7}}}}})
			add("test · new, the key refused", "POST", t, admin, map[string]any{"kind": "openai_compatible", "base_url": "http://siem.test:8000/v1", "api_key": "k", "name": "Mine"},
				[]EngineRule{{Path: models, Status: 401, Body: map[string]any{"error": "bad key"}}})
			for _, st := range []int{400, 402, 403, 404, 413, 429, 500, 503, 529, 418, 302} {
				add("test · new, the provider answers "+strconv.Itoa(st), "POST", t, admin, map[string]any{"kind": "openai_compatible", "base_url": "http://siem.test:8000/v1", "api_key": "k"},
					[]EngineRule{{Path: models, Status: st, Body: map[string]any{}}})
			}
			for name, body := range map[string]any{"a list": []int{1}, "no data": map[string]any{"x": 1}, "data not a list": map[string]any{"data": "abc"},
				"items not objects": map[string]any{"data": []int{1}}, "not JSON": "<html>"} {
				add("test · new, the catalog is "+name, "POST", t, admin, map[string]any{"kind": "openai_compatible", "base_url": "http://siem.test:8000/v1", "api_key": "k"},
					[]EngineRule{{Path: models, Body: body}})
			}
			add("test · new, nobody there", "POST", t, admin, map[string]any{"kind": "openai_compatible", "base_url": "http://siem.test:9/v1", "api_key": "k"}, nil)
			add("test · saved, with a new key and address", "POST", t, admin, map[string]any{"key": "provider:" + first, "api_key": "k2", "base_url": "http://siem.test:8000/v1"}, nil)
			add("delete · observer", "DELETE", p1, w.As("obs_flag"), nil, nil)
			add("delete · x, confirm=maybe", "DELETE", "/ai/providers/x?confirm=maybe", admin, nil, nil)
			add("delete · unknown", "DELETE", "/ai/providers/9999", admin, nil, nil)
			add("delete · in use", "DELETE", p1, admin, nil, nil)
			add("delete · in use, confirmed", "DELETE", p1+"?confirm=yes", admin, nil, nil)
			add("delete · again", "DELETE", p1, admin, nil, nil)
			add("delete · unused", "DELETE", "/ai/providers/"+second, admin, nil, nil)
			add("the providers at the end", "GET", "/ai/providers", admin, nil, nil)
			return s
		},
	})
}
