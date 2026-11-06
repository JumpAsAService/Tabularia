// Package server is the framework the Python gateway runs on, as far as a request sees it
// (uvicorn, Starlette, FastAPI: router.go), and the helpers the handlers answer with.
package server

import (
	"bytes"
	"encoding/json"
	"io"
	"net/http"
	"strconv"
	"strings"

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

// WebSocket is a Route's Method for a FastAPI WebSocket route (the inventory's «WS»).
const WebSocket = "WS"

// IsWebSocket is uvicorn's test of a WebSocket handshake (H11Protocol._get_upgrade): the
// last Connection header has an «upgrade» token, and the last Upgrade header is «websocket».
func IsWebSocket(r *http.Request) bool {
	conn, up := r.Header.Values("Connection"), r.Header.Values("Upgrade")
	if len(conn) == 0 || len(up) == 0 || strings.ToLower(up[len(up)-1]) != "websocket" {
		return false
	}
	for _, t := range strings.Split(conn[len(conn)-1], ",") {
		if strings.ToLower(strings.TrimSpace(t)) == "upgrade" {
			return true
		}
	}
	return false
}

// FastAPIPath is a Route's Pattern as FastAPI declares the path: «{name...}» is «{name:path}»,
// «/{$}» the root alone.
func FastAPIPath(pattern string) string {
	if pattern == "/{$}" {
		return "/"
	}
	return strings.Replace(pattern, "...}", ":path}", 1)
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
	// Starlette's ServerErrorMiddleware is outside every other one: no CORS on this answer
	for x := w; x != nil; {
		if c, ok := x.(*corsWriter); ok {
			c.done = true
			break
		}
		u, ok := x.(interface{ Unwrap() http.ResponseWriter })
		if !ok {
			break
		}
		x = u.Unwrap()
	}
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
