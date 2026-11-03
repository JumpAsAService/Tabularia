package api

import (
	"context"
	"net/http"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/auth"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/dashboards"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/perm"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyd/gatewaymodels"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/server"
)

// routes/dashboards.py: screens of live tiles kept in a folder with their versions, seen with
// VIEW on their folder (404 to whoever lacks it), changed with EDIT. What they share with the
// reports is objectsAPI; their definition is checked as a dashboard's (internal/dashboards).
func init() {
	register("dashboards", func(d *Deps) []server.Route {
		h := &objectsAPI{d: d, kind: dashboards.Kind, pushmail: dashboards.Pushmail, param: "dashboard_id", openAudit: "dashboard.open",
			createModel: gatewaymodels.DashboardCreate, updateModel: gatewaymodels.DashboardUpdate,
			checkModel: gatewaymodels.DashboardsCheckRequest, empty: dashboards.Empty,
			check:  dashboardChecked,
			review: dashboardProblems, inOurWords: dashboards.InOurWordsErr}
		route := func(method, pattern string, fn func(http.ResponseWriter, *http.Request, *auth.Principal)) server.Route {
			return server.Route{Method: method, Pattern: pattern, Handler: server.Authed(d.Auth, pattern, fn)}
		}
		body := func(method, pattern string, fn bodyHandler) server.Route {
			return server.Route{Method: method, Pattern: pattern, Handler: withBody(d.Auth, pattern, fn)}
		}
		return []server.Route{
			route(http.MethodGet, "/dashboards", h.list),
			route(http.MethodGet, "/projects/{project_id}/dashboards", h.listProject),
			route(http.MethodGet, "/dashboards/search", h.search),
			route(http.MethodGet, "/dashboards/folders", h.folders),
			body(http.MethodPost, "/dashboards/check", h.checkDefinition),
			route(http.MethodGet, "/dashboards/{dashboard_id}", h.get),
			route(http.MethodPost, "/dashboards/{dashboard_id}/views", h.opened),
			body(http.MethodPost, "/projects/{project_id}/dashboards", h.create),
			body(http.MethodPatch, "/dashboards/{dashboard_id}", h.update),
			route(http.MethodPost, "/dashboards/{dashboard_id}/duplicate", h.duplicate),
			route(http.MethodDelete, "/dashboards/{dashboard_id}", h.remove),
			route(http.MethodGet, "/dashboards/{dashboard_id}/versions", h.versions),
			route(http.MethodGet, "/dashboards/{dashboard_id}/versions/{version}", h.version),
			route(http.MethodPost, "/dashboards/{dashboard_id}/versions/{version}/restore", h.restore),
			body(http.MethodPost, "/dashboards/preview", h.previewNumbers),
			body(http.MethodPost, "/dashboards/{dashboard_id}/data", h.data),
			body(http.MethodPost, "/dashboards/{dashboard_id}/render", h.renderDashboard),
			route(http.MethodGet, "/dashboards/{dashboard_id}/render", h.renderDashboardFile),
			route(http.MethodGet, "/dashboards/{dashboard_id}/parameters", h.dashboardParameters),
			body(http.MethodPost, "/dashboards/{dashboard_id}/parameters", h.dashboardParametersEdited),
			body(http.MethodPost, "/dashboards/{dashboard_id}/fields", h.dashboardFields),
		}
	})
}

func dashboardChecked(ctx context.Context, h *objectsAPI, c *perm.Checker, definition, current any) (pyjson.Object, error) {
	r, err := dashboards.Review(ctx, h.reviewer(c), definition, current)
	if err != nil {
		return nil, err
	}
	if len(r.Problems) > 0 {
		return nil, r.Refusal()
	}
	return r.Definition, nil
}

func dashboardProblems(ctx context.Context, h *objectsAPI, c *perm.Checker, definition, current any) ([]pyjson.Object, error) {
	r, err := dashboards.Review(ctx, h.reviewer(c), definition, current)
	if err != nil {
		return nil, err
	}
	return r.Problems, nil
}

// folders is GET /dashboards/folders (folders_for_a_new_dashboard): where this person can make
// one, EDIT, in order.
func (h *objectsAPI) folders(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
	c := perm.For(h.d.DB, &p.User)
	ids, err := c.Projects(r.Context(), perm.Edit)
	if err != nil {
		h.fail(w, r, err)
		return
	}
	server.JSON(w, http.StatusOK, perm.Sorted(ids))
}
