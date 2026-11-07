package api

// Data APIs (docs/specs/data-apis.md): an object of a folder that publishes datasources as a
// read-only HTTP API, answered by pods of its own on Kubernetes. This file: the object — the
// lists, the page, making, editing, moving, copying, deleting, the versions, activating and
// pausing; data_apis_keys.go: its keys. Routes of the Go gateway only
// (server/go_only_routes.go).
//
// Who: VIEW on the folder sees a data API; MANAGE on it (or an administrator) makes, edits
// and manages its keys, and must be able to read every datasource it publishes. Its runtime
// (replicas, CPU, memory) is the administrator's alone (data_apis_admin.go): here a new one
// and a copy get DefaultRuntime, and an edit that would change it is refused. Where: on
// Kubernetes only, with DATA_API__ENABLED; elsewhere the lists are empty and the rest
// answers 409, as the saved scripts do. The pods' side is data_apis_pods.go, the reconcile
// loop that runs them jobs_data_apis.go.

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"strconv"
	"strings"

	"github.com/jackc/pgx/v5"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/audit"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/auth"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/config"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/copynames"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/dataapis"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/perm"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/server"
)

func init() {
	register("data_apis", func(d *Deps) []server.Route {
		route := func(method, pattern string, h func(*Deps, http.ResponseWriter, *http.Request, *auth.Principal)) server.Route {
			return server.Route{Method: method, Pattern: pattern, Handler: server.Authed(d.Auth, pattern,
				func(w http.ResponseWriter, r *http.Request, p *auth.Principal) { h(d, w, r, p) })}
		}
		return []server.Route{
			route(http.MethodGet, "/data-apis/limits", dataAPILimits),
			route(http.MethodGet, "/data-apis", listDataAPIs),
			route(http.MethodGet, "/projects/{project_id}/data-apis", listProjectDataAPIs),
			route(http.MethodPost, "/projects/{project_id}/data-apis", createDataAPI),
			route(http.MethodGet, "/data-apis/{api_id}", getDataAPI),
			route(http.MethodPatch, "/data-apis/{api_id}", updateDataAPI),
			route(http.MethodDelete, "/data-apis/{api_id}", deleteDataAPI),
			route(http.MethodPost, "/data-apis/{api_id}/duplicate", duplicateDataAPI),
			route(http.MethodPost, "/data-apis/{api_id}/activate", activateDataAPI),
			route(http.MethodPost, "/data-apis/{api_id}/pause", pauseDataAPI),
			route(http.MethodGet, "/data-apis/{api_id}/versions", listDataAPIVersions),
			route(http.MethodGet, "/data-apis/{api_id}/versions/{version}", getDataAPIVersion),
			route(http.MethodPost, "/data-apis/{api_id}/versions/{version}/restore", restoreDataAPIVersion),
			route(http.MethodGet, "/data-apis/{api_id}/keys", listDataAPIKeys),
			route(http.MethodPost, "/data-apis/{api_id}/keys", createDataAPIKey),
			route(http.MethodPost, "/data-apis/{api_id}/keys/{key_id}/revoke", revokeDataAPIKey),
			route(http.MethodPost, "/data-apis/{api_id}/keys/{key_id}/renew", renewDataAPIKey),
		}
	})
}

// ── where the feature is ──────────────────────────────────────────────────────

// dataAPIsOff says why the data APIs are not offered here: "kubernetes" (no scripting
// environments, so no Kubernetes: the criterion of the saved scripts and the notebooks),
// "disabled" (DATA_API__ENABLED is off); "" when they are.
func dataAPIsOff(d *Deps) string {
	if !(d.Config.ScriptsEnabled && d.Config.ScriptsNamespace != "" && d.Config.ScriptsRegistry != "") {
		return "kubernetes"
	}
	if !d.Config.DataAPI.Enabled {
		return "disabled"
	}
	return ""
}

func dataAPIsOn(d *Deps) bool { return dataAPIsOff(d) == "" }

// dataAPINeedOn answers the 409 of an installation without data APIs.
func dataAPINeedOn(d *Deps, w http.ResponseWriter) bool {
	switch dataAPIsOff(d) {
	case "kubernetes":
		detail(w, http.StatusConflict, "Data APIs need Kubernetes")
		return false
	case "disabled":
		detail(w, http.StatusConflict, "Data APIs are switched off on this installation (DATA_API__ENABLED)")
		return false
	}
	return true
}

// ── shapes ────────────────────────────────────────────────────────────────────

type dataAPIPerson struct {
	ID       int64  `json:"id"`
	Email    string `json:"email"`
	FullName string `json:"full_name"`
}

type dataAPIOut struct {
	ID             int64          `json:"id"`
	Name           string         `json:"name"`
	Slug           string         `json:"slug"`
	Path           string         `json:"path"`
	Description    string         `json:"description"`
	ProjectID      int64          `json:"project_id"`
	State          string         `json:"state"`
	CurrentVersion int64          `json:"current_version"`
	Resources      int            `json:"resources"`
	Creator        *dataAPIPerson `json:"creator"`
	CreatedAt      *string        `json:"created_at"`
	UpdatedAt      *string        `json:"updated_at"`
	// MANAGE on the folder (or an administrator), not an observer: editing, the keys
	CanManage bool `json:"can_manage"`
	// an administrator (not an observer): the runtime (PUT /admin/data-apis/{id}/runtime)
	CanSetRuntime bool `json:"can_set_runtime"`
}

