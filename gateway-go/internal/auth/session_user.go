package auth

import (
	"context"
	"errors"
	"net/http"
	"strings"

	"github.com/jackc/pgx/v5"
)

// SessionUser is deps/auth.py session_user: the person of Tabularia's session `sid`, or nil —
// the session still open and the account active, its activity touched as any request's (the
// notebooks' proxy, whose frames hold while the session that opened them does).
func (a *Authenticator) SessionUser(ctx context.Context, r *http.Request, userID int64, sid string) (*Principal, error) {
	p, err := a.sessionUser(ctx, userID, sid)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	if !p.User.IsActive {
		return nil, nil
	}
	a.touchSession(ctx, r, p)
	return p, nil
}

// CookieUser is deps/auth.py cookie_user: the person of the session cookie, without the
// anti-CSRF header, never an API token; nil when there is none.
func (a *Authenticator) CookieUser(ctx context.Context, r *http.Request) (*Principal, error) {
	c, err := r.Cookie(CookieName)
	if err != nil || c.Value == "" || strings.HasPrefix(c.Value, APITokenPrefix) {
		return nil, nil
	}
	userID, sid, ok := a.parseJWT(c.Value)
	if !ok {
		return nil, nil
	}
	return a.SessionUser(ctx, r, userID, sid)
}
