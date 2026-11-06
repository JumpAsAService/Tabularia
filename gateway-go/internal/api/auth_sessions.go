package api

import (
	"crypto/subtle"
	"errors"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"time"
	"unicode"

	"github.com/jackc/pgx/v5"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/audit"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/auth"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/loginthrottle"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/passwords"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/personalfolders"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyd"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyd/gatewaymodels"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/reqinfo"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/roles"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/server"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/sessions"
)

// TokenOut is schemas/models.py Token.
type TokenOut struct {
	AccessToken string `json:"access_token"`
	TokenType   string `json:"token_type"`
}

// SessionOut is routes/auth.py SessionOut: an open session, as its owner sees it.
type SessionOut struct {
	ID         string  `json:"id"`
	CreatedAt  string  `json:"created_at"`
	LastSeenAt string  `json:"last_seen_at"`
	ExpiresAt  string  `json:"expires_at"`
	IP         *string `json:"ip"`
	UserAgent  *string `json:"user_agent"`
	Method     string  `json:"method"`
	Current    bool    `json:"current"`
}

// maxListing is routes/auth.py MAX_LISTING.
const maxListing = 200

func init() {
	register("auth.sessions", func(d *Deps) []server.Route {
		return []server.Route{
			{Method: http.MethodPost, Pattern: "/auth/login", Handler: withBody(anonymous(d, login(d)))},
			{Method: http.MethodGet, Pattern: "/auth/setup", Handler: anonymous(d, setupStatus(d))},
			{Method: http.MethodPost, Pattern: "/auth/setup", Handler: withBody(anonymous(d, setup(d)))},
			{Method: http.MethodPost, Pattern: "/auth/bootstrap", Handler: withBody(anonymous(d, bootstrap(d)))},
			{Method: http.MethodPost, Pattern: "/auth/logout", Handler: server.Authed(d.Auth, "/auth/logout", logout(d))},
			{Method: http.MethodGet, Pattern: "/auth/sessions", Handler: server.Authed(d.Auth, "/auth/sessions", mySessions(d))},
			{Method: http.MethodDelete, Pattern: "/auth/sessions/{sid}", Handler: server.Authed(d.Auth, "/auth/sessions/{sid}", closeMySession(d))},
			{Method: http.MethodPost, Pattern: "/auth/sessions/close-others", Handler: server.Authed(d.Auth, "/auth/sessions/close-others", closeOtherSessions(d))},
			{Method: http.MethodGet, Pattern: "/auth/observer", Handler: observer(d, "/auth/observer", func(w http.ResponseWriter, _ *http.Request, _ *auth.Principal) {
				returnsNone(w)
			})},
		}
	})
}

// anonymous is a route without authentication: the request's address and agent for the audit.
func anonymous(d *Deps, h http.HandlerFunc) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		h(w, reqinfo.With(r, d.Auth.ClientIP, nil))
	})
}

func (d *Deps) opener() *sessions.Opener {
	return &sessions.Opener{DB: d.DB, Secret: d.Config.JWTSecret, Algorithm: d.Config.JWTAlgorithm,
		TTL: time.Duration(d.Panel.AccessTTLMinutes()) * time.Minute}
}

var (
	nobodyOnce sync.Once
	nobodyHash string
)

// nobody is routes/auth.py _NOBODY: the hash of a password nobody knows, checked when the
// account has none, so that every refused sign-in costs the same bcrypt time.
func nobody() string {
	nobodyOnce.Do(func() {
		h, err := passwords.Hash(sessions.TokenURLSafe(24))
		if err != nil {
			panic(err)
		}
		nobodyHash = h
	})
	return nobodyHash
}

// clientIP is audit.client_ip: nil when the server knows none.
func clientIP(r *http.Request) *string {
	if ip := reqinfo.From(r.Context()).ClientIP; ip != "" {
		return &ip
	}
	return nil
}

