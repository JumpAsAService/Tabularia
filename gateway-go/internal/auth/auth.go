package auth

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"log/slog"
	"net/http"
	"strconv"
	"strings"
	"time"

	"github.com/golang-jwt/jwt/v5"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/clientip"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/language"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/roles"
)

// User is the `users` row (models/user.py), the columns the gateway reads.
type User struct {
	ID             int64
	Email          string
	FullName       string
	HashedPassword *string
	IsActive       bool
	IsSuperuser    bool // the PERSONAL flag: permissions.IsAdmin adds the groups
	IsObserver     bool
	IsService      bool
	Locale         *string
	LastSeenAt     *time.Time
}

// Principal is who makes the request.
type Principal struct {
	User User
	// SessionID is the session's id (request.state.sid); empty with an API token
	SessionID string
	// TokenName and TokenScope with an API token (request.state.api_token, api_token_scope)
	TokenName  string
	TokenScope string

	sessionLastSeen time.Time
}

// Settings are the ones an administrator may change from the panel: read at each request.
type Settings interface {
	SessionIdleMinutes() int
	PersonalTokens() bool
}

// Authenticator holds what authenticating a request needs.
type Authenticator struct {
	DB           *pgxpool.Pool
	Secret       []byte
	Algorithm    string
	CookieDomain string
	Settings     Settings
	ClientIP     *clientip.Resolver
	Log          *slog.Logger
	// Now is the clock (tests move it); UTC
	Now func() time.Time
}

func (a *Authenticator) now() time.Time {
	if a.Now != nil {
		return a.Now().UTC()
	}
	return time.Now().UTC()
}

// Authenticate is get_current_user. `route` is the matched route's pattern, for the
// read-only tokens' POSTs that only read (api_tokens.READING_POSTS).
func (a *Authenticator) Authenticate(ctx context.Context, r *http.Request, route string) (*Principal, *Refusal) {
	cred, refusal := fromRequest(r)
	if refusal != nil {
		return nil, refusal
	}
	if strings.HasPrefix(cred.token, APITokenPrefix) {
		return a.apiToken(ctx, r, cred, route)
	}
	userID, sid, ok := a.parseJWT(cred.token)
	if !ok {
		return nil, a.refuse(r, cred)
	}
	p, err := a.sessionUser(ctx, userID, sid)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, a.refuse(r, cred) // user gone, session closed, expired or still
	}
	if err != nil {
		a.Log.Error("reading the session", "err", err)
		return nil, internalError()
	}
	if !p.User.IsActive {
		return nil, a.refuse(r, cred)
	}
	a.touchSession(ctx, r, p)
	return p, nil
}

func (a *Authenticator) refuse(r *http.Request, cred credential) *Refusal {
	return invalidCredentials(r, cred.fromCookie, a.CookieDomain)
}

// parseJWT is core/security.decode_access_token plus the reading of `sub` and `sid`: the
// signature with the configured algorithm only, `exp`, `nbf` and `iat` with a minute of
// leeway, `sub` a string holding a whole number, `sid` present.
func (a *Authenticator) parseJWT(token string) (int64, string, bool) {
	claims := jwt.MapClaims{}
	_, err := jwt.ParseWithClaims(token, claims, func(*jwt.Token) (any, error) { return a.Secret, nil },
		jwt.WithValidMethods([]string{a.Algorithm}), jwt.WithLeeway(ClockSkew), jwt.WithIssuedAt(),
		jwt.WithTimeFunc(a.now))
	if err != nil {
		return 0, "", false
	}
	sub, ok := claims["sub"].(string)
	if !ok {
		return 0, "", false
	}
	id, err := pythonInt(sub)
	if err != nil {
		return 0, "", false
	}
	raw, present := claims["sid"]
	if !present {
		// a token from before the sessions: it cannot be revoked, so it does not hold
		return 0, "", false
	}
	return id, pythonStr(raw), true
}

// pythonInt is Python's int() on a string: surrounding whitespace, a sign, underscores
// between digits.
func pythonInt(s string) (int64, error) {
	s = strings.TrimSpace(s)
	if strings.Contains(s, "__") || strings.HasPrefix(strings.TrimLeft(s, "+-"), "_") || strings.HasSuffix(s, "_") {
		return 0, strconv.ErrSyntax
	}
	return strconv.ParseInt(strings.ReplaceAll(s, "_", ""), 10, 64)
}

