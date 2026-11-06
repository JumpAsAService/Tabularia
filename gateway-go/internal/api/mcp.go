package api

import (
	"io"
	"net/http"
	"strings"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/mcp"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyurl"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/server"
)

// routes/mcp.py: the MCP server, JSON-RPC 2.0 over HTTP without sessions. initialize, ping,
// tools/list and the protocol's errors are answered here; a request carrying a tools/call
// goes whole to the AI service, where the agent's tools run (services/agent).
func init() {
	register("mcp", func(d *Deps) []server.Route {
		return []server.Route{
			{Method: http.MethodPost, Pattern: "/mcp", Handler: mcpEndpoint(d)},
			{Method: http.MethodGet, Pattern: "/mcp", Handler: mcpNoStream(d)},
			{Method: http.MethodDelete, Pattern: "/mcp", Handler: mcpNoStream(d)},
		}
	})
}

// PROTOCOL_VERSIONS, newest first; the JSON-RPC errors; MAX_BATCH
var mcpVersions = []string{"2025-11-25", "2025-06-18", "2025-03-26"}

const (
	rpcParseError     = -32700
	rpcInvalidRequest = -32600
	rpcMethodNotFound = -32601
	rpcInvalidParams  = -32602
	mcpMaxBatch       = 50
)

func rpcError(id any, code int64, message string) pyjson.Object {
	return pyjson.Object{{Key: "jsonrpc", Value: "2.0"}, {Key: "id", Value: id},
		{Key: "error", Value: pyjson.Object{{Key: "code", Value: code}, {Key: "message", Value: message}}}}
}

func rpcResult(id any, result any) pyjson.Object {
	return pyjson.Object{{Key: "jsonrpc", Value: "2.0"}, {Key: "id", Value: id}, {Key: "result", Value: result}}
}

// writeRaw is Starlette's JSONResponse with a body already written as json.dumps would.
func writeRaw(w http.ResponseWriter, status int, v any) {
	s, err := pyjson.Dumps(v, pyjson.Options{Compact: true})
	if err != nil || !isFinite(v) {
		server.InternalError(w) // allow_nan=False
		return
	}
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_, _ = w.Write([]byte(s))
}

// isFinite: no NaN nor infinity anywhere (JSONResponse refuses them).
func isFinite(v any) bool {
	switch x := v.(type) {
	case float64:
		return x == x && x <= 1.7976931348623157e308 && x >= -1.7976931348623157e308
	case pyjson.Object:
		for _, kv := range x {
			if !isFinite(kv.Value) {
				return false
			}
		}
	case []any:
		for _, e := range x {
			if !isFinite(e) {
				return false
			}
		}
	}
	return true
}

// mcpSwitchedOn is _switched_on.
func mcpSwitchedOn(d *Deps, w http.ResponseWriter) bool {
	if !d.Panel.MCPEnabled() {
		adminDetail(w, http.StatusNotFound, "MCP is turned off on this installation")
		return false
	}
	return true
}

// sameOrigin is deps/auth.py same_origin: the Origin names the host it was sent to, or
// one of APP__CORS_ORIGINS. Starlette reads the headers as latin-1, and the host's case is
// str.lower's; urlsplit's ValueError (an Origin whose netloc NFKC breaks) is the error.
func sameOrigin(d *Deps, r *http.Request) (bool, error) {
	origin := latin1(r.Header.Get("Origin"))
	if origin == "" || origin == "null" {
		return false, nil
	}
	for _, o := range d.Config.Admin.CORSOrigins {
		if strings.TrimRight(origin, "/") == strings.TrimRight(o, "/") {
			return true, nil
		}
	}
	host := pyurl.Lower(latin1(r.Host))
	if host == "" {
		return false, nil
	}
	parts, err := pyurl.URLSplit(origin)
	if err != nil {
		return false, err
	}
	return pyurl.Lower(parts.Netloc) == host, nil
}

// latin1 is a header's bytes as Starlette decodes them: one character per byte.
func latin1(s string) string {
	for i := 0; i < len(s); i++ {
		if s[i] >= 0x80 {
			rs := make([]rune, len(s))
			for j := 0; j < len(s); j++ {
				rs[j] = rune(s[j])
			}
			return string(rs)
		}
	}
	return s
}

