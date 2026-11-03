package api

import (
	"net/http"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/auth"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/reports"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/server"
)

// routes/reports.py: the pixel-perfect reports, seen with VIEW on their folder (404 to whoever
// lacks it), changed with EDIT.
func init() {
	register("reports", func(d *Deps) []server.Route {
		h := &objectsAPI{d: d, kind: reports.Kind, pushmail: reports.Pushmail, param: "report_id"}
		route := func(method, pattern string, fn func(http.ResponseWriter, *http.Request, *auth.Principal)) server.Route {
			return server.Route{Method: method, Pattern: pattern, Handler: server.Authed(d.Auth, pattern, fn)}
		}
		return []server.Route{
			route(http.MethodGet, "/reports", h.list),
			route(http.MethodGet, "/reports/search", h.search),
			route(http.MethodPost, "/reports/{report_id}/views", h.opened),
			route(http.MethodGet, "/projects/{project_id}/reports", h.listProject),
			route(http.MethodGet, "/reports/{report_id}", h.get),
			route(http.MethodPost, "/reports/{report_id}/duplicate", h.duplicate),
			route(http.MethodDelete, "/reports/{report_id}", h.remove),
			route(http.MethodGet, "/reports/{report_id}/versions", h.versions),
			route(http.MethodGet, "/reports/{report_id}/versions/{version}", h.version),
		}
	})
}
