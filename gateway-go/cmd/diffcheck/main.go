// diffcheck sends the same requests to the Python gateway and to the Go gateway, both on the
// same disposable database (scripts/diffcheck.sh), and fails on the first difference: the
// status, the JSON, the headers the browser and the proxies read, and the rows the request
// writes (docs/specs/gateway-go.md, «Compatibility contract»).
package main

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"reflect"
	"regexp"
	"strconv"
	"strings"
	"time"

	"github.com/golang-jwt/jwt/v5"
	"github.com/jackc/pgx/v5/pgxpool"
)

// headers compared as they are; Set-Cookie with its moment of expiry blanked
var compared = []string{"Content-Type", "Www-Authenticate", "Location", "Cache-Control", "Content-Disposition"}

var expires = regexp.MustCompile(`expires=[^;]+`)

type world struct {
	ctx    context.Context
	db     *pgxpool.Pool
	secret []byte
	py, gw string
	people map[string]int64
}

// a request of the corpus: who asks, how, and what
type request struct {
	name   string
	method string
	path   string
	auth   func(*http.Request)
	header map[string]string
}

type answer struct {
	Status  int
	Headers map[string]string
	Body    any
	Rows    map[string]any
}

func main() {
	w := &world{ctx: context.Background(), secret: []byte(must("DIFF_SECRET")), py: must("DIFF_PYTHON"), gw: must("DIFF_GO"), people: map[string]int64{}}
	db, err := pgxpool.New(w.ctx, must("DIFF_DSN"))
	check(err)
	w.db = db
	w.seed()

	corpus := w.corpus()
	fail := 0
	for _, phase := range []struct {
		name  string
		panel map[string]string
	}{
		{"environment's settings", nil},
		// the panel's values, which both gateways read again every ten seconds
		{"panel: idle timeout 30 min, no personal tokens", map[string]string{"auth.session_idle_minutes": "30", "auth.personal_tokens": "false", "auth.api_token_max_days": "90"}},
	} {
		w.exec(`DELETE FROM gateway_settings`)
		for k, v := range phase.panel {
			w.exec(`INSERT INTO gateway_settings (key, value, updated_at) VALUES ($1, $2, now())`, k, v)
		}
		if phase.panel != nil {
			time.Sleep(11 * time.Second)
		}
		fmt.Printf("── %s\n", phase.name)
		for _, r := range corpus {
			a, b := w.ask(w.py, r), w.ask(w.gw, r)
			if diff := differences(a, b); diff != "" {
				fail++
				fmt.Printf("  DIFF  %s %s %s\n%s\n", r.method, r.path, r.name, diff)
				continue
			}
			fmt.Printf("  same  %-6s %-10s %-48s %d\n", r.method, r.path, r.name, a.Status)
		}
	}
	if fail > 0 {
		fmt.Printf("%d differences\n", fail)
		os.Exit(1)
	}
	fmt.Printf("%d requests, no difference\n", 2*len(corpus))
}

// ── the people and their credentials ─────────────────────────────────────────

