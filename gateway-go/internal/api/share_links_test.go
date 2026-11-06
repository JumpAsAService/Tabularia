package api

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"os"
	"strings"
	"testing"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/auth"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/clientip"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/config"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/server"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/sharelinks"
)

// The share links on a real database (gateway-go/scripts/test.sh): the creators' routes, the
// recipient's, and what must not be possible. The rows are committed (the routes use the pool)
// and removed at the end.

type shareSettingsStub struct{}

func (shareSettingsStub) SessionIdleMinutes() int { return 0 }
func (shareSettingsStub) PersonalTokens() bool    { return true }

type shareFixture struct {
	t                     *testing.T
	ctx                   context.Context
	db                    *pgxpool.Pool
	d                     *Deps
	mux                   *http.ServeMux
	creator, editor       *auth.User
	folder, report, board int64
	ip                    int
}

const shareReportDef = `{"v": 1, "locale": "en", "parameters": [{"name": "region", "label": "Region", "type": "text",
	"choices": {"values": ["North", "South"]}, "default": "North"}],
	"datasets": [{"name": "sales", "source": {"kind": "sql", "sql": "SELECT secret_column FROM secret_table", "connection_id": 3}}]}`

func newShareFixture(t *testing.T) *shareFixture {
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
	f := &shareFixture{t: t, ctx: ctx, db: db}
	// the auth package's tests TRUNCATE the users (and with them these links): one at a time
	lock, err := db.Acquire(ctx)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := lock.Exec(ctx, `SELECT pg_advisory_lock(7101)`); err != nil {
		t.Fatal(err)
	}
	log := slog.New(slog.NewTextHandler(io.Discard, nil))
	f.d = &Deps{DB: db, Log: log, Config: &config.Config{},
		Auth: &auth.Authenticator{DB: db, Secret: []byte("a test secret, not of anything"), Algorithm: "HS256",
			Settings: shareSettingsStub{}, ClientIP: clientip.New(nil), Log: log}}
	f.mux = http.NewServeMux()
	for _, m := range []string{"share_public", "share_links"} {
		for _, r := range modules[m](f.d) {
			f.mux.Handle(r.Method+" "+r.Pattern, r.Handler)
		}
	}
	// an app route, to prove the link's cookie opens nothing else
	f.mux.Handle("GET /reports/{report_id}", server.Authed(f.d.Auth, "/reports/{report_id}", reportsAPI(f.d).get))
	one := func(sql string, args ...any) int64 {
		t.Helper()
		var id int64
		if err := db.QueryRow(ctx, sql, args...).Scan(&id); err != nil {
			t.Fatalf("%s: %v", sql, err)
		}
		return id
	}
	stamp := time.Now().UnixNano()
	user := func(name string) *auth.User {
		email := fmt.Sprintf("sl-%s-%d@x.test", name, stamp)
		id := one(`INSERT INTO users (email, full_name, hashed_password, is_active, is_superuser, is_observer, is_service, created_at)
			VALUES ($1, $2, 'x', true, false, false, false, now()) RETURNING id`, email, name)
		return &auth.User{ID: id, Email: email, FullName: name, IsActive: true}
	}
	f.creator, f.editor = user("creator"), user("editor")
	f.folder = one(`INSERT INTO projects (name, description, created_at) VALUES ($1, '', now()) RETURNING id`, fmt.Sprintf("sl-%d", stamp))
	one(`INSERT INTO permissions (project_id, user_id, capability) VALUES ($1, $2, 'manage') RETURNING id`, f.folder, f.creator.ID)
	one(`INSERT INTO permissions (project_id, user_id, capability) VALUES ($1, $2, 'edit') RETURNING id`, f.folder, f.editor.ID)
	object := func(table, versions, fk, name, content string) int64 {
		id := one(`INSERT INTO `+table+` (name, description, project_id, owner_id, current_version, created_at, updated_at)
			VALUES ($1, '', $2, $3, 1, now(), now()) RETURNING id`, name, f.folder, f.creator.ID)
		one(`INSERT INTO `+versions+` (`+fk+`, version, content, note, created_by, created_at) VALUES ($1, 1, $2, '', $3, now()) RETURNING id`,
			id, content, f.creator.ID)
		return id
	}
	f.report = object("reports", "report_versions", "report_id", "Sales report", shareReportDef)
	f.board = object("dashboards", "dashboard_versions", "dashboard_id", "Sales board", shareReportDef)
	t.Cleanup(func() {
		for _, q := range []string{
			`DELETE FROM login_attempts WHERE email LIKE ' link:%'`,
			`DELETE FROM audit_logs WHERE target_type = 'share_link' AND target_id IN (SELECT id FROM share_links WHERE creator_id = $1)`,
			`DELETE FROM share_links WHERE creator_id = $1`,
		} {
			if _, err := db.Exec(ctx, q, f.creator.ID); err != nil && !strings.Contains(q, "login_attempts") {
				t.Error(q, err)
			}
		}
		_, _ = db.Exec(ctx, `DELETE FROM login_attempts WHERE email LIKE ' link:%'`)
		_, _ = db.Exec(ctx, `DELETE FROM audit_logs WHERE actor_id = ANY($1)`, []int64{f.creator.ID, f.editor.ID})
		_, _ = db.Exec(ctx, `UPDATE share_link_settings SET enabled = true, max_days = 90, ai_comments_per_hour = 20 WHERE id = 1`)
		for _, q := range []string{`DELETE FROM report_versions WHERE report_id = $1`, `DELETE FROM reports WHERE id = $1`} {
			_, _ = db.Exec(ctx, q, f.report)
		}
		for _, q := range []string{`DELETE FROM dashboard_versions WHERE dashboard_id = $1`, `DELETE FROM dashboards WHERE id = $1`} {
			_, _ = db.Exec(ctx, q, f.board)
		}
		_, _ = db.Exec(ctx, `DELETE FROM permissions WHERE project_id = $1`, f.folder)
		_, _ = db.Exec(ctx, `DELETE FROM projects WHERE id = $1`, f.folder)
		_, _ = db.Exec(ctx, `DELETE FROM users WHERE id = ANY($1)`, []int64{f.creator.ID, f.editor.ID})
		_, _ = lock.Exec(ctx, `SELECT pg_advisory_unlock(7101)`)
		lock.Release()
		db.Close()
	})
	return f
}

