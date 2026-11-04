package api

// What the routes of the scripting modules (sql_scripts, scripts, scripting, registries,
// notebooks…) share: the integer path parameters as FastAPI reads them, the forwarding of a
// request to the Python gateway when a feature the port leaves there is on, and the small
// helpers of their answers.

import (
	"bytes"
	"context"
	"errors"
	"io"
	"math/big"
	"net/http"

	"github.com/jackc/pgx/v5"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/auth"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyd"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/server"
)

// scFromPyd turns a value pyd validated (its *Object dicts) into pyjson's (Object), the
// form the ported services work with.
func scFromPyd(v any) any {
	switch x := v.(type) {
	case *pyd.Object:
		out := make(pyjson.Object, 0, x.Len())
		for _, k := range x.Keys() {
			e, _ := x.Get(k)
			out = append(out, pyjson.KV{Key: k, Value: scFromPyd(e)})
		}
		return out
	case []any:
		out := make([]any, len(x))
		for i, e := range x {
			out[i] = scFromPyd(e)
		}
		return out
	case *big.Int:
		f, _ := new(big.Float).SetInt(x).Float64()
		return f
	}
	return v
}

// scBodyHandler is a route's handler with its body as FastAPI read it.
type scBodyHandler func(d *Deps, w http.ResponseWriter, r *http.Request, p *auth.Principal, body pyd.Body)

// scWithBody is withBody (FastAPI's body read before the dependencies), then the
// authentication, then the handler with the body.
func scWithBody(d *Deps, pattern string, h scBodyHandler) http.Handler {
	return withBody(server.Authed(d.Auth, pattern, func(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
		h(d, w, r, p, bodyOf(r))
	}))
}

// scForward hands the request to the Python gateway as it came, its body included: the
// port leaves there what depends on a feature it does not carry (sqlglot's or croniter's reading of a text, the cluster's pods).
func scForward(d *Deps, w http.ResponseWriter, r *http.Request) {
	if raw := rawOf(r); raw != nil {
		r.Body = io.NopCloser(bytes.NewReader(raw))
		r.ContentLength = int64(len(raw))
	}
	d.Upstream.ServeHTTP(w, r)
}

// scRefuseWith answers an HTTPException whose detail is not a text (a dict the page reads).
func scRefuseWith(w http.ResponseWriter, status int, why any) {
	server.JSON(w, status, map[string]any{"detail": why})
}

// scNoRows tells a missing row from a failure.
func scNoRows(err error) bool { return errors.Is(err, pgx.ErrNoRows) }

// scInTx runs fn in a transaction, committed when fn returns nil.
func scInTx(ctx context.Context, d *Deps, fn func(tx pgx.Tx) error) error {
	tx, err := d.DB.Begin(ctx)
	if err != nil {
		return err
	}
	defer func() { _ = tx.Rollback(ctx) }()
	if err := fn(tx); err != nil {
		return err
	}
	return tx.Commit(ctx)
}

func scPtrStr(s string) *string { return &s }
func scPtrInt(i int64) *int64   { return &i }

// scClip is Python's text[:n], in code points.
func scClip(s string, n int) string {
	r := []rune(s)
	if len(r) > n {
		return string(r[:n])
	}
	return s
}

// scEngineDetail is `resp.json().get("detail")` said as a text, or the body itself when it
// is not JSON (the ValueError branch). false: the JSON is not an object, whose `.get` is an
// AttributeError in Python (a 500).
func scEngineDetail(raw []byte) (string, bool) {
	v, err := pyjson.Loads(string(raw))
	if err != nil {
		return string(raw), true
	}
	o, ok := v.(pyjson.Object)
	if !ok {
		return "", false
	}
	d, _ := o.Get("detail")
	return pyjson.Str(d), true
}

// scRelay answers the engine's JSON as the route returns it (resp.json()).
func scRelay(w http.ResponseWriter, raw []byte) {
	v, err := pyjson.Loads(string(raw))
	if err != nil {
		server.InternalError(w)
		return
	}
	server.JSON(w, http.StatusOK, v)
}
