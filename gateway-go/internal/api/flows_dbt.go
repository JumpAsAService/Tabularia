package api

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"net/url"
	"sort"
	"strconv"
	"strings"
	"time"
	"unicode"

	"golang.org/x/text/unicode/norm"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/aimodels"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/audit"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/auth"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/dbtexport"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/language"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/perm"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyd"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyd/gatewaymodels"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyval"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/server"
)

// routes/flows.py: the dbt export of a flow (_flow_for_dbt, export_flow_dbt,
// export_flow_dbt_with_options, export_flow_dbt_plan, _export_dbt). The project is built here
// (services/dbt_export.py, internal/dbtexport) and compiled by the engine. An export that asks
// for the AI (descriptions, translations: services/dbt_ai.py, pydantic-ai) goes whole to the
// AI service.

func init() {
	register("flows_dbt", func(d *Deps) []server.Route {
		a := func(pattern string, h func(http.ResponseWriter, *http.Request, *auth.Principal)) http.Handler {
			return server.Authed(d.Auth, pattern, h)
		}
		return []server.Route{
			{Method: http.MethodGet, Pattern: "/flows/{flow_id}/export/dbt", Handler: a("/flows/{flow_id}/export/dbt", d.exportFlowDbt)},
			{Method: http.MethodPost, Pattern: "/flows/{flow_id}/export/dbt",
				Handler: withBody(a("/flows/{flow_id}/export/dbt", d.exportFlowDbtWithOptions))},
			{Method: http.MethodGet, Pattern: "/flows/{flow_id}/export/dbt/plan", Handler: a("/flows/{flow_id}/export/dbt/plan", d.exportFlowDbtPlan)},
		}
	})
}

// flowForDbt is _flow_for_dbt: administrators only (personal flag or admin group, not the
// observers), with VIEW on the flow's folder.
func (q *request) flowForDbt(id int64) *dbtexport.Flow {
	f := q.flow(id)
	if f == nil || !q.ensure(f.ProjectID, perm.View) {
		return nil
	}
	admin, err := q.c.IsAdmin(q.ctx)
	if err != nil {
		q.fail(err)
		return nil
	}
	if !admin {
		q.refuse(http.StatusForbidden, "The dbt export is for administrators only: the project carries data and definitions from other folders too")
		return nil
	}
	return &dbtexport.Flow{ID: f.ID, Name: f.Name, Description: f.Description, ProjectID: f.ProjectID, OwnerID: f.OwnerID,
		Definition: f.Definition, Engine: f.Engine, ProductionEngine: f.ProductionEngine}
}

// exportFlowDbt is GET /flows/{flow_id}/export/dbt: the project with the default values.
func (d *Deps) exportFlowDbt(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
	var ps pyd.Params
	ids, _ := pathIntsInto(&ps, r, "flow_id")
	target := ps.Query(pyd.ParseQuery(r.URL.RawQuery), "target", pyd.Str().Pattern("^(duckdb|native|clickhouse)$"), "duckdb")
	if !validated(w, &ps) {
		return
	}
	q := d.req(w, r, p)
	f := q.flowForDbt(ids[0])
	if f == nil {
		return
	}
	q.exportDbt(f, target.(string), nil)
}

// exportFlowDbtWithOptions is POST /flows/{flow_id}/export/dbt: the dialog's choices.
func (d *Deps) exportFlowDbtWithOptions(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
	var ps pyd.Params
	ids, _ := pathIntsInto(&ps, r, "flow_id")
	body := ps.Body(bodyOf(r), gatewaymodels.DbtOptions)
	if !validated(w, &ps) {
		return
	}
	options := dbtOptionsOf(body)
	if options.AIDescriptions || options.AITranslations {
		// the AI writes descriptions or proposes translations (pydantic-ai): the AI service's
		toAIService(d, w, r, rawOf(r))
		return
	}
	q := d.req(w, r, p)
	f := q.flowForDbt(ids[0])
	if f == nil {
		return
	}
	q.exportDbt(f, options.Target, options)
}

// dbtOptionsOf reads a validated DbtOptions.
func dbtOptionsOf(mv *pyd.ModelValue) *dbtexport.Options {
	o := &dbtexport.Options{Target: mv.Get("target").(string), Package: mv.Get("package").(string), Folder: mv.Get("folder").(string),
		Prefix: mv.Get("prefix").(string), Layers: mv.Get("layers").(bool), Sources: map[string]dbtexport.SourceOption{},
		Materializations: map[string]string{}, Tests: mv.Get("tests").(bool), Emails: mv.Get("emails").(bool),
		AIDescriptions: mv.Get("ai_descriptions").(bool), AITranslations: mv.Get("ai_translations").(bool)}
	if s, ok := mv.Get("schema_").(string); ok {
		o.Schema = &s
	}
	if srcs, ok := mv.Get("sources").(*pyd.Object); ok {
		for _, k := range srcs.Keys() {
			v, _ := srcs.Get(k)
			so := dbtexport.SourceOption{}
			if sv, ok := v.(*pyd.ModelValue); ok {
				if n, ok := sv.Get("name").(string); ok {
					so.Name = &n
				}
				so.Declared = sv.Get("declared").(bool)
			}
			o.SourceKeys = append(o.SourceKeys, k)
			o.Sources[k] = so
		}
	}
	if mats, ok := mv.Get("materializations").(*pyd.Object); ok {
		for _, k := range mats.Keys() {
			v, _ := mats.Get(k)
			o.MaterializationKeys = append(o.MaterializationKeys, k)
			o.Materializations[k], _ = v.(string)
		}
	}
	return o
}