// as calls a creator's route handler as this person.
func (f *shareFixture) as(u *auth.User, h func(http.ResponseWriter, *http.Request, *auth.Principal), method, target string, body any,
	path map[string]string) *httptest.ResponseRecorder {
	var raw []byte
	if body != nil {
		raw, _ = json.Marshal(body)
	}
	r := httptest.NewRequest(method, target, bytes.NewReader(raw))
	for k, v := range path {
		r.SetPathValue(k, v)
	}
	w := httptest.NewRecorder()
	h(w, r, &auth.Principal{User: *u})
	return w
}

type shareCall struct {
	method, path string
	body         any
	cookie       string
	header       map[string]string
	ip           string
}

func (f *shareFixture) call(c shareCall) *httptest.ResponseRecorder {
	var raw io.Reader
	if c.body != nil {
		b, _ := json.Marshal(c.body)
		raw = bytes.NewReader(b)
	}
	r := httptest.NewRequest(c.method, c.path, raw)
	if c.method == http.MethodPost {
		r.Header.Set("X-Tabularia-Csrf", "1")
	}
	r.Header.Set("Content-Type", "application/json")
	if c.cookie != "" {
		r.Header.Set("Cookie", c.cookie)
	}
	for k, v := range c.header {
		r.Header.Set(k, v)
	}
	if c.ip == "" {
		c.ip = "192.0.2.10"
	}
	r.RemoteAddr = c.ip + ":5555"
	w := httptest.NewRecorder()
	f.mux.ServeHTTP(w, r)
	return w
}

