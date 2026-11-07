package api

// The data APIs in Administration (docs/specs/data-apis.md, «Administration»): every data API
// of the installation with its creator, its state, its runtime, its pods and its traffic; an
// administrator sets the runtime of any of them (only an administrator does: replicas or
// autoscaling, CPU and memory of each pod), and pauses or deletes any of them, whoever made it,
// even with the feature switched off (so what is left of it can be cleaned). An observer reads
// the list; only an administrator acts. The audit records each action as the page's do, marked
// as the administration's.

import (
	"net/http"

	"github.com/jackc/pgx/v5"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/auth"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/dataapis"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/roles"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/server"
)

func init() {
	register("data_apis_admin", func(d *Deps) []server.Route {
		route := func(method, pattern string, h dataAPIHandler) server.Route {
			return server.Route{Method: method, Pattern: pattern, Handler: server.Authed(d.Auth, pattern,
				func(w http.ResponseWriter, r *http.Request, p *auth.Principal) { h(d, w, r, p) })}
		}
		return []server.Route{
			route(http.MethodGet, "/admin/data-apis", adminListDataAPIsRoute),
			route(http.MethodPut, "/admin/data-apis/{api_id}/runtime", adminDataAPIRuntimeRoute),
			route(http.MethodPost, "/admin/data-apis/{api_id}/pause", adminPauseDataAPIRoute),
			route(http.MethodDelete, "/admin/data-apis/{api_id}", adminDeleteDataAPIRoute),
		}
	})
}

type dataAPIHandler = func(*Deps, http.ResponseWriter, *http.Request, *auth.Principal)

// The routes as registered: the list for an administrator or an observer, the actions for an
// administrator only (adminRole's rule).
var (
	adminListDataAPIsRoute   = dataAPIAdminGuard(false, adminDataAPIs)
	adminDataAPIRuntimeRoute = dataAPIAdminGuard(true, adminSetDataAPIRuntime)
	adminPauseDataAPIRoute   = dataAPIAdminGuard(true, adminPauseDataAPI)
	adminDeleteDataAPIRoute  = dataAPIAdminGuard(true, adminDeleteDataAPI)
)

func dataAPIAdminGuard(admin bool, h dataAPIHandler) dataAPIHandler {
	return func(d *Deps, w http.ResponseWriter, r *http.Request, p *auth.Principal) {
		rs, err := roles.Of(r.Context(), d.DB, p.User.ID, p.User.IsSuperuser, p.User.IsObserver)
		switch {
		case err != nil:
			adminFail(d, w, "reading the roles", err)
		case admin && !rs.Admin:
			adminDetail(w, http.StatusForbidden, "Admin privileges required")
		case !admin && !rs.Observer:
			adminDetail(w, http.StatusForbidden, "Administrative read access required")
		default:
			h(d, w, r, p)
		}
	}
}

type dataAPIAdminCreator struct {
	dataAPIPerson
	Active bool `json:"active"`
}

type dataAPIAdminOut struct {
	ID        int64                `json:"id"`
	Name      string               `json:"name"`
	Slug      string               `json:"slug"`
	Path      string               `json:"path"`
	ProjectID int64                `json:"project_id"`
	State     string               `json:"state"`
	Runtime   dataapis.Runtime     `json:"runtime"`
	Creator   *dataAPIAdminCreator `json:"creator"`
	Resources int                  `json:"resources"`
	// the keys that answer now
	Keys      int     `json:"keys"`
	CreatedAt *string `json:"created_at"`
	UpdatedAt *string `json:"updated_at"`
	// the pods as the reconcile loop last saw them; checked_at nil: never (a draft, just activated)
	Wanted    int     `json:"wanted"`
	Ready     int     `json:"ready"`
	Error     string  `json:"error"`
	CheckedAt *string `json:"checked_at"`
	// the last 24 hours
	Requests   int64   `json:"requests_24h"`
	Errors     int64   `json:"errors_24h"`
	Throttled  int64   `json:"throttled_24h"`
	LastUsedAt *string `json:"last_used_at"`
}

