package api

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"net/url"
	"time"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/auth"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/dashboards"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/perm"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyd"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyd/gatewaymodels"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pystr"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/reports"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/server"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/versioned"
)

// asked is _asked: what a request for numbers asks, its values given checked (400).
func asked(b *pyd.ModelValue) (*dashboards.Asked, error) {
	given, err := reports.GivenParams(toPython(b.Get("params")).(pyjson.Object))
	if err != nil {
		return nil, err
	}
	a := &dashboards.Asked{Given: given, Refresh: b.Get("refresh") == true}
	if t := b.Get("tiles"); t != nil {
		a.Tiles, a.HasTiles = toPython(t).([]any), true
	}
	a.Pages, _ = toPython(b.Get("page")).(pyjson.Object)
	a.State, a.HasState = stateOf(b.Get("state"))
	a.Clicks, _ = toPython(b.Get("clicks")).([]any)
	return a, nil
}

func (h *objectsAPI) answerTiles(w http.ResponseWriter, r *http.Request, raw []byte, answer pyjson.Object, err error) {
	if err == nil {
		answer, err = dashboards.TilesOut(answer)
	}
	if err != nil {
		h.writeErr(w, r, raw, err)
		return
	}
	server.JSON(w, http.StatusOK, answer)
}

// data is POST /dashboards/{dashboard_id}/data (dashboard_data): the current version's numbers,
// tile by tile, read with the caller's rights.
func (h *objectsAPI) data(w http.ResponseWriter, r *http.Request, p *auth.Principal, body pyd.Body, raw []byte) {
	var params pyd.Params
	did := params.Path(h.param, r.PathValue(h.param), pyd.Int())
	b := params.Body(body, gatewaymodels.DataRequest)
	if resp := params.Response(); resp != nil {
		resp.Write(w)
		return
	}
	ctx, c := r.Context(), perm.For(h.d.DB, &p.User)
	it := h.visible(w, r, c, idOf(did))
	if it == nil {
		return
	}
	content, err := h.kind.ContentOf(ctx, h.d.DB, it, nil)
	if err != nil {
		h.fail(w, r, err)
		return
	}
	def, ok := content.(pyjson.Object)
	if !ok {
		h.fail(w, r, versioned.ErrNotAnObject)
		return
	}
	a, err := asked(b)
	if err != nil {
		h.writeErr(w, r, raw, err)
		return
	}
	answer, err := dashboards.Numbers(ctx, h.resolver(c), p.User.ID, def, a, it.Name, it.ID, "")
	h.answerTiles(w, r, raw, answer, err)
}

// previewNumbers is POST /dashboards/preview (preview_dashboard): the numbers of a definition
// not saved yet, checked as a save is.
func (h *objectsAPI) previewNumbers(w http.ResponseWriter, r *http.Request, p *auth.Principal, body pyd.Body, raw []byte) {
	var params pyd.Params
	b := params.Body(body, gatewaymodels.DashboardsPreviewRequest)
	if resp := params.Response(); resp != nil {
		resp.Write(w)
		return
	}
	ctx, c := r.Context(), perm.For(h.d.DB, &p.User)
	it, ok := h.beingEdited(w, r, c, b.Get("dashboard_id"), b.Get("project_id"))
	if !ok {
		return
	}
	definition := toPython(b.Get("definition"))
	var current any
	if it != nil {
		var err error
		if current, err = h.kind.ContentOf(ctx, h.d.DB, it, nil); err != nil {
			h.fail(w, r, err)
			return
		}
	}
	def, err := dashboardChecked(ctx, h, c, definition, current)
	if err != nil {
		h.writeErr(w, r, raw, err)
		return
	}
	where, title := "new", ""
	if it != nil {
		where, title = fmt.Sprint(it.ID), it.Name
	}
	editing := "preview-" + where
	if tab, _ := b.Get("tab").(string); tab != "" {
		editing += "-" + tab
	}
	a, err := asked(b)
	if err != nil {
		h.writeErr(w, r, raw, err)
		return
	}
	answer, err := dashboards.Numbers(ctx, h.resolver(c), p.User.ID, def, a, title, 0, editing)
	h.answerTiles(w, r, raw, answer, err)
}

