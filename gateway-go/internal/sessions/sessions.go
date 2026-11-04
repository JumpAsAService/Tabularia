// Package sessions is services/sessions.py: opening, listing and closing the sessions of
// user_sessions, and the HttpOnly cookie that carries a session's token to the browser.
// Recognizing a session is internal/auth's.
package sessions

import (
	"context"
	"crypto/rand"
	"encoding/base64"
	"errors"
	"fmt"
	"net/http"
	"strconv"
	"strings"
	"time"
	"unicode/utf8"

	"github.com/golang-jwt/jwt/v5"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/auth"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/roles"
)

// Querier is a pool, a connection or a transaction.
type Querier interface {
	Exec(ctx context.Context, sql string, args ...any) (pgconn.CommandTag, error)
	Query(ctx context.Context, sql string, args ...any) (pgx.Rows, error)
	QueryRow(ctx context.Context, sql string, args ...any) pgx.Row
}

// ErrSuperseded is SignInSuperseded: the verified password is no longer the account's (or the
// account was disabled) while the sign-in was going on.
var ErrSuperseded = errors.New("sign-in superseded")

// Session is a user_sessions row.
type Session struct {
	ID         string
	UserID     int64
	CreatedAt  time.Time
	LastSeenAt time.Time
	ExpiresAt  time.Time
	IP         *string
	UserAgent  *string
	Method     string
}

// Opener opens sessions: the JWT's secret, algorithm and maximum length.
type Opener struct {
	DB        *pgxpool.Pool
	Secret    []byte
	Algorithm string
	TTL       time.Duration // JWT__ACCESS_TTL_MINUTES
	Now       func() time.Time
}

func (o *Opener) now() time.Time {
	if o.Now != nil {
		return naive(o.Now())
	}
	return naive(time.Now())
}

func naive(t time.Time) time.Time {
	u := t.UTC()
	return time.Date(u.Year(), u.Month(), u.Day(), u.Hour(), u.Minute(), u.Second(), u.Nanosecond()/1000*1000, time.UTC)
}

// TokenURLSafe is secrets.token_urlsafe(n): n random bytes, base64url without padding.
func TokenURLSafe(n int) string {
	b := make([]byte, n)
	if _, err := rand.Read(b); err != nil {
		panic(err)
	}
	return base64.RawURLEncoding.EncodeToString(b)
}

// Open is open_session: the user's row read locked (FOR SHARE), the session born only while
// the password checked (`verified`, nil = not checked) is still the account's and the account
// active. `ip` and `agent` are the request's; of an observer neither is kept.
func (o *Opener) Open(ctx context.Context, userID int64, ip, agent string, method string, verified *string) (*Session, string, error) {
	tx, err := o.DB.Begin(ctx)
	if err != nil {
		return nil, "", err
	}
	defer tx.Rollback(ctx) //nolint:errcheck
	var hash *string
	var active, superuser, observer bool
	err = tx.QueryRow(ctx, `SELECT hashed_password, is_active, is_superuser, is_observer FROM users WHERE id = $1 FOR SHARE`, userID).
		Scan(&hash, &active, &superuser, &observer)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, "", ErrSuperseded
	}
	if err != nil {
		return nil, "", err
	}
	if !active || (verified != nil && (hash == nil || *hash != *verified)) {
		return nil, "", ErrSuperseded
	}
	rs, err := roles.Of(ctx, tx, userID, superuser, observer)
	if err != nil {
		return nil, "", err
	}
	at := o.now()
	s := &Session{ID: TokenURLSafe(24), UserID: userID, CreatedAt: at, LastSeenAt: at, ExpiresAt: at.Add(o.TTL), Method: method}
	if !rs.ObserverOnly() {
		if ip != "" {
			s.IP = &ip
		}
		if agent = firstRunes(agent, 512); agent != "" {
			s.UserAgent = &agent
		}
	}
	if _, err := tx.Exec(ctx, `INSERT INTO user_sessions (id, user_id, created_at, last_seen_at, expires_at, ip, user_agent, method)
		VALUES ($1, $2, $3, $4, $5, $6, $7, $8)`, s.ID, s.UserID, s.CreatedAt, s.LastSeenAt, s.ExpiresAt, s.IP, s.UserAgent, s.Method); err != nil {
		return nil, "", err
	}
	if err := tx.Commit(ctx); err != nil {
		return nil, "", err
	}
	token, err := o.Token(userID, s.ID, s.ExpiresAt)
	return s, token, err
}

