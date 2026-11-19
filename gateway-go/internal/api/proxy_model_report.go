package api

import (
	"errors"
	"net/http"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/auth"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/perm"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/server"
)

// The report of a statistical model's run (docs/specs/statistical-models.md, «Report»): the
// pages of a model node's latest trial or run, drawn by the engine from the results the run
// kept, with the engine's layout for that model. A drawing of the results: no query reaches
// the data, no Job starts.

func init() {
	register("model_report", func(d *Deps) []server.Route {
		return []server.Route{
			{Method: http.MethodPost, Pattern: "/tasks/model-report",
				Handler: server.Authed(d.Auth, "/tasks/model-report", d.proxyModelReport)},
		}
	})
}

// modelReportFormats are a statistics report's: the pages on screen, PDF, Excel.
var modelReportFormats = statReportFormats

// modelRun is what the report reads of a model step's run (C1 writes it: `runs.kind =
// 'model'`, its `script` the step's JSON — node_id first, then model, params, step and
// sample when the run kept them — and `model_results_key` the results it imported).
type modelRun struct {
	status       string
	script       *string
	outputBucket *string
	resultsKey   *string
}

// errNoResultsColumn: a database without the models' migration (0044) has no results to draw.
var errNoResultsColumn = errors.New("runs.model_results_key does not exist")

func modelRunOf(q *request, flowID, runID int64, nodeID any) (*modelRun, error) {
	key, _ := pyjson.Dumps(pyjson.Object{{Key: "node_id", Value: nodeID}}, pyjson.Options{EnsureASCII: true})
	prefix := key[:len(key)-1] + ","
	var r modelRun
	err := q.d.DB.QueryRow(q.ctx, `SELECT status, script, output_bucket, model_results_key FROM runs
		WHERE id = $1 AND flow_id = $2 AND kind = 'model' AND starts_with(script, $3)`, runID, flowID, prefix).
		Scan(&r.status, &r.script, &r.outputBucket, &r.resultsKey)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, nil
	}
	if isUndefinedColumn(err) {
		return nil, errNoResultsColumn
	}
	return &r, err
}

// isUndefinedColumn: PostgreSQL's 42703, a column the table does not have.
func isUndefinedColumn(err error) bool {
	var pg *pgconn.PgError
	return errors.As(err, &pg) && pg.Code == "42703"
}

// modelNode is the node `id` of a flow's definition when it is a model node.
func modelNode(definition string, id any) pyjson.Object {
	parsed, err := pyjson.Loads(definition)
	if err != nil {
		return nil
	}
	nodes, _ := pyjson.Or(pyjson.Obj(parsed).Value("nodes"), []any{}).([]any)
	for _, n := range nodes {
		node := pyjson.Obj(n)
		if node != nil && node.Value("type") == "model" && pyjson.Equal(node.Value("id"), id) {
			return node
		}
	}
	return nil
}

// proxyModelReport is POST /tasks/model-report {flow_id, node_id, run_id, format, locale}.
//
// Rights: VIEW on the flow's folder and the flow opens for the person (as the node outputs'
// route asks); the run is one of that node's, of that flow, and finished with its results.
// What the engine is handed is the gateway's, never the browser's: where the results are,
// the model and the parameters it ran with (the run's, else the node's), the node's name,
// the development samples the run read. The answer is the engine's {task_id, filename}; the
// render is followed and taken as every export (/tasks/exports/{task_id}, …/file). A route
// of the Go gateway only.
func (d *Deps) proxyModelReport(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
	_, payload, ok := readJSON(w, r)
	if !ok {
		return
	}
	refuse := func(detail string) {
		server.JSON(w, http.StatusUnprocessableEntity, map[string]string{"detail": detail})
	}
	given, isObj := payload.(pyjson.Object)
	if !isObj {
		refuse("The request body must be a JSON object")
		return
	}
	flowID, okFlow := pyjson.IsInt(given.Value("flow_id"))
	runID, okRun := pyjson.IsInt(given.Value("run_id"))
	nodeID, hasNode := given.Get("node_id")
	if !okFlow || !okRun || !hasNode || nodeID == nil {
		refuse("The report of a model needs its flow, its node and its run: «flow_id», «node_id», «run_id»")
		return
	}
	format, isText := pyjson.Or(given.Value("format"), "svg").(string)
	if !isText || !modelReportFormats[format] {
		refuse("The format of a model's report is svg (the pages on screen), pdf or xlsx")
		return
	}
	locale, _ := given.Value("locale").(string)
	if !statReportLocales[locale] {
		locale = "en"
	}
	q := d.req(w, r, p)
	f := q.flow(flowID)
	if f == nil || !q.ensure(f.ProjectID, perm.View) || q.refused(q.acc.EnsureCanOpen(q.ctx, f.Definition)) {
		return
	}
	node := modelNode(f.Definition, nodeID)
	if node == nil {
		q.refuse(http.StatusNotFound, "This flow has no model node with that id")
		return
	}
	run, err := modelRunOf(q, f.ID, runID, nodeID)
	if errors.Is(err, errNoResultsColumn) {
		q.refuse(http.StatusNotFound, "This run has no results to draw")
		return
	}
	if err != nil {
		q.fail(err)
		return
	}
	if run == nil {
		q.refuse(http.StatusNotFound, "This run is not one of that node's")
		return
	}
	if run.status != "SUCCESS" || str0(run.resultsKey) == "" {
		q.refuse(http.StatusNotFound, "This run has no results to draw: it did not finish, or it failed")
		return
	}
	kept := pyjson.Object{}
	if v, err := pyjson.Loads(str0(run.script)); err == nil {
		kept = pyjson.Obj(v)
	}
	data := pyjson.Obj(pyjson.Or(node.Value("data"), pyjson.Object{}))
	model := pyjson.Or(kept.Value("model"), data.Value("model"))
	params := pyjson.Or(kept.Value("params"), pyjson.Or(data.Value("params"), pyjson.Object{}))
	bucket := str0(run.outputBucket)
	if bucket == "" {
		bucket = d.Config.EngineBucket
	}
	o := pyjson.Object{}.Set("bucket", bucket).Set("results_key", str0(run.resultsKey)).Set("model", model).
		Set("params", params).Set("format", format).Set("locale", locale)
	if label, isLabel := data.Value("label").(string); isLabel && label != "" {
		o = o.Set("step", truncateRunes(label, statReportStepMax))
	}
	if sample, isList := kept.Value("sample").([]any); isList && len(sample) > 0 {
		o = o.Set("sample", sample)
	}
	// as an export: code (an API token) waits its turn, the interface is told at once
	var wait int64
	if p.TokenName != "" {
		wait = d.Config.Flows.ReportsAPIWaitSeconds
	}
	o = o.Set("owner", exportOwner(p.User.ID)).Set("wait_seconds", wait)
	d.forward(w, r, http.MethodPost, "/reports/model", dumps(o), false)
}

// statReportStepMax is the longest step name the engine's report routes take.
const statReportStepMax = 200

func truncateRunes(s string, n int) string {
	r := []rune(s)
	if len(r) > n {
		return string(r[:n])
	}
	return s
}
