package main

import (
	"crypto/sha256"
	"encoding/hex"
	"net/http"
	"strconv"
	"time"

	"github.com/golang-jwt/jwt/v5"
)

// The people every suite may use, seeded once in both databases. Each has a session
// «s-<key>»; tokens are named in suite_auth.go.
//
//	admin        superuser by flag, in Sales
//	editor       in Sales, Watchers (observers' group) and Analysts, Italian
//	obs_flag     observer by flag
//	obs_group    observer by the Watchers group
//	admin_group  administrator by the Admins group
//	inactive     a disabled account
//	service      a service account (no password)
//	sso          no local password
//	writer       a plain person with no group: personal tokens hold
var peopleSpec = []struct {
	key, email                                     string
	superuser, observer, service, active, password bool
	locale                                         *string
}{
	{"admin", "admin@x.test", true, false, false, true, true, nil},
	{"editor", "editor@x.test", false, false, false, true, true, ptr("it")},
	{"obs_flag", "obs@x.test", false, true, false, true, true, nil},
	{"obs_group", "obsg@x.test", false, false, false, true, true, nil},
	{"admin_group", "adming@x.test", false, false, false, true, true, nil},
	{"inactive", "off@x.test", false, false, false, false, true, nil},
	{"service", "svc@x.test", false, false, true, true, false, nil},
	{"sso", "sso@x.test", false, false, false, true, false, nil},
	{"writer", "writer@x.test", false, false, false, true, true, nil},
}

// Groups by name, seeded with the people.
var Groups = map[string]int64{}

func ptr[T any](v T) *T { return &v }

func (w *World) seedPeople() {
	w.Exec(`TRUNCATE users, user_sessions, api_tokens, groups, user_groups, gateway_settings, projects, permissions,
		audit_logs RESTART IDENTITY CASCADE`)
	for _, p := range peopleSpec {
		var hash *string
		if p.password {
			hash = ptr("$2b$12$notarealhashnotarealhashnotarealhashnotarealhash12")
		}
		w.People[p.key] = w.Insert(`INSERT INTO users (email, full_name, hashed_password, is_active, is_superuser, is_observer,
			is_service, locale, created_at) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, '2026-01-01') RETURNING id`,
			p.email, "Person "+p.key, hash, p.active, p.superuser, p.observer, p.service, p.locale)
	}
	group := func(name string, admin, observer bool, members ...string) {
		Groups[name] = w.Insert(`INSERT INTO groups (name, description, is_admin, is_observer, created_at)
			VALUES ($1, '', $2, $3, '2026-01-01') RETURNING id`, name, admin, observer)
		for _, m := range members {
			w.Exec(`INSERT INTO user_groups (user_id, group_id) VALUES ($1, $2)`, w.People[m], Groups[name])
		}
	}
	group("Sales", false, false, "editor", "admin")
	group("Watchers", false, true, "obs_group", "editor")
	group("Admins", true, false, "admin_group")
	group("Analysts", false, false, "editor")
	for _, p := range peopleSpec {
		w.Session(p.key, "s-"+p.key, 5*time.Minute, time.Hour)
	}
}

// Session opens a session last used `idle` ago that ends in `left`.
func (w *World) Session(person, sid string, idle, left time.Duration) {
	now := time.Now().UTC()
	w.Exec(`INSERT INTO user_sessions (id, user_id, created_at, last_seen_at, expires_at, method) VALUES ($1, $2, $3, $4, $5, 'password')`,
		sid, w.People[person], now.Add(-time.Hour), now.Add(-idle), now.Add(left))
}

// Token adds an API token of the person.
func (w *World) Token(person, raw, scope string, left time.Duration) {
	sum := sha256.Sum256([]byte(raw))
	w.Exec(`INSERT INTO api_tokens (user_id, name, prefix, token_hash, scope, created_at, expires_at) VALUES ($1, 'script', $2, $3, $4, now(), $5)`,
		w.People[person], raw[:8], hex.EncodeToString(sum[:]), scope, time.Now().UTC().Add(left))
}

// JWT is a session's token as core/security.py signs it.
func (w *World) JWT(person, sid string) string {
	now := time.Now()
	t, err := jwt.NewWithClaims(jwt.SigningMethodHS256, jwt.MapClaims{
		"sub": strconv.FormatInt(w.People[person], 10), "sid": sid, "iat": now.Unix(), "exp": now.Add(time.Hour).Unix(),
	}).SignedString(w.secret)
	if err != nil {
		fail("%v", err)
	}
	return t
}

// As signs the request in with the person's session cookie, and the CSRF header a page sends.
func (w *World) As(person string) func(*http.Request) {
	tok := w.JWT(person, "s-"+person)
	return func(r *http.Request) {
		r.AddCookie(&http.Cookie{Name: "tab_token", Value: tok})
		r.Header.Set("X-Tabularia-Csrf", "1")
	}
}

// Cookie and Bearer put a credential as it is.
func Cookie(v string) func(*http.Request) {
	return func(r *http.Request) { r.AddCookie(&http.Cookie{Name: "tab_token", Value: v}) }
}

func Bearer(v string) func(*http.Request) {
	return func(r *http.Request) { r.Header.Set("Authorization", "Bearer "+v) }
}