// dataAPIDatasource is the datasource of a resource, as the page shows it: its name and its
// columns only to whoever may read it.
type dataAPIDatasource struct {
	ID       int64             `json:"id"`
	Name     *string           `json:"name"`
	Readable bool              `json:"readable"`
	Exists   bool              `json:"exists"`
	Columns  []dataapis.Column `json:"columns"`
}

type dataAPIResourceOut struct {
	dataapis.Resource
	Datasource dataAPIDatasource `json:"datasource"`
}

type dataAPIDetail struct {
	dataAPIOut
	Runtime      dataapis.Runtime     `json:"runtime"`
	ResourceList []dataAPIResourceOut `json:"resource_list"`
	// Suspended says why an active data API would not answer now: "creator_inactive",
	// "creator_rights", "datasource_rights"; "" when nothing stops it
	Suspended string `json:"suspended"`
}

type dataAPIOuts struct {
	d      *Deps
	ctx    context.Context
	c      *perm.Checker
	people map[int64]*auth.User
	manage map[int64]bool
}

func newDataAPIOuts(ctx context.Context, d *Deps, p *auth.Principal) *dataAPIOuts {
	return &dataAPIOuts{d: d, ctx: ctx, c: perm.For(d.DB, &p.User), people: map[int64]*auth.User{}, manage: map[int64]bool{}}
}

func (o *dataAPIOuts) person(id int64) (*auth.User, error) {
	if u, ok := o.people[id]; ok {
		return u, nil
	}
	u, err := loadUser(o.ctx, o.d.DB, id)
	if err != nil {
		return nil, err
	}
	o.people[id] = u
	return u, nil
}

// mayManage: MANAGE on the folder (an administrator always), never an observer alone.
func dataAPIMayManage(ctx context.Context, c *perm.Checker, projectID int64) (bool, error) {
	rs, err := c.Roles(ctx)
	if err != nil || rs.ObserverOnly() {
		return false, err
	}
	return c.Has(ctx, projectID, perm.Manage)
}

func (o *dataAPIOuts) canManage(projectID int64) (bool, error) {
	if ok, seen := o.manage[projectID]; seen {
		return ok, nil
	}
	ok, err := dataAPIMayManage(o.ctx, o.c, projectID)
	if err != nil {
		return false, err
	}
	o.manage[projectID] = ok
	return ok, nil
}

func (o *dataAPIOuts) of(a *dataapis.API, resources int) (dataAPIOut, error) {
	out := dataAPIOut{ID: a.ID, Name: a.Name, Slug: a.Slug, Path: "/data/" + a.Slug + "/", Description: a.Description,
		ProjectID: a.ProjectID, State: a.State, CurrentVersion: a.CurrentVersion, Resources: resources,
		CreatedAt: isoPtr(&a.CreatedAt), UpdatedAt: isoPtr(&a.UpdatedAt)}
	creator, err := o.person(a.CreatorID)
	if err != nil {
		return out, err
	}
	if creator != nil {
		out.Creator = &dataAPIPerson{ID: creator.ID, Email: creator.Email, FullName: creator.FullName}
	}
	out.CanManage, err = o.canManage(a.ProjectID)
	if err != nil {
		return out, err
	}
	rs, err := o.c.Roles(o.ctx)
	out.CanSetRuntime = rs.Admin
	return out, err
}

func (o *dataAPIOuts) detail(a *dataapis.API) (*dataAPIDetail, error) {
	def, err := dataapis.CurrentDefinition(o.ctx, o.d.DB, a)
	if err != nil {
		return nil, err
	}
	base, err := o.of(a, len(def.Resources))
	if err != nil {
		return nil, err
	}
	out := &dataAPIDetail{dataAPIOut: base, Runtime: def.Runtime, ResourceList: []dataAPIResourceOut{}}
	ids := []int64{}
	for _, r := range def.Resources {
		ids = append(ids, r.Source.ID)
	}
	sources, err := dataapis.Datasources(o.ctx, o.d.DB, ids)
	if err != nil {
		return nil, err
	}
	for _, r := range def.Resources {
		ro := dataAPIResourceOut{Resource: r, Datasource: dataAPIDatasource{ID: r.Source.ID, Columns: []dataapis.Column{}}}
		if ds := sources[r.Source.ID]; ds != nil {
			ro.Datasource.Exists = true
			readable, err := o.c.Has(o.ctx, ds.ProjectID, perm.View)
			if err != nil {
				return nil, err
			}
			if readable {
				name := ds.Name
				ro.Datasource.Name, ro.Datasource.Readable = &name, true
				if ds.Columns != nil {
					ro.Datasource.Columns = ds.Columns
				}
			}
		}
		out.ResourceList = append(out.ResourceList, ro)
	}
	if out.Suspended, err = dataAPISuspension(o.ctx, o.d, a, def, sources); err != nil {
		return nil, err
	}
	return out, nil
}

// dataAPISuspension is why the data API would not answer on its creator's authority now (the
// pods check the same at every poll): "" when nothing stops it.
func dataAPISuspension(ctx context.Context, d *Deps, a *dataapis.API, def dataapis.Definition,
	sources map[int64]*dataapis.Datasource) (string, error) {
	creator, err := loadUser(ctx, d.DB, a.CreatorID)
	if err != nil {
		return "", err
	}
	if creator == nil || !creator.IsActive {
		return "creator_inactive", nil
	}
	c := perm.For(d.DB, creator)
	ok, err := dataAPIMayManage(ctx, c, a.ProjectID)
	if err != nil || !ok {
		return "creator_rights", err
	}
	for _, r := range def.Resources {
		ds := sources[r.Source.ID]
		if ds == nil {
			return "datasource_rights", nil
		}
		ok, err := c.Has(ctx, ds.ProjectID, perm.View)
		if err != nil || !ok {
			return "datasource_rights", err
		}
	}
	return "", nil
}

