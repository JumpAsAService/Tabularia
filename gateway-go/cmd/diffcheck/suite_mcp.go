package main

import (
	"net/http"
	"time"
)

// routes/mcp.py: the transport answered in Go (initialize, ping, tools/list, the protocol's
// errors); a request carrying a tools/call goes whole to Python, where the agent's tools run.
func init() {
	register(Suite{
		Module: "mcp",
		Seed: func(w *World) {
			w.Token("writer", "tab_mcp_writer_write_000000", "write", time.Hour)
			w.Token("writer", "tab_mcp_writer_read_0000000", "read", time.Hour)
			w.Token("service", "tab_mcp_service_read_000000", "read", time.Hour)
			w.Token("obs_flag", "tab_mcp_observer_00000000000", "write", time.Hour)
			w.Token("inactive", "tab_mcp_inactive_00000000000", "write", time.Hour)
		},
		Phases: []Phase{
			{Name: "environment's settings"},
			{Name: "panel: metadata-only for MCP, confirmed in Tabularia only", Panel: map[string]string{
				"mcp.metadata_only": "true", "mcp.confirm_in_tabularia_only": "true"}},
			{Name: "panel: MCP off", Panel: map[string]string{"mcp.enabled": "false"}},
		},
		Steps: func(w *World) []Step {
			var s []Step
			write, read := Bearer("tab_mcp_writer_write_000000"), Bearer("tab_mcp_writer_read_0000000")
			post := func(name string, auth func(*http.Request), body any, h map[string]string) {
				s = append(s, Step{Name: name, Method: "POST", Path: "/mcp", Auth: auth, JSON: body, Header: h})
			}
			raw := func(name string, auth func(*http.Request), body string, ct string) {
				s = append(s, Step{Name: name, Method: "POST", Path: "/mcp", Auth: auth, Raw: []byte(body), ContentType: ct})
			}
			rpc := func(id any, method string, params any) map[string]any {
				m := map[string]any{"jsonrpc": "2.0", "id": id, "method": method}
				if params != nil {
					m["params"] = params
				}
				return m
			}
			init := rpc(1, "initialize", map[string]any{"protocolVersion": "2025-06-18"})
			post("nobody", nil, init, nil)
			post("the session's cookie", w.As("writer"), init, nil)
			post("Basic", func(r *http.Request) { r.Header.Set("Authorization", "Basic eDp5") }, init, nil)
			post("a JWT in the header", Bearer(w.JWT("writer", "s-writer")), init, nil)
			post("an unknown token", Bearer("tab_nope"), init, nil)
			post("an observer's token", Bearer("tab_mcp_observer_00000000000"), init, nil)
			post("an inactive account's token", Bearer("tab_mcp_inactive_00000000000"), init, nil)
			post("another site's page", write, init, map[string]string{"Origin": "https://evil.example"})
			post("the interface's origin", write, init, map[string]string{"Origin": "http://localhost:3000/"})
			post("an origin naming the host", write, ping(), map[string]string{"Origin": "http://HOST.test", "Host": "host.test"})
			post("origin null", write, ping(), map[string]string{"Origin": "null"})
			post("an unsupported protocol version", write, init, map[string]string{"Mcp-Protocol-Version": "2024-01-01"})
			post("a supported protocol version", write, init, map[string]string{"Mcp-Protocol-Version": "2025-03-26"})
			for _, tok := range []struct {
				name string
				auth func(*http.Request)
			}{{"write", write}, {"read", read}, {"service read", Bearer("tab_mcp_service_read_000000")}} {
				post("initialize · "+tok.name, tok.auth, init, nil)
				post("tools/list · "+tok.name, tok.auth, rpc("a", "tools/list", nil), nil)
			}
			post("initialize · an unknown version asked", write, rpc(2, "initialize", map[string]any{"protocolVersion": "1999"}), nil)
			post("initialize · a list as version", write, rpc(2, "initialize", map[string]any{"protocolVersion": []int{1}}), nil)
			post("initialize · no params", write, rpc(nil, "initialize", nil), nil)
			post("ping, a float id", write, rpc(1.5, "ping", nil), nil)
			post("ping, an object id", write, rpc(map[string]any{"x": []int{1}}, "ping", nil), nil)
			post("params a list", write, rpc(3, "ping", []int{1}), nil)
			post("params empty list", write, rpc(3, "ping", []int{}), nil)
			post("params a string", write, rpc(3, "ping", "x"), nil)
			post("unknown method", write, rpc(4, "resources/list", nil), nil)
			post("a notification", write, map[string]any{"jsonrpc": "2.0", "method": "notifications/initialized"}, nil)
			post("a method that is no string", write, map[string]any{"jsonrpc": "2.0", "id": 5, "method": 7}, nil)
			post("jsonrpc 1.0", write, map[string]any{"jsonrpc": "1.0", "id": 6, "method": "ping"}, nil)
			post("a number", write, 7, nil)
			post("null", write, nil, nil)
			raw("no body", write, "", "")
			raw("not JSON", write, "{nope", "application/json")
			raw("NaN as id", write, `{"jsonrpc":"2.0","id":NaN,"method":"ping"}`, "application/json")
			raw("a big id", write, `{"jsonrpc":"2.0","id":123456789012345678901234567890,"method":"ping"}`, "application/json")
			raw("bytes that are not UTF-8", write, "\xff\xfe{}", "application/json")
			post("an empty batch", write, []any{}, nil)
			many := make([]any, 51)
			for i := range many {
				many[i] = rpc(i, "ping", nil)
			}
			post("a batch too long", write, many, nil)
			post("a batch", write, []any{rpc(1, "ping", nil), map[string]any{"jsonrpc": "2.0", "method": "notifications/x"}, 5,
				rpc(2, "tools/list", nil)}, nil)
			post("a batch of notifications only", write, []any{map[string]any{"jsonrpc": "2.0", "method": "n"}}, nil)
			// a tool: Python's
			post("tools/call · forwarded", write, rpc(9, "tools/call", map[string]any{"name": "list_folders", "arguments": map[string]any{}}), nil)
			post("tools/call · a writing tool with a read token", read, rpc(9, "tools/call", map[string]any{"name": "flow_draft_new", "arguments": map[string]any{}}), nil)
			post("tools/call · unknown", write, rpc(9, "tools/call", map[string]any{"name": "nope"}), nil)
			post("tools/call in a batch", write, []any{rpc(1, "ping", nil), rpc(9, "tools/call", map[string]any{"name": "list_folders"})}, nil)
			for _, m := range []string{"GET", "DELETE"} {
				s = append(s, Step{Name: m + " · nobody", Method: m, Path: "/mcp"})
			}
			return s
		},
	})
}

func ping() map[string]any { return map[string]any{"jsonrpc": "2.0", "id": 1, "method": "ping"} }