// mcpEndpoint is POST /mcp: mcp_user (only an API token in the header), then the messages.
func mcpEndpoint(d *Deps) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if !mcpSwitchedOn(d, w) {
			return
		}
		if r.Header.Get("Origin") != "" {
			same, err := sameOrigin(d, r)
			switch {
			case err != nil:
				server.InternalError(w) // urlsplit's ValueError, uncaught in Python
				return
			case !same:
				adminDetail(w, http.StatusForbidden, "Origin not allowed")
				return
			}
		}
		scheme, credential, _ := strings.Cut(r.Header.Get("Authorization"), " ")
		credential = strings.TrimSpace(credential)
		if !strings.EqualFold(scheme, "bearer") || !strings.HasPrefix(credential, "tab_") {
			w.Header().Set("WWW-Authenticate", "Bearer")
			adminDetail(w, http.StatusUnauthorized, "An API token is needed: Authorization: Bearer tab_…")
			return
		}
		p, refusal := d.Auth.TokenUser(r.Context(), r, credential)
		if refusal != nil {
			server.Refuse(w, refusal)
			return
		}
		raw, _ := io.ReadAll(r.Body)
		if v := r.Header.Get("Mcp-Protocol-Version"); v != "" && !contains(mcpVersions, v) {
			writeRaw(w, http.StatusBadRequest, rpcError(nil, rpcInvalidRequest, "Unsupported MCP-Protocol-Version: "+v))
			return
		}
		text := raw
		if len(text) == 0 {
			text = []byte("null")
		}
		var body any
		var err error
		decoded, ok := jsonBytesText(text)
		if ok {
			body, err = pyjson.Loads(decoded)
		}
		if !ok || err != nil {
			writeRaw(w, http.StatusBadRequest, rpcError(nil, rpcParseError, "Parse error"))
			return
		}
		level := p.TokenScope
		withheld := d.Panel.MetadataOnly("mcp")
		if list, ok := body.([]any); ok { // a batch (protocol 2025-03-26)
			if len(list) == 0 {
				writeRaw(w, http.StatusBadRequest, rpcError(nil, rpcInvalidRequest, "Empty batch"))
				return
			}
			if len(list) > mcpMaxBatch {
				writeRaw(w, http.StatusBadRequest, rpcError(nil, rpcInvalidRequest, "A batch holds at most 50 messages"))
				return
			}
			for _, m := range list {
				if callsATool(m) {
					toAIService(d, w, r, raw) // the tools are the assistant's: the AI service
					return
				}
			}
			answers := []any{}
			for _, m := range list {
				if a := mcpHandle(d, level, withheld, m); a != nil {
					answers = append(answers, a)
				}
			}
			if len(answers) == 0 {
				w.WriteHeader(http.StatusAccepted)
				return
			}
			writeRaw(w, http.StatusOK, answers)
			return
		}
		if callsATool(body) {
			toAIService(d, w, r, raw) // the tools are the assistant's: the AI service
			return
		}
		a := mcpHandle(d, level, withheld, body)
		if a == nil {
			w.WriteHeader(http.StatusAccepted)
			return
		}
		writeRaw(w, http.StatusOK, a)
	})
}

func contains(list []string, s string) bool {
	for _, x := range list {
		if x == s {
			return true
		}
	}
	return false
}

// callsATool: a message _handle would run a tool for.
func callsATool(m any) bool {
	obj, ok := m.(pyjson.Object)
	if !ok {
		return false
	}
	method, _ := obj.Get("method")
	return method == "tools/call"
}

// mcpHandle is _handle: one message → its answer, or nil for a notification.
func mcpHandle(d *Deps, level string, withheld bool, msg any) any {
	obj, isObj := msg.(pyjson.Object)
	if !isObj {
		return rpcError(nil, rpcInvalidRequest, "Invalid Request")
	}
	id, _ := obj.Get("id")
	if v, _ := obj.Get("jsonrpc"); v != "2.0" {
		return rpcError(id, rpcInvalidRequest, "Invalid Request")
	}
	methodV, _ := obj.Get("method")
	method, isStr := methodV.(string)
	_, hasID := obj.Get("id")
	if !isStr || !hasID {
		return nil // a notification, or a client's answer: nothing to answer
	}
	paramsV, _ := obj.Get("params")
	if !pyTruthy(paramsV) {
		paramsV = pyjson.Object{}
	}
	params, ok := paramsV.(pyjson.Object)
	if !ok {
		return rpcError(id, rpcInvalidParams, "params must be an object")
	}
	switch method {
	case "initialize":
		version := mcpVersions[0]
		if asked, ok := params.Get("protocolVersion"); ok {
			if s, isStr := asked.(string); isStr && contains(mcpVersions, s) {
				version = s
			}
		}
		return rpcResult(id, pyjson.Object{
			{Key: "protocolVersion", Value: version},
			{Key: "capabilities", Value: pyjson.Object{{Key: "tools", Value: pyjson.Object{{Key: "listChanged", Value: false}}}}},
			{Key: "serverInfo", Value: pyjson.Object{{Key: "name", Value: "tabularia"}, {Key: "title", Value: "Tabularia"},
				{Key: "version", Value: d.Config.Admin.Version}}},
			{Key: "instructions", Value: mcp.Instructions(level, withheld, d.Panel.MCPConfirmInTabulariaOnly())},
		})
	case "ping":
		return rpcResult(id, pyjson.Object{})
	case "tools/list":
		return rpcResult(id, pyjson.Object{{Key: "tools", Value: mcp.Tools(level, withheld)}})
	}
	return rpcError(id, rpcMethodNotFound, "Method not found: "+method)
}

// mcpNoStream is GET and DELETE /mcp: no stream to open, no session to close.
func mcpNoStream(d *Deps) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		if !mcpSwitchedOn(d, w) {
			return
		}
		w.Header().Set("Allow", "POST")
		w.WriteHeader(http.StatusMethodNotAllowed)
	})
}