// pyG is f"{x:g}".
func pyG(f float64) string { return strconv.FormatFloat(f, 'g', 6, 64) }

// exportDbt is _export_dbt: the payload built, the engine compiles and zips it.
func (q *request) exportDbt(f *dbtexport.Flow, target string, options *dbtexport.Options) {
	lang := language.Of(q.r.Header.Get("Accept-Language"))
	email := q.p.User.Email
	payload, _, err := dbtexport.Build(q.ctx, q.d.DB, f, q.d.Config.EngineBucket, target, &email, options)
	var de *dbtexport.Error
	if errors.As(err, &de) {
		q.refuse(http.StatusUnprocessableEntity, dbtexport.ErrorMessage(de, lang))
		return
	}
	if err != nil {
		q.fail(err)
		return
	}
	limit := q.d.Panel.DbtExportTimeout()
	ctx, cancel := context.WithTimeout(q.ctx, time.Duration(limit*float64(time.Second)))
	defer cancel()
	res, raw, err := q.d.Engine.JSON(ctx, http.MethodPost, "/dbt/export", payload)
	if err != nil {
		if errors.Is(err, context.DeadlineExceeded) || errors.Is(ctx.Err(), context.DeadlineExceeded) {
			s := pyG(limit)
			e := &dbtexport.Error{Message: "the engine did not build the dbt project within " + s + " s", Code: "engine_timeout",
				Params: pyjson.Object{{Key: "seconds", Value: s}}}
			q.refuse(http.StatusGatewayTimeout, dbtexport.ErrorMessage(e, lang))
			return
		}
		// httpx's words for an engine that does not answer the connection
		detail := "All connection attempts failed"
		var ue *url.Error
		if !errors.As(err, &ue) || !strings.Contains(err.Error(), "connect") {
			detail = err.Error()
		}
		detail = pyStrCut(detail, 200)
		e := &dbtexport.Error{Message: "the engine cannot be reached (" + detail + ")", Code: "engine_unreachable",
			Params: pyjson.Object{{Key: "detail", Value: detail}}}
		q.refuse(http.StatusBadGateway, dbtexport.ErrorMessage(e, lang))
		return
	}
	if res.StatusCode != http.StatusOK {
		q.engineRefusal(res.StatusCode, raw, lang)
		return
	}
	name := dbtFileName(f.Name)
	queue := "_dbt_" + target
	if options != nil && options.Package == "folder" {
		queue += "_folder"
	}
	queue += ".zip"
	w := q.w
	w.Header().Set("Content-Disposition", dbtContentDisposition(name, queue))
	w.Header().Set("Content-Type", "application/zip")
	w.Header().Set("Content-Length", strconv.Itoa(len(raw)))
	w.WriteHeader(http.StatusOK)
	_, _ = w.Write(raw)
	detail := pyjson.Object{{Key: "format", Value: "dbt"}, {Key: "target", Value: target}}
	if options != nil {
		detail = append(detail, pyjson.KV{Key: "options", Value: options.Dump()})
	}
	kind := "flow"
	q.audit(audit.Event{Action: "export.download", TargetType: &kind, TargetID: &f.ID, TargetLabel: &f.Name, Detail: detail})
}

func pyStrCut(s string, n int) string {
	r := []rune(s)
	if len(r) > n {
		return string(r[:n])
	}
	return s
}

// engineRefusal is what the route answers for an engine that does not give the zip: its
// refusal in the reader's language, with its status (502 below 400).
func (q *request) engineRefusal(status int, body []byte, lang string) {
	var raw any
	if v, err := pyjson.Loads(string(body)); err == nil {
		if o, ok := v.(pyjson.Object); ok {
			raw = o.Value("detail")
		}
	}
	var detail any
	err := pyval.Catch(func() {
		if rd, ok := raw.(pyjson.Object); ok {
			prm := pyval.Or(rd.Value("params"), pyjson.Object{})
			model := pyval.Get(prm, "model")
			if pyval.Eq(rd.Value("code"), "ai_translation_invalid") {
				pyval.Key(model) // `model in proposals`: an unhashable model raises
			}
		}
		var err error
		if raw != nil {
			detail, err = dbtexport.EngineMessage(raw, lang)
		} else {
			detail, err = dbtexport.Translate("internal", pyjson.Object{{Key: "detail", Value: fmt.Sprintf("engine %d", status)}}, lang, "")
		}
		pyval.Check(err)
	})
	if err != nil {
		q.fail(err)
		return
	}
	code := status
	if code < 400 {
		code = http.StatusBadGateway
	}
	server.JSON(q.w, code, map[string]any{"detail": detail})
}