func (f *shareFixture) create(body map[string]any) (string, int64) {
	f.t.Helper()
	in := map[string]any{"kind": "report", "object_id": f.report, "label": "For Mario", "password": "temporary-pass-123",
		"expires_at": time.Now().Add(48 * time.Hour).UTC().Format(time.RFC3339)}
	for k, v := range body {
		in[k] = v
	}
	w := f.as(f.creator, createShareLink(f.d), http.MethodPost, "/share-links", in, nil)
	if w.Code != http.StatusCreated {
		f.t.Fatalf("create: %d %s", w.Code, w.Body.String())
	}
	var out struct {
		Token string  `json:"token"`
		Path  string  `json:"path"`
		Link  linkOut `json:"link"`
	}
	_ = json.Unmarshal(w.Body.Bytes(), &out)
	if out.Path != "/share/"+out.Token || len(out.Token) != 43 {
		f.t.Fatalf("created: %s", w.Body.String())
	}
	return out.Token, out.Link.ID
}

// cookieOf is the link's cookie as a browser would send it back, checked as written.
func cookieOf(t *testing.T, w *httptest.ResponseRecorder, token string) string {
	t.Helper()
	set := w.Header().Get("Set-Cookie")
	for _, want := range []string{"tab_share=", "HttpOnly", "SameSite=Strict", "Path=/share/" + token} {
		if !strings.Contains(set, want) {
			t.Fatalf("cookie %q: no %s", set, want)
		}
	}
	return strings.SplitN(set, ";", 2)[0]
}

func checkShareHeaders(t *testing.T, w *httptest.ResponseRecorder, what string) {
	t.Helper()
	h := w.Header()
	if h.Get("Cache-Control") != "no-store" || h.Get("X-Robots-Tag") != "noindex, nofollow" || h.Get("Referrer-Policy") != "no-referrer" {
		t.Errorf("%s: headers %v", what, h)
	}
}