func dataAPIResourceCounts(ctx context.Context, d *Deps, ids []int64) (map[int64]int, error) {
	out := map[int64]int{}
	if len(ids) == 0 {
		return out, nil
	}
	rows, err := d.DB.Query(ctx, `SELECT data_api_id, count(*) FROM data_api_resources WHERE data_api_id = ANY($1) GROUP BY data_api_id`, ids)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	for rows.Next() {
		var id int64
		var n int
		if err := rows.Scan(&id, &n); err != nil {
			return nil, err
		}
		out[id] = n
	}
	return out, rows.Err()
}

func sendDataAPIList(d *Deps, w http.ResponseWriter, r *http.Request, p *auth.Principal, projects []int64) {
	ctx := r.Context()
	if !dataAPIsOn(d) {
		server.JSON(w, http.StatusOK, []dataAPIOut{})
		return
	}
	o := newDataAPIOuts(ctx, d, p)
	list, err := dataapis.Visible(ctx, d.DB, o.c, projects)
	if err != nil {
		fail(d, w, "data apis: the list", err)
		return
	}
	ids := make([]int64, len(list))
	for i, a := range list {
		ids[i] = a.ID
	}
	counts, err := dataAPIResourceCounts(ctx, d, ids)
	if err != nil {
		fail(d, w, "data apis: the resources", err)
		return
	}
	out := make([]dataAPIOut, 0, len(list))
	for _, a := range list {
		ao, err := o.of(a, counts[a.ID])
		if err != nil {
			fail(d, w, "data apis: the list", err)
			return
		}
		out = append(out, ao)
	}
	server.JSON(w, http.StatusOK, out)
}

func sendDataAPIDetail(d *Deps, w http.ResponseWriter, r *http.Request, p *auth.Principal, id int64, status int) {
	ctx := r.Context()
	a, err := dataapis.Get(ctx, d.DB, id)
	if err != nil || a == nil {
		if err == nil {
			err = errors.New("the data API is gone")
		}
		fail(d, w, "data apis: reading it back", err)
		return
	}
	det, err := newDataAPIOuts(ctx, d, p).detail(a)
	if err != nil {
		fail(d, w, "data apis: the detail", err)
		return
	}
	server.JSON(w, status, det)
}

// ── guards ────────────────────────────────────────────────────────────────────

// dataAPIVisible is the data API of the path when this person sees it (404 otherwise, as if it
// did not exist), on an installation that has them (409 otherwise).
func dataAPIVisible(d *Deps, w http.ResponseWriter, r *http.Request, p *auth.Principal) (*dataapis.API, *perm.Checker, bool) {
	if !dataAPINeedOn(d, w) {
		return nil, nil, false
	}
	ids, ok := pathInts(w, r, "api_id")
	if !ok {
		return nil, nil, false
	}
	ctx := r.Context()
	a, err := dataapis.Get(ctx, d.DB, ids[0])
	if err != nil {
		fail(d, w, "data apis: reading it", err)
		return nil, nil, false
	}
	c := perm.For(d.DB, &p.User)
	if a != nil {
		ok, err := c.Has(ctx, a.ProjectID, perm.View)
		if err != nil {
			fail(d, w, "data apis: the rights", err)
			return nil, nil, false
		}
		if ok {
			return a, c, true
		}
	}
	detail(w, http.StatusNotFound, "Data API not found")
	return nil, nil, false
}

// dataAPIManaged is dataAPIVisible plus MANAGE on its folder (403 otherwise).
func dataAPIManaged(d *Deps, w http.ResponseWriter, r *http.Request, p *auth.Principal) (*dataapis.API, *perm.Checker, bool) {
	a, c, ok := dataAPIVisible(d, w, r, p)
	if !ok {
		return nil, nil, false
	}
	if !dataAPIEnsureManage(d, w, r.Context(), c, a.ProjectID) {
		return nil, nil, false
	}
	return a, c, true
}

func dataAPIEnsureManage(d *Deps, w http.ResponseWriter, ctx context.Context, c *perm.Checker, projectID int64) bool {
	ok, err := dataAPIMayManage(ctx, c, projectID)
	if err != nil {
		fail(d, w, "data apis: the rights", err)
		return false
	}
	if !ok {
		detail(w, http.StatusForbidden, fmt.Sprintf("Data APIs need MANAGE on their folder (folder %d)", projectID))
		return false
	}
	return true
}

// ── what a definition is checked against ─────────────────────────────────────

// dataAPIRefusal is a request refused for what it asks: a status and a sentence.
type dataAPIRefusal struct {
	status int
	text   string
}

func (e *dataAPIRefusal) Error() string { return e.text }

func refused422(err error) *dataAPIRefusal {
	return &dataAPIRefusal{http.StatusUnprocessableEntity, dataapis.Sentence(err.Error())}
}