// closeTheCookiesSession is sessions.close_the_cookies_session: a new sign-in from the same
// browser closes the session of the cookie it had.
func closeTheCookiesSession(d *Deps, r *http.Request) error {
	c, err := r.Cookie(auth.CookieName)
	if err != nil || c.Value == "" {
		return nil
	}
	sid, ok := d.Auth.TokenSessionID(c.Value)
	if !ok {
		return nil
	}
	_, err = sessions.Close(r.Context(), d.DB, sid, 0)
	return err
}

// signedIn writes the cookie and answers the token: in the body too, but not to the interface
// (which sends the anti-CSRF header and has the HttpOnly cookie).
func signedIn(d *Deps, w http.ResponseWriter, r *http.Request, status int, s *sessions.Session, token string) {
	w.Header().Add("Set-Cookie", sessions.CookieOf(r, d.Config.CookieDomain, token, s.ExpiresAt))
	if r.Header.Get(auth.CSRFHeader) != "" {
		token = ""
	}
	server.JSON(w, status, TokenOut{AccessToken: token, TokenType: "bearer"})
}

// login is routes/auth.py login: the brake first, bcrypt in every case, then the session.
func login(d *Deps) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		var ps pyd.Params
		body := ps.Body(bodyOf(r), gatewaymodels.LoginRequest)
		if !validated(w, &ps) {
			return
		}
		ctx := r.Context()
		email, password := body.Get("email").(string), body.Get("password").(string)
		ip := clientIP(r)
		failed := func(actor *auth.User, label, reason string) {
			audit.Record(ctx, d.DB, d.Log, audit.Event{Actor: actor, ActorLabel: label, Action: "auth.login_failed",
				Outcome: "failure", Detail: pyjson.Object{{Key: "reason", Value: reason}}})
		}
		wait, err := loginthrottle.RequestWait(ctx, d.DB, ip, email)
		if err != nil {
			fail(d, w, "reading the attempts", err)
			return
		}
		if wait > 0 {
			failed(nil, email, "throttled")
			tooMany(w, "Too many sign-in attempts. Try again in %d seconds.", wait)
			return
		}
		u, err := scanUserRow(d.DB.QueryRow(ctx, `SELECT `+userRowColumns+` FROM users WHERE email = $1 LIMIT 1`, email))
		if errors.Is(err, pgx.ErrNoRows) {
			u, err = nil, nil
		}
		if err != nil {
			fail(d, w, "reading the user", err)
			return
		}
		var verified *string
		if u != nil {
			verified = u.HashedPassword
		}
		usable := u != nil && !u.IsService && verified != nil && *verified != ""
		against := nobody()
		if usable {
			against = *verified
		}
		right := passwords.Verify(password, against)
		var actor *auth.User
		if u != nil {
			actor = &u.User
		}
		if !(usable && right) {
			failed(actor, email, "wrong credentials")
			if err := loginthrottle.RecordFailure(ctx, d.DB, ip, email); err != nil {
				fail(d, w, "counting the attempt", err)
				return
			}
			detail(w, http.StatusUnauthorized, "Incorrect email or password")
			return
		}
		if !u.IsActive {
			failed(actor, "", "deactivated")
			detail(w, http.StatusForbidden, "This user has been deactivated")
			return
		}
		if err := loginthrottle.RecordSuccess(ctx, d.DB, ip, email); err != nil {
			fail(d, w, "counting the attempt", err)
			return
		}
		if err := closeTheCookiesSession(d, r); err != nil {
			fail(d, w, "closing the previous session", err)
			return
		}
		s, token, err := d.opener().Open(ctx, u.ID, reqinfo.From(ctx).ClientIP, r.Header.Get("User-Agent"), "password", verified)
		if errors.Is(err, sessions.ErrSuperseded) {
			// the password changed (or the account was disabled) while checking
			failed(actor, email, "password changed while signing in")
			detail(w, http.StatusUnauthorized, "Incorrect email or password")
			return
		}
		if err != nil {
			fail(d, w, "opening the session", err)
			return
		}
		audit.Record(ctx, d.DB, d.Log, audit.Event{Actor: actor, Action: "auth.login"})
		// an account from before personal folders gets its own now
		personalfolders.Ensure(ctx, d.DB, d.Log, u.ID, actor, "", "sign-in")
		signedIn(d, w, r, http.StatusOK, s, token)
	}
}

