package api

import (
	"context"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"regexp"
	"strings"
	"time"
	"unicode"

	"golang.org/x/text/unicode/norm"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/audit"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/auth"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/engine"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/perm"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyd"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyd/gatewaymodels"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pystr"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/reports"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/server"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/versioned"
)

func (h *objectsAPI) resolver(c *perm.Checker) *reports.Resolver {
	return &reports.Resolver{Q: h.d.DB, C: c, Engine: h.d.Engine, Log: h.d.Log, SQL: h.d.SQL}
}

// exportWait is routes/proxy.py export_wait: code (an API token, the API's routes) waits its
// turn on the export queue as long as for the document; the interface is told at once.
func (h *objectsAPI) exportWait(p *auth.Principal, origin string) int {
	if origin == "api" || origin == "notebook" || p.TokenName != "" {
		return h.d.Config.ReportsAPIWaitSeconds
	}
	return 0
}

// auditRender is _audit_render.
func (h *objectsAPI) auditRender(ctx context.Context, p *auth.Principal, it *versioned.Item, outcome string, detail pyjson.Object) {
	var id *int64
	label := ""
	if it != nil {
		id, label = &it.ID, it.Name
	}
	audit.Record(ctx, h.d.DB, h.d.Log, audit.Event{Actor: &p.User, Action: h.kind.Label + ".render", Outcome: outcome,
		TargetType: &h.kind.Label, TargetID: id, TargetLabel: &label, Detail: detail})
}

// renderStarted is RenderStarted.
type renderStarted struct {
	TaskID   any `json:"task_id"`
	Filename any `json:"filename"`
}

// writeErr answers an error of a render: a refusal, the engine not answering (main.py's 503), a 500.
func (h *objectsAPI) writeErr(w http.ResponseWriter, r *http.Request, raw []byte, err error) {
	var down *reports.EngineDown
	if errors.As(err, &down) {
		// main.py _service_down
		server.JSON(w, http.StatusServiceUnavailable, map[string]any{"detail": "The engine is not answering: try again in a moment"})
		return
	}
	h.answerErr(w, r, raw, err)
}

// startRender is reports.render: the checks before it, the render, its audit.
func (h *objectsAPI) startRender(ctx context.Context, p *auth.Principal, c *perm.Checker, it *versioned.Item, content any, format string,
	given reports.Given, state pyjson.Object, hasState bool, origin string, auditSuccess bool) (*reports.Started, error) {
	if err := reports.CheckState(state, hasState); err != nil {
		return nil, err
	}
	var name *string
	if it != nil {
		name = &it.Name
	}
	def, ok := content.(pyjson.Object)
	if !ok { // the Python code fails reading it (`definition.get`): a failure audited, then a 500
		h.auditRender(ctx, p, it, "failure", pyjson.Object{{Key: "format", Value: format}, {Key: "from", Value: origin},
			{Key: "parameters", Value: reports.GivenDetail(given)}})
		return nil, versioned.ErrNotAnObject
	}
	started, detail, err := h.resolver(c).Render(ctx, def, reports.RenderRequest{Name: name, Format: format, Given: given, State: state,
		HasState: hasState, Origin: origin, Owner: fmt.Sprintf("user:%d", p.User.ID), Wait: h.exportWait(p, origin)})
	if err != nil {
		if !errors.Is(err, reports.ErrNeedsPython) {
			h.auditRender(ctx, p, it, "failure", detail)
		}
		return nil, err
	}
	if auditSuccess {
		h.auditRender(ctx, p, it, "success", detail)
	}
	return started, nil
}

func stateOf(v any) (pyjson.Object, bool) {
	if v == nil {
		return nil, false
	}
	o, _ := toPython(v).(pyjson.Object)
	return o, true
}

// render is POST /reports/{report_id}/render (render_report): the current version rendered with
// these parameters, followed and downloaded as an export.
func (h *objectsAPI) render(w http.ResponseWriter, r *http.Request, p *auth.Principal, body pyd.Body, raw []byte) {
	var params pyd.Params
	rid := params.Path(h.param, r.PathValue(h.param), pyd.Int())
	b := params.Body(body, gatewaymodels.ReportsRenderRequest)
	if resp := params.Response(); resp != nil {
		resp.Write(w)
		return
	}
	ctx, c := r.Context(), perm.For(h.d.DB, &p.User)
	it := h.visible(w, r, c, idOf(rid))
	if it == nil {
		return
	}
	content, err := h.kind.ContentOf(ctx, h.d.DB, it, nil)
	if err != nil {
		h.fail(w, r, err)
		return
	}
	if reports.HasComment(content) { // the comment is written first, by the AI service
		toAIService(h.d, w, r, raw)
		return
	}
	given, err := reports.GivenParams(toPython(b.Get("params")).(pyjson.Object))
	if err != nil {
		h.writeErr(w, r, raw, err)
		return
	}
	state, hasState := stateOf(b.Get("state"))
	started, err := h.startRender(ctx, p, c, it, content, b.Get("format").(string), given, state, hasState, "interface", true)
	if err != nil {
		h.writeErr(w, r, raw, err)
		return
	}
	server.JSON(w, http.StatusOK, renderStarted{started.TaskID, started.Filename})
}