// checkDataAPIDefinition checks the resources of a definition this person saves against their
// datasources; and this person must be able to read the datasource of every resource that is
// new or changed (`before`: the resources as they were; what someone else published and is
// left as it is stays). The runtime is not theirs: it is left as it comes.
func checkDataAPIDefinition(ctx context.Context, d *Deps, c *perm.Checker, def dataapis.Definition,
	before []dataapis.Resource) (dataapis.Definition, *dataAPIRefusal, error) {
	ids := []int64{}
	for _, r := range def.Resources {
		ids = append(ids, r.Source.ID)
	}
	sources, err := dataapis.Datasources(ctx, d.DB, ids)
	if err != nil {
		return def, nil, err
	}
	def, err = dataapis.CheckDefinition(def, sources)
	if err != nil {
		return def, refused422(err), nil
	}
	old := map[string]dataapis.Resource{}
	for _, r := range before {
		old[r.Name] = r
	}
	for _, r := range def.Resources {
		if prev, ok := old[r.Name]; ok && dataapis.SameResource(prev, r) {
			continue
		}
		readable, err := c.Has(ctx, sources[r.Source.ID].ProjectID, perm.View)
		if err != nil {
			return def, nil, err
		}
		if !readable {
			return def, refused422(fmt.Errorf("the resource «%s»: the datasource does not exist, or you may not read it", r.Name)), nil
		}
	}
	return def, nil, nil
}

// runtimeAdminOnly is the 403 of a save that would change the runtime: only an administrator
// sets it, from the administration (a page that sends it back as it is, is fine).
func runtimeAdminOnly(w http.ResponseWriter) {
	server.JSON(w, http.StatusForbidden, map[string]string{"code": "runtime_admin_only",
		"detail": "The runtime (replicas, CPU, memory) is set by an administrator, in Administration → Data APIs"})
}

// runtimeLimits are the bounds of a runtime, as the pages check them before the gateway does.
func runtimeLimits(c config.DataAPIConfig) map[string]any {
	return map[string]any{"min_cpu": dataapis.MinCPU, "max_cpu": dataapis.MaxCPU, "min_memory": dataapis.MinMemory,
		"max_memory": dataapis.MaxMemory, "max_replicas": dataapis.MostReplicas(c.MaxTotalReplicas)}
}

// checkDataAPIPods refuses a runtime that would take the active data APIs past the most
// pods the administrator allows.
func checkDataAPIPods(ctx context.Context, d *Deps, q dataapis.Querier, id int64, rt dataapis.Runtime) (*dataAPIRefusal, error) {
	others, err := dataapis.ActivePods(ctx, q, id)
	if err != nil {
		return nil, err
	}
	if total := d.Config.DataAPI.MaxTotalReplicas; others+rt.Pods() > total {
		return &dataAPIRefusal{http.StatusConflict, fmt.Sprintf(
			"The active data APIs may run at most %d pods together: %d are taken, this one asks for %d", total, others, rt.Pods())}, nil
	}
	return nil, nil
}

func checkDataAPIName(name string) (string, *dataAPIRefusal) {
	name = strings.TrimSpace(name)
	if name == "" {
		return "", &dataAPIRefusal{http.StatusUnprocessableEntity, "A data API needs a name"}
	}
	if len([]rune(name)) > dataapis.MaxName {
		return "", &dataAPIRefusal{http.StatusUnprocessableEntity, fmt.Sprintf("The name is longer than %d characters", dataapis.MaxName)}
	}
	return name, nil
}

func checkDataAPIDescription(text string) (string, *dataAPIRefusal) {
	text = strings.TrimSpace(text)
	if len([]rune(text)) > dataapis.MaxDescription {
		return "", &dataAPIRefusal{http.StatusUnprocessableEntity, fmt.Sprintf("The description is longer than %d characters", dataapis.MaxDescription)}
	}
	return text, nil
}

// uniqueDataAPI refuses a name another data API of the folder has, or an address another
// one has.
func uniqueDataAPI(ctx context.Context, q dataapis.Querier, projectID int64, name, slug string, except int64) (*dataAPIRefusal, error) {
	taken, err := dataapis.NameTaken(ctx, q, projectID, name, except)
	if err != nil || taken {
		return &dataAPIRefusal{http.StatusConflict, "A data API with this name already exists in the folder"}, err
	}
	taken, err = dataapis.SlugTaken(ctx, q, slug, except)
	if err != nil || taken {
		return &dataAPIRefusal{http.StatusConflict, fmt.Sprintf("Another data API already answers at /data/%s/", slug)}, err
	}
	return nil, nil
}

func answerRefusal(d *Deps, w http.ResponseWriter, what string, ref *dataAPIRefusal, err error) bool {
	if err != nil {
		fail(d, w, what, err)
		return true
	}
	if ref != nil {
		detail(w, ref.status, ref.text)
		return true
	}
	return false
}

func decodeDataAPIBody(w http.ResponseWriter, r *http.Request, v any) bool {
	dec := json.NewDecoder(http.MaxBytesReader(nil, r.Body, 1<<20))
	if err := dec.Decode(v); err != nil {
		detail(w, http.StatusUnprocessableEntity, "The request is not valid JSON, or not of the expected shape")
		return false
	}
	return true
}

func auditDataAPI(ctx context.Context, d *Deps, actor *auth.User, action string, a *dataapis.API, more pyjson.Object) {
	target := "data_api"
	label := a.Name
	dt := append(pyjson.Object{{Key: "project_id", Value: a.ProjectID}, {Key: "slug", Value: a.Slug}}, more...)
	audit.Record(ctx, d.DB, d.Log, audit.Event{Actor: actor, Action: action, TargetType: &target, TargetID: &a.ID, TargetLabel: &label,
		Detail: dt})
}

func inTx(ctx context.Context, d *Deps, fn func(pgx.Tx) error) error {
	tx, err := d.DB.Begin(ctx)
	if err != nil {
		return err
	}
	defer tx.Rollback(ctx) //nolint:errcheck
	if err := fn(tx); err != nil {
		return err
	}
	return tx.Commit(ctx)
}

// ── routes ────────────────────────────────────────────────────────────────────