// firstRun is first_run.enabled: AUTH__FIRST_RUN_SETUP, never in production.
func (d *Deps) firstRun() bool { return d.Config.FirstRunSetup && !d.Config.IsProduction() }

// setupStatus is routes/auth.py setup_status: whether the sign-in page asks for the first
// administrator.
func setupStatus(d *Deps) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		needed := false
		if d.firstRun() {
			var n int64
			if err := d.DB.QueryRow(r.Context(), `SELECT count(*) FROM users`).Scan(&n); err != nil {
				fail(d, w, "counting the accounts", err)
				return
			}
			needed = n == 0
		}
		server.JSON(w, http.StatusOK, map[string]bool{"needed": needed})
	}
}

// setup is routes/auth.py setup: the first administrator, once, signed in at once.
func setup(d *Deps) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		var ps pyd.Params
		body := ps.Body(bodyOf(r), gatewaymodels.SetupRequest)
		if !validated(w, &ps) {
			return
		}
		if !d.firstRun() {
			detail(w, http.StatusNotFound, "Not Found")
			return
		}
		ctx := r.Context()
		email := pyd.Lower(pyd.Strip(body.Get("email").(string)))
		if !strings.Contains(strings.Trim(email, "@"), "@") || pyd.Len(email) > 254 || strings.IndexFunc(email, unicode.IsSpace) >= 0 {
			detail(w, http.StatusUnprocessableEntity, "Enter a valid email address.")
			return
		}
		password := body.Get("password").(string)
		if problem := gatewaymodels.PasswordProblem(password); problem != "" {
			detail(w, http.StatusUnprocessableEntity, "Password: "+problem+".")
			return
		}
		tx, err := d.DB.Begin(ctx)
		if err != nil {
			fail(d, w, "creating the administrator", err)
			return
		}
		defer tx.Rollback(ctx) //nolint:errcheck
		// two first administrators cannot be created at once (first_run._SETUP_LOCK)
		var n int64
		if _, err = tx.Exec(ctx, `SELECT pg_advisory_xact_lock(742011)`); err == nil {
			err = tx.QueryRow(ctx, `SELECT count(*) FROM users`).Scan(&n)
		}
		if err != nil {
			fail(d, w, "creating the administrator", err)
			return
		}
		if n > 0 {
			detail(w, http.StatusConflict, "The administrator already exists: sign in.")
			return
		}
		name := []rune(pyd.Strip(body.Get("full_name").(string)))
		if len(name) > 200 {
			name = name[:200]
		}
		fullName := string(name)
		if fullName == "" {
			fullName = "Administrator"
		}
		hash, err := passwords.Hash(password)
		if err != nil {
			fail(d, w, "hashing the password", err)
			return
		}
		u, err := scanUserRow(tx.QueryRow(ctx, `INSERT INTO users (email, full_name, hashed_password, is_active, is_superuser,
			is_observer, is_service, created_at) VALUES ($1, $2, $3, true, true, false, false, $4) RETURNING `+userRowColumns,
			email, fullName, hash, time.Now().UTC()))
		if err == nil {
			err = tx.Commit(ctx)
		}
		if err != nil {
			fail(d, w, "creating the administrator", err)
			return
		}
		audit.Record(ctx, d.DB, d.Log, audit.Event{Actor: &u.User, Action: "auth.first_run_setup", TargetType: ptr("user"),
			TargetID: &u.ID, TargetLabel: &u.Email})
		personalfolders.Ensure(ctx, d.DB, d.Log, u.ID, &u.User, "", "first-run")
		s, token, err := d.opener().Open(ctx, u.ID, reqinfo.From(ctx).ClientIP, r.Header.Get("User-Agent"), "password", &hash)
		if err != nil {
			fail(d, w, "opening the session", err)
			return
		}
		audit.Record(ctx, d.DB, d.Log, audit.Event{Actor: &u.User, Action: "auth.login"})
		signedIn(d, w, r, http.StatusCreated, s, token)
	}
}

