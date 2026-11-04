package server

import (
	"io"
	"net/http/httptest"
	"testing"
)

func TestAnOrchestratorAnswersOnlyHealthAndRoot(t *testing.T) {
	h := Orchestrator("Tabularia")
	for _, c := range []struct {
		method, path string
		status       int
		want         string
	}{
		{"GET", "/health", 200, `{"status":"ok"}`},
		{"GET", "/", 200, `{"message":"Tabularia gateway is running"}`},
		{"POST", "/health", 405, `{"detail":"Method Not Allowed"}`},
		{"GET", "/projects", 404, `{"detail":"Not Found"}`},
		{"GET", "/a/../health", 404, `{"detail":"Not Found"}`},
	} {
		rec := httptest.NewRecorder()
		h.ServeHTTP(rec, httptest.NewRequest(c.method, c.path, nil))
		body, _ := io.ReadAll(rec.Body)
		if rec.Code != c.status || string(body) != c.want {
			t.Errorf("%s %s: %d %q", c.method, c.path, rec.Code, body)
		}
	}
}