// pythonStr is str() of a JSON value, for `sid`: a string as it is; anything else as
// Python would print it (a session id is a string; this only keeps odd tokens refused)
func pythonStr(v any) string {
	switch x := v.(type) {
	case string:
		return x
	case nil:
		return "None"
	case bool:
		if x {
			return "True"
		}
		return "False"
	case float64:
		if x == float64(int64(x)) {
			return strconv.FormatInt(int64(x), 10)
		}
		return strconv.FormatFloat(x, 'g', -1, 64)
	}
	return "\x00not-a-session" // a list or an object: no session has such an id
}

const userColumns = `u.id, u.email, u.full_name, u.hashed_password, u.is_active, u.is_superuser,
	u.is_observer, u.is_service, u.locale, u.last_seen_at`

func scanUser(u *User) []any {
	return []any{&u.ID, &u.Email, &u.FullName, &u.HashedPassword, &u.IsActive, &u.IsSuperuser,
		&u.IsObserver, &u.IsService, &u.Locale, &u.LastSeenAt}
}

// sessionUser is _load_user: the user and their session in one SELECT, the session valid
// (sessions.validate: within its maximum length and, with the idle timeout, used recently;
// the minute the activity is written with is added to the timeout).
func (a *Authenticator) sessionUser(ctx context.Context, userID int64, sid string) (*Principal, error) {
	at := a.now()
	q := `SELECT ` + userColumns + `, s.last_seen_at
		FROM users u JOIN user_sessions s ON s.user_id = u.id
		WHERE u.id = $1 AND s.id = $2 AND s.expires_at > $3`
	args := []any{userID, sid, naive(at)}
	if minutes := a.Settings.SessionIdleMinutes(); minutes > 0 {
		q += ` AND s.last_seen_at >= $4`
		args = append(args, naive(at.Add(-time.Duration(minutes)*time.Minute-ActivityEvery)))
	}
	p := &Principal{SessionID: sid}
	if err := a.DB.QueryRow(ctx, q, args...).Scan(append(scanUser(&p.User), &p.sessionLastSeen)...); err != nil {
		return nil, err
	}
	return p, nil
}

// naive is how the gateway's columns hold time: UTC without a zone (gateway-datetime-utc).
func naive(t time.Time) time.Time {
	u := t.UTC()
	return time.Date(u.Year(), u.Month(), u.Day(), u.Hour(), u.Minute(), u.Second(), u.Nanosecond(), time.UTC)
}

func old(t *time.Time, now time.Time) bool {
	return t == nil || now.Sub(*t) >= ActivityEvery
}

// touchSession is _touch_last_seen: at most once a minute the session's and the user's
// last activity, with the address in the session — not for an observer, whose address is
// not kept (and is deleted). It never breaks the request.
func (a *Authenticator) touchSession(ctx context.Context, r *http.Request, p *Principal) {
	now := a.now()
	seen := p.sessionLastSeen
	if !old(p.User.LastSeenAt, now) && !old(&seen, now) {
		return
	}
	rs, err := roles.Of(ctx, a.DB, p.User.ID, p.User.IsSuperuser, p.User.IsObserver)
	observerOnly := rs.ObserverOnly()
	if err != nil {
		a.Log.Debug("last_seen not updated", "user", p.User.ID, "err", err)
		return
	}
	tx, err := a.DB.Begin(ctx)
	if err != nil {
		a.Log.Debug("last_seen not updated", "user", p.User.ID, "err", err)
		return
	}
	defer tx.Rollback(ctx) //nolint:errcheck — after a commit it does nothing
	if observerOnly {
		_, err = tx.Exec(ctx, `UPDATE user_sessions SET ip = NULL, user_agent = NULL, last_seen_at = $1 WHERE id = $2`, naive(now), p.SessionID)
	} else {
		_, err = tx.Exec(ctx, `UPDATE user_sessions SET ip = $1, last_seen_at = $2 WHERE id = $3`, nullable(a.ClientIP.Of(r)), naive(now), p.SessionID)
	}
	if err == nil {
		_, err = tx.Exec(ctx, `UPDATE users SET last_seen_at = $1 WHERE id = $2`, naive(now), p.User.ID)
	}
	if err == nil {
		err = tx.Commit(ctx)
	}
	if err != nil {
		a.Log.Debug("last_seen not updated", "user", p.User.ID, "err", err)
		return
	}
	n := naive(now)
	p.User.LastSeenAt = &n
}

func nullable(s string) *string {
	if s == "" {
		return nil
	}
	return &s
}