// bootstrapToken is first_run.bootstrap_token: the start-up token in SECURITY__KEYS_DIR.
func (d *Deps) bootstrapToken() string {
	if !d.firstRun() || d.Config.KeysDir == "" {
		return ""
	}
	b, err := os.ReadFile(filepath.Join(d.Config.KeysDir, "bootstrap.token"))
	if err != nil {
		return ""
	}
	return strings.TrimSpace(string(b))
}

// bootstrap is routes/auth.py bootstrap: the sample-data container's sign-in, the start-up
// token exchanged for a session as the first administrator.
func bootstrap(d *Deps) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		var ps pyd.Params
		body := ps.Body(bodyOf(r), gatewaymodels.BootstrapRequest)
		if !validated(w, &ps) {
			return
		}
		expected := d.bootstrapToken()
		if expected == "" {
			detail(w, http.StatusNotFound, "Not Found")
			return
		}
		ctx := r.Context()
		if subtle.ConstantTimeCompare([]byte(body.Get("token").(string)), []byte(expected)) != 1 {
			audit.Record(ctx, d.DB, d.Log, audit.Event{ActorLabel: "bootstrap token", Action: "auth.login_failed", Outcome: "failure",
				Detail: pyjson.Object{{Key: "reason", Value: "wrong start-up token"}}})
			detail(w, http.StatusUnauthorized, "Wrong start-up token")
			return
		}
		u, err := scanUserRow(d.DB.QueryRow(ctx, `SELECT `+userRowColumns+` FROM users
			WHERE is_superuser AND is_active AND NOT is_service ORDER BY id LIMIT 1`))
		if errors.Is(err, pgx.ErrNoRows) {
			detail(w, http.StatusConflict, "No administrator yet: create it at the sign-in page first.")
			return
		}
		if err != nil {
			fail(d, w, "reading the administrator", err)
			return
		}
		_, token, err := d.opener().Open(ctx, u.ID, reqinfo.From(ctx).ClientIP, r.Header.Get("User-Agent"), "bootstrap", nil)
		if err != nil {
			fail(d, w, "opening the session", err)
			return
		}
		audit.Record(ctx, d.DB, d.Log, audit.Event{Actor: &u.User, Action: "auth.bootstrap_login"})
		server.JSON(w, http.StatusOK, TokenOut{AccessToken: token, TokenType: "bearer"})
	}
}

// deleteCookie is sessions.delete_cookie on an answer.
func deleteCookie(d *Deps, w http.ResponseWriter, r *http.Request) {
	w.Header().Add("Set-Cookie", auth.DeletingCookie(r, d.Config.CookieDomain, time.Now()))
}

// logout is routes/auth.py logout: the session of the request closes, wherever its token was
// copied; the cookie goes.
func logout(d *Deps) authedFunc {
	return func(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
		ctx := r.Context()
		if p.SessionID != "" {
			if _, err := sessions.Close(ctx, d.DB, p.SessionID, p.User.ID); err != nil {
				fail(d, w, "closing the session", err)
				return
			}
		}
		audit.Record(ctx, d.DB, d.Log, audit.Event{Actor: &p.User, Action: "auth.logout"})
		deleteCookie(d, w, r)
		noContent(w)
	}
}

// shared is routes/auth.py _shared: an observer and nothing else, a public demo's guest whose
// account many sign in with.
func shared(d *Deps, r *http.Request, p *auth.Principal) (bool, error) {
	rs, err := roles.Of(r.Context(), d.DB, p.User.ID, p.User.IsSuperuser, p.User.IsObserver)
	return rs.ObserverOnly(), err
}

