package api

import (
	"net/http"
	"strconv"
)

// intPath runs the route when its integer path parameters are plain digits within a
// PostgreSQL integer (beyond it psycopg compares as bigint and finds nothing, pgx refuses); anything else (a sign, spaces, «1.0», letters, a huge number) is for FastAPI's
// validation to answer — after the authentication, with pydantic's own 422 — and goes to
// the Python gateway untouched until that validation is ported (internal/pyd).
func intPath(d *Deps, h http.Handler, names ...string) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		for _, n := range names {
			if _, ok := plainInt(r.PathValue(n)); !ok {
				d.Upstream.ServeHTTP(w, r)
				return
			}
		}
		h.ServeHTTP(w, r)
	})
}

// plainInt reads digits only, up to 2^31-1.
func plainInt(s string) (int64, bool) {
	if s == "" || len(s) > 10 {
		return 0, false
	}
	for i := 0; i < len(s); i++ {
		if s[i] < '0' || s[i] > '9' {
			return 0, false
		}
	}
	n, err := strconv.ParseInt(s, 10, 64)
	return n, err == nil && n <= 1<<31-1
}

// pathInt is a parameter intPath let through.
func pathInt(r *http.Request, name string) int64 {
	n, _ := plainInt(r.PathValue(name))
	return n
}
