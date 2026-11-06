package server

import (
	"io"
	"net/http"
	"net/http/httptest"
	"regexp"
	"strings"
	"testing"
)

func mark(name string) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/plain")
		_, _ = w.Write([]byte(name + " " + r.PathValue("flow_id") + r.PathValue("model_id")))
	})
}

func serve(h http.Handler, method, target string, header map[string]string) *httptest.ResponseRecorder {
	r := httptest.NewRequest(method, "/", nil)
	r.RequestURI = target // as the client sent it: httptest would clean nothing, but parse
	for k, v := range header {
		r.Header.Set(k, v)
	}
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, r)
	return rec
}

// Every request answered as FastAPI does: the route's handler, 405 with the first partial
// route's methods, the trailing slash's redirect, 404; the path decoded as uvicorn does.
func TestTheRoutesAsStarletteTriesThem(t *testing.T) {
	h := New([]Route{
		{Method: "GET", Pattern: "/health", Handler: Health()},
		{Method: "GET", Pattern: "/flows/{flow_id}", Handler: mark("get")},
		{Method: "PATCH", Pattern: "/flows/{flow_id}", Handler: mark("patch")},
		{Method: "PUT", Pattern: "/ai/models/{model_id...}", Handler: mark("model")},
		{Method: "GET", Pattern: "/openapi.json", Handler: mark("schema")},
	}, Options{})
	for _, c := range []struct {
		method, target string
		status         int
		body, location string
	}{
		{"GET", "/health", 200, `{"status":"ok"}`, ""},
		{"POST", "/health", 405, `{"detail":"Method Not Allowed"}`, ""},
		{"GET", "/flows/7", 200, "get 7", ""},
		{"PATCH", "/flows/a%20b", 200, "patch a b", ""},
		{"GET", "/flows/a%2Fb", 404, `{"detail":"Not Found"}`, ""}, // «%2F» splits the segment
		{"GET", "/flows/%C3%A8", 200, "get è", ""},
		{"GET", "/flows/%FF%C3", 200, "get ��", ""},
		{"GET", "/flows/a%zz", 200, "get a%zz", ""},
		{"GET", "/flows/7%0A", 200, "get 7\n", ""}, // Python's «$» before a final newline
		{"PUT", "/ai/models/openai/gpt%2F4", 200, "model openai/gpt/4", ""},
		{"GET", "/flows/7/", 307, "", "http://example.com/flows/7"},
		{"GET", "/health//?a=1", 307, "", "http://example.com/health?a=1"},
		{"GET", "/a/../health", 404, `{"detail":"Not Found"}`, ""}, // never cleaned
		{"GET", "//health", 404, `{"detail":"Not Found"}`, ""},
		{"GET", "/nothing", 404, `{"detail":"Not Found"}`, ""},
		{"HEAD", "/openapi.json", 200, "", ""}, // a Starlette Route: HEAD is its GET, without body
		{"HEAD", "/health", 405, "", ""},
	} {
		rec := serve(h, c.method, c.target, nil)
		body, _ := io.ReadAll(rec.Body)
		if rec.Code != c.status || (c.method != "HEAD" && string(body) != c.body) || rec.Header().Get("Location") != c.location {
			t.Errorf("%s %s: %d %q %q", c.method, c.target, rec.Code, body, rec.Header().Get("Location"))
		}
	}
	if rec := serve(h, "POST", "/flows/7", nil); rec.Code != 405 || rec.Header().Get("Allow") != "GET" {
		t.Errorf("the first partial route's methods: %q", rec.Header().Get("Allow"))
	}
	if rec := serve(h, "HEAD", "/health", nil); rec.Header().Get("Content-Length") != "31" || rec.Header().Get("Allow") != "GET" {
		t.Errorf("HEAD: %v", rec.Header())
	}
}

func TestEveryPythonPathCompiles(t *testing.T) {
	for _, p := range append(append([]pyRoute{}, fastAPIRoutes...), notebooksHostRoutes...) {
		if _, err := regexp.Compile(goRegex(p.regex)); err != nil {
			t.Errorf("%s: %v", p.path, err)
		}
	}
}