// preview is POST /reports/preview (preview_report): the editor's definition not saved yet,
// checked as a save is, then rendered.
func (h *objectsAPI) preview(w http.ResponseWriter, r *http.Request, p *auth.Principal, body pyd.Body, raw []byte) {
	var params pyd.Params
	b := params.Body(body, gatewaymodels.ReportsPreviewRequest)
	if resp := params.Response(); resp != nil {
		resp.Write(w)
		return
	}
	ctx, c := r.Context(), perm.For(h.d.DB, &p.User)
	it, ok := h.beingEdited(w, r, c, b.Get("report_id"), b.Get("project_id"))
	if !ok {
		return
	}
	definition := toPython(b.Get("definition"))
	if reports.HasComment(definition) { // the comment is written first, by the AI service
		toAIService(h.d, w, r, raw)
		return
	}
	checked, err := h.reviewer(c).Checked(ctx, definition, nil)
	if err != nil {
		h.writeErr(w, r, raw, err)
		return
	}
	given, err := reports.GivenParams(toPython(b.Get("params")).(pyjson.Object))
	if err != nil {
		h.writeErr(w, r, raw, err)
		return
	}
	state, hasState := stateOf(b.Get("state"))
	started, err := h.startRender(ctx, p, c, it, checked, b.Get("format").(string), given, state, hasState, "editor", true)
	if err != nil {
		h.writeErr(w, r, raw, err)
		return
	}
	server.JSON(w, http.StatusOK, renderStarted{started.TaskID, started.Filename})
}

// queryParams is _query_params: the parameters' values of a query string, `format` aside.
func queryParams(q *pyd.Query) reports.Given {
	out := reports.Given{}
	for _, kv := range q.Items() {
		if kv[0] == "format" {
			continue
		}
		prev, _ := out.Get(kv[0])
		l, _ := prev.([]any)
		out = out.Set(kv[0], append(l, kv[1]))
	}
	return out
}

// renderFile is GET /reports/{report_id}/render (render_report_file): the API, which waits for
// the file and sends it.
func (h *objectsAPI) renderFile(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
	var params pyd.Params
	query := pyd.ParseQuery(r.URL.RawQuery)
	rid := params.Path(h.param, r.PathValue(h.param), pyd.Int())
	format := params.Query(query, "format", pyd.Literal("pdf", "xlsx", "html"), "pdf")
	if resp := params.Response(); resp != nil {
		resp.Write(w)
		return
	}
	ctx, c := r.Context(), perm.For(h.d.DB, &p.User)
	it := h.visible(w, r, c, idOf(rid))
	if it == nil {
		return
	}
	content, err := h.kind.ContentOf(ctx, h.d.DB, it, nil)
	if err != nil {
		h.fail(w, r, err)
		return
	}
	if reports.HasComment(content) { // the comment is written first, by the AI service
		toAIService(h.d, w, r, nil)
		return
	}
	started, err := h.startRender(ctx, p, c, it, content, format.(string), queryParams(query), nil, false, "api", false)
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
		h.writeErr(w, r, nil, err)
		return
	}
	h.auditRender(ctx, p, it, "success", started.Detail)
	h.forwardFile(w, r, "/tasks/exports/"+task+"/file?owner="+url.QueryEscape(owner))
}