// readingPosts is api_tokens.READING_POSTS: the POSTs that only read, which a read-only
// token may use.
var readingPosts = map[string]bool{
	"/tasks/export": true, "/tasks/exports": true, "/tasks/preview": true,
	"/reports/{report_id}/render": true, "/dashboards/{dashboard_id}/data": true,
	"/dashboards/{dashboard_id}/render": true,
}

var readOnly = map[string]string{
	"en": "Read-only token: this request changes data",
	"it": "Token di sola lettura: questa richiesta modifica dati",
	"fr": "Jeton en lecture seule : cette requête modifie des données",
	"de": "Nur-Lese-Token: Diese Anfrage ändert Daten",
	"es": "Token de solo lectura: esta petición modifica datos",
}

// apiToken is _token_user: an API token (`tab_…`) only from the Authorization header,
// found by its fingerprint and not expired; the account active; a personal token only
// while personal tokens are on and never an observer's; a read-only token only for what
// changes nothing.
func (a *Authenticator) apiToken(ctx context.Context, r *http.Request, cred credential, route string) (*Principal, *Refusal) {
	if cred.fromCookie {
		return nil, a.refuse(r, cred)
	}
	sum := sha256.Sum256([]byte(cred.token))
	p := &Principal{}
	var tokenID int64
	var lastUsed *time.Time
	var adminG, observerG bool
	err := a.DB.QueryRow(ctx, `SELECT `+userColumns+`, t.id, t.name, t.scope, t.last_used_at,
			EXISTS (SELECT 1 FROM user_groups ug JOIN groups g ON g.id = ug.group_id WHERE ug.user_id = u.id AND g.is_admin),
			EXISTS (SELECT 1 FROM user_groups ug JOIN groups g ON g.id = ug.group_id WHERE ug.user_id = u.id AND g.is_observer)
		FROM users u JOIN api_tokens t ON t.user_id = u.id
		WHERE t.token_hash = $1 AND t.expires_at > $2`,
		hex.EncodeToString(sum[:]), naive(a.now()),
	).Scan(append(scanUser(&p.User), &tokenID, &p.TokenName, &p.TokenScope, &lastUsed, &adminG, &observerG)...)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, a.refuse(r, cred) // never existed, revoked or expired
	}
	if err != nil {
		a.Log.Error("reading the API token", "err", err)
		return nil, internalError()
	}
	u := &p.User
	if !u.IsActive {
		return nil, a.refuse(r, cred)
	}
	if !u.IsService && !a.Settings.PersonalTokens() {
		return nil, a.refuse(r, cred) // personal tokens turned off: the ones given already too
	}
	if !u.IsService && (u.IsObserver || observerG) && !(u.IsSuperuser || adminG) {
		return nil, a.refuse(r, cred) // observer only: no personal tokens
	}
	if route != levelCheckedByCaller && p.TokenScope == "read" && !safeMethods[r.Method] && !(r.Method == http.MethodPost && readingPosts[route]) {
		return nil, &Refusal{Status: http.StatusForbidden, Detail: language.Pick(readOnly, r.Header.Get("Accept-Language"))}
	}
	a.touchToken(ctx, r, p, tokenID, lastUsed, (u.IsObserver || observerG) && !(u.IsSuperuser || adminG))
	return p, nil
}

// touchToken is _touch_token: the token's last use and the user's activity, at most once
// a minute; an observer's address is not kept.
func (a *Authenticator) touchToken(ctx context.Context, r *http.Request, p *Principal, tokenID int64, lastUsed *time.Time, observerOnly bool) {
	now := a.now()
	if !old(lastUsed, now) && !old(p.User.LastSeenAt, now) {
		return
	}
	var ip *string
	if !observerOnly {
		if s := a.ClientIP.Of(r); s != "" {
			if len(s) > 64 {
				s = s[:64]
			}
			ip = &s
		}
	}
	tx, err := a.DB.Begin(ctx)
	if err != nil {
		a.Log.Debug("token's last use not updated", "user", p.User.ID, "err", err)
		return
	}
	defer tx.Rollback(ctx) //nolint:errcheck
	_, err = tx.Exec(ctx, `UPDATE api_tokens SET last_used_ip = $1, last_used_at = $2 WHERE id = $3`, ip, naive(now), tokenID)
	if err == nil {
		_, err = tx.Exec(ctx, `UPDATE users SET last_seen_at = $1 WHERE id = $2`, naive(now), p.User.ID)
	}
	if err == nil {
		err = tx.Commit(ctx)
	}
	if err != nil {
		a.Log.Debug("token's last use not updated", "user", p.User.ID, "err", err)
	}
}
