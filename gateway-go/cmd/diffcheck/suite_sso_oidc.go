package main

import (
	"encoding/base64"
	"encoding/json"
	"net/http"
	"net/url"
	"strings"
)

// ssoRandom: what a sign-in's answer holds at random (its state, nonce, PKCE challenge, cookie)
var ssoRandom = []string{`state=[^&#]+`, `nonce=[^&#]+`, `code_challenge=[^&#]+`, `tab_sso_tx_[0-9a-f]{16}`, `=eyJ[A-Za-z0-9_.-]+`}

// routes/sso.py with single sign-on configured, against the fake engine's OpenID provider
// (cmd/fakeengine/idp.go). Run only when asked, with DIFF_ENV="OIDC__ISSUER=http://siem.test:8000/idp
// OIDC__CLIENT_ID=tabularia OIDC__CLIENT_SECRET=s3cr3t OIDC__REDIRECT_URI=http://gw.test/auth/sso/callback
// OIDC__POST_LOGIN_URL=http://ui.test/auth/callback OIDC__GROUPS_CLAIM=groups,realm_access.roles
// OIDC__SUPERUSER_GROUP=tab-admins OIDC__AUTO_CREATE_GROUPS=true". Each sign-in is a login (its
// state, nonce, PKCE challenge and cookie are each side's own, kept by Capture) and a
// callback whose code tells the provider what to answer, with that side's nonce and challenge.
func init() {
	type tx struct{ state, nonce, challenge, cookie string }
	txs := map[string]*tx{}
	scrub := ssoRandom
	capture := func(side string, res *http.Response) {
		t := &tx{}
		if u, err := url.Parse(res.Header.Get("Location")); err == nil {
			q := u.Query()
			t.state, t.nonce, t.challenge = q.Get("state"), q.Get("nonce"), q.Get("code_challenge")
		}
		for _, c := range res.Header.Values("Set-Cookie") {
			if strings.HasPrefix(c, "tab_sso_tx_") {
				t.cookie, _, _ = strings.Cut(c, ";")
			}
		}
		txs[side] = t
	}
	type how struct {
		spec       map[string]any
		state      string // "" = the side's own
		noCookie   bool
		oldCookie  bool // the cookie under the name of the previous version
		badCookie  bool
		query      string // instead of code and state
		wrongNonce bool
	}
	prepare := func(h how) func(string, *http.Request) {
		return func(side string, r *http.Request) {
			t := txs[side]
			if t == nil {
				t = &tx{}
			}
			spec := map[string]any{"nonce": t.nonce, "challenge": t.challenge}
			if h.wrongNonce {
				spec["nonce"] = "not-the-nonce"
			}
			for k, v := range h.spec {
				spec[k] = v
			}
			raw, _ := json.Marshal(spec)
			state := t.state
			if h.state != "" {
				state = h.state
			}
			q := "code=" + base64.RawURLEncoding.EncodeToString(raw) + "&state=" + url.QueryEscape(state)
			if h.query != "" {
				q = h.query
			}
			r.URL.RawQuery = q
			if h.noCookie || t.cookie == "" {
				return
			}
			name, value, _ := strings.Cut(t.cookie, "=")
			if h.oldCookie {
				name = "tab_sso_tx"
			}
			if h.badCookie {
				value = value[:len(value)-4] + "AAAA"
			}
			r.AddCookie(&http.Cookie{Name: name, Value: value})
		}
	}
	register(Suite{
		Module: "sso_oidc",
		Asked:  true,
		Seed: func(w *World) {
			maskColumns("user_sessions", "id", "expires_at") // a sign-in's session: random, and hours from now
			w.Exec(`INSERT INTO groups (name, description, is_admin, is_observer, created_at) VALUES ('team-a', '', false, false, '2026-01-01'),
				('Readers', '', false, true, '2026-01-01')`)
			w.Exec(`INSERT INTO users (email, full_name, hashed_password, is_active, is_superuser, is_observer, is_service, created_at)
				VALUES ('linked.one@example.test', '', 'x', true, false, false, false, '2026-01-01'),
				('taken@example.test', '', NULL, true, false, false, false, '2026-01-01')`)
			w.Exec(`UPDATE users SET oidc_issuer = 'http://other/idp', oidc_subject = 'someone' WHERE email = 'taken@example.test'`)
		},
		Phases: []Phase{
			{Name: "the environment's settings"},
			{Name: "the panel: not authoritative, an allowlist, a domain", Panel: map[string]string{"oidc.authoritative": "false",
				"oidc.group_allowlist": `"team-a, Readers, Analysts"`, "oidc.allowed_email_domains": `" @Example.test, x.test"`,
				"oidc.button_label": `"Company login"`, "oidc.login_tx_ttl_seconds": "900", "oidc.groups_claim": `"groups"`}},
			{Name: "the panel: values that do not hold", Panel: map[string]string{"oidc.require_allowlisted_group": "true",
				"oidc.discovery_ttl_seconds": "30", "oidc.button_label": `"` + strings.Repeat("b", 61) + `"`, "oidc.auto_create_groups": `"yes"`,
				"oidc.groups_claim": `"  "`}},
		},
		Steps: func(w *World) []Step {
			var s []Step
			login := func(name string) {
				s = append(s, Step{Name: "login: " + name, Method: "GET", Path: "/auth/sso/login", HeaderScrub: scrub, Capture: capture})
			}
			callback := func(name string, h how) {
				s = append(s, Step{Name: "callback: " + name, Method: "GET", Path: "/auth/sso/callback", HeaderScrub: scrub,
					Prepare: prepare(h)})
			}
			signIn := func(name string, h how) {
				login(name)
				callback(name, h)
			}
			s = append(s, Step{Name: "the configuration", Method: "GET", Path: "/auth/sso/config"})
			signIn("a new person, groups created", how{spec: map[string]any{"claims": map[string]any{"groups": []any{"/team-a", "new-team", " "},
				"name": "New Person", "email_verified": true}}})
			signIn("the same, groups changed, an administrator", how{spec: map[string]any{"claims": map[string]any{
				"groups": []any{"tab-admins", "Readers", "Analysts"}, "email": "New.Person@Example.test"}}})
			signIn("the same, no groups", how{spec: map[string]any{"claims": map[string]any{"name": "Renamed"}}})
			signIn("groups from the access token", how{spec: map[string]any{"claims": map[string]any{"sub": "sub-2", "email": "two@example.test",
				"groups": "team-a"}, "access": map[string]any{"realm_access": map[string]any{"roles": []any{"Analysts", "team-a"}}}}})
			signIn("groups from userinfo", how{spec: map[string]any{"claims": map[string]any{"sub": "sub-3", "upn": "three@x.test", "email": nil},
				"opaque": map[string]any{"sub": "sub-3", "realm_access.roles": []any{"Sales"}, "groups": []any{"team-a"}}}})
			signIn("userinfo of another person", how{spec: map[string]any{"claims": map[string]any{"sub": "sub-3", "upn": "three@x.test", "email": nil},
				"opaque": map[string]any{"sub": "sub-9", "groups": []any{"team-a"}}}})
			signIn("an existing account, email not verified", how{spec: map[string]any{"claims": map[string]any{"sub": "sub-4",
				"email": "linked.one@example.test"}}})
			signIn("an existing account linked", how{spec: map[string]any{"claims": map[string]any{"sub": "sub-4",
				"email": "linked.one@example.test", "email_verified": "TRUE", "name": "Linked"}}})
			signIn("an account of another identity", how{spec: map[string]any{"claims": map[string]any{"sub": "sub-5",
				"email": "taken@example.test", "email_verified": true}}})
			signIn("a service account", how{spec: map[string]any{"claims": map[string]any{"sub": "sub-6", "email": "svc@x.test",
				"email_verified": true}}})
			signIn("a disabled account", how{spec: map[string]any{"claims": map[string]any{"sub": "sub-7", "email": "off@x.test",
				"email_verified": true}}})
			signIn("email declared not verified", how{spec: map[string]any{"claims": map[string]any{"email_verified": false}}})
			signIn("no email", how{spec: map[string]any{"claims": map[string]any{"email": "nobody", "sub": "sub-8"}}})
			signIn("the wrong nonce", how{wrongNonce: true})
			signIn("expired", how{spec: map[string]any{"exp_in": -3600}})
			signIn("another audience", how{spec: map[string]any{"claims": map[string]any{"aud": "someone-else"}}})
			signIn("another issuer", how{spec: map[string]any{"claims": map[string]any{"iss": "http://evil/idp"}}})
			signIn("without iat", how{spec: map[string]any{"claims": map[string]any{"iat": nil}}})
			signIn("an unknown key id", how{spec: map[string]any{"kid": "k9"}})
			signIn("a key not published", how{spec: map[string]any{"other": true}})
			signIn("the code refused", how{spec: map[string]any{"status": 400}})
			signIn("no id_token", how{spec: map[string]any{"no_id": true}})
			signIn("an answer not JSON", how{spec: map[string]any{"raw": "nope"}})
			signIn("another state", how{state: "not-the-state"})
			signIn("no cookie", how{noCookie: true})
			signIn("the old cookie's name", how{oldCookie: true, spec: map[string]any{"claims": map[string]any{"groups": []any{"team-a"}}}})
			signIn("a cookie tampered with", how{badCookie: true})
			login("the provider refuses")
			callback("the provider refuses", how{query: "error=access_denied&error_description=no&state=abc"})
			callback("no code", how{query: "state=abc"})
			callback("nothing", how{query: ""})
			s = append(s, Step{Name: "callback: signed in already", Method: "GET", Path: "/auth/sso/callback", HeaderScrub: scrub,
				Auth: w.As("writer"), Prepare: prepare(how{spec: map[string]any{"claims": map[string]any{"sub": "sub-10", "email": "ten@example.test"}}})})
			return s
		},
	})
}