// waitForFile is _wait_for_file: until the engine has written the file; 504 past
// REPORTS__API_WAIT_SECONDS, the engine's reason when it failed. Asked often at first, then
// once a second.
func (h *objectsAPI) waitForFile(ctx context.Context, task, owner string) error {
	deadline := time.Now().Add(time.Duration(h.d.Config.ReportsAPIWaitSeconds) * time.Second)
	delay := 250 * time.Millisecond
	for {
		cctx, cancel := context.WithTimeout(ctx, engine.Check)
		res, raw, err := h.d.Engine.JSON(cctx, http.MethodGet, "/tasks/exports/"+task+"?owner="+url.QueryEscape(owner), nil)
		cancel()
		if err != nil {
			return &reports.EngineDown{Err: err}
		}
		var state pyjson.Object
		if res.StatusCode < 400 {
			v, err := pyjson.Loads(string(raw))
			o, ok := v.(pyjson.Object)
			if err != nil || !ok {
				return reports.ErrInternal
			}
			state = o
		} else {
			state = pyjson.Object{{Key: "status", Value: "failed"}, {Key: "code", Value: int64(502)},
				{Key: "detail", Value: fmt.Sprintf("The engine answered %d", res.StatusCode)}}
		}
		status, _ := state.Get("status")
		if status == "ready" {
			return nil
		}
		if status == "failed" {
			codeV, _ := state.Get("code")
			code := int64(500)
			if pystr.Truthy(codeV) {
				f, ok := pystr.Number(codeV)
				if !ok {
					return reports.ErrInternal
				}
				code = int64(f)
			}
			d, _ := state.Get("detail")
			text := "The render failed"
			if pystr.Truthy(d) {
				text = pystr.Str(d)
			}
			s := http.StatusUnprocessableEntity
			if code >= 500 {
				s = http.StatusBadGateway
			}
			return &reports.Refusal{Status: s, Detail: pystr.Sentence(text)}
		}
		if ctx.Err() != nil { // the caller stopped waiting: the render is stopped too
			h.stopTask(task)
			return &reports.Refusal{Status: 499, Detail: "The caller stopped waiting: the render was stopped"}
		}
		if time.Now().Add(delay).After(deadline) {
			h.stopTask(task)
			return &reports.Refusal{Status: http.StatusGatewayTimeout, Detail: fmt.Sprintf(
				"The report was not ready within %d s (REPORTS__API_WAIT_SECONDS)", h.d.Config.ReportsAPIWaitSeconds)}
		}
		time.Sleep(delay)
		delay = min(time.Second, delay*3/2)
	}
}

// stopTask is _stop: the hourly sweep removes what is left when it fails.
func (h *objectsAPI) stopTask(task string) {
	ctx, cancel := context.WithTimeout(context.Background(), engine.Check)
	defer cancel()
	_, _, _ = h.d.Engine.JSON(ctx, http.MethodDelete, "/tasks/"+task, nil)
}

// forwardFile is routes/proxy.py _forward of a GET: the engine's answer streamed back, with its
// content type, disposition (a plain name before an accented one) and length.
func (h *objectsAPI) forwardFile(w http.ResponseWriter, r *http.Request, path string) {
	req, err := h.d.Engine.Request(r.Context(), http.MethodGet, path, nil)
	if err != nil {
		h.fail(w, r, err)
		return
	}
	for k, vs := range r.Header {
		switch strings.ToLower(k) {
		case "host", "content-length", "authorization", "cookie", "x-tabularia-csrf", "connection":
			continue
		}
		for _, v := range vs {
			req.Header.Add(k, v)
		}
	}
	res, err := h.d.Engine.Do(req)
	if err != nil {
		server.JSON(w, http.StatusServiceUnavailable, map[string]any{"detail": "The engine is not answering"})
		return
	}
	defer res.Body.Close()
	for _, k := range []string{"Content-Type", "Content-Disposition", "Content-Length"} {
		if v := res.Header.Get(k); v != "" {
			if k == "Content-Disposition" {
				v = asciiFallback(v)
			}
			w.Header().Set(k, v)
		}
	}
	w.WriteHeader(res.StatusCode)
	_, _ = io.Copy(w, res.Body)
}

var (
	extendedName = regexp.MustCompile(`(?i)filename\*\s*=\s*utf-8''([^;\s]+)`)
	plainName    = regexp.MustCompile(`(?i)(^|;)\s*filename\s*=`)
	spacesRe     = regexp.MustCompile(`\s+`)
)

// asciiFallback is routes/proxy.py ascii_fallback: a plain file name before the accented one.
func asciiFallback(disposition string) string {
	m := extendedName.FindStringSubmatch(disposition)
	if m == nil || plainName.MatchString(disposition) {
		return disposition
	}
	name, err := url.PathUnescape(m[1])
	if err != nil {
		name = m[1]
	}
	stem, ext := name, ""
	if i := strings.LastIndex(name, "."); i >= 0 {
		stem, ext = name[:i], name[i+1:]
	}
	var plain strings.Builder
	for _, c := range norm.NFKD.String(stem) {
		if unicode.Is(unicode.Mn, c) {
			continue
		}
		if c < 0x80 && unicode.IsPrint(c) && c != '"' && c != '\\' {
			plain.WriteRune(c)
		} else {
			plain.WriteRune(' ')
		}
	}
	p := strings.Trim(spacesRe.ReplaceAllString(plain.String(), " "), " .")
	if p == "" {
		p = "report"
	}
	var e strings.Builder
	for _, c := range ext {
		if c < 0x80 && (unicode.IsLetter(c) || unicode.IsDigit(c)) {
			e.WriteRune(c)
		}
	}
	kind, rest, _ := strings.Cut(disposition, ";")
	dot := ""
	if e.Len() > 0 {
		dot = "." + e.String()
	}
	return fmt.Sprintf(`%s; filename="%s%s";%s`, strings.TrimSpace(kind), p, dot, rest)
}