// dbtFileName is the zip's name from the flow's: letters, digits, - and _ kept.
func dbtFileName(name string) string {
	var b strings.Builder
	for _, r := range name {
		if unicode.IsLetter(r) || unicode.IsNumber(r) || r == '-' || r == '_' {
			b.WriteRune(r)
		} else {
			b.WriteByte('_')
		}
	}
	if s := strings.Trim(b.String(), "_"); s != "" {
		return s
	}
	return "flow"
}

// dbtContentDisposition is _content_disposition: the file name in ASCII for old clients
// (accents transliterated, other letters `_`) and as RFC 5987 `filename*`.
func dbtContentDisposition(base, queue string) string {
	var b strings.Builder
	for _, r := range norm.NFKD.String(base) {
		switch {
		case r < 0x80:
			b.WriteRune(r)
		case norm.NFD.PropertiesString(string(r)).CCC() != 0:
		default:
			b.WriteByte('_')
		}
	}
	ascii := strings.Trim(b.String(), "_")
	if ascii == "" {
		ascii = "flow"
	}
	return `attachment; filename="` + ascii + queue + `"; filename*=UTF-8''` + quoteAll(base+queue)
}

// quoteAll is urllib.parse.quote(s, safe=""): every byte but letters, digits and "_.-~".
func quoteAll(s string) string {
	var b strings.Builder
	for i := 0; i < len(s); i++ {
		c := s[i]
		if (c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z') || (c >= '0' && c <= '9') || c == '_' || c == '.' || c == '-' || c == '~' {
			b.WriteByte(c)
		} else {
			fmt.Fprintf(&b, "%%%02X", c)
		}
	}
	return b.String()
}

// exportFlowDbtPlan is GET /flows/{flow_id}/export/dbt/plan: what the download dialog needs.
func (d *Deps) exportFlowDbtPlan(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
	ids, ok := pathInts(w, r, "flow_id")
	if !ok {
		return
	}
	q := d.req(w, r, p)
	f := q.flowForDbt(ids[0])
	if f == nil {
		return
	}
	plan, err := dbtexport.Plan(q.ctx, d.DB, f, d.Config.EngineBucket, language.Of(r.Header.Get("Accept-Language")))
	if err != nil {
		q.fail(err)
		return
	}
	env := d.aiEnv()
	enabled, err := aimodels.EnabledModelIDs(q.ctx, d.DB, env)
	if err != nil {
		q.fail(err)
		return
	}
	model := aimodels.Preferred(d.Panel.AIDefaultModel(), enabled)
	var described any
	if model != nil && *model != "" {
		s, err := aimodels.Described(q.ctx, d.DB, *model)
		if err != nil {
			q.fail(err)
			return
		}
		described = s
	}
	stored, err := dbtVersionTexts(q.ctx, d, f.ID)
	if err != nil {
		q.fail(err)
		return
	}
	plan = plan.Set("ai", pyjson.Object{{Key: "available", Value: model != nil}, {Key: "model", Value: described},
		{Key: "metadata_only", Value: d.Panel.MetadataOnly("chat")}, {Key: "stored", Value: stored}})
	server.JSON(w, http.StatusOK, plan)
}

// dbtVersionTexts is dbt_ai.version_texts: what the AI wrote that is kept for the flow's
// current version.
func dbtVersionTexts(ctx context.Context, d *Deps, flowID int64) (pyjson.Object, error) {
	var version int64
	if err := d.DB.QueryRow(ctx, `SELECT coalesce(max(version), 0) FROM flow_versions WHERE flow_id = $1`, flowID).Scan(&version); err != nil {
		return nil, err
	}
	rows, err := d.DB.Query(ctx, `SELECT kind, model_id FROM dbt_ai_texts WHERE flow_id = $1 AND flow_version = $2
		AND kind IN ('describe', 'translate') AND text <> ''`, flowID, version)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	descriptions, translations := false, false
	models := map[string]bool{}
	for rows.Next() {
		var kind, model string
		if err := rows.Scan(&kind, &model); err != nil {
			return nil, err
		}
		descriptions = descriptions || kind == "describe"
		translations = translations || kind == "translate"
		if model != "" {
			models[model] = true
		}
	}
	if err := rows.Err(); err != nil {
		return nil, err
	}
	list := make([]string, 0, len(models))
	for m := range models {
		list = append(list, m)
	}
	sort.Strings(list)
	return pyjson.Object{{Key: "descriptions", Value: descriptions}, {Key: "translations", Value: translations},
		{Key: "models", Value: list}}, nil
}
