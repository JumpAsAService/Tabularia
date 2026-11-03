package auth

import (
	"crypto/tls"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"
)

func TestTheHeaderWinsOverTheCookie(t *testing.T) {
	r := httptest.NewRequest("POST", "/x", nil)
	r.Header.Set("Authorization", "bearer abc")
	r.AddCookie(&http.Cookie{Name: CookieName, Value: "cookie"})
	c, ref := fromRequest(r)
	if ref != nil || c.token != "abc" || c.fromCookie {
		t.Fatalf("%+v %v", c, ref)
	}
}

func TestAnotherSchemeOrAnEmptyBearerFallsToTheCookie(t *testing.T) {
	for _, h := range []string{"Basic abc", "Bearer", "Bearer ", "Token abc"} {
		r := httptest.NewRequest("GET", "/x", nil)
		r.Header.Set("Authorization", h)
		r.AddCookie(&http.Cookie{Name: CookieName, Value: "cookie"})
		c, ref := fromRequest(r)
		if ref != nil || c.token != "cookie" || !c.fromCookie {
			t.Errorf("%q: %+v %v", h, c, ref)
		}
	}
}

func TestNothingIsA401WithoutTheCookieDeleted(t *testing.T) {
	_, ref := fromRequest(httptest.NewRequest("GET", "/x", nil))
	if ref == nil || ref.Status != 401 || ref.Detail != "Invalid credentials" || ref.Header.Get("Www-Authenticate") != "Bearer" || ref.Header.Get("Set-Cookie") != "" {
		t.Fatalf("%+v", ref)
	}
}

func TestTheCookieChangesNothingWithoutTheCSRFHeader(t *testing.T) {
	for _, m := range []string{"POST", "PUT", "PATCH", "DELETE"} {
		r := httptest.NewRequest(m, "/x", nil)
		r.AddCookie(&http.Cookie{Name: CookieName, Value: "cookie"})
		_, ref := fromRequest(r)
		if ref == nil || ref.Status != 403 || ref.Detail != "The request is missing the X-Tabularia-Csrf header" {
			t.Errorf("%s: %+v", m, ref)
		}
		r.Header.Set("X-Tabularia-Csrf", "1")
		if _, ref := fromRequest(r); ref != nil {
			t.Errorf("%s with the header: %+v", m, ref)
		}
	}
	for _, m := range []string{"GET", "HEAD", "OPTIONS"} {
		r := httptest.NewRequest(m, "/x", nil)
		r.AddCookie(&http.Cookie{Name: CookieName, Value: "cookie"})
		if _, ref := fromRequest(r); ref != nil {
			t.Errorf("%s: %+v", m, ref)
		}
	}
}

// the header captured from the Python gateway on 2026-11-03
func TestTheDeletingCookieIsStarlettes(t *testing.T) {
	at := time.Date(2026, 11, 3, 18, 34, 22, 0, time.UTC)
	r := httptest.NewRequest("GET", "/x", nil)
	want := `tab_token=""; expires=Tue, 03 Nov 2026 18:34:22 GMT; HttpOnly; Max-Age=0; Path=/; SameSite=lax`
	if got := DeletingCookie(r, "", at); got != want {
		t.Errorf("\n got %s\nwant %s", got, want)
	}
	r.Header.Set("X-Forwarded-Proto", "https, http")
	want = `tab_token=""; Domain=example.com; expires=Tue, 03 Nov 2026 18:34:22 GMT; HttpOnly; Max-Age=0; Path=/; SameSite=lax; Secure`
	if got := DeletingCookie(r, "example.com", at); got != want {
		t.Errorf("\n got %s\nwant %s", got, want)
	}
	r = httptest.NewRequest("GET", "/x", nil)
	r.TLS = &tls.ConnectionState{}
	if !InHTTPS(r) {
		t.Error("a TLS connection is https")
	}
}

func TestARefusedCookieIsDeleted(t *testing.T) {
	r := httptest.NewRequest("GET", "/x", nil)
	ref := invalidCredentials(r, true, "")
	if ref.Status != 401 || ref.Header.Get("Set-Cookie") == "" || ref.Header.Get("Www-Authenticate") != "Bearer" {
		t.Errorf("%+v", ref)
	}
}