// GET /data-apis/limits: whether the data APIs are offered here, and the caps the page
// checks before the gateway does.
func dataAPILimits(d *Deps, w http.ResponseWriter, r *http.Request, p *auth.Principal) {
	c := d.Config.DataAPI
	off := dataAPIsOff(d)
	out := map[string]any{"enabled": off == "", "reason": off}
	if off == "" {
		n, err := dataapis.Count(r.Context(), d.DB)
		if err != nil {
			fail(d, w, "data apis: counting", err)
			return
		}
		out["count"] = n
		out["max_apis"] = c.MaxAPIs
		out["max_total_replicas"] = c.MaxTotalReplicas
		out["runtime_limits"] = runtimeLimits(c)
		out["key_max_days"] = c.KeyMaxDays
		out["key_default_rate"] = c.KeyDefaultRate
		out["key_max_rate"] = dataapis.MaxRate
		out["max_grace_days"] = dataapis.MaxGraceDays
		out["default_page"] = dataapis.DefaultPage
		out["max_page"] = dataapis.MaxPage
		out["operators"] = dataapis.Operators
		out["default_runtime"] = dataapis.DefaultRuntime()
		// where the other systems call: the public address (dataApi.publicHost) or the host the
		// data APIs are published on; both empty: the interface's own
		out["base_url"] = c.Pods.PublicBaseURL
		out["host"] = dataapis.IngressHost(c.Pods)
	}
	server.JSON(w, http.StatusOK, out)
}

// GET /data-apis: every data API this person sees.
func listDataAPIs(d *Deps, w http.ResponseWriter, r *http.Request, p *auth.Principal) {
	sendDataAPIList(d, w, r, p, nil)
}

// GET /projects/{project_id}/data-apis: those of a folder.
func listProjectDataAPIs(d *Deps, w http.ResponseWriter, r *http.Request, p *auth.Principal) {
	ids, ok := pathInts(w, r, "project_id")
	if !ok {
		return
	}
	sendDataAPIList(d, w, r, p, ids)
}

// GET /data-apis/{api_id}
func getDataAPI(d *Deps, w http.ResponseWriter, r *http.Request, p *auth.Principal) {
	a, _, ok := dataAPIVisible(d, w, r, p)
	if !ok {
		return
	}
	sendDataAPIDetail(d, w, r, p, a.ID, http.StatusOK)
}

type dataAPICreateIn struct {
	Name        string              `json:"name"`
	Slug        string              `json:"slug"`
	Description string              `json:"description"`
	Resources   []dataapis.Resource `json:"resources"`
	Runtime     *dataapis.Runtime   `json:"runtime"`
}

// POST /projects/{project_id}/data-apis: a new data API, a draft.
func createDataAPI(d *Deps, w http.ResponseWriter, r *http.Request, p *auth.Principal) {
	if !dataAPINeedOn(d, w) {
		return
	}
	ids, ok := pathInts(w, r, "project_id")
	if !ok {
		return
	}
	ctx := r.Context()
	var in dataAPICreateIn
	if !decodeDataAPIBody(w, r, &in) {
		return
	}
	pr, err := readProject(ctx, d.DB, ids[0])
	if err != nil {
		fail(d, w, "data apis: the folder", err)
		return
	}
	if pr == nil {
		detail(w, http.StatusNotFound, "Folder not found")
		return
	}
	c := perm.For(d.DB, &p.User)
	if !dataAPIEnsureManage(d, w, ctx, c, pr.ID) {
		return
	}
	n, err := dataapis.Count(ctx, d.DB)
	if err != nil {
		fail(d, w, "data apis: counting", err)
		return
	}
	if n >= d.Config.DataAPI.MaxAPIs {
		detail(w, http.StatusConflict, fmt.Sprintf("The installation has its most data APIs (%d): delete one first", d.Config.DataAPI.MaxAPIs))
		return
	}
	name, ref := checkDataAPIName(in.Name)
	if ref == nil {
		in.Description, ref = checkDataAPIDescription(in.Description)
	}
	slug := strings.TrimSpace(in.Slug)
	if ref == nil {
		if problem := dataapis.CheckSlug(slug); problem != "" {
			ref = &dataAPIRefusal{http.StatusUnprocessableEntity, problem}
		}
	}
	if answerRefusal(d, w, "", ref, nil) {
		return
	}
	def := dataapis.Definition{Resources: in.Resources, Runtime: dataapis.DefaultRuntime()}
	if in.Runtime != nil && !dataapis.SameRuntime(*in.Runtime, def.Runtime) {
		runtimeAdminOnly(w)
		return
	}
	def, ref, err = checkDataAPIDefinition(ctx, d, c, def, nil)
	if answerRefusal(d, w, "data apis: the definition", ref, err) {
		return
	}
	ref, err = uniqueDataAPI(ctx, d.DB, pr.ID, name, slug, 0)
	if answerRefusal(d, w, "data apis: the names", ref, err) {
		return
	}
	var a *dataapis.API
	err = inTx(ctx, d, func(tx pgx.Tx) error {
		a, err = dataapis.Insert(ctx, tx, name, slug, in.Description, pr.ID, p.User.ID, def, "created")
		return err
	})
	if uniqueViolation(err) {
		detail(w, http.StatusConflict, "A data API with this name or address already exists")
		return
	}
	if err != nil {
		fail(d, w, "data apis: making it", err)
		return
	}
	auditDataAPI(ctx, d, &p.User, "data_api.create", a, pyjson.Object{{Key: "resources", Value: int64(len(def.Resources))}})
	sendDataAPIDetail(d, w, r, p, a.ID, http.StatusCreated)
}