func (w *world) seed() {
	w.exec(`TRUNCATE users, user_sessions, api_tokens, groups, user_groups, gateway_settings RESTART IDENTITY CASCADE`)
	type person struct {
		key, email                           string
		superuser, observer, service, active bool
		password                             bool
		locale                               *string
	}
	it := "it"
	for _, p := range []person{
		{"admin", "admin@x.test", true, false, false, true, true, nil},
		{"editor", "editor@x.test", false, false, false, true, true, &it},
		{"obs_flag", "obs@x.test", false, true, false, true, true, nil},
		{"obs_group", "obsg@x.test", false, false, false, true, true, nil},
		{"admin_group", "adming@x.test", false, false, false, true, true, nil},
		{"inactive", "off@x.test", false, false, false, false, true, nil},
		{"service", "svc@x.test", false, false, true, true, false, nil},
		{"sso", "sso@x.test", false, false, false, true, false, nil},
		{"writer", "writer@x.test", false, false, false, true, true, nil}, // no observer's role: personal tokens hold
	} {
		var hash *string
		if p.password {
			h := "$2b$12$notarealhashnotarealhashnotarealhashnotarealhash12"
			hash = &h
		}
		var id int64
		check(w.db.QueryRow(w.ctx, `INSERT INTO users (email, full_name, hashed_password, is_active, is_superuser, is_observer, is_service, locale, created_at)
			VALUES ($1, $2, $3, $4, $5, $6, $7, $8, now()) RETURNING id`, p.email, "Person "+p.key, hash, p.active, p.superuser, p.observer, p.service, p.locale).Scan(&id))
		w.people[p.key] = id
	}
	group := func(name string, admin, observer bool, members ...string) {
		var gid int64
		check(w.db.QueryRow(w.ctx, `INSERT INTO groups (name, description, is_admin, is_observer, created_at) VALUES ($1, '', $2, $3, now()) RETURNING id`, name, admin, observer).Scan(&gid))
		for _, m := range members {
			w.exec(`INSERT INTO user_groups (user_id, group_id) VALUES ($1, $2)`, w.people[m], gid)
		}
	}
	group("Sales", false, false, "editor", "admin")
	group("Watchers", false, true, "obs_group", "editor")
	group("Admins", true, false, "admin_group")
	group("Analysts", false, false, "editor")

	for key := range w.people {
		w.session(key, "s-"+key, 5*time.Minute, time.Hour)
	}
	w.session("editor", "s-expired", time.Minute, -time.Minute)
	w.session("editor", "s-still", 40*time.Minute, time.Hour)
	w.token("editor", "tab_editor_write_0000000000", "write", time.Hour)
	w.token("editor", "tab_editor_read_00000000000", "read", time.Hour)
	w.token("editor", "tab_editor_old_000000000000", "write", -time.Minute)
	w.token("service", "tab_service_read_0000000000", "read", time.Hour)
	w.token("writer", "tab_writer_write_0000000000", "write", time.Hour)
	w.token("writer", "tab_writer_read_00000000000", "read", time.Hour)
	w.token("obs_group", "tab_observer_00000000000000", "write", time.Hour)
}

func (w *world) session(person, sid string, idle, left time.Duration) {
	now := time.Now().UTC()
	w.exec(`INSERT INTO user_sessions (id, user_id, created_at, last_seen_at, expires_at, method) VALUES ($1, $2, $3, $4, $5, 'password')`,
		sid, w.people[person], now.Add(-time.Hour), now.Add(-idle), now.Add(left))
}

func (w *world) jwt(person, sid string) string {
	now := time.Now()
	t, err := jwt.NewWithClaims(jwt.SigningMethodHS256, jwt.MapClaims{
		"sub": strconv.FormatInt(w.people[person], 10), "sid": sid, "iat": now.Unix(), "exp": now.Add(time.Hour).Unix(),
	}).SignedString(w.secret)
	check(err)
	return t
}

func (w *world) token(person, raw, scope string, left time.Duration) {
	sum := sha256.Sum256([]byte(raw))
	w.exec(`INSERT INTO api_tokens (user_id, name, prefix, token_hash, scope, created_at, expires_at) VALUES ($1, 'script', $2, $3, $4, now(), $5)`,
		w.people[person], raw[:8], hex.EncodeToString(sum[:]), scope, time.Now().UTC().Add(left))
}

// ── the corpus ───────────────────────────────────────────────────────────────

func cookie(v string) func(*http.Request) {
	return func(r *http.Request) { r.AddCookie(&http.Cookie{Name: "tab_token", Value: v}) }
}

func bearer(v string) func(*http.Request) {
	return func(r *http.Request) { r.Header.Set("Authorization", "Bearer "+v) }
}

