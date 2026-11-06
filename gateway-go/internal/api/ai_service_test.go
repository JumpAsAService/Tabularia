package api

import (
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/config"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/server"
)

// The assistant's routes reach the AI service as they came; the rest of the same paths are
// answered as FastAPI does (405, 404), never forwarded.
func TestTheAssistantsRoutesGoToTheAIService(t *testing.T) {
	var to, body string
	mark := func(name string) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			b, _ := io.ReadAll(r.Body)
			to, body = name+" "+r.Method+" "+r.URL.RequestURI(), string(b)
		})
	}
	d := &Deps{Config: &config.Config{}, Log: slog.New(slog.NewTextHandler(io.Discard, nil)), AIService: mark("ai")}
	h := server.New(Routes(d), server.Options{})
	for _, c := range []struct{ method, path, want string }{
		{"POST", "/ai/chat", "ai POST /ai/chat"},
		{"GET", "/ai/chats?limit=3", "ai GET /ai/chats?limit=3"},
		{"DELETE", "/ai/chats/abc", "ai DELETE /ai/chats/abc"},
		{"PUT", "/ai/models/openai/gpt-x", "ai PUT /ai/models/openai/gpt-x"},
		{"GET", "/ai/chats/abc/turns/2/steps/s1/export", "ai GET /ai/chats/abc/turns/2/steps/s1/export"},
		{"POST", "/agent/plans/7/confirm", "ai POST /agent/plans/7/confirm"},
		{"POST", "/dashboards/3/comment", "ai POST /dashboards/3/comment"},
		{"POST", "/reports/preview/comment", "ai POST /reports/preview/comment"},
		{"PATCH", "/ai/chats/abc", ""}, // not a route of the assistant: a 405
		{"GET", "/ai/models/x", ""},    // not one: a 404
	} {
		to = ""
		h.ServeHTTP(httptest.NewRecorder(), httptest.NewRequest(c.method, c.path, strings.NewReader(`{"q":1}`)))
		if to != c.want {
			t.Errorf("%s %s → %q, want %q", c.method, c.path, to, c.want)
		}
		if strings.HasPrefix(c.want, "ai POST") && body != `{"q":1}` {
			t.Errorf("%s: the body arrived as %q", c.path, body)
		}
	}
}

func TestTheInventorySeesFastAPIsPaths(t *testing.T) {
	if muxPattern("/ai/models/{model_id:path}") != "/ai/models/{model_id...}" || muxPattern("/ai/chats/{chat_id}") != "/ai/chats/{chat_id}" {
		t.Error("muxPattern")
	}
	found := false
	for _, r := range AIServiceRoutes() {
		found = found || r == [2]string{"PUT", "/ai/models/{model_id:path}"}
	}
	if !found {
		t.Error("the inventory needs FastAPI's path")
	}
}