// dashboardRender is dashboards.render: a document of the current version, read with the
// caller's rights; audited with what it showed.
func (h *objectsAPI) dashboardRender(ctx context.Context, p *auth.Principal, c *perm.Checker, it *versioned.Item, def pyjson.Object,
	format string, given reports.Given, clicks []any, state pyjson.Object, hasState bool, origin string, auditSuccess bool) (*reports.Started, error) {
	a := &dashboards.Asked{Given: given, Clicks: clicks, State: state, HasState: hasState}
	if a.Clicks == nil {
		a.Clicks = []any{}
	}
	detail := pyjson.Object{{Key: "format", Value: format}, {Key: "from", Value: origin}, {Key: "parameters", Value: reports.GivenDetail(given)}}
	owner := fmt.Sprintf("user:%d", p.User.ID)
	rs := h.resolver(c)
	started, err := func() (*reports.Started, error) {
		if err := dashboards.CheckAsked(def, a); err != nil {
			return nil, err
		}
		if len(a.Clicks) > 0 {
			var shown []any
			for _, cv := range a.Clicks {
				co := append(pyjson.Object{}, cv.(pyjson.Object)...)
				v, _ := co.Get("values")
				shown = append(shown, co.Set("values", reports.Shown(v)))
			}
			detail = detail.Set("clicks", shown)
		}
		cache := dashboards.CacheOf(ctx, h.d.Engine, def, it.ID, false)
		plan, err := dashboards.PlanOf(ctx, rs, def, a, cache)
		if err != nil {
			return nil, err
		}
		shownParams := pyjson.Object{}
		for _, kv := range plan.Params {
			shownParams = append(shownParams, pyjson.KV{Key: kv.Key, Value: reports.Shown(kv.Value)})
		}
		detail = detail.Set("parameters", shownParams)
		name := reports.Filename(it.Name, format)
		wait := h.exportWait(p, origin)
		var stateV any
		if hasState {
			stateV = state
		}
		body := append(dashboards.LaidOut(def), pyjson.KV{Key: "datasets", Value: plan.Datasets}, pyjson.KV{Key: "refused", Value: plan.Refused},
			pyjson.KV{Key: "params", Value: plan.Params}, pyjson.KV{Key: "clicks", Value: a.Clicks}, pyjson.KV{Key: "state", Value: stateV},
			pyjson.KV{Key: "format", Value: format}, pyjson.KV{Key: "filename", Value: name}, pyjson.KV{Key: "owner", Value: owner},
			pyjson.KV{Key: "wait_seconds", Value: int64(wait)}, pyjson.KV{Key: "locale", Value: localeOf(def)},
			pyjson.KV{Key: "title", Value: pystr.Cut(it.Name, 300)}, pyjson.KV{Key: "cache", Value: cache})
		cctx, cancel := context.WithTimeout(ctx, time.Duration(60+wait)*time.Second)
		defer cancel()
		res, raw, err := h.d.Engine.JSON(cctx, http.MethodPost, "/reports/dashboard-render", body)
		if err != nil {
			return nil, &reports.EngineDown{Err: err}
		}
		if res.StatusCode == 404 || res.StatusCode == 405 {
			return nil, &reports.Refusal{Status: 503, Detail: "The engine cannot make documents of a dashboard yet: update it to the gateway's version"}
		}
		if res.StatusCode >= 400 {
			return nil, reports.EngineFailure(res.StatusCode, raw, pystr.Sentence)
		}
		v, err := pyjson.Loads(string(raw))
		answer, ok := v.(pyjson.Object)
		if err != nil || !ok {
			return nil, reports.ErrInternal
		}
		task, has := answer.Get("task_id")
		if !has {
			return nil, reports.ErrInternal
		}
		file, _ := answer.Get("filename")
		if !pystr.Truthy(file) {
			file = name
		}
		return &reports.Started{TaskID: task, Filename: file}, nil
	}()
	if err != nil {
		var ref *reports.Refusal
		var u *reports.Unreadable
		var down *reports.EngineDown
		switch {
		case errors.As(err, &ref), errors.As(err, &u):
			detail = detail.Set("error", pystr.Cut(dashboards.InOurWords(reports.Text(err)), 500))
		case errors.As(err, &down):
			detail = detail.Set("error", "The engine is not answering")
		}
		if !errors.Is(err, reports.ErrNeedsPython) {
			h.auditRender(ctx, p, it, "failure", detail)
		}
		return nil, err
	}
	started.Detail = detail
	if auditSuccess {
		h.auditRender(ctx, p, it, "success", detail)
	}
	return started, nil
}