func TestShareLinkFromTemporaryPasswordToOwnPassword(t *testing.T) {
	f := newShareFixture(t)
	token, id := f.create(nil)
	base := "/share/" + token

	// before any password: nothing
	w := f.call(shareCall{method: "GET", path: base})
	if w.Code != 401 {
		t.Fatalf("about without a session: %d", w.Code)
	}
	checkShareHeaders(t, w, "401")
	// the temporary password leads to choosing one's own, and to nothing else
	w = f.call(shareCall{method: "POST", path: base + "/session", body: map[string]string{"password": "temporary-pass-123"}})
	if w.Code != 200 || !strings.Contains(w.Body.String(), `"change_password":true`) || !strings.Contains(w.Body.String(), `"min_length":12`) {
		t.Fatalf("temporary: %d %s", w.Code, w.Body.String())
	}
	change := cookieOf(t, w, token)
	if w := f.call(shareCall{method: "GET", path: base, cookie: change}); w.Code != 401 {
		t.Errorf("the change session opens the report: %d", w.Code)
	}
	for _, c := range []struct{ pw, code string }{{"short", "weak_password"}, {"password", "weak_password"},
		{strings.Repeat("é", 40), "weak_password"}, {"temporary-pass-123", "same_password"}} {
		w := f.call(shareCall{method: "POST", path: base + "/password", body: map[string]string{"password": c.pw}, cookie: change})
		if w.Code != 422 || !strings.Contains(w.Body.String(), c.code) {
			t.Errorf("%q: %d %s", c.pw, w.Code, w.Body.String())
		}
	}
	w = f.call(shareCall{method: "POST", path: base + "/password", body: map[string]string{"password": "my own long password"}, cookie: change})
	if w.Code != 204 {
		t.Fatalf("own password: %d %s", w.Code, w.Body.String())
	}
	view := cookieOf(t, w, token)
	// the report, without its sources
	w = f.call(shareCall{method: "GET", path: base, cookie: view})
	if w.Code != 200 || !strings.Contains(w.Body.String(), `"name":"Sales report"`) {
		t.Fatalf("about: %d %s", w.Code, w.Body.String())
	}
	if b := w.Body.String(); strings.Contains(b, "secret_table") || strings.Contains(b, "connection_id") || !strings.Contains(b, `"datasets":[{"name":"sales"}]`) {
		t.Errorf("the definition's sources reached the page: %s", b)
	}
	checkShareHeaders(t, w, "200")
	w = f.call(shareCall{method: "GET", path: base + "/parameters?region=South", cookie: view})
	if w.Code != 200 || !strings.Contains(w.Body.String(), `"region"`) {
		t.Errorf("parameters: %d %s", w.Code, w.Body.String())
	}
	// the temporary password is gone; the own one opens directly
	if w := f.call(shareCall{method: "POST", path: base + "/session", body: map[string]string{"password": "temporary-pass-123"}, ip: "192.0.2.11"}); w.Code != 401 {
		t.Errorf("the temporary password still opens: %d", w.Code)
	}
	w = f.call(shareCall{method: "POST", path: base + "/session", body: map[string]string{"password": "my own long password"}, ip: "192.0.2.11"})
	if w.Code != 200 || !strings.Contains(w.Body.String(), `"change_password":false`) {
		t.Errorf("own password: %d %s", w.Code, w.Body.String())
	}
	// only one recipient: the change of a second one loses
	l, _ := sharelinks.ByID(f.ctx, f.db, id)
	if won, err := sharelinks.SetOwnPassword(f.ctx, f.db, id, "an old temporary hash", "x"); err != nil || won || l.PasswordTemporary {
		t.Errorf("a second choice: %v %v %v", won, err, l.PasswordTemporary)
	}
	// a new temporary password closes every session of the link
	if w := f.as(f.creator, resetShareLinkPassword(f.d), "POST", "/", map[string]string{"password": "another-temporary-1"},
		map[string]string{"link_id": fmt.Sprint(id)}); w.Code != 204 {
		t.Fatalf("reset: %d %s", w.Code, w.Body.String())
	}
	if w := f.call(shareCall{method: "GET", path: base, cookie: view}); w.Code != 401 {
		t.Errorf("a session survived the reset: %d", w.Code)
	}
	if w := f.call(shareCall{method: "POST", path: base + "/session", body: map[string]string{"password": "another-temporary-1"}, ip: "192.0.2.12"}); !strings.Contains(w.Body.String(), `"change_password":true`) {
		t.Errorf("after the reset: %d %s", w.Code, w.Body.String())
	}
	// the audit: every step, never a password or the token
	rows, err := f.db.Query(f.ctx, `SELECT action, coalesce(detail, '') FROM audit_logs WHERE target_type = 'share_link' AND target_id = $1 ORDER BY id`, id)
	if err != nil {
		t.Fatal(err)
	}
	var actions []string
	for rows.Next() {
		var a, d string
		_ = rows.Scan(&a, &d)
		actions = append(actions, a)
		if strings.Contains(d, token) || strings.Contains(d, "temporary-pass") || strings.Contains(d, "my own long") {
			t.Errorf("a secret in the audit: %s %s", a, d)
		}
	}
	want := "share_link.create share_link.password_set share_link.open share_link.unlock_failed share_link.open share_link.password_reset"
	if got := strings.Join(actions, " "); got != want {
		t.Errorf("audit: %s\nwant   %s", got, want)
	}
}