type dataAPIUpdateIn struct {
	Name        *string              `json:"name"`
	Slug        *string              `json:"slug"`
	Description *string              `json:"description"`
	ProjectID   *int64               `json:"project_id"`
	Resources   *[]dataapis.Resource `json:"resources"`
	Runtime     *dataapis.Runtime    `json:"runtime"`
}

// PATCH /data-apis/{api_id}: its name, address, description, folder, resources. A change of
// what it publishes is a new version. The runtime may come back as it is (the page sends the
// whole document); a different one is refused (runtimeAdminOnly).
func updateDataAPI(d *Deps, w http.ResponseWriter, r *http.Request, p *auth.Principal) {
	a, c, ok := dataAPIManaged(d, w, r, p)
	if !ok {
		return
	}
	var in dataAPIUpdateIn
	if !decodeDataAPIBody(w, r, &in) {
		return
	}
	saveDataAPI(d, w, r, p, a, c, in, "edited", 0)
}

// saveDataAPI is an edit (or a version restored, `restoredFrom` > 0): checked, written in one
// transaction, recorded.
func saveDataAPI(d *Deps, w http.ResponseWriter, r *http.Request, p *auth.Principal, a *dataapis.API, c *perm.Checker,
	in dataAPIUpdateIn, note string, restoredFrom int64) {
	ctx := r.Context()
	changed := []any{}
	name, slug, description, project := a.Name, a.Slug, a.Description, a.ProjectID
	var ref *dataAPIRefusal
	if in.Name != nil {
		if name, ref = checkDataAPIName(*in.Name); answerRefusal(d, w, "", ref, nil) {
			return
		}
	}
	if in.Description != nil {
		if description, ref = checkDataAPIDescription(*in.Description); answerRefusal(d, w, "", ref, nil) {
			return
		}
	}
	if in.Slug != nil {
		slug = strings.TrimSpace(*in.Slug)
		if problem := dataapis.CheckSlug(slug); problem != "" {
			detail(w, http.StatusUnprocessableEntity, problem)
			return
		}
		if slug != a.Slug && a.State == dataapis.Active {
			detail(w, http.StatusConflict, "The address of an active data API does not change: pause it first")
			return
		}
	}
	if in.ProjectID != nil && *in.ProjectID != a.ProjectID {
		target, err := readProject(ctx, d.DB, *in.ProjectID)
		if err != nil {
			fail(d, w, "data apis: the folder", err)
			return
		}
		if target == nil {
			detail(w, http.StatusNotFound, "Folder not found")
			return
		}
		if !dataAPIEnsureManage(d, w, ctx, c, target.ID) {
			return
		}
		project = target.ID
	}
	current, err := dataapis.CurrentDefinition(ctx, d.DB, a)
	if err != nil {
		fail(d, w, "data apis: the definition", err)
		return
	}
	if in.Runtime != nil && !dataapis.SameRuntime(*in.Runtime, current.Runtime) {
		runtimeAdminOnly(w)
		return
	}
	def := current
	newDefinition := false
	if in.Resources != nil {
		def.Resources = *in.Resources
		def, ref, err = checkDataAPIDefinition(ctx, d, c, def, current.Resources)
		if answerRefusal(d, w, "data apis: the definition", ref, err) {
			return
		}
		before, _ := json.Marshal(current)
		after, _ := json.Marshal(def)
		newDefinition = string(before) != string(after) || restoredFrom > 0
	}
	ref, err = uniqueDataAPI(ctx, d.DB, project, name, slug, a.ID)
	if answerRefusal(d, w, "data apis: the names", ref, err) {
		return
	}
	for _, f := range []struct {
		key     string
		changed bool
	}{{"name", name != a.Name}, {"slug", slug != a.Slug}, {"description", description != a.Description},
		{"project_id", project != a.ProjectID}, {"definition", newDefinition}} {
		if f.changed {
			changed = append(changed, f.key)
		}
	}
	err = inTx(ctx, d, func(tx pgx.Tx) error {
		if newDefinition {
			if err := dataapis.SaveDefinition(ctx, tx, a, def, note, p.User.ID); err != nil {
				return err
			}
		}
		_, err := tx.Exec(ctx, `UPDATE data_apis SET name = $2, slug = $3, description = $4, project_id = $5, updated_at = $6
			WHERE id = $1`, a.ID, name, slug, description, project, dataapis.Now())
		return err
	})
	if uniqueViolation(err) {
		detail(w, http.StatusConflict, "A data API with this name or address already exists")
		return
	}
	if answerRefusal(d, w, "data apis: saving", ref, err) {
		return
	}
	if len(changed) > 0 {
		a.Name, a.Slug, a.ProjectID = name, slug, project
		more := pyjson.Object{{Key: "changed", Value: changed}}
		if newDefinition {
			more = append(more, pyjson.KV{Key: "version", Value: a.CurrentVersion})
		}
		if restoredFrom > 0 {
			more = append(more, pyjson.KV{Key: "restored_from", Value: restoredFrom})
		}
		auditDataAPI(ctx, d, &p.User, "data_api.update", a, more)
	}
	sendDataAPIDetail(d, w, r, p, a.ID, http.StatusOK)
}

// DELETE /data-apis/{api_id}: with its versions, resources and keys; the reconcile loop removes
// its objects from the cluster within its round.
func deleteDataAPI(d *Deps, w http.ResponseWriter, r *http.Request, p *auth.Principal) {
	a, _, ok := dataAPIManaged(d, w, r, p)
	if !ok {
		return
	}
	ctx := r.Context()
	if err := dataapis.Delete(ctx, d.DB, a.ID); err != nil {
		fail(d, w, "data apis: deleting it", err)
		return
	}
	auditDataAPI(ctx, d, &p.User, "data_api.delete", a, pyjson.Object{{Key: "state", Value: a.State}})
	returnsNone(w)
}