func sessionOut(s sessions.Session, current string) SessionOut {
	return SessionOut{ID: s.ID, CreatedAt: pyjson.Isoformat(s.CreatedAt), LastSeenAt: pyjson.Isoformat(s.LastSeenAt),
		ExpiresAt: pyjson.Isoformat(s.ExpiresAt), IP: s.IP, UserAgent: s.UserAgent, Method: s.Method, Current: s.ID == current}
}

// mySessions is routes/auth.py my_sessions: where one is connected; the observer only the
// session they ask from.
func mySessions(d *Deps) authedFunc {
	return func(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
		ctx := r.Context()
		obs, err := shared(d, r, p)
		if err != nil {
			fail(d, w, "reading the roles", err)
			return
		}
		out := []SessionOut{}
		if obs {
			if p.SessionID != "" {
				s, err := sessions.Get(ctx, d.DB, p.SessionID)
				if err != nil {
					fail(d, w, "reading the session", err)
					return
				}
				if s != nil {
					out = append(out, sessionOut(*s, p.SessionID))
				}
			}
			server.JSON(w, http.StatusOK, out)
			return
		}
		list, err := sessions.OpenOnes(ctx, d.DB, p.User.ID, d.Panel.SessionIdleMinutes(), maxListing)
		if err != nil {
			fail(d, w, "reading the sessions", err)
			return
		}
		for _, s := range list {
			out = append(out, sessionOut(s, p.SessionID))
		}
		server.JSON(w, http.StatusOK, out)
	}
}

const observersOwnOnly = "Observers can only close their own session"

// closeMySession is routes/auth.py close_my_session: one of one's own sessions (the others'
// are not seen: 404); the observer only the one they ask from.
func closeMySession(d *Deps) authedFunc {
	return func(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
		ctx, sid := r.Context(), r.PathValue("sid")
		current := p.SessionID != "" && sid == p.SessionID
		if !current {
			obs, err := shared(d, r, p)
			if err != nil {
				fail(d, w, "reading the roles", err)
				return
			}
			if obs {
				detail(w, http.StatusForbidden, observersOwnOnly)
				return
			}
		}
		closed, err := sessions.Close(ctx, d.DB, sid, p.User.ID)
		if err != nil {
			fail(d, w, "closing the session", err)
			return
		}
		if !closed {
			detail(w, http.StatusNotFound, "Session not found")
			return
		}
		audit.Record(ctx, d.DB, d.Log, audit.Event{Actor: &p.User, Action: "auth.session_revoke", TargetType: ptr("user"),
			TargetID: &p.User.ID, TargetLabel: &p.User.Email,
			Detail: pyjson.Object{{Key: "sessions", Value: 1}, {Key: "current", Value: current}}})
		if current {
			deleteCookie(d, w, r)
		}
		noContent(w)
	}
}

// closeOtherSessions is routes/auth.py close_other_sessions: «sign out of the other devices».
func closeOtherSessions(d *Deps) authedFunc {
	return func(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
		ctx := r.Context()
		obs, err := shared(d, r, p)
		if err != nil {
			fail(d, w, "reading the roles", err)
			return
		}
		if obs {
			detail(w, http.StatusForbidden, observersOwnOnly)
			return
		}
		n, err := sessions.CloseAll(ctx, d.DB, p.User.ID, p.SessionID)
		if err != nil {
			fail(d, w, "closing the sessions", err)
			return
		}
		if n > 0 {
			audit.Record(ctx, d.DB, d.Log, audit.Event{Actor: &p.User, Action: "auth.session_revoke", TargetType: ptr("user"),
				TargetID: &p.User.ID, TargetLabel: &p.User.Email, Detail: pyjson.Object{{Key: "sessions", Value: n}}})
		}
		returnsNone(w)
	}
}
