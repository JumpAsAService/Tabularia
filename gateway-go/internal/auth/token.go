// Package auth is the authentication of a request, as gateway/app/deps/auth.py does it:
// the token from the Authorization header or the session cookie, the anti-CSRF header for
// a cookie that changes something, the JWT and its session read in one query, the API
// tokens, the refusals (a refused cookie is deleted), the last activity written at most
// once a minute.
package auth

import (
	"fmt"
	"net/http"
	"strings"
	"time"
)

const (
	// services/sessions.py
	CookieName = "tab_token"
	CSRFHeader = "X-Tabularia-Csrf"
	// services/api_tokens.py
	APITokenPrefix = "tab_"
	// how often the last activity is written (sessions.ACTIVITY_EVERY, api_tokens.USE_EVERY)
	ActivityEvery = 60 * time.Second
	// core/security.py CLOCK_SKEW_SECONDS
	ClockSkew = 60 * time.Second
)

// methods that change nothing: with the cookie alone they do not ask for the CSRF header
var safeMethods = map[string]bool{"GET": true, "HEAD": true, "OPTIONS": true}

// credential is where the token came from: the cookie's refusal deletes the cookie, the
// header's does not (a script's expired token tried from /docs must not sign the browser
// out).
type credential struct {
	token      string
	fromCookie bool
}

// fromRequest is _RequestToken.__call__: the header, when it carries a bearer token, wins;
// otherwise the cookie, which for a method that changes something needs the CSRF header.
func fromRequest(r *http.Request) (credential, *Refusal) {
	if t := bearer(r.Header.Get("Authorization")); t != "" {
		return credential{token: t}, nil
	}
	c, err := r.Cookie(CookieName)
	if err != nil || c.Value == "" {
		return credential{}, invalidCredentials(r, false, "")
	}
	if !safeMethods[r.Method] && r.Header.Get(CSRFHeader) == "" {
		return credential{}, &Refusal{Status: http.StatusForbidden, Detail: "The request is missing the X-Tabularia-Csrf header"}
	}
	return credential{token: c.Value, fromCookie: true}, nil
}

// bearer is FastAPI's OAuth2PasswordBearer with auto_error off: «Bearer <token>», the
// scheme in any case, split at the first space; anything else is no token.
func bearer(header string) string {
	scheme, param, _ := strings.Cut(header, " ")
	if !strings.EqualFold(scheme, "bearer") {
		return ""
	}
	return param
}

// Refusal is an HTTPException of the Python gateway: status, `{"detail": …}` and headers.
// Internal is an exception no handler catches (a database that does not answer): Starlette's
// plain-text 500.
type Refusal struct {
	Status   int
	Detail   string
	Header   http.Header
	Internal bool
}

// internalError is the Refusal of an unexpected error.
func internalError() *Refusal {
	return &Refusal{Status: http.StatusInternalServerError, Detail: "Internal Server Error", Internal: true}
}

func (e *Refusal) Error() string { return fmt.Sprintf("%d %s", e.Status, e.Detail) }

// invalidCredentials is _refused: the 401 with WWW-Authenticate, and when the token was
// the cookie's, the Set-Cookie that deletes it.
func invalidCredentials(r *http.Request, fromCookie bool, cookieDomain string) *Refusal {
	h := http.Header{"Www-Authenticate": {"Bearer"}}
	if fromCookie {
		h.Set("Set-Cookie", DeletingCookie(r, cookieDomain, time.Now()))
	}
	return &Refusal{Status: http.StatusUnauthorized, Detail: "Invalid credentials", Header: h}
}

// DeletingCookie is sessions.deleting_header, byte for byte as Starlette writes it:
// Python's http.cookies puts the attributes in alphabetical order, and `expires=0`
// becomes the moment of the answer.
func DeletingCookie(r *http.Request, domain string, now time.Time) string {
	var b strings.Builder
	b.WriteString(CookieName + `=""`)
	if domain != "" {
		b.WriteString("; Domain=" + domain)
	}
	b.WriteString("; expires=" + now.UTC().Format("Mon, 02 Jan 2006 15:04:05 GMT"))
	b.WriteString("; HttpOnly; Max-Age=0; Path=/; SameSite=lax")
	if InHTTPS(r) {
		b.WriteString("; Secure")
	}
	return b.String()
}

// InHTTPS is sessions._in_https: behind a proxy that terminates TLS the gateway receives
// http, and what the proxy declares counts.
func InHTTPS(r *http.Request) bool {
	if r.TLS != nil {
		return true
	}
	first, _, _ := strings.Cut(r.Header.Get("X-Forwarded-Proto"), ",")
	return strings.EqualFold(strings.TrimSpace(first), "https")
}