// copySlug is the first free address «slug-copy», «slug-copy-2» …, within 63 characters.
func copySlug(ctx context.Context, q dataapis.Querier, slug string) (string, error) {
	for i := 1; i < 1000; i++ {
		suffix := "-copy"
		if i > 1 {
			suffix += "-" + strconv.Itoa(i)
		}
		base := slug
		if len(base)+len(suffix) > 63 {
			base = strings.TrimRight(base[:63-len(suffix)], "-")
		}
		candidate := base + suffix
		taken, err := dataapis.SlugTaken(ctx, q, candidate, 0)
		if err != nil {
			return "", err
		}
		if !taken {
			return candidate, nil
		}
	}
	return "", errors.New("no free address for the copy")
}

// POST /data-apis/{api_id}/duplicate: a copy in its folder, a draft, with no keys and the
// default runtime, whose creator is whoever copies it (who must read every datasource it
// publishes).
func duplicateDataAPI(d *Deps, w http.ResponseWriter, r *http.Request, p *auth.Principal) {
	src, c, ok := dataAPIManaged(d, w, r, p)
	if !ok {
		return
	}
	ctx := r.Context()
	n, err := dataapis.Count(ctx, d.DB)
	if err != nil {
		fail(d, w, "data apis: counting", err)
		return
	}
	if n >= d.Config.DataAPI.MaxAPIs {
		detail(w, http.StatusConflict, fmt.Sprintf("The installation has its most data APIs (%d): delete one first", d.Config.DataAPI.MaxAPIs))
		return
	}
	def, err := dataapis.CurrentDefinition(ctx, d.DB, src)
	if err != nil {
		fail(d, w, "data apis: the definition", err)
		return
	}
	def.Runtime = dataapis.DefaultRuntime()
	def, ref, err := checkDataAPIDefinition(ctx, d, c, def, nil)
	if answerRefusal(d, w, "data apis: the definition", ref, err) {
		return
	}
	var a *dataapis.API
	err = inTx(ctx, d, func(tx pgx.Tx) error {
		name, err := copynames.Free(ctx, tx, "data_apis", src.ProjectID, src.Name)
		if err != nil {
			return err
		}
		slug, err := copySlug(ctx, tx, src.Slug)
		if err != nil {
			return err
		}
		a, err = dataapis.Insert(ctx, tx, name, slug, src.Description, src.ProjectID, p.User.ID, def, "created")
		return err
	})
	if uniqueViolation(err) || copynames.Clash(err) {
		detail(w, http.StatusConflict, copynames.ClashDetail)
		return
	}
	if err != nil {
		fail(d, w, "data apis: the copy", err)
		return
	}
	auditDataAPI(ctx, d, &p.User, "data_api.create", a, pyjson.Object{{Key: "duplicate_of", Value: src.ID}})
	sendDataAPIDetail(d, w, r, p, a.ID, http.StatusCreated)
}

// POST /data-apis/{api_id}/activate: served, within the most pods allowed, on the authority of
// its creator, who must still have it. The reconcile loop starts its pods within its round.
func activateDataAPI(d *Deps, w http.ResponseWriter, r *http.Request, p *auth.Principal) {
	a, _, ok := dataAPIManaged(d, w, r, p)
	if !ok {
		return
	}
	ctx := r.Context()
	if a.State == dataapis.Active {
		sendDataAPIDetail(d, w, r, p, a.ID, http.StatusOK)
		return
	}
	def, err := dataapis.CurrentDefinition(ctx, d.DB, a)
	if err != nil {
		fail(d, w, "data apis: the definition", err)
		return
	}
	if len(def.Resources) == 0 {
		detail(w, http.StatusConflict, "A data API needs at least one resource to be activated")
		return
	}
	if _, err := dataapis.CheckRuntime(def.Runtime, d.Config.DataAPI.MaxTotalReplicas); err != nil {
		detail(w, http.StatusConflict, dataapis.Sentence("the runtime is not valid ("+err.Error()+
			"): an administrator sets it again, in Administration → Data APIs"))
		return
	}
	ids := []int64{}
	for _, res := range def.Resources {
		ids = append(ids, res.Source.ID)
	}
	sources, err := dataapis.Datasources(ctx, d.DB, ids)
	if err != nil {
		fail(d, w, "data apis: the datasources", err)
		return
	}
	why, err := dataAPISuspension(ctx, d, a, def, sources)
	if err != nil {
		fail(d, w, "data apis: the creator's rights", err)
		return
	}
	if why != "" {
		detail(w, http.StatusConflict, map[string]string{
			"creator_inactive":  "The data API's creator no longer has an active account: duplicate it, and activate the copy",
			"creator_rights":    "The data API's creator no longer has MANAGE on its folder: duplicate it, and activate the copy",
			"datasource_rights": "The data API's creator may no longer read every datasource it publishes",
		}[why])
		return
	}
	var ref *dataAPIRefusal
	err = inTx(ctx, d, func(tx pgx.Tx) error {
		if _, err := tx.Exec(ctx, `SELECT pg_advisory_xact_lock(7140)`); err != nil { // two activations at once count each other
			return err
		}
		// the runtime as it is now: an administrator may have changed it since it was read
		now, err := dataapis.GetForUpdate(ctx, tx, a.ID)
		if err != nil {
			return err
		}
		if now != nil {
			def.Runtime = now.Runtime
		}
		if ref, err = checkDataAPIPods(ctx, d, tx, a.ID, def.Runtime); err != nil || ref != nil {
			return err
		}
		// a new pod token at every activation: the reconcile loop makes it at its next round,
		// and the previous one stops now
		if err := dataapis.ForgetPodToken(ctx, tx, a.ID); err != nil {
			return err
		}
		return dataapis.SetState(ctx, tx, a.ID, dataapis.Active)
	})
	if answerRefusal(d, w, "data apis: activating", ref, err) {
		return
	}
	auditDataAPI(ctx, d, &p.User, "data_api.activate", a, pyjson.Object{{Key: "from", Value: a.State},
		{Key: "pods", Value: int64(def.Runtime.Pods())}})
	sendDataAPIDetail(d, w, r, p, a.ID, http.StatusOK)
}

