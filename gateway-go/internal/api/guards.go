package api

import (
	"math/big"
	"net/http"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/auth"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyd"
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

// pathInts reads the route's integer path parameters as FastAPI does, in the order the
// Python function declares them, and answers the 422 of every bad one. FastAPI validates them
// after the dependencies (authentication, the guards). An integer too big for int64 matches no
// row in Python (psycopg sends it as a numeric): 0, which no row has, does the same.
func pathInts(w http.ResponseWriter, r *http.Request, names ...string) ([]int64, bool) {
	var ps pyd.Params
	out, ok := pathIntsInto(&ps, r, names...)
	if resp := ps.Response(); resp != nil {
		resp.Write(w)
		return nil, false
	}
	return out, ok
}

// pathIntsInto validates the path parameters into `ps`, for a route that also has a body (the
// errors of both together).
func pathIntsInto(ps *pyd.Params, r *http.Request, names ...string) ([]int64, bool) {
	out := make([]int64, len(names))
	for i, n := range names {
		switch v := ps.Path(n, r.PathValue(n), pyd.Int()).(type) {
		case int64:
			out[i] = v
		case *big.Int:
			out[i] = 0
		}
	}
	return out, !ps.Failed()
}

// validated answers the 422 of the parameters, when there is one.
func validated(w http.ResponseWriter, ps *pyd.Params) bool {
	if resp := ps.Response(); resp != nil {
		resp.Write(w)
		return false
	}
	return true
}

// optStr, optInt and optBool read an Optional field of a validated body (nil = None).
func optStr(mv *pyd.ModelValue, name string) *string {
	if v, ok := mv.Get(name).(string); ok {
		return &v
	}
	return nil
}

func optInt(mv *pyd.ModelValue, name string) *int64 {
	switch v := mv.Get(name).(type) {
	case int64:
		return &v
	case *big.Int:
		zero := int64(0)
		return &zero
	}
	return nil
}

func optBool(mv *pyd.ModelValue, name string) *bool {
	if v, ok := mv.Get(name).(bool); ok {
		return &v
	}
	return nil
}
