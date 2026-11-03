// Package server routes a request: to the Go handler when its method and path are among the
// routes ported, to the Python gateway otherwise.
package server

import (
	"bytes"
	"encoding/json"
	"io"
	"net/http"
	"strconv"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/auth"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/reqinfo"
)

// Route is a ported route: the method and the path as FastAPI declares it
// (`/projects/{project_id}/flows`, the same syntax as net/http's patterns).
type Route struct {
	Method  string
	Pattern string
	Handler http.Handler
}

// New answers the routes ported and forwards everything else. Only the exact method is
// answered here: a HEAD or an OPTIONS on a ported GET still goes to the Python gateway, so
// that no answer exists that the Python gateway would not give.
func New(routes []Route, upstream http.Handler) http.Handler {
	mux := http.NewServeMux()
	byPattern := map[string]map[string]http.Handler{}
	for _, r := range routes {
		if byPattern[r.Pattern] == nil {
			byPattern[r.Pattern] = map[string]http.Handler{}
		}
		h := r.Handler
		if !(r.Method == http.MethodPost && r.Pattern == "/files") {
			h = BodyLimit(h)
		}
		byPattern[r.Pattern][r.Method] = h
	}
	for pattern, methods := range byPattern {
		mux.HandleFunc(pattern, func(w http.ResponseWriter, req *http.Request) {
			if h, ok := methods[req.Method]; ok {
				h.ServeHTTP(w, req)
				return
			}
			upstream.ServeHTTP(w, req)
		})
	}
	mux.Handle("/", upstream)
	return mux
}

// JSON writes an answer as Starlette's JSONResponse does: compact, UTF-8, no HTML escaping.
func JSON(w http.ResponseWriter, status int, v any) {
	var b bytes.Buffer
	enc := json.NewEncoder(&b)
	enc.SetEscapeHTML(false)
	if err := enc.Encode(v); err != nil {
		http.Error(w, `{"detail":"Internal Server Error"}`, http.StatusInternalServerError)
		return
	}
	body := bytes.TrimSuffix(b.Bytes(), []byte("\n"))
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_, _ = w.Write(body)
}

// Refuse writes an HTTPException: `{"detail": …}` with its headers; an internal error as
// Starlette writes one.
func Refuse(w http.ResponseWriter, e *auth.Refusal) {
	if e.Internal {
		InternalError(w)
		return
	}
	for k, vs := range e.Header {
		for _, v := range vs {
			w.Header().Add(k, v)
		}
	}
	JSON(w, e.Status, map[string]string{"detail": e.Detail})
}

// Authed wraps a handler that needs a signed-in person; `route` is its pattern, for the
// read-only tokens' POSTs that only read.
func Authed(a *auth.Authenticator, route string, h func(http.ResponseWriter, *http.Request, *auth.Principal)) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		p, refusal := a.Authenticate(r.Context(), r, route)
		if refusal != nil {
			Refuse(w, refusal)
			return
		}
		h(w, reqinfo.With(r, a.ClientIP, p), p)
	})
}

// InternalError is Starlette's answer to an exception no handler catches: a 500 with
// «Internal Server Error» as plain text.
func InternalError(w http.ResponseWriter) {
	w.Header().Set("Content-Type", "text/plain; charset=utf-8")
	w.WriteHeader(http.StatusInternalServerError)
	_, _ = w.Write([]byte("Internal Server Error"))
}

// Health is GET /health (app/main.py): the process is serving.
func Health() http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		JSON(w, http.StatusOK, map[string]string{"status": "ok"})
	})
}

// MaxBody is core/body_limit.py MAX_BODY_BYTES.
const MaxBody = 8 * 1024 * 1024

// BodyLimit is core/body_limit.py BodyLimitMiddleware, in front of every ported route but
// the upload: a 413 before anything else when the declared size is too large, or when
// an undeclared one (chunked) goes past it.
func BodyLimit(h http.Handler) http.Handler {
	refuse := func(w http.ResponseWriter) {
		body := []byte(`{"detail":"Request body too large (limit 8 MB)"}`)
		w.Header().Set("Content-Type", "application/json")
		w.Header().Set("Content-Length", strconv.Itoa(len(body)))
		w.WriteHeader(http.StatusRequestEntityTooLarge)
		_, _ = w.Write(body)
	}
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.ContentLength > MaxBody {
			refuse(w)
			return
		}
		if r.ContentLength < 0 && r.Body != nil && r.Body != http.NoBody {
			raw, err := io.ReadAll(io.LimitReader(r.Body, MaxBody+1))
			if err != nil {
				InternalError(w)
				return
			}
			if len(raw) > MaxBody {
				refuse(w)
				return
			}
			r.Body = io.NopCloser(bytes.NewReader(raw))
		}
		h.ServeHTTP(w, r)
	})
}
