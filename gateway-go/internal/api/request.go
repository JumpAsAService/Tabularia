package api

import (
	"context"
	"io"
	"math/big"
	"net/http"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyd"
)

type bodyKey struct{}

// withBody is FastAPI's reading of a route's body parameter, BEFORE its dependencies: a
// JSON that does not parse is answered here, even to whoever is not signed in. The body
// read waits in the request for bodyOf.
func withBody(h http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		raw, err := io.ReadAll(r.Body)
		if err != nil {
			pyd.BadRequestBody().Write(w)
			return
		}
		b, early := pyd.ReadBody(raw, r.Header.Get("Content-Type"))
		if early != nil {
			early.Write(w)
			return
		}
		h.ServeHTTP(w, r.WithContext(context.WithValue(r.Context(), bodyKey{}, b)))
	})
}

// bodyOf is the body withBody read.
func bodyOf(r *http.Request) pyd.Body {
	b, _ := r.Context().Value(bodyKey{}).(pyd.Body)
	return b
}

// rowID is a validated integer as a row id: false when no row can have it (beyond a
// PostgreSQL integer: psycopg compares it as a bigint and finds nothing).
func rowID(v any) (int64, bool) {
	switch n := v.(type) {
	case int64:
		return n, n >= -1<<31 && n <= 1<<31-1
	case *big.Int:
		return 0, false
	}
	return 0, false
}

// pyStr is a validated string field (nil for None).
func pyStr(v any) *string {
	if s, ok := v.(string); ok {
		return &s
	}
	return nil
}

// noContent is a route declared with status_code=204: FastAPI's JSONResponse class still
// names its content type, with no body.
func noContent(w http.ResponseWriter) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(http.StatusNoContent)
}
