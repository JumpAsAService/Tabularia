package api

import (
	"fmt"
	"net/http"
	"strings"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/server"
)

// SSOConfigOut is sso.public_config: whether the sign-in page shows the SSO button.
type SSOConfigOut struct {
	Enabled     bool   `json:"enabled"`
	ButtonLabel string `json:"button_label"`
}

// routes/sso.py, on an installation WITHOUT single sign-on: the button is not shown, and the
// flow's two routes send back to the interface with «sso_disabled». With SSO configured
// (OIDC__ISSUER and OIDC__CLIENT_ID) the three routes are sso_oidc.go's.
func init() {
	register("sso", func(d *Deps) []server.Route {
		if d.Config.SSOEnabled() {
			return nil
		}
		disabled := http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
			redirectWith(w, d.Config.OIDCPostLoginURL, "error=sso_disabled")
		})
		return []server.Route{
			{Method: http.MethodGet, Pattern: "/auth/sso/config", Handler: http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
				server.JSON(w, http.StatusOK, SSOConfigOut{Enabled: false, ButtonLabel: d.Config.OIDCButtonLabel})
			})},
			{Method: http.MethodGet, Pattern: "/auth/sso/login", Handler: disabled},
			{Method: http.MethodGet, Pattern: "/auth/sso/callback", Handler: disabled},
		}
	})
}

// redirectWith is sso._redirect_with: the interface's page with a fragment, a 303.
func redirectWith(w http.ResponseWriter, base, fragment string) {
	sep := "#"
	if strings.Contains(base, "#") {
		sep = "&"
	}
	redirect(w, base+sep+fragment, http.StatusSeeOther)
}

// redirect is Starlette's RedirectResponse: the address quoted as urllib's quote with its safe
// characters, no body.
func redirect(w http.ResponseWriter, url string, status int) {
	w.Header().Set("Location", quoteURL(url))
	w.Header().Set("Content-Length", "0")
	w.WriteHeader(status)
}

// quoteURL is quote(url, safe=":/%#?=@[]!$&'()*+,;").
func quoteURL(s string) string {
	const safe = ":/%#?=@[]!$&'()*+,;" + "_.-~"
	var b strings.Builder
	for i := 0; i < len(s); i++ {
		c := s[i]
		if c < 0x80 && (c >= 'a' && c <= 'z' || c >= 'A' && c <= 'Z' || c >= '0' && c <= '9' || strings.IndexByte(safe, c) >= 0) {
			b.WriteByte(c)
			continue
		}
		fmt.Fprintf(&b, "%%%02X", c)
	}
	return b.String()
}
