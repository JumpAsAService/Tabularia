package api

import (
	"io"
	"log/slog"
	"testing"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/config"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/server"
)

// Every route of the Python gateway has its Go handler, and Go has no route FastAPI does not
// declare apart from the Go-only ones listed by name (server/go_only_routes.go): the router
// answers from FastAPI's table (server.New), so a gap would be a 500 and an extra route
// unreachable. With and without single sign-on (sso.go, sso_oidc.go).
func TestEveryPythonRouteHasItsGoHandler(t *testing.T) {
	for _, sso := range []bool{false, true} {
		cfg := &config.Config{}
		if sso {
			cfg.OIDCIssuer, cfg.OIDCClientID = "https://idp.test", "tabularia"
		}
		d := &Deps{Config: cfg, Log: slog.New(slog.NewTextHandler(io.Discard, nil))}
		have := map[[2]string]bool{{"GET", "/health"}: true}
		for _, r := range Routes(d) {
			have[[2]string{r.Method, server.FastAPIPath(r.Pattern)}] = true
		}
		want := map[[2]string]bool{}
		for _, r := range server.PythonRoutes(server.MainApp) {
			want[r] = true
			if r[0] == "HEAD" && have[[2]string{"GET", r[1]}] {
				continue // Starlette's Route: its GET answers HEAD
			}
			if !have[r] {
				t.Errorf("sso=%v: %s %s has no Go handler", sso, r[0], r[1])
			}
		}
		// the routes only Go has, each declared by name in server/go_only_routes.go
		for _, r := range server.GoOnlyRoutes() {
			want[r] = true
			if !have[r] {
				t.Errorf("sso=%v: the Go-only %s %s has no Go handler", sso, r[0], r[1])
			}
		}
		for r := range have {
			if !want[r] {
				t.Errorf("sso=%v: %s %s is not a route of the Python gateway", sso, r[0], r[1])
			}
		}
		host := map[[2]string]bool{}
		for _, r := range modules["notebook_proxy"](d) {
			host[[2]string{r.Method, server.FastAPIPath(r.Pattern)}] = true
		}
		for _, r := range server.PythonRoutes(server.NotebooksHostApp) {
			if !host[r] {
				t.Errorf("the notebooks' host: %s %s has no Go handler", r[0], r[1])
			}
		}
	}
}