func (w *world) corpus() []request {
	var c []request
	add := func(name, method, path string, auth func(*http.Request), h map[string]string) {
		c = append(c, request{name, method, path, auth, h})
	}
	for _, p := range []string{"admin", "editor", "obs_flag", "obs_group", "admin_group", "inactive", "service", "sso", "writer"} {
		add(p+" · cookie", "GET", "/auth/me", cookie(w.jwt(p, "s-"+p)), nil)
		add(p+" · header", "GET", "/auth/me", bearer(w.jwt(p, "s-"+p)), nil)
	}
	add("nobody", "GET", "/auth/me", nil, nil)
	add("a cookie that is no token", "GET", "/auth/me", cookie("not.a.token"), nil)
	add("a header that is no token", "GET", "/auth/me", bearer("not.a.token"), nil)
	add("Basic, then the cookie", "GET", "/auth/me", func(r *http.Request) {
		r.Header.Set("Authorization", "Basic eDp5")
		cookie(w.jwt("editor", "s-editor"))(r)
	}, nil)
	add("expired session", "GET", "/auth/me", cookie(w.jwt("editor", "s-expired")), nil)
	add("session still for 40 min", "GET", "/auth/me", cookie(w.jwt("editor", "s-still")), nil)
	add("someone else's session", "GET", "/auth/me", cookie(w.jwt("admin", "s-editor")), nil)
	add("closed session", "GET", "/auth/me", cookie(w.jwt("editor", "s-gone")), nil)
	add("an observer's write token (the editor is in Watchers)", "GET", "/auth/me", bearer("tab_editor_write_0000000000"), nil)
	add("an observer's read token", "GET", "/auth/me", bearer("tab_editor_read_00000000000"), nil)
	add("a personal write token", "GET", "/auth/me", bearer("tab_writer_write_0000000000"), nil)
	add("a personal read token", "GET", "/auth/me", bearer("tab_writer_read_00000000000"), nil)
	add("expired token", "GET", "/auth/me", bearer("tab_editor_old_000000000000"), nil)
	add("service account's token", "GET", "/auth/me", bearer("tab_service_read_0000000000"), nil)
	add("an observer's personal token", "GET", "/auth/me", bearer("tab_observer_00000000000000"), nil)
	add("a token in the cookie", "GET", "/auth/me", cookie("tab_editor_write_0000000000"), nil)
	add("a personal read token, in Italian", "GET", "/auth/me", bearer("tab_writer_read_00000000000"), map[string]string{"Accept-Language": "it-IT,it;q=0.9"})
	// not ported: forwarded to Python, which must answer the same through the Go gateway
	add("forwarded: POST without the CSRF header", "POST", "/auth/me", cookie(w.jwt("editor", "s-editor")), nil)
	add("forwarded: HEAD", "HEAD", "/auth/me", cookie(w.jwt("editor", "s-editor")), nil)
	add("forwarded: a list", "GET", "/projects", cookie(w.jwt("admin", "s-admin")), nil)
	add("forwarded: a read token writing", "POST", "/projects", bearer("tab_writer_read_00000000000"), map[string]string{"Accept-Language": "it"})
	return c
}

// ── asking and comparing ─────────────────────────────────────────────────────

// the rows a request may write, put back before each request so that both find the same
func (w *world) reset() {
	w.exec(`UPDATE users SET last_seen_at = NULL`)
	w.exec(`UPDATE user_sessions SET ip = '9.9.9.9', user_agent = 'old agent', last_seen_at = now() - interval '5 minutes' WHERE id NOT IN ('s-still')`)
	w.exec(`UPDATE user_sessions SET ip = '9.9.9.9', user_agent = 'old agent', last_seen_at = now() - interval '40 minutes' WHERE id = 's-still'`)
	w.exec(`UPDATE api_tokens SET last_used_at = NULL, last_used_ip = NULL`)
}