// GET /admin/data-apis: whether the feature is on here, and every data API.
func adminDataAPIs(d *Deps, w http.ResponseWriter, r *http.Request, _ *auth.Principal) {
	ctx := r.Context()
	rows, err := dataapis.AdminList(ctx, d.DB, dataapis.Now())
	if err != nil {
		adminFail(d, w, "data apis: the administration's list", err)
		return
	}
	people := map[int64]*auth.User{}
	list := make([]dataAPIAdminOut, 0, len(rows))
	for _, a := range rows {
		o := dataAPIAdminOut{ID: a.ID, Name: a.Name, Slug: a.Slug, Path: "/data/" + a.Slug + "/", ProjectID: a.ProjectID, State: a.State,
			Runtime:   a.Runtime,
			Resources: a.Resources, Keys: a.Keys, CreatedAt: isoPtr(&a.CreatedAt), UpdatedAt: isoPtr(&a.UpdatedAt),
			CheckedAt: isoPtr(a.CheckedAt), Requests: a.Requests, Errors: a.Errors, Throttled: a.Throttled, LastUsedAt: isoPtr(a.LastUsedAt)}
		if a.Status != nil {
			o.Wanted, o.Ready, o.Error = a.Status.Wanted, a.Status.Ready, a.Status.Error
		}
		u, seen := people[a.CreatorID]
		if !seen {
			if u, err = loadUser(ctx, d.DB, a.CreatorID); err != nil {
				adminFail(d, w, "data apis: the creators", err)
				return
			}
			people[a.CreatorID] = u
		}
		if u != nil {
			o.Creator = &dataAPIAdminCreator{dataAPIPerson{ID: u.ID, Email: u.Email, FullName: u.FullName}, u.IsActive}
		}
		list = append(list, o)
	}
	off := dataAPIsOff(d)
	w.Header().Set("Cache-Control", "no-store")
	server.JSON(w, http.StatusOK, map[string]any{"enabled": off == "", "reason": off, "max_apis": d.Config.DataAPI.MaxAPIs,
		"max_total_replicas": d.Config.DataAPI.MaxTotalReplicas, "runtime_limits": runtimeLimits(d.Config.DataAPI),
		"default_runtime": dataapis.DefaultRuntime(), "base_url": d.Config.DataAPI.Pods.PublicBaseURL,
		"host": dataapis.IngressHost(d.Config.DataAPI.Pods), "apis": list})
}

// adminDataAPIOf is the data API of the path, any of the installation (404 when there is none).
func adminDataAPIOf(d *Deps, w http.ResponseWriter, r *http.Request) *dataapis.API {
	ids, ok := pathInts(w, r, "api_id")
	if !ok {
		return nil
	}
	a, err := dataapis.Get(r.Context(), d.DB, ids[0])
	if err != nil {
		adminFail(d, w, "data apis: reading it", err)
		return nil
	}
	if a == nil {
		adminDetail(w, http.StatusNotFound, "Data API not found")
	}
	return a
}