// POST /data-apis/{api_id}/pause: no longer served (the reconcile loop takes its pods to zero
// within its round, and the pods stop answering at their next manifest); its keys and
// definition stay.
func pauseDataAPI(d *Deps, w http.ResponseWriter, r *http.Request, p *auth.Principal) {
	a, _, ok := dataAPIManaged(d, w, r, p)
	if !ok {
		return
	}
	ctx := r.Context()
	if a.State != dataapis.Active {
		detail(w, http.StatusConflict, "Only an active data API can be paused")
		return
	}
	if err := dataapis.SetState(ctx, d.DB, a.ID, dataapis.Paused); err != nil {
		fail(d, w, "data apis: pausing", err)
		return
	}
	auditDataAPI(ctx, d, &p.User, "data_api.pause", a, nil)
	sendDataAPIDetail(d, w, r, p, a.ID, http.StatusOK)
}

type dataAPIVersionOut struct {
	Version   int64   `json:"version"`
	Note      string  `json:"note"`
	CreatedAt *string `json:"created_at"`
	CreatedBy *string `json:"created_by"`
	Current   bool    `json:"current"`
}

// GET /data-apis/{api_id}/versions
func listDataAPIVersions(d *Deps, w http.ResponseWriter, r *http.Request, p *auth.Principal) {
	a, _, ok := dataAPIVisible(d, w, r, p)
	if !ok {
		return
	}
	list, err := dataapis.Versions(r.Context(), d.DB, a.ID)
	if err != nil {
		fail(d, w, "data apis: the versions", err)
		return
	}
	out := make([]dataAPIVersionOut, 0, len(list))
	for _, v := range list {
		out = append(out, dataAPIVersionOut{Version: v.Version, Note: v.Note, CreatedAt: isoPtr(&v.CreatedAt), CreatedBy: v.CreatedBy,
			Current: v.Version == a.CurrentVersion})
	}
	server.JSON(w, http.StatusOK, out)
}

func dataAPIVersionOf(d *Deps, w http.ResponseWriter, r *http.Request, a *dataapis.API) (dataapis.Definition, int64, bool) {
	v, err := strconv.ParseInt(r.PathValue("version"), 10, 32)
	if err != nil || v < 1 {
		detail(w, http.StatusNotFound, "Version not found")
		return dataapis.Definition{}, 0, false
	}
	def, found, err := dataapis.VersionContent(r.Context(), d.DB, a.ID, v)
	if err != nil {
		fail(d, w, "data apis: the version", err)
		return def, 0, false
	}
	if !found {
		detail(w, http.StatusNotFound, "Version not found")
		return def, 0, false
	}
	return def, v, true
}

// GET /data-apis/{api_id}/versions/{version}: what that version publishes.
func getDataAPIVersion(d *Deps, w http.ResponseWriter, r *http.Request, p *auth.Principal) {
	a, _, ok := dataAPIVisible(d, w, r, p)
	if !ok {
		return
	}
	def, _, ok := dataAPIVersionOf(d, w, r, a)
	if !ok {
		return
	}
	server.JSON(w, http.StatusOK, def)
}

// POST /data-apis/{api_id}/versions/{version}/restore: that version's resources become the
// newest, checked as an edit (its columns must still be in the datasources); the runtime stays
// the administrator's.
func restoreDataAPIVersion(d *Deps, w http.ResponseWriter, r *http.Request, p *auth.Principal) {
	a, c, ok := dataAPIManaged(d, w, r, p)
	if !ok {
		return
	}
	def, v, ok := dataAPIVersionOf(d, w, r, a)
	if !ok {
		return
	}
	if v == a.CurrentVersion {
		sendDataAPIDetail(d, w, r, p, a.ID, http.StatusOK)
		return
	}
	saveDataAPI(d, w, r, p, a, c, dataAPIUpdateIn{Resources: &def.Resources},
		"restored from v"+strconv.FormatInt(v, 10), v)
}

// refuseDataAPIPublished is the 409 of deleting a datasource that data APIs publish: they are
// named, and it stays until it is taken out of them.
func refuseDataAPIPublished(ctx context.Context, d *Deps, w http.ResponseWriter, datasourceID int64) bool {
	names, err := dataapis.PublishersOf(ctx, d.DB, datasourceID, 6)
	if err != nil {
		fail(d, w, "data apis: who publishes the datasource", err)
		return true
	}
	if len(names) == 0 {
		return false
	}
	shown := names
	if len(shown) > 5 {
		shown = shown[:5]
	}
	quoted := make([]string, len(shown))
	for i, n := range shown {
		quoted[i] = "'" + n + "'"
	}
	list := strings.Join(quoted, ", ")
	if len(names) > 5 {
		list += ", …"
	}
	detail(w, http.StatusConflict, fmt.Sprintf("The datasource is published by data APIs (%s): take it out of them first", list))
	return true
}
