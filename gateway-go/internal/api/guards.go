package api

import (
	"net/http"
	"strconv"
	"strings"
	"unicode"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/auth"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/roles"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/server"
)

type authedFunc = func(http.ResponseWriter, *http.Request, *auth.Principal)

// superuser is deps/auth.py require_superuser: an EFFECTIVE administrator (the flag or an
// administrators' group), 403 otherwise.
func superuser(d *Deps, route string, h authedFunc) http.Handler {
	return server.Authed(d.Auth, route, func(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
		rs, err := roles.Of(r.Context(), d.DB, p.User.ID, p.User.IsSuperuser, p.User.IsObserver)
		if err != nil {
			d.Log.Error("reading the roles", "user", p.User.ID, "err", err)
			server.InternalError(w)
			return
		}
		if !rs.Admin {
			server.JSON(w, http.StatusForbidden, map[string]string{"detail": "Admin privileges required"})
			return
		}
		h(w, r, p)
	})
}

// observer is deps/auth.py require_observer: an administrator or an observer, to READ an
// administration panel.
func observer(d *Deps, route string, h authedFunc) http.Handler {
	return server.Authed(d.Auth, route, func(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
		rs, err := roles.Of(r.Context(), d.DB, p.User.ID, p.User.IsSuperuser, p.User.IsObserver)
		if err != nil {
			d.Log.Error("reading the roles", "user", p.User.ID, "err", err)
			server.InternalError(w)
			return
		}
		if !rs.Observer {
			server.JSON(w, http.StatusForbidden, map[string]string{"detail": "Administrative read access required"})
			return
		}
		h(w, r, p)
	})
}

// fail answers an error of the database (or any unexpected one) as Starlette does: logged,
// a plain-text 500.
func fail(d *Deps, w http.ResponseWriter, what string, err error) {
	d.Log.Error(what, "err", err)
	server.InternalError(w)
}

// detail answers an HTTPException: `{"detail": …}`.
func detail(w http.ResponseWriter, status int, text string) {
	server.JSON(w, status, map[string]string{"detail": text})
}

// noContent is a route that returns `Response(status_code=204)`: no body, no Content-Type.
func noContent(w http.ResponseWriter) {
	w.WriteHeader(http.StatusNoContent)
}

// returnsNone is a route declared with status_code=204 that returns None: FastAPI answers
// with its JSONResponse, an empty body that still says application/json.
func returnsNone(w http.ResponseWriter) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(http.StatusNoContent)
}

// validationError is one item of FastAPI's 422 (RequestValidationError).
type validationError struct {
	Type  string `json:"type"`
	Loc   []any  `json:"loc"`
	Msg   string `json:"msg"`
	Input any    `json:"input"`
}

// pathInts reads the route's integer path parameters as FastAPI does (pydantic's lax int
// from a string), in the order the Python function declares them: every bad one in the 422,
// written here. FastAPI validates them after the dependencies (authentication, the guards).
func pathInts(w http.ResponseWriter, r *http.Request, names ...string) ([]int64, bool) {
	out := make([]int64, len(names))
	var errs []validationError
	for i, n := range names {
		raw := r.PathValue(n)
		v, ok := pydanticInt(raw)
		if !ok {
			errs = append(errs, validationError{Type: "int_parsing", Loc: []any{"path", n},
				Msg: "Input should be a valid integer, unable to parse string as an integer", Input: raw})
			continue
		}
		out[i] = v
	}
	if len(errs) > 0 {
		server.JSON(w, http.StatusUnprocessableEntity, map[string]any{"detail": errs})
		return nil, false
	}
	return out, true
}

// pydanticInt is pydantic's int from a string in lax mode: surrounding (Unicode) whitespace,
// a sign, ASCII digits with single underscores between them, and a fraction of zeros only
// («5.00» is 5).
func pydanticInt(s string) (int64, bool) {
	s = strings.TrimFunc(s, unicode.IsSpace)
	if whole, frac, ok := strings.Cut(s, "."); ok {
		if frac == "" || strings.Trim(frac, "0") != "" {
			return 0, false
		}
		s = whole
	}
	digits := strings.TrimPrefix(strings.TrimPrefix(s, "+"), "-")
	if len(s)-len(digits) > 1 || digits == "" || digits[0] == '_' || digits[len(digits)-1] == '_' || strings.Contains(digits, "__") {
		return 0, false
	}
	for _, c := range digits {
		if (c < '0' || c > '9') && c != '_' {
			return 0, false
		}
	}
	v, err := strconv.ParseInt(strings.ReplaceAll(s, "_", ""), 10, 64)
	return v, err == nil
}
