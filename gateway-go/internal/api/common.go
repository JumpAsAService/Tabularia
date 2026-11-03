package api

import (
	"context"
	"io"
	"net/http"
	"time"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyd"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
)

// Helpers every module's routes share.

// isoPtr is a UtcDateTime field that may be null.
func isoPtr(t *time.Time) *string {
	if t == nil {
		return nil
	}
	s := pyjson.Isoformat(*t)
	return &s
}

type bodyKey struct{}

// withBody is FastAPI's reading of a route's body, BEFORE its dependencies: a JSON that does
// not parse is a 422 (a 400 when it cannot be read) even for whoever is not signed in. The
// body read waits in the request for the validation (bodyOf), after the guards.
func withBody(h http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		raw, err := io.ReadAll(r.Body)
		if err != nil {
			pyd.BadRequestBody().Write(w)
			return
		}
		body, early := pyd.ReadBody(raw, r.Header.Get("Content-Type"))
		if early != nil {
			early.Write(w)
			return
		}
		h.ServeHTTP(w, r.WithContext(context.WithValue(r.Context(), bodyKey{}, body)))
	})
}

// bodyOf is the body withBody read.
func bodyOf(r *http.Request) pyd.Body {
	b, _ := r.Context().Value(bodyKey{}).(pyd.Body)
	return b
}