func TestShareLinkUnavailableTokensSayTheSame(t *testing.T) {
	f := newShareFixture(t)
	token, id := f.create(nil)
	other, otherID := f.create(map[string]any{"label": "other"})
	same := func(what, path string) {
		t.Helper()
		w := f.call(shareCall{method: "POST", path: path + "/session", body: map[string]string{"password": "temporary-pass-123"}, ip: "192.0.2.20"})
		if w.Code != 404 || !strings.Contains(w.Body.String(), sharelinks.NotAvailable) {
			t.Errorf("%s: %d %s", what, w.Code, w.Body.String())
		}
		checkShareHeaders(t, w, what)
	}
	same("unknown", "/share/"+strings.Repeat("A", 43))
	same("malformed", "/share/x")
	if _, err := f.db.Exec(f.ctx, `UPDATE share_links SET expires_at = now() - interval '1 hour' WHERE id = $1`, id); err != nil {
		t.Fatal(err)
	}
	same("expired", "/share/"+token)
	if w := f.as(f.creator, revokeShareLink(f.d), "POST", "/", nil, map[string]string{"link_id": fmt.Sprint(otherID)}); w.Code != 204 {
		t.Fatalf("revoke: %d", w.Code)
	}
	same("revoked", "/share/"+other)

	// the creator's rights, now: a link stops with MANAGE and comes back with it
	third, _ := f.create(map[string]any{"label": "third"})
	opens := func() int {
		return f.call(shareCall{method: "POST", path: "/share/" + third + "/session", body: map[string]string{"password": "temporary-pass-123"},
			ip: "192.0.2.21"}).Code
	}
	if c := opens(); c != 200 {
		t.Fatalf("a live link: %d", c)
	}
	_, _ = f.db.Exec(f.ctx, `UPDATE permissions SET capability = 'edit' WHERE project_id = $1 AND user_id = $2`, f.folder, f.creator.ID)
	if c := opens(); c != 404 {
		t.Errorf("creator without MANAGE: %d", c)
	}
	_, _ = f.db.Exec(f.ctx, `UPDATE permissions SET capability = 'manage' WHERE project_id = $1 AND user_id = $2`, f.folder, f.creator.ID)
	_, _ = f.db.Exec(f.ctx, `UPDATE users SET is_active = false WHERE id = $1`, f.creator.ID)
	if c := opens(); c != 404 {
		t.Errorf("creator deactivated: %d", c)
	}
	_, _ = f.db.Exec(f.ctx, `UPDATE users SET is_active = true WHERE id = $1`, f.creator.ID)
	// the switch: off, no link answers and none is made; on, they come back
	_, _ = f.db.Exec(f.ctx, `UPDATE share_link_settings SET enabled = false WHERE id = 1`)
	if c := opens(); c != 404 {
		t.Errorf("switched off: %d", c)
	}
	in := map[string]any{"kind": "report", "object_id": f.report, "password": "temporary-pass-123",
		"expires_at": time.Now().Add(time.Hour).UTC().Format(time.RFC3339)}
	if w := f.as(f.creator, createShareLink(f.d), "POST", "/share-links", in, nil); w.Code != 403 {
		t.Errorf("made while off: %d", w.Code)
	}
	_, _ = f.db.Exec(f.ctx, `UPDATE share_link_settings SET enabled = true WHERE id = 1`)
	if c := opens(); c != 200 {
		t.Errorf("switched on again: %d", c)
	}
}

func TestShareLinkPasswordsAreBraked(t *testing.T) {
	f := newShareFixture(t)
	token, id := f.create(nil)
	try := func(pw, ip string) *httptest.ResponseRecorder {
		return f.call(shareCall{method: "POST", path: "/share/" + token + "/session", body: map[string]string{"password": pw}, ip: ip})
	}
	for i := 0; i < 5; i++ {
		if w := try("wrong password", "192.0.2.30"); w.Code != 401 {
			t.Fatalf("try %d: %d", i, w.Code)
		}
	}
	w := try("temporary-pass-123", "192.0.2.30")
	if w.Code != 429 || w.Header().Get("Retry-After") == "" {
		t.Errorf("the sixth from the same address: %d %s", w.Code, w.Body.String())
	}
	if w := try("temporary-pass-123", "192.0.2.31"); w.Code != 200 {
		t.Errorf("another address is not braked by the first: %d", w.Code)
	}
	// on the link, whatever the address: the fiftieth locks it until a new temporary password
	_, _ = f.db.Exec(f.ctx, `UPDATE share_links SET failures = $2 WHERE id = $1`, id, sharelinks.LockAfter-1)
	if w := try("wrong password", "192.0.2.32"); w.Code != 401 {
		t.Fatalf("the fiftieth: %d", w.Code)
	}
	if w := try("temporary-pass-123", "192.0.2.33"); w.Code != 404 {
		t.Errorf("a locked link opened: %d", w.Code)
	}
	var locked int
	_ = f.db.QueryRow(f.ctx, `SELECT count(*) FROM audit_logs WHERE action = 'share_link.locked' AND target_id = $1`, id).Scan(&locked)
	if locked != 1 {
		t.Errorf("locked audited %d times", locked)
	}
	if w := f.as(f.creator, resetShareLinkPassword(f.d), "POST", "/", map[string]string{"password": "a-new-temporary-1"},
		map[string]string{"link_id": fmt.Sprint(id)}); w.Code != 204 {
		t.Fatal(w.Code)
	}
	if w := try("a-new-temporary-1", "192.0.2.34"); w.Code != 200 {
		t.Errorf("after the reset: %d", w.Code)
	}
}

