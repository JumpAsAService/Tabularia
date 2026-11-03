// Package server routes a request: to the Go handler when its method and path are among the
// routes ported, to the Python gateway otherwise.
package server

import (
	"bytes"
	"encoding/json"
	"net/http"

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
		byPattern[r.Pattern][r.Method] = r.Handler
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