func TestCORSAsTheMiddlewareAnswers(t *testing.T) {
	h := New([]Route{{Method: "GET", Pattern: "/health", Handler: Health()}}, Options{CORSOrigins: []string{"http://ui.test"}})
	rec := serve(h, "GET", "/health", map[string]string{"Origin": "http://ui.test"})
	if rec.Header().Get("Access-Control-Allow-Origin") != "http://ui.test" || rec.Header().Get("Vary") != "Origin" ||
		rec.Header().Get("Access-Control-Allow-Credentials") != "true" || rec.Header().Get("Access-Control-Expose-Headers") != "Content-Disposition" {
		t.Errorf("simple: %v", rec.Header())
	}
	rec = serve(h, "GET", "/nothing", map[string]string{"Origin": "http://evil.test"})
	if rec.Code != 404 || rec.Header().Get("Access-Control-Allow-Origin") != "" || rec.Header().Get("Access-Control-Allow-Credentials") != "true" {
		t.Errorf("another origin: %d %v", rec.Code, rec.Header())
	}
	rec = serve(h, "OPTIONS", "/anything", map[string]string{"Origin": "http://ui.test", "Access-Control-Request-Method": "PATCH",
		"Access-Control-Request-Headers": "X-Tabularia-CSRF"})
	if rec.Code != 200 || rec.Body.String() != "OK" || rec.Header().Get("Access-Control-Allow-Headers") != "X-Tabularia-CSRF" {
		t.Errorf("preflight: %d %q %v", rec.Code, rec.Body.String(), rec.Header())
	}
	rec = serve(h, "OPTIONS", "/health", map[string]string{"Origin": "http://evil.test", "Access-Control-Request-Method": "TRACE"})
	if rec.Code != 400 || rec.Body.String() != "Disallowed CORS origin, method" {
		t.Errorf("refused preflight: %d %q", rec.Code, rec.Body.String())
	}
	if rec := serve(h, "OPTIONS", "/health", nil); rec.Code != 405 {
		t.Errorf("OPTIONS without CORS: %d", rec.Code)
	}
}

func TestTheBodyCeilingBeforeTheRoutes(t *testing.T) {
	h := New(nil, Options{})
	r := httptest.NewRequest("POST", "/nothing", strings.NewReader("x"))
	r.ContentLength = MaxBody + 1
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, r)
	if rec.Code != 413 {
		t.Errorf("%d", rec.Code)
	}
	r = httptest.NewRequest("POST", "/files", strings.NewReader("x"))
	r.ContentLength = MaxBody + 1
	rec = httptest.NewRecorder()
	h.ServeHTTP(rec, r)
	if rec.Code == 413 {
		t.Error("the upload is exempt")
	}
}

func TestAWebSocketHandshake(t *testing.T) {
	h := New([]Route{{Method: WebSocket, Pattern: "/notebooks/{notebook_id}/app/{path...}", Handler: mark("ws")}}, Options{})
	ws := map[string]string{"Connection": "Upgrade", "Upgrade": "websocket", "Sec-WebSocket-Key": "dGhlIHNhbXBsZSBub25jZQ==", "Sec-WebSocket-Version": "13"}
	if rec := serve(h, "GET", "/notebooks/1/app/ws", ws); rec.Code != 200 || !strings.HasPrefix(rec.Body.String(), "ws") {
		t.Errorf("routed: %d %q", rec.Code, rec.Body.String())
	}
	if rec := serve(h, "GET", "/health", ws); rec.Code != 403 || rec.Body.Len() != 0 {
		t.Errorf("no WebSocket route: %d", rec.Code)
	}
	ws["Sec-WebSocket-Key"] = "short"
	if rec := serve(h, "GET", "/notebooks/1/app/ws", ws); rec.Code != 400 ||
		rec.Body.String() != "Failed to open a WebSocket connection: invalid Sec-WebSocket-Key header: short.\n" {
		t.Errorf("a bad key: %d %q", rec.Code, rec.Body.String())
	}
	if rec := serve(h, "POST", "/notebooks/1/app/ws", ws); rec.Code != 405 {
		t.Errorf("a POST: %d", rec.Code)
	}
}

func TestUnquoteAsPython(t *testing.T) {
	for in, want := range map[string]string{
		"/a%20b": "/a b", "/%e2%82": "/�", "/%e2%82%ac": "/€", "/%ff%fe": "/��", "/%": "/%", "/%4": "/%4",
		"/%ed%a0%80": "/���", "/%f0%9f%98": "/�", "/%c3x%a8": "/�x�",
	} {
		if got := Unquote(in); got != want {
			t.Errorf("%q: %q, want %q", in, got, want)
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

// an uncaught exception's 500 comes from ServerErrorMiddleware, outside CORS: no CORS headers
func TestNoCORSOnAnUncaughtError(t *testing.T) {
	h := New([]Route{{Method: "GET", Pattern: "/health", Handler: http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) { InternalError(w) })}},
		Options{CORSOrigins: []string{"http://ui.test"}})
	rec := serve(h, "GET", "/health", map[string]string{"Origin": "http://ui.test"})
	if rec.Code != 500 || rec.Header().Get("Access-Control-Allow-Credentials") != "" || rec.Header().Get("Access-Control-Allow-Origin") != "" {
		t.Errorf("%d %v", rec.Code, rec.Header())
	}
}
