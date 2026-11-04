package api

// The assistant's routes (decision 4, docs/specs/gateway-go.md «The AI service»): built on
// pydantic-ai, they stay Python and are forwarded, as they came, to the AI service (the gateway's
// image with APP__ROLE=ai) instead of the Python gateway behind. The Python code authenticates
// them as it always has; nothing is read or written here.

import (
	"bytes"
	"io"
	"net/http"
	"strings"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/server"
)

// aiServiceRoutes are the routes forwarded whole, as FastAPI declares them (routes/ai.py,
// routes/agent_plans.py, the comments of routes/reports.py and routes/dashboards.py).
var aiServiceRoutes = [][2]string{
	{http.MethodPost, "/ai/chat"},
	{http.MethodPost, "/ai/flow-assist"},
	{http.MethodPost, "/ai/script-assist"},
	{http.MethodGet, "/ai/flow-chats/latest"},
	{http.MethodGet, "/ai/chats"},
	{http.MethodGet, "/ai/chats/{chat_id}"},
	{http.MethodDelete, "/ai/chats/{chat_id}"},
	{http.MethodGet, "/ai/chats/{chat_id}/turns/{seq}/steps/{step_id}/export"},
	{http.MethodGet, "/ai/models"},
	{http.MethodPut, "/ai/models/{model_id:path}"},
	{http.MethodPost, "/agent/plans/{plan_id}/confirm"},
	{http.MethodPost, "/reports/preview/comment"},
	{http.MethodPost, "/dashboards/preview/comment"},
	{http.MethodPost, "/dashboards/{dashboard_id}/comment"},
}

// AIServiceRoutes are the routes the Go gateway forwards to the AI service, [method, path] as
// FastAPI declares them (cmd/routes, the inventory); /mcp's `tools/call` and the renders with an
// AI comment go there too, from their own handlers.
func AIServiceRoutes() [][2]string { return append([][2]string{}, aiServiceRoutes...) }

// muxPattern is a FastAPI path as net/http's mux reads it: `{name:path}` is `{name...}`.
func muxPattern(path string) string {
	if i := strings.Index(path, ":path}"); i >= 0 {
		return path[:i] + "...}" + path[i+len(":path}"):]
	}
	return path
}

// aiService is the AI service's forwarding handler (the Python gateway behind for code that
// builds the dependencies without it: cmd/gateway always sets it).
func aiService(d *Deps) http.Handler {
	if d.AIService != nil {
		return d.AIService
	}
	return d.Upstream
}

// toAIService forwards a request whose body was read already (`raw`; nil: none) to the AI
// service, as it came.
func toAIService(d *Deps, w http.ResponseWriter, r *http.Request, raw []byte) {
	r2 := r.Clone(r.Context())
	r2.Body = io.NopCloser(bytes.NewReader(raw))
	r2.ContentLength = int64(len(raw))
	aiService(d).ServeHTTP(w, r2)
}

func init() {
	register("ai_service", func(d *Deps) []server.Route {
		out := make([]server.Route, 0, len(aiServiceRoutes))
		for _, rt := range aiServiceRoutes {
			out = append(out, server.Route{Method: rt[0], Pattern: muxPattern(rt[1]), Handler: http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				aiService(d).ServeHTTP(w, r)
			})})
		}
		return out
	})
}