func TestTheLinksCookieOpensNoOtherRouteAndTheAppsOpensNoLink(t *testing.T) {
	f := newShareFixture(t)
	token, _ := f.create(nil)
	other, _ := f.create(map[string]any{"label": "other"})
	w := f.call(shareCall{method: "POST", path: "/share/" + token + "/session", body: map[string]string{"password": "temporary-pass-123"}})
	cookie := cookieOf(t, w, token)
	w = f.call(shareCall{method: "POST", path: "/share/" + token + "/password", body: map[string]string{"password": "my own long password"}, cookie: cookie})
	view := cookieOf(t, w, token)
	if w := f.call(shareCall{method: "GET", path: fmt.Sprintf("/reports/%d", f.report), cookie: view}); w.Code != 401 {
		t.Errorf("the link's cookie opened an app route: %d", w.Code)
	}
	// the same cookie under the app's name is no session of the app either
	if w := f.call(shareCall{method: "GET", path: fmt.Sprintf("/reports/%d", f.report), cookie: "tab_token=" + strings.TrimPrefix(view, "tab_share=")}); w.Code != 401 {
		t.Errorf("as the app's cookie: %d", w.Code)
	}
	if w := f.call(shareCall{method: "GET", path: "/share/" + other, cookie: view}); w.Code != 401 {
		t.Errorf("the link's cookie opened another link: %d", w.Code)
	}
	if w := f.call(shareCall{method: "GET", path: "/share/" + token, cookie: "tab_token=anything"}); w.Code != 401 {
		t.Errorf("the app's cookie opened a link: %d", w.Code)
	}
	if w := f.call(shareCall{method: "GET", path: "/share/" + token, header: map[string]string{sharelinks.SessionHeader: strings.TrimPrefix(view, "tab_share=")}}); w.Code != 401 {
		t.Errorf("a link with a password took its session as a header: %d", w.Code)
	}
	// without the app's CSRF header a POST is refused
	r := httptest.NewRequest("POST", "/share/"+token+"/session", strings.NewReader(`{"password":"x"}`))
	rec := httptest.NewRecorder()
	f.mux.ServeHTTP(rec, r)
	if rec.Code != 403 {
		t.Errorf("a POST without the CSRF header: %d", rec.Code)
	}
	checkShareHeaders(t, rec, "403")
}