// PUT /admin/data-apis/{api_id}/runtime: the replicas (or autoscaling), CPU and memory of the
// data API's pods, of any data API; within the bounds of dataapis.CheckRuntime and, for an
// active one, the most pods of all the active ones together (409 past it). Not a version: what
// it publishes stays. An active data API's pods follow at the reconcile loop's next round, a
// rolling update (a new pod ready before an old one stops); DuckDB's memory follows the pod's
// limit, as the pods read it. The answer is the runtime, tidied.
func adminSetDataAPIRuntime(d *Deps, w http.ResponseWriter, r *http.Request, p *auth.Principal) {
	if !dataAPINeedOn(d, w) {
		return
	}
	a := adminDataAPIOf(d, w, r)
	if a == nil {
		return
	}
	var in dataapis.Runtime
	if !decodeDataAPIBody(w, r, &in) {
		return
	}
	rt, err := dataapis.CheckRuntime(in, d.Config.DataAPI.MaxTotalReplicas)
	if err != nil {
		adminDetail(w, http.StatusUnprocessableEntity, dataapis.Sentence("the runtime: "+err.Error()))
		return
	}
	ctx := r.Context()
	var before dataapis.Runtime
	var ref *dataAPIRefusal
	err = inTx(ctx, d, func(tx pgx.Tx) error {
		// the activations' lock: they and this count the pods of each other
		if _, err := tx.Exec(ctx, `SELECT pg_advisory_xact_lock(7140)`); err != nil {
			return err
		}
		now, err := dataapis.GetForUpdate(ctx, tx, a.ID)
		if err != nil {
			return err
		}
		if now == nil {
			ref = &dataAPIRefusal{http.StatusNotFound, "Data API not found"}
			return nil
		}
		a, before = now, now.Runtime
		if dataapis.SameRuntime(before, rt) {
			return nil
		}
		if a.State == dataapis.Active {
			if ref, err = checkDataAPIPods(ctx, d, tx, a.ID, rt); err != nil || ref != nil {
				return err
			}
		}
		return dataapis.SetRuntime(ctx, tx, a.ID, rt)
	})
	if answerRefusal(d, w, "data apis: the runtime", ref, err) {
		return
	}
	if !dataapis.SameRuntime(before, rt) {
		auditDataAPI(ctx, d, &p.User, "data_api.runtime", a, pyjson.Object{{Key: "before", Value: runtimeObject(before)},
			{Key: "after", Value: runtimeObject(rt)}, {Key: "state", Value: a.State}, {Key: "by", Value: "administration"}})
	} else {
		rt = before
	}
	server.JSON(w, http.StatusOK, rt)
}

// runtimeObject is a runtime as the audit keeps it: the replicas it uses, the CPU and memory.
func runtimeObject(rt dataapis.Runtime) pyjson.Object {
	out := pyjson.Object{{Key: "autoscale", Value: rt.Autoscale}}
	if rt.Autoscale {
		out = append(out, pyjson.KV{Key: "min_replicas", Value: int64(rt.MinReplicas)}, pyjson.KV{Key: "max_replicas", Value: int64(rt.MaxReplicas)})
	} else {
		out = append(out, pyjson.KV{Key: "replicas", Value: int64(rt.Replicas)})
	}
	return append(out, pyjson.KV{Key: "cpu", Value: rt.CPU}, pyjson.KV{Key: "memory", Value: rt.Memory})
}

// POST /admin/data-apis/{api_id}/pause: no longer served, as from its page.
func adminPauseDataAPI(d *Deps, w http.ResponseWriter, r *http.Request, p *auth.Principal) {
	a := adminDataAPIOf(d, w, r)
	if a == nil {
		return
	}
	if a.State != dataapis.Active {
		adminDetail(w, http.StatusConflict, "Only an active data API can be paused")
		return
	}
	ctx := r.Context()
	if err := dataapis.SetState(ctx, d.DB, a.ID, dataapis.Paused); err != nil {
		adminFail(d, w, "data apis: pausing", err)
		return
	}
	auditDataAPI(ctx, d, &p.User, "data_api.pause", a, pyjson.Object{{Key: "by", Value: "administration"}})
	adminNone(w)
}

// DELETE /admin/data-apis/{api_id}: with its versions, resources and keys, as from Explore; the
// reconcile loop removes its objects from the cluster within its round.
func adminDeleteDataAPI(d *Deps, w http.ResponseWriter, r *http.Request, p *auth.Principal) {
	a := adminDataAPIOf(d, w, r)
	if a == nil {
		return
	}
	ctx := r.Context()
	if err := dataapis.Delete(ctx, d.DB, a.ID); err != nil {
		adminFail(d, w, "data apis: deleting it", err)
		return
	}
	auditDataAPI(ctx, d, &p.User, "data_api.delete", a, pyjson.Object{{Key: "state", Value: a.State}, {Key: "by", Value: "administration"}})
	adminNone(w)
}