func localeOf(def pyjson.Object) any {
	v, _ := def.Get("locale")
	if pystr.Truthy(v) {
		return v
	}
	return "en"
}

// renderDashboard is POST /dashboards/{dashboard_id}/render (render_dashboard).
func (h *objectsAPI) renderDashboard(w http.ResponseWriter, r *http.Request, p *auth.Principal, body pyd.Body, raw []byte) {
	var params pyd.Params
	did := params.Path(h.param, r.PathValue(h.param), pyd.Int())
	b := params.Body(body, gatewaymodels.DashboardsRenderRequest)
	if resp := params.Response(); resp != nil {
		resp.Write(w)
		return
	}
	ctx, c := r.Context(), perm.For(h.d.DB, &p.User)
	it := h.visible(w, r, c, idOf(did))
	if it == nil {
		return
	}
	given, err := reports.GivenParams(toPython(b.Get("params")).(pyjson.Object))
	if err != nil {
		h.writeErr(w, r, raw, err)
		return
	}
	content, err := h.kind.ContentOf(ctx, h.d.DB, it, nil)
	if err != nil {
		h.fail(w, r, err)
		return
	}
	def, ok := content.(pyjson.Object)
	if !ok {
		h.fail(w, r, versioned.ErrNotAnObject)
		return
	}
	if reports.HasComment(def) { // the comment is written first, by the AI service
		toAIService(h.d, w, r, raw)
		return
	}
	clicks, _ := toPython(b.Get("clicks")).([]any)
	state, hasState := stateOf(b.Get("state"))
	started, err := h.dashboardRender(ctx, p, c, it, def, b.Get("format").(string), given, clicks, state, hasState, "interface", true)
	if err != nil {
		h.writeErr(w, r, raw, err)
		return
	}
	server.JSON(w, http.StatusOK, renderStarted{started.TaskID, started.Filename})
}

// renderDashboardFile is GET /dashboards/{dashboard_id}/render (render_dashboard_file): the API,
// with the parameters and the clicks of the query string.
func (h *objectsAPI) renderDashboardFile(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
	var params pyd.Params
	query := pyd.ParseQuery(r.URL.RawQuery)
	did := params.Path(h.param, r.PathValue(h.param), pyd.Int())
	format := params.Query(query, "format", pyd.Literal("pdf", "png"), "pdf")
	if resp := params.Response(); resp != nil {
		resp.Write(w)
		return
	}
	ctx, c := r.Context(), perm.For(h.d.DB, &p.User)
	it := h.visible(w, r, c, idOf(did))
	if it == nil {
		return
	}
	given, clicks := dashboards.InTheAddress(queryParams(query))
	content, err := h.kind.ContentOf(ctx, h.d.DB, it, nil)
	if err != nil {
		h.fail(w, r, err)
		return
	}
	def, ok := content.(pyjson.Object)
	if !ok {
		h.fail(w, r, versioned.ErrNotAnObject)
		return
	}
	if reports.HasComment(def) { // the comment is written first, by the AI service
		toAIService(h.d, w, r, nil)
		return
	}
	started, err := h.dashboardRender(ctx, p, c, it, def, format.(string), given, clicks, nil, false, "api", false)
	if err != nil {
		h.writeErr(w, r, nil, err)
		return
	}
	owner := fmt.Sprintf("user:%d", p.User.ID)
	task := pystr.Str(started.TaskID)
	if err := h.waitForFile(ctx, task, owner); err != nil {
		detail := append(pyjson.Object{}, started.Detail...)
		var ref *reports.Refusal
		if errors.As(err, &ref) {
			detail = detail.Set("error", pystr.Cut(pystr.Str(ref.Detail), 500))
		}
		h.auditRender(ctx, p, it, "failure", detail)
		h.writeErr(w, r, nil, dashboards.InOurWordsErr(err))
		return
	}
	h.auditRender(ctx, p, it, "success", started.Detail)
	h.forwardFile(w, r, "/tasks/exports/"+task+"/file?owner="+url.QueryEscape(owner))
}

