package auth

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"os"
	"strconv"
	"testing"
	"time"

	"github.com/golang-jwt/jwt/v5"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/clientip"
)

// The authentication on a PostgreSQL the Python migrations made (scripts/test.sh).

type fixedSettings struct {
	idle     int
	personal bool
}

func (s *fixedSettings) SessionIdleMinutes() int { return s.idle }
func (s *fixedSettings) PersonalTokens() bool    { return s.personal }

type world struct {
	t     *testing.T
	ctx   context.Context
	db    *pgxpool.Pool
	set   *fixedSettings
	now   time.Time
	authn *Authenticator
}

func newWorld(t *testing.T) *world {
	t.Helper()
	dsn := os.Getenv("TEST_DATABASE_URL")
	if dsn == "" {
		t.Skip("TEST_DATABASE_URL not set: run gateway-go/scripts/test.sh")
	}
	ctx := context.Background()
	db, err := pgxpool.New(ctx, dsn)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(db.Close)
	if _, err := db.Exec(ctx, `TRUNCATE users, user_sessions, api_tokens, groups, user_groups RESTART IDENTITY CASCADE`); err != nil {
		t.Fatal(err)
	}
	w := &world{t: t, ctx: ctx, db: db, set: &fixedSettings{personal: true}, now: time.Now().UTC().Truncate(time.Second)}
	w.authn = &Authenticator{
		DB: db, Secret: secret, Algorithm: "HS256", Settings: w.set,
		ClientIP: clientip.New([]string{"127.0.0.1"}), Log: slog.New(slog.NewTextHandler(io.Discard, nil)),
		Now: func() time.Time { return w.now },
	}
	return w
}

func (w *world) exec(q string, args ...any) {
	w.t.Helper()
	if _, err := w.db.Exec(w.ctx, q, args...); err != nil {
		w.t.Fatal(err)
	}
}

func (w *world) user(email string, superuser, observer, service, active bool) int64 {
	w.t.Helper()
	var id int64
	err := w.db.QueryRow(w.ctx, `INSERT INTO users (email, full_name, hashed_password, is_active, is_superuser, is_observer, is_service, created_at)
		VALUES ($1, '', 'x', $2, $3, $4, $5, $6) RETURNING id`, email, active, superuser, observer, service, naive(w.now)).Scan(&id)
	if err != nil {
		w.t.Fatal(err)
	}
	return id
}

func (w *world) group(name string, admin, observer bool, members ...int64) {
	w.t.Helper()
	var gid int64
	if err := w.db.QueryRow(w.ctx, `INSERT INTO groups (name, description, is_admin, is_observer, created_at) VALUES ($1, '', $2, $3, $4) RETURNING id`,
		name, admin, observer, naive(w.now)).Scan(&gid); err != nil {
		w.t.Fatal(err)
	}
	for _, m := range members {
		w.exec(`INSERT INTO user_groups (user_id, group_id) VALUES ($1, $2)`, m, gid)
	}
}

// session opens a session row last used `idle` ago; the token is the JWT core/security signs
func (w *world) session(userID int64, sid string, idle, left time.Duration) string {
	w.exec(`INSERT INTO user_sessions (id, user_id, created_at, last_seen_at, expires_at, method, ip, user_agent)
		VALUES ($1, $2, $3, $4, $5, 'password', '9.9.9.9', 'old agent')`,
		sid, userID, naive(w.now.Add(-time.Hour)), naive(w.now.Add(-idle)), naive(w.now.Add(left)))
	tok, err := jwt.NewWithClaims(jwt.SigningMethodHS256, jwt.MapClaims{
		"sub": strconv.FormatInt(userID, 10), "sid": sid, "iat": w.now.Unix(), "exp": w.now.Add(left).Unix(),
	}).SignedString(secret)
	if err != nil {
		w.t.Fatal(err)
	}
	return tok
}

func (w *world) token(userID int64, raw, scope string, left time.Duration) {
	sum := sha256.Sum256([]byte(raw))
	w.exec(`INSERT INTO api_tokens (user_id, name, prefix, token_hash, scope, created_at, expires_at)
		VALUES ($1, 'script', $2, $3, $4, $5, $6)`, userID, raw[:8], hex.EncodeToString(sum[:]), scope, naive(w.now), naive(w.now.Add(left)))
}

func cookieReq(method, tok string) *http.Request {
	r := httptest.NewRequest(method, "/x", nil)
	r.RemoteAddr = "127.0.0.1:4000"
	r.Header.Set("X-Forwarded-For", "5.6.7.8")
	r.Header.Set("X-Tabularia-Csrf", "1")
	r.AddCookie(&http.Cookie{Name: CookieName, Value: tok})
	return r
}