func TestWhoMakesLinksAndWhatTheyAre(t *testing.T) {
	f := newShareFixture(t)
	in := map[string]any{"kind": "report", "object_id": f.report, "password": "temporary-pass-123",
		"expires_at": time.Now().Add(time.Hour).UTC().Format(time.RFC3339)}
	if w := f.as(f.editor, createShareLink(f.d), "POST", "/share-links", in, nil); w.Code != 403 {
		t.Errorf("EDIT made a link: %d", w.Code)
	}
	w := f.as(f.editor, shareLinksOfObject(f.d), "GET", fmt.Sprintf("/share-links?kind=report&object_id=%d", f.report), nil, nil)
	if w.Code != 200 || !strings.Contains(w.Body.String(), `"can_create":false`) || !strings.Contains(w.Body.String(), `"links":[]`) {
		t.Errorf("EDIT's list: %s", w.Body.String())
	}
	bad := []map[string]any{
		{"password": "short"},
		{"expires_at": time.Now().Add(-time.Hour).UTC().Format(time.RFC3339)},
		{"expires_at": time.Now().Add(100 * 24 * time.Hour).UTC().Format(time.RFC3339)},
		{"expires_at": "tomorrow"},
		{"downloads": []string{"html"}},
		{"embeddable": true, "origins": []string{"https://intranet.example.com"}}, // not confirmed
		{"embeddable": true, "confirmed": true, "origins": []string{}},
		{"embeddable": true, "confirmed": true, "origins": []string{"https://*.example.com"}},
		{"kind": "flow"},
	}
	for _, b := range bad {
		body := map[string]any{}
		for k, v := range in {
			body[k] = v
		}
		for k, v := range b {
			body[k] = v
		}
		if w := f.as(f.creator, createShareLink(f.d), "POST", "/share-links", body, nil); w.Code < 400 {
			t.Errorf("%v accepted: %d", b, w.Code)
		}
	}
	if w := f.as(f.creator, createShareLink(f.d), "POST", "/share-links", in, nil); w.Code != 201 {
		t.Errorf("MANAGE: %d %s", w.Code, w.Body.String())
	}
	tokenUser := *f.creator
	w = httptest.NewRecorder()
	raw, _ := json.Marshal(in)
	createShareLink(f.d)(w, httptest.NewRequest("POST", "/share-links", bytes.NewReader(raw)), &auth.Principal{User: tokenUser, TokenName: "a script"})
	if w.Code != 403 {
		t.Errorf("with an API token: %d", w.Code)
	}
	w = f.as(f.creator, shareLinksOfObject(f.d), "GET", fmt.Sprintf("/share-links?kind=report&object_id=%d", f.report), nil, nil)
	if !strings.Contains(w.Body.String(), `"can_create":true`) || !strings.Contains(w.Body.String(), `"state":"waiting"`) ||
		strings.Contains(w.Body.String(), "password_hash") || strings.Contains(w.Body.String(), "token\"") {
		t.Errorf("MANAGE's list: %s", w.Body.String())
	}
	// someone who cannot manage it does not know it
	var id int64
	_ = f.db.QueryRow(f.ctx, `SELECT id FROM share_links WHERE creator_id = $1`, f.creator.ID).Scan(&id)
	if w := f.as(f.editor, revokeShareLink(f.d), "POST", "/", nil, map[string]string{"link_id": fmt.Sprint(id)}); w.Code != 404 {
		t.Errorf("EDIT revoked: %d", w.Code)
	}
}

