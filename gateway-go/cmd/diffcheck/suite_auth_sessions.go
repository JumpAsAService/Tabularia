package main

import (
	"net/http"
	"strings"
	"time"
)

// routes/auth.py beyond /auth/me: signing in and out, the sessions, the first run, the
// observer's check for the reverse proxy.
func init() {
	register(Suite{
		Module: "auth",
		Seed: func(w *World) {
			maskColumns("user_sessions", "id", "expires_at") // random, and twelve hours from now
			pw := "a login password 1"
			w.Person("au-pw", "au-pw@x.test", pw, false, false, false)
			w.Person("au-obs", "au-obs@x.test", pw, false, true, false)
			w.Person("au-boss", "au-boss@x.test", pw, true, false, false)
			w.Person("au-svc", "au-svc@service.invalid", "", false, false, true)
			w.Person("au-sso", "au-sso@x.test", "", false, false, false)
			w.Person("au-off", "au-off@x.test", pw, false, false, false)
			w.Exec(`UPDATE users SET is_active = false WHERE email = 'au-off@x.test'`)
			w.Person("au-many", "au-many@x.test", pw, false, false, false)
			w.Session("au-many", "s-au-many-2", 10*time.Minute, 2*time.Hour)
			w.Session("au-many", "s-au-many-3", 20*time.Minute, 3*time.Hour)
			w.Session("au-many", "s-au-many-old", time.Minute, -time.Minute)
			w.Exec(`UPDATE user_sessions SET ip = '9.9.9.9', user_agent = 'firefox' WHERE id = 's-au-many-2'`)
			w.Token("au-many", "tab_au_many_000000000000000", "write", time.Hour)
			w.Session("au-obs", "s-au-obs-2", 10*time.Minute, 2*time.Hour)
			w.Person("au-brake", "au-brake@x.test", pw, false, false, false)
			// five errors from the comparison's address 30.5 seconds ago: braked for about half a minute
			w.Exec(`INSERT INTO login_attempts (ip, email, failures, last_at)
				VALUES ('5.6.7.8', 'au-brake@x.test', 5, now() at time zone 'utc' - interval '30.5 seconds')`)
		},
		Steps: func(w *World) []Step {
			var s []Step
			pw := "a login password 1"
			login := func(name, email, password string, auth func(*http.Request), header map[string]string) {
				s = append(s, Step{Name: name, Method: "POST", Path: "/auth/login", Auth: auth, Header: header,
					JSON: map[string]any{"email": email, "password": password}, Mask: []string{"access_token"},
					ClockSeconds: strings.Contains(name, "braked")})
			}
			csrf := map[string]string{"X-Tabularia-Csrf": "1"}
			login("a wrong password", "au-pw@x.test", "wrong", nil, nil)
			login("an unknown email", "nobody@x.test", pw, nil, nil)
			login("a service account", "au-svc@service.invalid", pw, nil, nil)
			login("an identity provider's account", "au-sso@x.test", pw, nil, nil)
			login("a deactivated account", "au-off@x.test", pw, nil, nil)
			login("braked", "au-brake@x.test", pw, nil, nil)
			login("braked, the email in capitals", " AU-BRAKE@x.test ", pw, nil, nil)
			login("signed in: the token in the body", "au-pw@x.test", pw, nil, nil)
			login("signed in by the interface: no token in the body", "au-pw@x.test", pw, nil, csrf)
			login("again, from a browser with a session: it closes", "au-pw@x.test", pw, w.As("au-pw"), csrf)
			login("an observer: its address is not kept", "au-obs@x.test", pw, nil, csrf)
			login("an administrator: the personal folder is born", "au-boss@x.test", pw, nil, csrf)
			login("again: the folder is there", "au-boss@x.test", pw, Cookie("not.a.token"), csrf)
			login("an email that is not the account's case", "AU-PW@x.test", pw, nil, nil)
			s = append(s, bodyEdges("POST", "/auth/login", nil)...)
			s = append(s, Step{Name: "a password that is a number", Method: "POST", Path: "/auth/login", JSON: map[string]any{"email": "au-pw@x.test", "password": 5}})
			s = append(s, Step{Name: "the session closed by the new sign-in", Method: "GET", Path: "/auth/me", Auth: w.As("au-pw")})

			get := func(name, path string, auth func(*http.Request)) {
				s = append(s, Step{Name: name, Method: "GET", Path: path, Auth: auth})
			}
			do := func(name, method, path string, auth func(*http.Request)) {
				s = append(s, Step{Name: name, Method: method, Path: path, Auth: auth})
			}
			// the sessions
			get("one's sessions", "/auth/sessions", w.As("au-many"))
			get("one's sessions, with a token", "/auth/sessions", Bearer("tab_au_many_000000000000000"))
			get("an observer sees its own only", "/auth/sessions", w.As("au-obs"))
			get("nobody", "/auth/sessions", nil)
			do("close another of one's sessions", "DELETE", "/auth/sessions/s-au-many-2", w.As("au-many"))
			do("again: gone", "DELETE", "/auth/sessions/s-au-many-2", w.As("au-many"))
			do("someone else's", "DELETE", "/auth/sessions/s-admin", w.As("au-many"))
			do("an expired one of one's own", "DELETE", "/auth/sessions/s-au-many-old", w.As("au-many"))
			do("an observer, another session", "DELETE", "/auth/sessions/s-au-obs-2", w.As("au-obs"))
			do("an observer, someone else's", "DELETE", "/auth/sessions/s-admin", w.As("au-obs"))
			do("with a token", "DELETE", "/auth/sessions/s-au-many-3", Bearer("tab_au_many_000000000000000"))
			do("the current one: the cookie goes", "DELETE", "/auth/sessions/s-au-many", w.As("au-many"))
			do("nobody", "DELETE", "/auth/sessions/s-admin", nil)
			do("close the others, an observer", "POST", "/auth/sessions/close-others", w.As("au-obs"))
			do("close the others", "POST", "/auth/sessions/close-others", w.As("au-boss"))
			do("close the others, none left", "POST", "/auth/sessions/close-others", w.As("au-boss"))
			do("close the others, with a token: all", "POST", "/auth/sessions/close-others", Bearer("tab_au_many_000000000000000"))
			do("close the others, no CSRF header", "POST", "/auth/sessions/close-others", Cookie(w.JWT("writer", "s-writer")))
			do("close the others, nobody", "POST", "/auth/sessions/close-others", nil)
			get("one's sessions now", "/auth/sessions", w.As("au-boss"))

			// the observer's check
			for _, p := range []string{"admin", "admin_group", "obs_flag", "obs_group", "writer", "service"} {
				get("may "+p+" observe", "/auth/observer", w.As(p))
			}
			get("may nobody observe", "/auth/observer", nil)

			// the first run (enabled on the comparison's stacks, with accounts already there)
			get("is the first administrator needed", "/auth/setup", nil)
			setup := func(name string, body any) {
				s = append(s, Step{Name: name, Method: "POST", Path: "/auth/setup", JSON: body, Mask: []string{"access_token"}})
			}
			setup("not an email", map[string]any{"email": "nobody", "password": "a fine password 1"})
			setup("an email with a space", map[string]any{"email": "a b@x.test", "password": "a fine password 1"})
			setup("only at signs", map[string]any{"email": "@@", "password": "a fine password 1"})
			setup("a published password", map[string]any{"email": "first@x.test", "password": "changeme"})
			setup("a short password", map[string]any{"email": "first@x.test", "password": "short"})
			setup("accounts exist", map[string]any{"email": " First@X.test ", "password": "a fine password 1"})
			setup("no password", map[string]any{"email": "first@x.test"})
			s = append(s, bodyEdges("POST", "/auth/setup", nil)...)
			s = append(s, Step{Name: "bootstrap without a start-up token", Method: "POST", Path: "/auth/bootstrap", JSON: map[string]any{"token": "x"}})
			s = append(s, Step{Name: "bootstrap without a body", Method: "POST", Path: "/auth/bootstrap"})

			// signing out
			do("sign out", "POST", "/auth/logout", w.As("au-obs"))
			do("the session is closed", "GET", "/auth/me", w.As("au-obs"))
			do("sign out with a token", "POST", "/auth/logout", Bearer("tab_au_many_000000000000000"))
			do("sign out, no CSRF header", "POST", "/auth/logout", Cookie(w.JWT("writer", "s-writer")))
			do("sign out, nobody", "POST", "/auth/logout", nil)
			return s
		},
	})
}