// Token is core/security.py create_access_token: sub, sid, iat, exp.
func (o *Opener) Token(userID int64, sid string, expires time.Time) (string, error) {
	now := time.Now()
	method := jwt.GetSigningMethod(o.Algorithm)
	if method == nil {
		return "", fmt.Errorf("JWT algorithm %q", o.Algorithm)
	}
	return jwt.NewWithClaims(method, jwt.MapClaims{
		"sub": strconv.FormatInt(userID, 10), "sid": sid, "iat": now.Unix(), "exp": expires.Unix(),
	}).SignedString(o.Secret)
}

func firstRunes(s string, n int) string {
	if utf8.RuneCountInString(s) <= n {
		return s
	}
	return string([]rune(s)[:n])
}

// Close is close: one session (of `userID` when not 0); whether it was there.
func Close(ctx context.Context, q Querier, sid string, userID int64) (bool, error) {
	var tag pgconn.CommandTag
	var err error
	if userID != 0 {
		tag, err = q.Exec(ctx, `DELETE FROM user_sessions WHERE id = $1 AND user_id = $2`, sid, userID)
	} else {
		tag, err = q.Exec(ctx, `DELETE FROM user_sessions WHERE id = $1`, sid)
	}
	return tag.RowsAffected() > 0, err
}

// CloseAll is close_all: every session of the person but `except` (when not empty); how many.
func CloseAll(ctx context.Context, q Querier, userID int64, except string) (int64, error) {
	var tag pgconn.CommandTag
	var err error
	if except != "" {
		tag, err = q.Exec(ctx, `DELETE FROM user_sessions WHERE user_id = $1 AND id <> $2`, userID, except)
	} else {
		tag, err = q.Exec(ctx, `DELETE FROM user_sessions WHERE user_id = $1`, userID)
	}
	return tag.RowsAffected(), err
}

// OpenOnes is open_ones for one person: the sessions still valid (sessions.validate), newest
// activity first, at most `limit`.
func OpenOnes(ctx context.Context, q Querier, userID int64, idleMinutes int, limit int) ([]Session, error) {
	at := naive(time.Now())
	query := `SELECT id, user_id, created_at, last_seen_at, expires_at, ip, user_agent, method FROM user_sessions
		WHERE expires_at > $1`
	args := []any{at}
	if idleMinutes > 0 {
		query += ` AND last_seen_at >= $2`
		args = append(args, at.Add(-time.Duration(idleMinutes)*time.Minute-auth.ActivityEvery))
	}
	query += fmt.Sprintf(` AND user_id = $%d ORDER BY last_seen_at DESC LIMIT %d`, len(args)+1, limit)
	args = append(args, userID)
	rows, err := q.Query(ctx, query, args...)
	if err != nil {
		return nil, err
	}
	return pgx.CollectRows(rows, func(r pgx.CollectableRow) (Session, error) {
		var s Session
		err := r.Scan(&s.ID, &s.UserID, &s.CreatedAt, &s.LastSeenAt, &s.ExpiresAt, &s.IP, &s.UserAgent, &s.Method)
		return s, err
	})
}

// Get reads one session by id.
func Get(ctx context.Context, q Querier, sid string) (*Session, error) {
	var s Session
	err := q.QueryRow(ctx, `SELECT id, user_id, created_at, last_seen_at, expires_at, ip, user_agent, method FROM user_sessions WHERE id = $1`, sid).
		Scan(&s.ID, &s.UserID, &s.CreatedAt, &s.LastSeenAt, &s.ExpiresAt, &s.IP, &s.UserAgent, &s.Method)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, nil
	}
	return &s, err
}

// CookieOf is write_cookie's Set-Cookie, as Starlette writes it (Python's http.cookies puts
// the attributes in alphabetical order): it lasts as long as the session.
func CookieOf(r *http.Request, domain, token string, expires time.Time) string {
	seconds := int(expires.Sub(naive(time.Now())).Seconds())
	if seconds < 0 {
		seconds = 0
	}
	var b strings.Builder
	b.WriteString(auth.CookieName + "=" + token)
	if domain != "" {
		b.WriteString("; Domain=" + domain)
	}
	fmt.Fprintf(&b, "; HttpOnly; Max-Age=%d; Path=/; SameSite=lax", seconds)
	if auth.InHTTPS(r) {
		b.WriteString("; Secure")
	}
	return b.String()
}