func TestAnEmbeddableLinkAsksNoPasswordAndNamesItsOrigins(t *testing.T) {
	f := newShareFixture(t)
	token, id := f.create(map[string]any{"kind": "dashboard", "object_id": f.board, "embeddable": true, "confirmed": true,
		"origins": []string{"https://intranet.example.com", "http://portal:8080"}, "password": ""})
	w := f.call(shareCall{method: "GET", path: "/share/" + token + "/frame?dest=iframe"})
	if w.Code != 200 || !strings.Contains(w.Body.String(), `"frame_ancestors":"https://intranet.example.com http://portal:8080"`) {
		t.Errorf("frame: %s", w.Body.String())
	}
	if w := f.call(shareCall{method: "GET", path: "/share/" + token + "/frame?dest=document"}); !strings.Contains(w.Body.String(), `"embeddable":true`) {
		t.Errorf("frame as a page: %s", w.Body.String())
	}
	var refused int
	_ = f.db.QueryRow(f.ctx, `SELECT count(*) FROM audit_logs WHERE action = 'share_link.refused_frame' AND target_id = $1`, id).Scan(&refused)
	if refused != 1 {
		t.Errorf("the page asked outside a frame is audited %d times", refused)
	}
	pw, _ := f.create(nil)
	if w := f.call(shareCall{method: "GET", path: "/share/" + pw + "/frame"}); !strings.Contains(w.Body.String(), `"frame_ancestors":"'none'"`) {
		t.Errorf("a link with a password is framed: %s", w.Body.String())
	}
	if w := f.call(shareCall{method: "GET", path: "/share/" + strings.Repeat("B", 43) + "/frame"}); !strings.Contains(w.Body.String(), `"frame_ancestors":"'none'"`) {
		t.Errorf("an unknown link is framed: %s", w.Body.String())
	}
	w = f.call(shareCall{method: "POST", path: "/share/" + token + "/session"})
	var s struct {
		Session string `json:"session"`
	}
	_ = json.Unmarshal(w.Body.Bytes(), &s)
	if w.Code != 200 || len(s.Session) != 43 || w.Header().Get("Set-Cookie") != "" {
		t.Fatalf("embed session: %d %s %v", w.Code, w.Body.String(), w.Header())
	}
	if w := f.call(shareCall{method: "GET", path: "/share/" + token, header: map[string]string{sharelinks.SessionHeader: s.Session}}); w.Code != 200 ||
		strings.Contains(w.Body.String(), "secret_table") || !strings.Contains(w.Body.String(), `"datasets":[{"name":"sales"}]`) {
		t.Errorf("embed about: %d %s", w.Code, w.Body.String())
	}
	if w := f.call(shareCall{method: "GET", path: "/share/" + token, cookie: "tab_share=" + s.Session}); w.Code != 401 {
		t.Errorf("an embeddable link took its session as a cookie: %d", w.Code)
	}
	// a download it does not allow
	if w := f.call(shareCall{method: "POST", path: "/share/" + token + "/render", body: map[string]any{"format": "pdf"},
		header: map[string]string{sharelinks.SessionHeader: s.Session}}); w.Code != 403 {
		t.Errorf("a download not allowed: %d %s", w.Code, w.Body.String())
	}
}

func TestTheAICapCountsPerLinkAndHour(t *testing.T) {
	f := newShareFixture(t)
	_, id := f.create(nil)
	now := time.Date(2026, 11, 7, 10, 5, 0, 0, time.UTC)
	for i := 0; i < 3; i++ {
		if ok, err := sharelinks.TakeAICall(f.ctx, f.db, id, 3, now); err != nil || !ok {
			t.Fatalf("call %d: %v %v", i, ok, err)
		}
	}
	if ok, _ := sharelinks.TakeAICall(f.ctx, f.db, id, 3, now.Add(10*time.Minute)); ok {
		t.Error("past the cap in the same hour")
	}
	if ok, _ := sharelinks.TakeAICall(f.ctx, f.db, id, 3, now.Add(time.Hour)); !ok {
		t.Error("a new hour starts again")
	}
	if ok, _ := sharelinks.TakeAICall(f.ctx, f.db, id, 0, now.Add(2*time.Hour)); ok {
		t.Error("0 is no AI comment at all")
	}
}

func TestTheSweepRemovesWhatIsOver(t *testing.T) {
	f := newShareFixture(t)
	_, id := f.create(nil)
	old, oldID := f.create(map[string]any{"label": "old"})
	_ = old
	if _, _, err := sharelinks.OpenSession(f.ctx, f.db, id, sharelinks.PurposeView, "192.0.2.40"); err != nil {
		t.Fatal(err)
	}
	_, _ = f.db.Exec(f.ctx, `UPDATE share_link_sessions SET last_seen_at = now() - interval '2 hours' WHERE link_id = $1`, id)
	_, _ = f.db.Exec(f.ctx, `UPDATE share_links SET revoked_at = now() - interval '40 days' WHERE id = $1`, oldID)
	if _, err := sharelinks.Sweep(f.ctx, f.db, sharelinks.Now()); err != nil {
		t.Fatal(err)
	}
	var sessions, links int
	_ = f.db.QueryRow(f.ctx, `SELECT count(*) FROM share_link_sessions WHERE link_id = $1`, id).Scan(&sessions)
	_ = f.db.QueryRow(f.ctx, `SELECT count(*) FROM share_links WHERE id = ANY($1)`, []int64{id, oldID}).Scan(&links)
	if sessions != 0 || links != 1 {
		t.Errorf("after the sweep: %d sessions, %d links", sessions, links)
	}
}