func (w *world) rows() map[string]any {
	out := map[string]any{}
	q, err := w.db.Query(w.ctx, `SELECT email, last_seen_at IS NOT NULL AND last_seen_at > now() - interval '1 minute' FROM users ORDER BY id`)
	check(err)
	for q.Next() {
		var e string
		var fresh bool
		check(q.Scan(&e, &fresh))
		out["user "+e+" seen now"] = fresh
	}
	q.Close()
	q, err = w.db.Query(w.ctx, `SELECT id, coalesce(ip, 'NULL'), coalesce(user_agent, 'NULL'), last_seen_at > now() - interval '1 minute' FROM user_sessions ORDER BY id`)
	check(err)
	for q.Next() {
		var id, ip, agent string
		var fresh bool
		check(q.Scan(&id, &ip, &agent, &fresh))
		out["session "+id] = fmt.Sprintf("ip=%s agent=%s seen now=%v", ip, agent, fresh)
	}
	q.Close()
	q, err = w.db.Query(w.ctx, `SELECT prefix, coalesce(last_used_ip, 'NULL'), last_used_at IS NOT NULL FROM api_tokens ORDER BY id`)
	check(err)
	for q.Next() {
		var p, ip string
		var used bool
		check(q.Scan(&p, &ip, &used))
		out["token "+p] = fmt.Sprintf("ip=%s used=%v", ip, used)
	}
	q.Close()
	return out
}

func (w *world) ask(base string, r request) answer {
	w.reset()
	req, err := http.NewRequest(r.method, base+r.path, nil)
	check(err)
	req.Header.Set("X-Forwarded-For", "5.6.7.8")
	req.Header.Set("User-Agent", "diffcheck")
	for k, v := range r.header {
		req.Header.Set(k, v)
	}
	if r.auth != nil {
		r.auth(req)
	}
	res, err := http.DefaultClient.Do(req)
	check(err)
	raw, _ := io.ReadAll(res.Body)
	res.Body.Close()
	a := answer{Status: res.StatusCode, Headers: map[string]string{}, Rows: w.rows()}
	for _, h := range compared {
		if v := res.Header.Get(h); v != "" {
			a.Headers[h] = v
		}
	}
	if v := res.Header.Values("Set-Cookie"); len(v) > 0 {
		a.Headers["Set-Cookie"] = expires.ReplaceAllString(strings.Join(v, " | "), "expires=…")
	}
	var parsed any
	if json.Unmarshal(raw, &parsed) == nil {
		a.Body = parsed
	} else {
		a.Body = string(bytes.TrimSpace(raw))
	}
	return a
}

func differences(py, gw answer) string {
	var out []string
	if py.Status != gw.Status {
		out = append(out, fmt.Sprintf("    status: python %d, go %d", py.Status, gw.Status))
	}
	if !reflect.DeepEqual(py.Headers, gw.Headers) {
		out = append(out, fmt.Sprintf("    headers:\n      python %v\n      go     %v", py.Headers, gw.Headers))
	}
	if !reflect.DeepEqual(py.Body, gw.Body) {
		p, _ := json.Marshal(py.Body)
		g, _ := json.Marshal(gw.Body)
		out = append(out, fmt.Sprintf("    body:\n      python %s\n      go     %s", p, g))
	}
	for k, v := range py.Rows {
		if !reflect.DeepEqual(v, gw.Rows[k]) {
			out = append(out, fmt.Sprintf("    %s: python %v, go %v", k, v, gw.Rows[k]))
		}
	}
	return strings.Join(out, "\n")
}

func (w *world) exec(q string, args ...any) {
	_, err := w.db.Exec(w.ctx, q, args...)
	check(err)
}

func must(name string) string {
	v := os.Getenv(name)
	if v == "" {
		fmt.Fprintf(os.Stderr, "%s is not set: run scripts/diffcheck.sh\n", name)
		os.Exit(2)
	}
	return v
}

func check(err error) {
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(2)
	}
}