// dashboardParameters is GET /dashboards/{dashboard_id}/parameters (dashboard_parameters).
func (h *objectsAPI) dashboardParameters(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
	ids, ok := pathInts(w, r, h.param)
	if !ok {
		return
	}
	ids = inColumn(ids)
	ctx, c := r.Context(), perm.For(h.d.DB, &p.User)
	it := h.visible(w, r, c, ids[0])
	if it == nil {
		return
	}
	content, err := h.kind.ContentOf(ctx, h.d.DB, it, nil)
	if err != nil {
		h.fail(w, r, err)
		return
	}
	def, ok := content.(pyjson.Object)
	if !ok {
		h.fail(w, r, versioned.ErrNotAnObject)
		return
	}
	shaped := dashboards.AsReport(def)
	cache := dashboards.CacheOf(ctx, h.d.Engine, def, it.ID, false)
	out, err := h.resolver(c).ParametersOutCached(ctx, shaped, queryParams(pyd.ParseQuery(r.URL.RawQuery)),
		func(string) (pyjson.Object, error) { return shaped, nil }, cache, true)
	if err != nil {
		h.writeErr(w, r, nil, dashboards.InOurWordsErr(err))
		return
	}
	server.JSON(w, http.StatusOK, out)
}

// dashboardParametersEdited is POST /dashboards/{dashboard_id}/parameters.
func (h *objectsAPI) dashboardParametersEdited(w http.ResponseWriter, r *http.Request, p *auth.Principal, body pyd.Body, raw []byte) {
	var params pyd.Params
	did := params.Path(h.param, r.PathValue(h.param), pyd.Int())
	b := params.Body(body, gatewaymodels.ParametersRequest)
	if resp := params.Response(); resp != nil {
		resp.Write(w)
		return
	}
	ctx, c := r.Context(), perm.For(h.d.DB, &p.User)
	if _, ok := h.beingEdited(w, r, c, did, nil); !ok {
		return
	}
	definition := toPython(b.Get("definition"))
	def, err := dashboards.CheckParameters(definition)
	if err != nil {
		h.writeErr(w, r, raw, problemRefusal(err, definition))
		return
	}
	given, err := reports.GivenParams(toPython(b.Get("params")).(pyjson.Object))
	if err != nil {
		h.writeErr(w, r, raw, err)
		return
	}
	out, err := h.resolver(c).ParametersOutCached(ctx, def, given, func(name string) (pyjson.Object, error) {
		return dashboards.CheckDataset(definition, name)
	}, nil, true)
	if err != nil {
		h.writeErr(w, r, raw, dashboards.InOurWordsErr(err))
		return
	}
	server.JSON(w, http.StatusOK, out)
}

// dashboardFields is POST /dashboards/{dashboard_id}/fields (dashboard_fields): EDIT.
func (h *objectsAPI) dashboardFields(w http.ResponseWriter, r *http.Request, p *auth.Principal, body pyd.Body, raw []byte) {
	var params pyd.Params
	did := params.Path(h.param, r.PathValue(h.param), pyd.Int())
	b := params.Body(body, gatewaymodels.DashboardsFieldsRequest)
	if resp := params.Response(); resp != nil {
		resp.Write(w)
		return
	}
	c := perm.For(h.d.DB, &p.User)
	it, ok := h.beingEdited(w, r, c, did, nil)
	if !ok {
		return
	}
	definition := toPython(b.Get("definition"))
	dataset := b.Get("dataset").(string)
	def, err := dashboards.CheckDataset(definition, dataset)
	if err != nil {
		h.writeErr(w, r, raw, problemRefusal(err, definition))
		return
	}
	h.fieldsOf(w, r, c, raw, def, dataset, fmt.Sprintf("u%d:dashboard:%d", p.User.ID, it.ID))
}
