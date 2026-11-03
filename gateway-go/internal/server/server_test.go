package server

import (
	"io"
	"net/http"
	"net/http/httptest"
	"testing"
)

func TestPortedRoutesAnswerHereEverythingElseUpstream(t *testing.T) {
	upstream := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { w.Write([]byte("python " + r.Method + " " + r.URL.Path)) })
	h := New([]Route{{Method: "GET", Pattern: "/health", Handler: Health()}}, upstream)
	for _, c := range []struct{ method, path, want string }{
		{"GET", "/health", `{"status":"ok"}`},
		{"HEAD", "/health", "python HEAD /health"}, // FastAPI answers HEAD on a GET with a 405: Python's to say
		{"POST", "/health", "python POST /health"},
		{"GET", "/projects", "python GET /projects"},
		{"GET", "/health/x", "python GET /health/x"},
		{"GET", "/a/../health", "python GET /a/../health"}, // not cleaned, not redirected
		{"GET", "//health", "python GET //health"},
	} {
		rec := httptest.NewRecorder()
		h.ServeHTTP(rec, httptest.NewRequest(c.method, c.path, nil))
		body, _ := io.ReadAll(rec.Body)
		if string(body) != c.want {
			t.Errorf("%s %s: %q", c.method, c.path, body)
		}
	}
}

// «%2F» splits a segment for Starlette: never one wildcard's value here
func TestEscapedSlashGoesUpstream(t *testing.T) {
	upstream := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { w.Write([]byte("python")) })
	h := New([]Route{{Method: "GET", Pattern: "/x/{id}", Handler: Health()}}, upstream)
	for path, want := range map[string]string{"/x/a%2Fb": "python", "/x/a%2fb": "python", "/x/a%20b": `{"status":"ok"}`} {
		rec := httptest.NewRecorder()
		h.ServeHTTP(rec, httptest.NewRequest("GET", path, nil))
		if body, _ := io.ReadAll(rec.Body); string(body) != want {
			t.Errorf("%s: %q", path, body)
		}
	}
}

// Starlette's JSONResponse: compact, UTF-8 as it is, «<» not escaped
func TestJSONAsStarlette(t *testing.T) {
	rec := httptest.NewRecorder()
	JSON(rec, 200, map[string]string{"detail": "a <b> & è"})
	if got := rec.Body.String(); got != `{"detail":"a <b> & è"}` || rec.Header().Get("Content-Type") != "application/json" {
		t.Errorf("%q %q", got, rec.Header().Get("Content-Type"))
	}
}
