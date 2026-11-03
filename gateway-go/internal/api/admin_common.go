package api

import (
	"math"
	"math/big"
	"net/http"
	"time"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/auth"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/language"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyd"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/roles"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/server"
)

// The administration panels' routes (audit, SIEM, webhooks, backups, AI): what they share.

type adminHandler = func(http.ResponseWriter, *http.Request, *auth.Principal)

// adminRole is the guard of deps/auth.py: require_observer (an administrator or an observer,
// to READ a panel) or require_superuser (an EFFECTIVE administrator, to change it).
func adminRole(d *Deps, route string, admin bool, h adminHandler) http.Handler {
	return server.Authed(d.Auth, route, func(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
		rs, err := roles.Of(r.Context(), d.DB, p.User.ID, p.User.IsSuperuser, p.User.IsObserver)
		if err != nil {
			adminFail(d, w, "reading the roles", err)
			return
		}
		switch {
		case admin && !rs.Admin:
			adminDetail(w, http.StatusForbidden, "Admin privileges required")
		case !admin && !rs.Observer:
			adminDetail(w, http.StatusForbidden, "Administrative read access required")
		default:
			h(w, r, p)
		}
	})
}

// observerThenAdmin is a router guarded by require_observer holding a route guarded by
// require_superuser: the router's dependency runs first.
func observerThenAdmin(d *Deps, route string, h adminHandler) http.Handler {
	return server.Authed(d.Auth, route, func(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
		rs, err := roles.Of(r.Context(), d.DB, p.User.ID, p.User.IsSuperuser, p.User.IsObserver)
		if err != nil {
			adminFail(d, w, "reading the roles", err)
			return
		}
		switch {
		case !rs.Observer:
			adminDetail(w, http.StatusForbidden, "Administrative read access required")
		case !rs.Admin:
			adminDetail(w, http.StatusForbidden, "Admin privileges required")
		default:
			h(w, r, p)
		}
	})
}

// isAdmin is permissions.is_admin of the request's person.
func isAdmin(d *Deps, r *http.Request, p *auth.Principal) (bool, error) {
	rs, err := roles.Of(r.Context(), d.DB, p.User.ID, p.User.IsSuperuser, p.User.IsObserver)
	return rs.Admin, err
}

// adminFail is an unexpected error (the database's): logged, Starlette's plain-text 500.
func adminFail(d *Deps, w http.ResponseWriter, what string, err error) {
	d.Log.Error(what, "err", err)
	server.InternalError(w)
}

// adminDetail is an HTTPException: `{"detail": …}`.
func adminDetail(w http.ResponseWriter, status int, text string) {
	server.JSON(w, status, map[string]string{"detail": text})
}

// adminNone is a route declared with status_code=204 that returns None: FastAPI's
// JSONResponse with the body taken away, so still application/json.
func adminNone(w http.ResponseWriter) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(http.StatusNoContent)
}

// denyFromToken is routes/api_tokens.py deny_if_from_token: what a token must never do,
// refused in the request's language.
func denyFromToken(w http.ResponseWriter, r *http.Request, p *auth.Principal, table map[string]string) bool {
	if p.TokenName == "" {
		return false
	}
	adminDetail(w, http.StatusForbidden, language.Pick(table, r.Header.Get("Accept-Language")))
	return true
}

// utcISO is a UtcDateTime field: naive UTC written with +00:00; nil stays null.
func utcISO(t *time.Time) *string {
	if t == nil {
		return nil
	}
	s := pyjson.Isoformat(*t)
	return &s
}

// nowNaive is datetime.now(timezone.utc).replace(tzinfo=None): how the columns hold time.
func nowNaive() time.Time {
	u := time.Now().UTC()
	return time.Date(u.Year(), u.Month(), u.Day(), u.Hour(), u.Minute(), u.Second(), u.Nanosecond()/1000*1000, time.UTC)
}

// naiveUTC is a validated datetime as psycopg2 compares it with a timestamp column: an
// aware one at its UTC instant (the server's zone is UTC), a naive one as written.
func naiveUTC(d pyd.DateTime) time.Time {
	u := d.Time.UTC()
	return time.Date(u.Year(), u.Month(), u.Day(), u.Hour(), u.Minute(), u.Second(), u.Nanosecond(), time.UTC)
}

// int4 is a validated int compared with an integer column: psycopg2 writes it as a literal,
// so a value past the column's range just matches nothing; ok=false for those.
func int4(v any) (int64, bool) {
	switch x := v.(type) {
	case int64:
		return x, x >= math.MinInt32 && x <= math.MaxInt32
	case *big.Int:
		return 0, false
	}
	return 0, false
}

// int8 is a validated int as a bigint parameter (LIMIT, OFFSET): ok=false past it, where
// PostgreSQL refuses the literal (a 500 in Python).
func int8(v any) (int64, bool) {
	x, ok := v.(int64)
	return x, ok
}

// truthy is Python's bool() of a value json.loads gave.
func truthy(v any) bool {
	switch x := v.(type) {
	case nil:
		return false
	case bool:
		return x
	case int64:
		return x != 0
	case float64:
		return x != 0
	case string:
		return x != ""
	case pyjson.Object:
		return len(x) > 0
	case []any:
		return len(x) > 0
	}
	return true
}