func headerReq(method, tok string) *http.Request {
	r := httptest.NewRequest(method, "/x", nil)
	r.RemoteAddr = "127.0.0.1:4000"
	r.Header.Set("X-Forwarded-For", "5.6.7.8")
	r.Header.Set("Authorization", "Bearer "+tok)
	return r
}

func (w *world) sessionRow(sid string) (ip, agent *string, seen time.Time) {
	w.t.Helper()
	if err := w.db.QueryRow(w.ctx, `SELECT ip, user_agent, last_seen_at FROM user_sessions WHERE id = $1`, sid).Scan(&ip, &agent, &seen); err != nil {
		w.t.Fatal(err)
	}
	return
}

func TestASessionSignsInAndItsActivityIsWrittenOnceAMinute(t *testing.T) {
	w := newWorld(t)
	uid := w.user("ada@example.com", false, false, false, true)
	tok := w.session(uid, "s1", 5*time.Minute, time.Hour)

	p, ref := w.authn.Authenticate(w.ctx, cookieReq("GET", tok), "/x")
	if ref != nil || p.User.ID != uid || p.SessionID != "s1" || p.User.Email != "ada@example.com" {
		t.Fatalf("%+v %v", p, ref)
	}
	ip, _, seen := w.sessionRow("s1")
	if ip == nil || *ip != "5.6.7.8" || !seen.Equal(naive(w.now)) {
		t.Errorf("activity not written: ip %v seen %v", ip, seen)
	}
	var userSeen time.Time
	w.db.QueryRow(w.ctx, `SELECT last_seen_at FROM users WHERE id = $1`, uid).Scan(&userSeen)
	if !userSeen.Equal(naive(w.now)) {
		t.Errorf("user's last seen %v", userSeen)
	}
	// 30 s later: nothing written
	w.exec(`UPDATE user_sessions SET ip = 'unchanged' WHERE id = 's1'`)
	w.now = w.now.Add(30 * time.Second)
	if _, ref := w.authn.Authenticate(w.ctx, cookieReq("GET", tok), "/x"); ref != nil {
		t.Fatal(ref)
	}
	if ip, _, _ := w.sessionRow("s1"); *ip != "unchanged" {
		t.Error("written again within the minute")
	}
}

func TestRefusedSessions(t *testing.T) {
	w := newWorld(t)
	uid := w.user("ada@example.com", false, false, false, true)
	off := w.user("off@example.com", false, false, false, false)
	expired := w.session(uid, "expired", time.Minute, -time.Second)
	// the JWT still valid, the row expired: the row decides
	w.exec(`UPDATE user_sessions SET expires_at = $1 WHERE id = 'expired'`, naive(w.now.Add(-time.Second)))
	closed := w.session(uid, "closed", time.Minute, time.Hour)
	w.exec(`DELETE FROM user_sessions WHERE id = 'closed'`)
	inactive := w.session(off, "inactive", time.Minute, time.Hour)
	other := w.session(uid, "other", time.Minute, time.Hour)
	w.exec(`UPDATE user_sessions SET user_id = $1 WHERE id = 'other'`, off) // a session of someone else
	for name, tok := range map[string]string{"expired": expired, "closed": closed, "inactive account": inactive, "another's session": other} {
		_, ref := w.authn.Authenticate(w.ctx, cookieReq("GET", tok), "/x")
		if ref == nil || ref.Status != 401 || ref.Header.Get("Set-Cookie") == "" {
			t.Errorf("%s: %+v", name, ref)
		}
		_, ref = w.authn.Authenticate(w.ctx, headerReq("GET", tok), "/x")
		if ref == nil || ref.Status != 401 || ref.Header.Get("Set-Cookie") != "" {
			t.Errorf("%s from the header must not delete the cookie: %+v", name, ref)
		}
	}
}

// sessions.validate: with the idle timeout, the minute of the activity's precision added
func TestTheIdleTimeout(t *testing.T) {
	w := newWorld(t)
	uid := w.user("ada@example.com", false, false, false, true)
	w.set.idle = 30
	fresh := w.session(uid, "fresh", 30*time.Minute+50*time.Second, time.Hour)
	still := w.session(uid, "still", 31*time.Minute+10*time.Second, time.Hour)
	if _, ref := w.authn.Authenticate(w.ctx, cookieReq("GET", fresh), "/x"); ref != nil {
		t.Errorf("within timeout + a minute: %v", ref)
	}
	if _, ref := w.authn.Authenticate(w.ctx, cookieReq("GET", still), "/x"); ref == nil {
		t.Error("left still past the timeout: accepted")
	}
	w.set.idle = 0
	if _, ref := w.authn.Authenticate(w.ctx, cookieReq("GET", still), "/x"); ref != nil {
		t.Errorf("without a timeout only the length counts: %v", ref)
	}
}

