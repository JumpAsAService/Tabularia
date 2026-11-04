package main

import (
	"net/http"
	"time"
)

// Addresses whose host is not ASCII, and MCP bodies that are not UTF-8: once handed to the
// Python gateway, now read in Go — urlsplit's NFKC check, str.lower of a hostname, MCP's
// json.loads of bytes — and with the AI service (APP__ROLE=ai) for httpx's IDNA encoding
// (/internal/python/idna-encode) and an Anthropic provider's catalog (its SDK). Named after
// «webhooks» and «siem» so that it runs after them: it changes the SIEM's address and leaves
// webhooks and providers of its own.
func init() {
	hosts := []string{
		"http://BÜCHER.example/", "https://bücher.example/h", "http://İstanbul.test/", "http://ΟΔΟΣ.test/x", "http://ΑΣ.ΣΑ/",
		"http://a／b.test/", "http://℀.test/", "http://ｅｘａｍｐｌｅ.com/", "http://ü:8080/", "https://user:pä@host.test/",
		"http://☃.net/", "http://ß.de/", "http://a‍b.test/", "http://日本語.jp/", "http://-ü.test/", "http://ü..test/",
		"http://[ü]/", "http://ü.test:99999/", "http://Ǆ.test/",
	}
	register(Suite{
		Module: "webhooks_idna",
		Seed: func(w *World) {
			w.Token("admin", "tab_whidna_admin_write_0000", "write", time.Hour)
		},
		Steps: func(w *World) []Step {
			var s []Step
			admin := w.As("admin")
			scrub := []string{`"id":"[0-9a-f-]{36}"`, `"created_at":"[^"]*"`}
			add := func(name, method, path string, auth func(*http.Request), body any) {
				s = append(s, Step{Name: name, Method: method, Path: path, Auth: auth, JSON: body, Mask: []string{"secret", "*.secret"}, Scrub: scrub})
			}
			// the webhooks: validate_url (urlsplit, then httpx with IDNA), the audit's host
			for _, u := range hosts {
				add("webhook · "+u, "POST", "/webhooks", admin, map[string]any{"name": "idna " + u, "url": u, "events": []string{"flow.run.failed"}})
			}
			add("the webhooks · admin", "GET", "/webhooks", admin, nil)
			add("the webhooks · observer: the hosts lower-cased", "GET", "/webhooks", w.As("obs_flag"), nil)
			// the SIEM: the address and its endpoint validated, the observer's scheme and host
			put := func(name string, body any) {
				s = append(s, Step{Name: name, Method: "PUT", Path: "/admin/siem", Auth: admin, JSON: body})
			}
			for _, u := range []string{"http://☃.net/", "http://a‍b.test/", "http://a／b.test/", "http://ü.test:99999/"} {
				put("siem · refused "+u, map[string]any{"url": u})
			}
			for _, u := range []string{"http://ΟΔΟΣ.test:8088", "https://BÜCHER.example/services/collector", "http://İstanbul.test/proxy?x=ü"} {
				put("siem · "+u, map[string]any{"url": u, "token": "t0k"})
				s = append(s, Step{Name: "siem read · observer · " + u, Method: "GET", Path: "/admin/siem", Auth: w.As("obs_flag")},
					Step{Name: "siem read · admin · " + u, Method: "GET", Path: "/admin/siem", Auth: admin})
			}
			// a send to a host that is not ASCII: encoded, then not resolved
			s = append(s, Step{Name: "siem try · a host that is not ASCII", Method: "POST", Path: "/admin/siem/test", Auth: admin})
			put("siem · back to no address", map[string]any{"url": "", "token": ""})
			// the providers: normalize_base_url (urlsplit only)
			for _, u := range []string{"https://ünï.example/v1", "https://ÜNÏ.example/v1/", "https://a／b.test/v1", "https://ΟΔΟΣ.test/v1"} {
				add("provider · "+u, "POST", "/ai/providers", admin, map[string]any{"name": "idna " + u, "kind": "openai_compatible", "api_key": "k", "base_url": u})
			}
			add("the providers · admin", "GET", "/ai/providers", admin, nil)
			add("the providers · observer", "GET", "/ai/providers", w.As("obs_flag"), nil)
			// an Anthropic provider's test: its SDK, in the AI service; the fake engine as Anthropic
			models := "^/v1/models$"
			anthropic := map[string]any{"kind": "anthropic", "base_url": "http://siem.test:8000", "api_key": "sk-ant-test"}
			try := func(name string, body map[string]any, rules []EngineRule) {
				s = append(s, Step{Name: "anthropic test · " + name, Method: "POST", Path: "/ai/providers/test", Auth: admin, JSON: body, Engine: rules})
			}
			model := func(id string) map[string]any {
				return map[string]any{"id": id, "type": "model", "display_name": id, "created_at": "2026-01-01T00:00:00Z"}
			}
			try("the catalog", anthropic, []EngineRule{{Method: "GET", Path: models, Body: map[string]any{
				"data": []any{model("claude-b"), model("claude-a"), model("claude-b")}, "has_more": false, "first_id": "claude-b", "last_id": "claude-b"}}})
			try("the key refused", anthropic, []EngineRule{{Method: "GET", Path: models, Status: 401,
				Body: map[string]any{"type": "error", "error": map[string]any{"type": "authentication_error", "message": "invalid x-api-key"}}}})
			try("not found", anthropic, []EngineRule{{Method: "GET", Path: models, Status: 404, Body: map[string]any{"type": "error"}}})
			try("the /v1 its docs show", map[string]any{"kind": "anthropic", "base_url": "http://siem.test:8000/v1/", "api_key": "k"},
				[]EngineRule{{Method: "GET", Path: models, Body: map[string]any{"data": []any{}, "has_more": false}}})
			try("a host that is not ASCII", map[string]any{"kind": "anthropic", "base_url": "http://ünï.test", "api_key": "k"}, nil)
			s = append(s, Step{Name: "anthropic test · an observer's session", Method: "POST", Path: "/ai/providers/test", Auth: w.As("obs_flag"), JSON: anthropic})
			return s
		},
	})
	register(Suite{
		Module: "mcp_encodings",
		Seed: func(w *World) {
			w.Token("writer", "tab_mcpenc_writer_write_000", "write", time.Hour)
		},
		Steps: func(w *World) []Step {
			var s []Step
			write := Bearer("tab_mcpenc_writer_write_000")
			raw := func(name, body string) {
				s = append(s, Step{Name: "body · " + name, Method: "POST", Path: "/mcp", Auth: write, Raw: []byte(body), ContentType: "application/json"})
			}
			ping := `{"jsonrpc":"2.0","id":"é","method":"ping"}`
			utf16 := func(text string, little, bom bool) string {
				var out []byte
				if bom {
					if little {
						out = append(out, 0xff, 0xfe)
					} else {
						out = append(out, 0xfe, 0xff)
					}
				}
				for _, r := range text {
					if r > 0xffff {
						r -= 0x10000
						hi, lo := 0xd800+(r>>10), 0xdc00+(r&0x3ff)
						for _, u := range []rune{hi, lo} {
							if little {
								out = append(out, byte(u), byte(u>>8))
							} else {
								out = append(out, byte(u>>8), byte(u))
							}
						}
						continue
					}
					if little {
						out = append(out, byte(r), byte(r>>8))
					} else {
						out = append(out, byte(r>>8), byte(r))
					}
				}
				return string(out)
			}
			utf32 := func(text string, little, bom bool) string {
				var out []byte
				put := func(r rune) {
					if little {
						out = append(out, byte(r), byte(r>>8), byte(r>>16), byte(r>>24))
					} else {
						out = append(out, byte(r>>24), byte(r>>16), byte(r>>8), byte(r))
					}
				}
				if bom {
					put(0xfeff)
				}
				for _, r := range text {
					put(r)
				}
				return string(out)
			}
			raw("UTF-8 with its BOM", "\xef\xbb\xbf"+ping)
			raw("two BOMs", "\xef\xbb\xbf\xef\xbb\xbf"+ping)
			raw("UTF-16 LE with its BOM", utf16(ping, true, true))
			raw("UTF-16 BE with its BOM", utf16(ping, false, true))
			raw("UTF-16 LE", utf16(ping, true, false))
			raw("UTF-16 BE", utf16(ping, false, false))
			raw("UTF-16 LE, a character past the BMP", utf16(`{"jsonrpc":"2.0","id":"😀","method":"ping"}`, true, true))
			raw("UTF-16, an odd byte", utf16(ping, true, true)+"\x00")
			raw("UTF-32 LE with its BOM", utf32(ping, true, true))
			raw("UTF-32 BE with its BOM", utf32(ping, false, true))
			raw("UTF-32 LE", utf32(ping, true, false))
			raw("UTF-32 BE", utf32(ping, false, false))
			raw("UTF-32, truncated", utf32(ping, true, true)[:10])
			raw("UTF-32, past U+10FFFF", "\xff\xfe\x00\x00"+"\x00\x00\x11\x00")
			raw("two bytes, UTF-16 LE", "1\x00")
			raw("a lone BOM", "\xef\xbb\xbf")
			raw("Latin-1", `{"jsonrpc":"2.0","id":"`+"\xe9"+`","method":"ping"}`)
			raw("a truncated UTF-8 character", `{"jsonrpc":"2.0","id":"`+"\xc3"+`","method":"ping"}`)
			raw("a batch in UTF-16", utf16(`[{"jsonrpc":"2.0","id":1,"method":"ping"},{"jsonrpc":"2.0","id":2,"method":"tools/list"}]`, true, true))
			origin := func(name, o string) {
				s = append(s, Step{Name: "origin · " + name, Method: "POST", Path: "/mcp", Auth: write, Raw: []byte(ping), ContentType: "application/json",
					Header: map[string]string{"Origin": o, "Host": "host.test"}})
			}
			origin("not ASCII", "http://ÜBER.test")
			origin("not ASCII, the host's own name in capitals", "http://HOST.test")
			origin("an IPv6 left open", "http://[::1")
			origin("a bracketed name", "http://[ü]")
			return s
		},
	})
}
