package api

// routes/notebook_data.py render_report: a report rendered from a notebook's pod, with the
// rights the notebook carries, waited for and sent back as the file (as GET /reports/{id}/render).

import (
	"errors"
	"fmt"
	"net/http"
	"net/url"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/auth"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/perm"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyd"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyd/gatewaymodels"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pystr"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/reports"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/server"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/versioned"
)

func init() {
	register("notebook_data_report", func(d *Deps) []server.Route {
		return []server.Route{{Method: http.MethodPost, Pattern: "/notebook-data/report", Handler: ndCaller(d, true, ndReport)}}
	})
}

// ndReport is POST /notebook-data/report (render_report).
func ndReport(d *Deps, w http.ResponseWriter, r *http.Request, call *ndCall, body pyd.Body) {
	var ps pyd.Params
	in := ps.Body(body, gatewaymodels.ReportIn)
	if !validated(w, &ps) {
		return
	}
	ctx := r.Context()
	c := perm.For(d.DB, call.user)
	h := &objectsAPI{d: d, kind: reports.Kind, pushmail: reports.Pushmail, param: "report_id"}
	visible, err := reports.Kind.Visible(ctx, d.DB, c, nil)
	if err != nil {
		fail(d, w, "notebook data: the reports", err)
		return
	}
	var items []ndItem
	byID := map[int64]*versioned.Item{}
	for _, it := range visible {
		items = append(items, ndItem{ID: it.ID, Name: it.Name, ProjectID: it.ProjectID})
		byID[it.ID] = it
	}
	picked, refusal, err := ndPick(d, r, items, in.Get("report"), "report")
	if err != nil {
		fail(d, w, "notebook data: the report", err)
		return
	}
	if refusal != nil {
		scRefuseWith(w, refusal.status, refusal.why)
		return
	}
	it := byID[picked.ID]
	content, err := reports.Kind.ContentOf(ctx, d.DB, it, nil)
	if err != nil {
		fail(d, w, "notebook data: the report's content", err)
		return
	}
	if reports.HasComment(content) { // the comment is written first, by the AI service
		toAIService(d, w, r, rawOf(r))
		return
	}
	params, _ := scFromPyd(in.Get("params")).(pyjson.Object)
	given, err := reports.GivenParams(params)
	if err != nil {
		h.writeErr(w, r, nil, err)
		return
	}
	format := in.Get("format").(string)
	p := &auth.Principal{User: *call.user}
	extra := pyjson.Object{{Key: "notebook", Value: call.notebookID}, {Key: "notebook_name", Value: call.notebook}, {Key: "mode", Value: call.sessionMode}}
	withExtra := func(detail pyjson.Object) pyjson.Object {
		out := pyjson.Object{}
		for _, kv := range detail {
			out = append(out, kv)
			if kv.Key == "from" {
				out = append(out, extra...)
			}
		}
		return out
	}
	def, ok := content.(pyjson.Object)
	if !ok {
		h.auditRender(ctx, p, it, "failure", withExtra(pyjson.Object{{Key: "format", Value: format}, {Key: "from", Value: "notebook"},
			{Key: "parameters", Value: reports.GivenDetail(given)}}))
		fail(d, w, "notebook data: the report's definition", versioned.ErrNotAnObject)
		return
	}
	owner := fmt.Sprintf("user:%d", call.user.ID)
	name := it.Name
	started, detailOut, err := h.resolver(c).Render(ctx, def, reports.RenderRequest{Name: &name, Format: format, Given: given,
		Origin: "notebook", Owner: owner, Wait: d.Config.ReportsAPIWaitSeconds})
	if err != nil {
		h.auditRender(ctx, p, it, "failure", withExtra(detailOut))
		h.writeErr(w, r, rawOf(r), err)
		return
	}
	task := pystr.Str(started.TaskID)
	if err := h.waitForFile(ctx, task, owner); err != nil {
		failed := withExtra(started.Detail)
		var ref *reports.Refusal
		if errors.As(err, &ref) {
			failed = failed.Set("error", pystr.Cut(pystr.Str(ref.Detail), 500))
		}
		h.auditRender(ctx, p, it, "failure", failed)
		h.writeErr(w, r, nil, err)
		return
	}
	h.auditRender(ctx, p, it, "success", withExtra(started.Detail))
	h.forwardFile(w, r, "/tasks/exports/"+task+"/file?owner="+url.QueryEscape(owner))
}