func TestAnObserversAddressIsNotKept(t *testing.T) {
	w := newWorld(t)
	obs := w.user("obs@example.com", false, false, false, true)
	w.group("observers", false, true, obs)
	tok := w.session(obs, "o1", 5*time.Minute, time.Hour)
	if _, ref := w.authn.Authenticate(w.ctx, cookieReq("GET", tok), "/x"); ref != nil {
		t.Fatal(ref)
	}
	if ip, agent, _ := w.sessionRow("o1"); ip != nil || agent != nil {
		t.Errorf("observer's address kept: %v %v", ip, agent)
	}
	// an administrator who is also in the observers' group is not «only an observer»
	adm := w.user("adm@example.com", false, false, false, true)
	w.group("admins", true, false, adm)
	w.exec(`INSERT INTO user_groups (user_id, group_id) SELECT $1, id FROM groups WHERE name = 'observers'`, adm)
	tok = w.session(adm, "a1", 5*time.Minute, time.Hour)
	w.authn.Authenticate(w.ctx, cookieReq("GET", tok), "/x")
	if ip, _, _ := w.sessionRow("a1"); ip == nil || *ip != "5.6.7.8" {
		t.Errorf("administrator's address not kept: %v", ip)
	}
}

func TestAPITokens(t *testing.T) {
	w := newWorld(t)
	ada := w.user("ada@example.com", false, false, false, true)
	svc := w.user("svc@example.com", false, true, true, true) // a service account flagged observer: still allowed
	obs := w.user("obs@example.com", false, true, false, true)
	w.token(ada, "tab_write_ada_0000000000", "write", time.Hour)
	w.token(ada, "tab_read_ada_00000000000", "read", time.Hour)
	w.token(svc, "tab_read_svc_00000000000", "read", time.Hour)
	w.token(obs, "tab_obs_000000000000000", "write", time.Hour)
	w.token(ada, "tab_old_ada_000000000000", "write", -time.Second)

	ok := func(name string, r *http.Request, route string) {
		t.Helper()
		if _, ref := w.authn.Authenticate(w.ctx, r, route); ref != nil {
			t.Errorf("%s: %v", name, ref)
		}
	}
	refused := func(name string, r *http.Request, route string, status int, detail string) {
		t.Helper()
		_, ref := w.authn.Authenticate(w.ctx, r, route)
		if ref == nil || ref.Status != status || (detail != "" && ref.Detail != detail) {
			t.Errorf("%s: %+v", name, ref)
		}
	}
	ok("write token, POST", headerReq("POST", "tab_write_ada_0000000000"), "/flows")
	ok("read token, GET", headerReq("GET", "tab_read_ada_00000000000"), "/flows")
	ok("read token, a POST that only reads", headerReq("POST", "tab_read_ada_00000000000"), "/reports/{report_id}/render")
	r := headerReq("POST", "tab_read_ada_00000000000")
	r.Header.Set("Accept-Language", "it-IT,it;q=0.9")
	refused("read token, a POST that changes", r, "/flows", 403, "Token di sola lettura: questa richiesta modifica dati")
	refused("expired token", headerReq("GET", "tab_old_ada_000000000000"), "/x", 401, "Invalid credentials")
	refused("unknown token", headerReq("GET", "tab_nobody"), "/x", 401, "")
	refused("an observer's personal token", headerReq("GET", "tab_obs_000000000000000"), "/x", 401, "")
	ok("a service account's token", headerReq("GET", "tab_read_svc_00000000000"), "/x")
	_, ref := w.authn.Authenticate(w.ctx, cookieReq("GET", "tab_write_ada_0000000000"), "/x")
	if ref == nil || ref.Status != 401 || ref.Header.Get("Set-Cookie") == "" {
		t.Errorf("a token in the cookie: %+v", ref)
	}
	w.set.personal = false
	refused("personal tokens off", headerReq("GET", "tab_write_ada_0000000000"), "/x", 401, "")
	ok("personal tokens off, a service account", headerReq("GET", "tab_read_svc_00000000000"), "/x")

	var ip *string
	var used *time.Time
	w.db.QueryRow(w.ctx, `SELECT last_used_ip, last_used_at FROM api_tokens WHERE token_hash = encode(sha256('tab_write_ada_0000000000'), 'hex')`).Scan(&ip, &used)
	if ip == nil || *ip != "5.6.7.8" || used == nil {
		t.Errorf("last use not written: %v %v", ip, used)
	}
}
