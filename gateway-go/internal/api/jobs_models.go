package api

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"math"
	"net/http"
	"os"
	"slices"
	"sort"
	"strconv"
	"strings"
	"time"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/auth"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/flowresolver"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/jobs"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/kube"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/modelcatalog"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/modelgroups"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/perm"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/scriptcfg"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/scriptnodes"
)

// A model node as a step of a run (docs/specs/statistical-models.md, «A run»): run as a Script
// node is (jobs_scripts.go), with the group's fixed runner instead of the person's code.
//
//  1. Before anything: the scripting machinery is there, the parameters pass the catalogue's
//     check, the group is enabled, the person may use it, its current version is ready and was
//     built from the recipe this Tabularia ships. Otherwise the step fails with the reason and
//     creates nothing.
//  2. The input: the chain to the node, ended by a `select` of the columns the model reads
//     (all of them for the `rows` output, which gives the input's columns back), written by
//     the engine into the scripts bucket; past the group's max_rows the step fails there.
//  3. The Job: the group's image by digest, the runner's files and task.json in its ConfigMap,
//     internet access off, in the scripts namespace (a trial: the trials' one, with its quota).
//     A refusal of the runner (exit 3) is said with the runner's sentence and not tried again.
//  4. The import: results.parquet checked by the engine against the catalogue
//     (app/engine/models/results.py) and copied into Tabularia's storage, with the model card;
//     output.parquet when the node has nodes after it (or is tried) and its output is not
//     `results`. The run keeps both keys: output_key is what the nodes after it read (the
//     results themselves when the output is `results`), model_results_key what the report reads.
//  5. MLflow, for a flow run of a node that names a connection: jobs_models_mlflow.go.
//  6. The step's files leave the scripts bucket, by exact key.

const (
	labelModelGroup = "tabularia.io/model-group"
	labelModel      = "tabularia.io/model"
)

// threadsOf is the threads of the linear algebra: the CPU limit, whole, at least one
// (docs/specs/statistical-models.md, «Determinism»).
func threadsOf(cpu string) int64 {
	cpu = strings.TrimSpace(cpu)
	if m, isMilli := strings.CutSuffix(cpu, "m"); isMilli {
		n, err := strconv.ParseFloat(m, 64)
		if err != nil {
			return 1
		}
		return max(1, int64(math.Ceil(n/1000)))
	}
	n, err := strconv.ParseFloat(cpu, 64)
	if err != nil {
		return 1
	}
	return max(1, int64(math.Ceil(n)))
}

// ── the step ─────────────────────────────────────────────────────────────────

// modelStep is what the checks made of a model node: everything the Job needs.
type modelStep struct {
	label   string
	model   string
	group   string
	checked map[string]any
	output  string // results | rows | table | forecast
	img     *ModelGroupImage
	timeout int64
	retries int64
	delay   int64
	budget  int64
	mlflow  *mlflowPlan // a flow run's MLflow block, nil when it logs nothing (always for a trial)
	predict bool        // a Predict step's (jobs_predict.go): its sentences name it so
}

// what names the step in its sentences: «model «Elasticity»», «Predict «Prices next month»».
func (s *modelStep) what() string {
	if s.predict {
		return "Predict «" + s.label + "»"
	}
	return "model «" + s.label + "»"
}

// modelLabel is the node's name, else its id.
func modelLabel(node pyjson.Object) string {
	if l, ok := nodeData(node).Value("label").(string); ok && strings.TrimSpace(l) != "" {
		return l
	}
	return pyjson.Str(pyjson.Or(node.Value("id"), "model"))
}

// modelParams are the node's params as the catalogue reads them (JSON's numbers, lists, maps).
func modelParams(data pyjson.Object) (map[string]any, error) {
	raw := pyjson.Or(data.Value("params"), pyjson.Object{})
	if _, isObj := raw.(pyjson.Object); !isObj {
		return nil, &scriptFailed{"its parameters are not an object"}
	}
	text, err := pyjson.Dumps(raw, pyjson.Options{})
	if err != nil {
		return nil, err
	}
	out := map[string]any{}
	if err := json.Unmarshal([]byte(text), &out); err != nil {
		return nil, err
	}
	return out, nil
}

// modelsOff is why statistical models cannot run on this installation, "" when they can.
func modelsOff(sc *scriptcfg.Settings) string {
	if v := strings.ToLower(strings.TrimSpace(os.Getenv("MODELS__ENABLED"))); v == "false" || v == "0" || v == "no" {
		return "statistical models are off on this installation"
	}
	switch sc.DisabledReason() {
	case "":
		return ""
	case "kubernetes":
		return "statistical models need Kubernetes: they are off on this installation"
	}
	return "statistical models need the scripting machinery, which is not configured on this installation"
}

// modelMaxChartPoints is MODELS__MAX_CHART_POINTS, 0 for the catalogue's.
func modelMaxChartPoints() int {
	n, err := strconv.Atoi(strings.TrimSpace(os.Getenv("MODELS__MAX_CHART_POINTS")))
	if err != nil || n <= 0 {
		return 0
	}
	return n
}

// checkModelGroup is the group's part of «Before anything», the groups' lane's answer
// (models_groups_image.go): enabled, its current version ready and built from the recipe
// shipped, and the person among those who may use it.
func checkModelGroup(ctx context.Context, d *Deps, user *auth.User, group string) (*ModelGroupImage, error) {
	img, err := ReadyModelGroupImage(ctx, d.DB, group)
	var off *ModelGroupUnavailable
	if errors.As(err, &off) {
		return nil, &scriptFailed{off.Message}
	}
	if err != nil {
		return nil, err
	}
	ok, err := ModelGroupUsable(ctx, d.DB, perm.For(d.DB, user), user.ID, group)
	if err != nil {
		return nil, err
	}
	if !ok {
		return nil, &scriptFailed{fmt.Sprintf("%s may not use the model group %s: ask an administrator", user.Email, group)}
	}
	return img, nil
}

// planModel is «Before anything» for a model node: the step, or why it cannot run
// (a *scriptFailed), before any row, input or Job exists.
func (q *queue) planModel(ctx context.Context, user *auth.User, node pyjson.Object) (*modelStep, error) {
	label := modelLabel(node)
	if why := modelsOff(q.sc); why != "" {
		return nil, &scriptFailed{why}
	}
	data := nodeData(node)
	model, _ := data.Value("model").(string)
	// the Bayesian model is a node of its own: a model node naming it never runs it
	// (docs/specs/bayes-report.md, Decisions)
	if model == bayesNodeType || node.Value("type") == bayesNodeType {
		return nil, &scriptFailed{fmt.Sprintf("model «%s»: the Bayesian model is a node of its own: add «Bayesian model» from the palette", label)}
	}
	group := modelcatalog.GroupOf(model)
	if group == "" {
		return nil, &scriptFailed{fmt.Sprintf("model «%s»: the model «%s» is not known to this version of Tabularia", label, model)}
	}
	params, err := modelParams(data)
	if err != nil {
		var sf *scriptFailed
		if errors.As(err, &sf) {
			return nil, &scriptFailed{fmt.Sprintf("model «%s»: %s", label, sf.msg)}
		}
		return nil, err
	}
	checked, err := modelcatalog.Check(model, params)
	if err != nil {
		var ce *modelcatalog.Error
		if errors.As(err, &ce) {
			return nil, &scriptFailed{fmt.Sprintf("model «%s»: %s", label, ce.Message)}
		}
		return nil, err
	}
	img, err := checkModelGroup(ctx, q.d, user, group)
	if err != nil {
		return nil, err
	}
	timeout, retries, delay, err := retrySettings(data)
	if err != nil {
		return nil, &scriptFailed{fmt.Sprintf("model «%s»: its timeout and retries are whole numbers", label)}
	}
	switch {
	case timeout < 1:
		return nil, &scriptFailed{fmt.Sprintf("model «%s»: its timeout must be at least one second", label)}
	case img.MaxTimeoutSeconds > 0 && timeout > img.MaxTimeoutSeconds:
		return nil, &scriptFailed{fmt.Sprintf("model «%s»: its timeout (%d s) is longer than the group «%s» allows (%d s)",
			label, timeout, group, img.MaxTimeoutSeconds)}
	case retries < 0 || retries > scriptnodes.MaxRetries:
		return nil, &scriptFailed{fmt.Sprintf("model «%s»: from 0 to %d retries", label, scriptnodes.MaxRetries)}
	case delay < 0:
		delay = 0
	}
	if why := modelSizeRefusal(group, checked, img); why != "" {
		return nil, &scriptFailed{fmt.Sprintf("model «%s»: %s", label, why)}
	}
	budget, err := budgetSeconds(data)
	if err != nil {
		return nil, err
	}
	output, _ := checked["output"].(string)
	if output == "" {
		output = "results"
	}
	return &modelStep{label: label, model: model, group: group, checked: checked, output: output, img: img,
		timeout: timeout, retries: retries, delay: delay, budget: budget}, nil
}

// modelSizeRefusal is why a model of a group with weights cannot run its size: the version in use was not built with it
// (the runner refuses it too, but only after the refusals about the data, and after a Job was started). "" when it can.
func modelSizeRefusal(group string, checked map[string]any, img *ModelGroupImage) string {
	g, ok := modelgroups.Get(group)
	size, _ := checked["size"].(string)
	if !ok || g.Recipe.Weights == nil || size == "" {
		return ""
	}
	if slices.Contains(img.Sizes, size) {
		return ""
	}
	name := "Timestral-" + strings.ToUpper(size)
	var present []string
	for _, s := range img.Sizes {
		present = append(present, strings.ToUpper(s))
	}
	choose := ""
	switch {
	case len(present) == 1:
		choose = "choose " + present[0] + ", or "
	case len(present) > 1:
		choose = "choose " + strings.Join(present[:len(present)-1], ", ") + " or " + present[len(present)-1] + ", or "
	}
	return name + " was not downloaded by this installation: " + choose + "ask an administrator to add it"
}

// weightsEnv is the environment of a Job of a group with weights: the weights are in the image, so nothing is fetched, and the
// libraries' caches and threads stay inside /tmp and the Job's CPU.
func weightsEnv(group, threads string) pyjson.Object {
	if g, ok := modelgroups.Get(group); !ok || g.Recipe.Weights == nil {
		return nil
	}
	return pyjson.Object{{Key: "HF_HUB_OFFLINE", Value: "1"}, {Key: "TORCH_HOME", Value: "/tmp/torch"},
		{Key: "MKL_NUM_THREADS", Value: threads}}
}

// modelMaxRows is the most input rows a model reads: its group's (the recipe's, or what an administrator set), or the model's
// own when that is smaller (ARIMA's 20,000, below the time series group's 200,000); 0 for no limit. The sentence of the
// refusal is the same either way.
func modelMaxRows(model string, group int64) int64 {
	if own := modelcatalog.MaxRows(model); own > 0 && (group <= 0 || own < group) {
		return own
	}
	return group
}

// modelMaxRowsOf is modelMaxRows for a run with its checked parameters: the smallest of the group's limit, the model's own and
// its variant's (the risk simulation's ARIMA variant reads 20,000 rows, its GARCH variant the group's); 0 for no limit.
func modelMaxRowsOf(model string, checked map[string]any, group int64) int64 {
	if own := modelcatalog.MaxRowsOf(model, checked); own > 0 && (group <= 0 || own < group) {
		return own
	}
	return group
}

// thousands writes a count as the sentences of the limits do: 3,400,000.
func thousands(n int64) string {
	s := strconv.FormatInt(n, 10)
	var b strings.Builder
	for i, c := range s {
		if i > 0 && (len(s)-i)%3 == 0 {
			b.WriteByte(',')
		}
		b.WriteRune(c)
	}
	return b.String()
}

// modelCellGroups are the groups whose models are limited by rows × predictors (the catalogue's
// max_cells, docs/specs/statistical-models.md, «Performance and limits»): the boosting group's
// trees hold the whole table in memory; the R groups read none of its limits.
var modelCellGroups = map[string]bool{"boosting": true}

// modelCellsSentence is why a model of such a group may not read `rows` rows with its predictors,
// "" when it may: «1,000,000 rows × 120 predictors make 120 million values; this group allows 50
// million: fewer predictors, or sample before the node».
func modelCellsSentence(group string, rows int64, checked map[string]any) string {
	most := int64(modelcatalog.Limit("max_cells"))
	predictors := 0
	switch p := checked["predictors"].(type) {
	case []any:
		predictors = len(p)
	case []string:
		predictors = len(p)
	}
	if !modelCellGroups[group] || most <= 0 || predictors == 0 {
		return ""
	}
	cells := rows * int64(predictors)
	if cells <= most {
		return ""
	}
	return fmt.Sprintf("%s rows × %d predictors make %s values; this group allows %s: fewer predictors, or sample before the node",
		thousands(rows), predictors, millions(cells), millions(most))
}

// millions is a count as the sentences of the limits write it: «120 million», «52.5 million»,
// «812,340» below a million.
func millions(n int64) string {
	if n < 1_000_000 {
		return thousands(n)
	}
	return strconv.FormatFloat(math.Round(float64(n)/100_000)/10, 'f', -1, 64) + " million"
}

// modelInput is the chain to a model node written into the scripts bucket: the rows written
// (nil when the engine did not say) and the development samples the chain read.
type modelInput struct {
	rows   *int64
	sample []any
}

// modelPrepareInput writes the chain before a model or Predict node, ended by the `select` of the
// columns it reads (nil: all of them), at inKey of the scripts bucket; what names the step in its
// sentences («model «Elasticity»»). A variable: the tests stand in for the engine.
var modelPrepareInput = func(q *queue, ctx context.Context, r *scriptNodeRun, child *childRun, storage *scriptStorage, inKey string,
	columns []string, what string) (*modelInput, error) {
	req, err := flowresolver.ResolveScriptInput(r.definition, r.node, r.resolveDS, q.d.Config.EngineBucket, q.d.flowMode(r.mode), r.outputs)
	if err != nil {
		return nil, err
	}
	if req == nil {
		return nil, &scriptFailed{fmt.Sprintf("%s has no input: connect the step it reads to it", what)}
	}
	ops, _ := req.Value("operations").([]any)
	in := &modelInput{sample: samplesOf(ops)}
	if len(columns) > 0 {
		sel := pyjson.Object{{Key: "type", Value: "select"}, {Key: "params", Value: pyjson.Object{{Key: "columns", Value: stringsAny(columns)}}}}
		req = req.Set("operations", append(append([]any{}, ops...), sel))
	}
	target := pyjson.Object{{Key: "type", Value: "s3"}, {Key: "connection", Value: storage.payload()}, {Key: "target", Value: pyjson.Object{
		{Key: "bucket", Value: storage.Bucket}, {Key: "key", Value: inKey}, {Key: "format", Value: "parquet"}, {Key: "partition_by", Value: []any{}}}}}
	summary := pyDumps(pyjson.Object{{Key: "type", Value: "script_input"}, {Key: "model_run_id", Value: child.id}, {Key: "bucket", Value: storage.Bucket},
		{Key: "key", Value: inKey}})
	var parent *int64
	if r.trial != 0 {
		parent = &child.id
	} else if r.parent != 0 {
		parent = &r.parent
	}
	runID, err := q.d.launchFlowRunTo(ctx, r.user, r.flow, req, r.trigger, parent, r.mode, target, &summary)
	if err != nil {
		return nil, err
	}
	run, err := q.waitRun(ctx, runID)
	if err != nil {
		return nil, err
	}
	if run.Status != "SUCCESS" {
		why := run.Status
		if run.Error != nil && *run.Error != "" {
			why = *run.Error
		}
		return nil, &scriptFailed{fmt.Sprintf("the input of %s could not be prepared (run %d): %s", what, run.ID, why)}
	}
	in.rows = run.RowsWritten
	return in, child.save(ctx, "input_key", run.OutputKey)
}

// samplesOf is stats.layouts.samples_of: the development samples of a chain, in the order they
// are met (in a branch too), each once — what the report's sample line says.
func samplesOf(operations any) []any {
	var found []any
	var walk func(any)
	walk = func(v any) {
		switch x := v.(type) {
		case []any:
			for _, e := range x {
				walk(e)
			}
		case pyjson.Object:
			if params, ok := x.Value("params").(pyjson.Object); ok && pyjson.Truthy(params.Value(flowresolver.DevSampleMark)) {
				n, nok := numberOf(params.Value("n"))
				f, fok := numberOf(params.Value("fraction"))
				switch {
				case x.Value("type") == "limit" && nok:
					found = append(found, pyjson.Object{{Key: "first", Value: int64(n)}})
				case x.Value("type") == "sample" && fok:
					found = append(found, pyjson.Object{{Key: "fraction", Value: f}})
				default:
					found = append(found, pyjson.Object{})
				}
			}
			for _, kv := range x {
				walk(kv.Value)
			}
		}
	}
	walk(operations)
	out := []any{}
	for _, s := range found {
		dup := false
		for _, o := range out {
			if pyjson.Equal(o, s) {
				dup = true
				break
			}
		}
		if !dup {
			out = append(out, s)
		}
	}
	return out
}

func numberOf(v any) (float64, bool) {
	switch x := v.(type) {
	case int64:
		return float64(x), true
	case float64:
		return x, true
	}
	return 0, false
}

// modelFingerprint is what a model node's output is up to date with: its model and parameters,
// and every node its input comes from, however far up (their kind and settings, not their
// names), with the edges between them. A change to any of them makes the output «not up to
// date» (docs/specs/statistical-models.md, «The trial»).
func modelFingerprint(definition pyjson.Object, node pyjson.Object) string {
	data := nodeData(node)
	return chainFingerprint(definition, node, pyDumps(pyjson.Object{{Key: "model", Value: data.Value("model")},
		{Key: "params", Value: pyjson.Or(data.Value("params"), pyjson.Object{})}}))
}

// chainFingerprint is the hash of a node's own settings (own) and of the chain before it.
func chainFingerprint(definition pyjson.Object, node pyjson.Object, own string) string {
	nodes := map[string]pyjson.Object{}
	for _, n := range objectList(definition.Value("nodes")) {
		nodes[jsonKey(n.Value("id"))] = n
	}
	edges := objectList(definition.Value("edges"))
	into := map[string][]pyjson.Object{}
	for _, e := range edges {
		if strings.HasPrefix(pyjson.Str(pyjson.Or(e.Value("targetHandle"), "")), "seq") {
			continue
		}
		t := jsonKey(e.Value("target"))
		into[t] = append(into[t], e)
	}
	seen := map[string]bool{}
	var chain []string
	var chainEdges []string
	stack := []string{jsonKey(node.Value("id"))}
	for len(stack) > 0 {
		cur := stack[len(stack)-1]
		stack = stack[:len(stack)-1]
		if seen[cur] {
			continue
		}
		seen[cur] = true
		var ups []string
		for _, e := range into[cur] {
			up := jsonKey(e.Value("source"))
			ups = append(ups, up)
			chainEdges = append(chainEdges, pyDumps(pyjson.Object{{Key: "s", Value: e.Value("source")}, {Key: "t", Value: e.Value("target")},
				{Key: "sh", Value: e.Value("sourceHandle")}, {Key: "th", Value: e.Value("targetHandle")}}))
		}
		if n, ok := nodes[cur]; ok {
			if p := n.Value("parentNode"); pyjson.Truthy(p) {
				ups = append(ups, jsonKey(p))
			}
			if cur != jsonKey(node.Value("id")) {
				chain = append(chain, pyDumps(pyjson.Object{{Key: "id", Value: n.Value("id")}, {Key: "type", Value: n.Value("type")},
					{Key: "parent", Value: n.Value("parentNode")}, {Key: "data", Value: withoutKeys(nodeData(n), "label")}}))
			}
		}
		stack = append(stack, ups...)
	}
	sort.Strings(chain)
	sort.Strings(chainEdges)
	h := sha256.New()
	h.Write([]byte(own))
	for _, part := range [][]string{chain, chainEdges} {
		h.Write([]byte{0})
		for _, s := range part {
			h.Write([]byte(s))
			h.Write([]byte{'\n'})
		}
	}
	return hex.EncodeToString(h.Sum(nil))
}

func withoutKeys(o pyjson.Object, keys ...string) pyjson.Object {
	out := pyjson.Object{}
	for _, kv := range o {
		drop := false
		for _, k := range keys {
			drop = drop || kv.Key == k
		}
		if !drop {
			out = append(out, kv)
		}
	}
	return out
}

// modelStepKeys are the files of a step in the scripts bucket, deleted by exact key at its end.
func modelStepKeys(prefix string) (in, out, results, artifacts string, all []string) {
	in, out, results, artifacts = prefix+"/input.parquet", prefix+"/output.parquet", prefix+"/results.parquet", prefix+"/artifacts/"
	all = []string{in, out, results}
	for _, f := range []string{"model.json", "run.json", "error.json", "MLmodel", "native.rds", "xgboost.json", "catboost.cbm"} {
		all = append(all, artifacts+f)
	}
	return
}

// modelAttemptJob is one attempt of a model Job (kjAttempt): a variable, so that the tests
// play the cluster.
var modelAttemptJob = kjAttempt

// runModel is a flow's model node as a step: where the nodes after it read, nil when it failed.
func (q *queue) runModel(ctx context.Context, r *scriptNodeRun) (*flowresolver.Source, error) {
	d := q.d
	s, err := q.planModel(ctx, r.user, r.node)
	if err != nil {
		return nil, err
	}
	if r.trial == 0 { // a trial logs nothing to MLflow (docs/specs/statistical-models.md, «The trial»)
		if s.mlflow, err = q.planMLflow(ctx, r.user, r.node, s.label); err != nil {
			return nil, err
		}
	}
	storage, err := loadScriptStorage(ctx, d.DB)
	if err != nil {
		return nil, err
	}
	if !storage.ready() {
		return nil, &scriptFailed{"the scripts bucket is not configured (Administration → Scripting environments)"}
	}
	nodeID := r.node.Value("id")
	_, hasNext, err := scriptIO(r.definition, nodeID)
	if err != nil {
		return nil, err
	}
	trial := r.trial != 0
	task, err := modelgroups.FitTask(s.model, s.checked, hasNext || trial, modelMaxChartPoints())
	if err != nil {
		var ce *modelcatalog.Error
		if errors.As(err, &ce) {
			return nil, &scriptFailed{fmt.Sprintf("model «%s»: %s", s.label, ce.Message)}
		}
		return nil, err
	}
	ns := q.sc.Scripts.Namespace
	if trial && q.sc.Scripts.TrialNamespace != "" {
		ns = q.sc.Scripts.TrialNamespace
	}
	stepID := newUUIDText()
	prefix := fmt.Sprintf("env-%d/%s", s.img.EnvironmentID, stepID)
	inKey, outKey, resultsKey, artifacts, written := modelStepKeys(prefix)
	bucket := d.Config.EngineBucket
	engineResultsKey := "out/" + newHex() + ".parquet"
	dataKey := engineResultsKey // what the nodes after it read: the results, or its rows
	engineOutKey := ""
	if task.WriteOutput {
		engineOutKey = "out/" + newHex() + ".parquet"
		dataKey = engineOutKey
	}
	columns := []string(nil)
	if s.output != "rows" { // the rows output gives the input's columns back: all of them come
		columns = modelcatalog.Columns(s.model, s.checked)
	}
	info := pyjson.Object{{Key: "node_id", Value: nodeID}, {Key: "label", Value: s.label}, {Key: "model", Value: s.model},
		{Key: "params", Value: pyjsonOf(s.checked)}, {Key: "variant", Value: task.Variant}, {Key: "output_kind", Value: s.output},
		{Key: "group", Value: s.group}, {Key: "environment", Value: s.group}, {Key: "language", Value: modelgroupsLanguage(s.group)},
		{Key: "version", Value: s.img.Version}, {Key: "image", Value: s.img.Image}, {Key: "step_id", Value: stepID},
		{Key: "input", Value: true}, {Key: "output", Value: task.WriteOutput}, {Key: "sample", Value: []any{}},
		{Key: "attempts", Value: int64(0)}, {Key: "exit_code", Value: nil}, {Key: "log", Value: ""}, {Key: "trial", Value: trial},
		{Key: "fingerprint", Value: modelFingerprint(r.definition, r.node)}, {Key: "budget_seconds", Value: s.budget},
		{Key: "namespace", Value: ns}, {Key: "internet_access", Value: false}, {Key: "input_rows", Value: nil}}
	childID, err := r.startChild(ctx, d, "model", r.flow.ID, fmt.Sprintf("%s v%d", s.group, s.img.Version), bucket, dataKey, info)
	if err != nil {
		return nil, err
	}
	child := &childRun{d: d, id: childID, info: info}
	r.io()
	defer func() { d.deleteObjects(context.Background(), storage, written) }()
	out, err := func() (*flowresolver.Source, error) {
		in, err := modelPrepareInput(q, ctx, r, child, storage, inKey, columns, "model «"+s.label+"»")
		if err != nil {
			return nil, err
		}
		child.info = child.info.Set("sample", in.sample)
		if in.rows != nil {
			child.info = child.info.Set("input_rows", *in.rows)
		}
		if err := child.save(ctx); err != nil {
			return nil, err
		}
		if most := modelMaxRowsOf(s.model, s.checked, s.img.MaxRows); in.rows != nil && most > 0 && *in.rows > most {
			return nil, &scriptFailed{fmt.Sprintf("model «%s»: the model would read %s rows; the group «%s» allows %s: filter or sample "+
				"before the node, or ask an administrator", s.label, thousands(*in.rows), s.group, thousands(most))}
		}
		if in.rows != nil {
			if why := modelCellsSentence(s.group, *in.rows, s.checked); why != "" {
				return nil, &scriptFailed{fmt.Sprintf("model «%s»: %s", s.label, why)}
			}
		}
		runID := r.orchestrationOf(childID)
		task.GroupVersion = s.img.Version
		task.IDs = modelgroups.IDs{FlowID: r.flow.ID, NodeID: pyjson.Str(nodeID), RunID: runID, StepRunID: childID}
		if in.rows != nil {
			task.InputRows = *in.rows
		}
		if g, ok := modelgroups.Get(s.group); ok && g.Recipe.Weights != nil {
			task.Licence = licenceOfTask(s.img)
			for _, size := range s.img.Sizes {
				task.Limits["weights_"+size] = 1 // the sizes this installation holds
			}
		}
		task.RowsFrom = "run"
		if trial {
			task.RowsFrom = "trial"
		}
		task.MLflow = s.mlflow != nil // the runner writes MLmodel and native.rds; the gateway logs them
		// the runner stops its boosting in time for its results to be written within the step's timeout
		setRunLimits(task.Limits, s.timeout, s.img, time.Now())
		code, err := modelgroups.CodeFiles(s.group, task.JSON(), false)
		if err != nil {
			return nil, err
		}
		data, _, items := modelgroups.ConfigMap(code) // a run's code holds no reference file: text only
		codeData := pyjson.Object{}
		for i := range code {
			k := fmt.Sprintf("f%d", i)
			codeData = append(codeData, pyjson.KV{Key: k, Value: data[k]})
		}
		itemList := make([]any, len(items))
		for i, it := range items {
			itemList[i] = pyjson.Object{{Key: "key", Value: it["key"]}, {Key: "path", Value: it["path"]}}
		}
		url := func(key string) string { return "s3://" + storage.Bucket + "/" + key }
		outURL := ""
		if task.WriteOutput {
			outURL = url(outKey)
		}
		threads := fmt.Sprint(threadsOf(s.img.CPULimit))
		base := merge(pyjson.Object{{Key: modelgroups.EnvInput, Value: url(inKey)}, {Key: modelgroups.EnvOutput, Value: outURL},
			{Key: modelgroups.EnvResults, Value: url(resultsKey)}, {Key: modelgroups.EnvArtifacts, Value: url(artifacts)},
			{Key: "TABULARIA_STEP_ID", Value: stepID}, {Key: "TABULARIA_RUN_ID", Value: fmt.Sprint(runID)},
			{Key: "TABULARIA_FLOW_ID", Value: fmt.Sprint(r.flow.ID)}, {Key: "TABULARIA_NODE_ID", Value: pyjson.Str(nodeID)},
			{Key: "OMP_NUM_THREADS", Value: threads}, {Key: "OPENBLAS_NUM_THREADS", Value: threads},
			{Key: "HOME", Value: "/tmp"}, {Key: "TMPDIR", Value: "/tmp"}}, weightsEnv(s.group, threads), storage.env())
		key, err := d.Fernet.Decrypt(storage.SecretEnc)
		if err != nil {
			return nil, err
		}
		secret := pyjson.Object{{Key: "AWS_ACCESS_KEY_ID", Value: storage.AccessKey}, {Key: "AWS_SECRET_ACCESS_KEY", Value: key}}
		labels := pyjson.Object{{Key: "app.kubernetes.io/managed-by", Value: "tabularia"}, {Key: "app.kubernetes.io/component", Value: "models"},
			{Key: labelEnv, Value: fmt.Sprint(s.img.EnvironmentID)}, {Key: labelModelGroup, Value: s.group}, {Key: labelModel, Value: s.model},
			{Key: labelScriptRun, Value: fmt.Sprint(childID)}, {Key: labelOrchestration, Value: fmt.Sprint(runID)},
			{Key: "tabularia.io/stage", Value: "run"}}
		offline := offlineNetwork(ctx, storage, d.Log)
		js := d.jobSettings()
		if js.logMaxBytes <= 0 { // a panel without the value: the runner's refusal is in the log's last lines
			js.logMaxBytes = 256 * 1024
		}
		manifest := func(name string, attempt int64) pyjson.Object {
			return modelJobManifest(js, name, s.img, merge(base, pyjson.Object{{Key: "TABULARIA_ATTEMPT", Value: fmt.Sprint(attempt)}}),
				labels, s.timeout, itemList, name+"-code", name+"-env", offline)
		}
		ok, refused, reason, err := q.modelAttempts(ctx, r.user, child, s, runID, storage, written[1:], manifest, codeData, secret, ns, js)
		if err != nil {
			return nil, err
		}
		if refused != "" {
			return nil, &scriptFailed{fmt.Sprintf("model «%s»: %s", s.label, refused)}
		}
		if !ok {
			return nil, &scriptFailed{fmt.Sprintf("model «%s» failed: %s", s.label, reason)}
		}
		return q.importModel(ctx, r, s, child, storage, resultsKey, outKey, artifacts, engineResultsKey, engineOutKey, task.WriteOutput)
	}()
	if err != nil {
		q.logFailedModel(ctx, r, s, child, err)
	}
	return q.closeChild(ctx, child, out, err, "model «"+s.label+"»")
}

// licenceOfTask is the acceptance the version in use holds, as task.json says it; nil without one.
func licenceOfTask(img *ModelGroupImage) *modelgroups.TaskLicence {
	if img.Licence == nil || img.Licence.By == "" {
		return nil
	}
	return &modelgroups.TaskLicence{By: img.Licence.By, At: img.Licence.At.UTC().Format("2006-01-02T15:04:05Z")}
}

// modelgroupsLanguage is the group's language («r»), for the run page.
func modelgroupsLanguage(group string) string {
	if g, ok := modelgroups.Get(group); ok {
		return g.Recipe.Language
	}
	return ""
}

// pyjsonOf is a value of encoding/json (the catalogue's checked parameters) as pyjson's.
func pyjsonOf(v any) any {
	b, err := json.Marshal(v)
	if err != nil {
		return nil
	}
	out, err := pyjson.Loads(string(b))
	if err != nil {
		return nil
	}
	return out
}

// setRunLimits adds to a task's limits what the runner measures its run against: `time_budget_seconds` (the step's timeout),
// `memory_bytes` (the group's memory limit: a runner that holds data in batches sizes them from it, never from a number of
// its own), `tmp_bytes` (the Job's /tmp, `modelTmpLimit`: what it may write to disk) and `started_unix` (the second the Job is
// created: the runner counts its time from it, not from its own start, which the pod's scheduling would delay).
// A memory limit the gateway cannot read adds no `memory_bytes`: the runner then has none to size from.
func setRunLimits(limits map[string]int, timeout int64, img *ModelGroupImage, now time.Time) {
	limits["time_budget_seconds"] = int(timeout)
	if memory := mgBytes(img.MemoryLimit); memory > 0 {
		limits["memory_bytes"] = int(memory)
	}
	limits["tmp_bytes"] = int(modelTmpMiB(img)) << 20
	limits["started_unix"] = int(now.Unix())
}

// modelTmpMiB is the size of a model Job's /tmp in MiB; modelTmpLimit says it as a Kubernetes quantity.
func modelTmpMiB(img *ModelGroupImage) int64 {
	const margin = 256 << 20
	input := mgBytes(img.MemoryLimit)
	if input <= 0 {
		input = 1 << 30
	}
	return int64((2*float64(maxModelFileBytes()) + input + margin + (1 << 20) - 1) / (1 << 20))
}

// modelTmpLimit is the size of a model Job's /tmp (an emptyDir: the node's disk, which the kubelet
// evicts the pod over when it is exceeded): twice the largest model file (the one downloaded and
// the one the library writes), the input (a parquet the runner reads whole, so it is no larger than
// the group's memory) and a margin for the calculations' scratch files. It follows the group's
// memory limit, which the administrator sets in Administration -> Statistical models.
func modelTmpLimit(img *ModelGroupImage) string {
	return fmt.Sprintf("%dMi", modelTmpMiB(img))
}

// modelJobManifest is a model step's Job: the group's image by digest, Tabularia's runner,
// the Script node's isolation (user 10001, read-only root, no capability, no ServiceAccount
// token), internet access off (the label the chart's policy selects, and the egress proxy
// when the chart runs one).
func modelJobManifest(js jobSettings, name string, img *ModelGroupImage, environment pyjson.Object, labels pyjson.Object,
	timeout int64, items []any, codeName, secretName string, net *offlineNet) pyjson.Object {
	environment = net.applyEnv(environment)
	language := modelgroupsLanguage(img.Group)
	if language == "python" {
		// no code relies on the order of a set or a dict of strings, but nothing is left to it
		environment = merge(environment, pyjson.Object{{Key: "PYTHONHASHSEED", Value: "0"}})
	}
	run, _ := modelgroups.Commands(language)
	command := make([]any, len(run))
	for i, c := range run {
		command[i] = c
	}
	container := pyjson.Object{{Key: "name", Value: "model"}, {Key: "image", Value: img.Image}, {Key: "command", Value: command},
		{Key: "workingDir", Value: modelgroups.CodeDir}, {Key: "env", Value: envList(environment)},
		{Key: "envFrom", Value: []any{pyjson.Object{{Key: "secretRef", Value: pyjson.Object{{Key: "name", Value: secretName}}}}}},
		{Key: "resources", Value: pyjson.Object{{Key: "requests", Value: pyjson.Object{{Key: "cpu", Value: img.CPURequest}, {Key: "memory", Value: img.MemoryRequest}}},
			{Key: "limits", Value: pyjson.Object{{Key: "cpu", Value: img.CPULimit}, {Key: "memory", Value: img.MemoryLimit}}}}},
		{Key: "securityContext", Value: pyjson.Object{{Key: "allowPrivilegeEscalation", Value: false}, {Key: "readOnlyRootFilesystem", Value: true},
			{Key: "capabilities", Value: pyjson.Object{{Key: "drop", Value: []any{"ALL"}}}}}},
		{Key: "volumeMounts", Value: []any{pyjson.Object{{Key: "name", Value: "code"}, {Key: "mountPath", Value: modelgroups.CodeDir}, {Key: "readOnly", Value: true}},
			pyjson.Object{{Key: "name", Value: "tmp"}, {Key: "mountPath", Value: "/tmp"}}}}}
	pod := pyjson.Object{{Key: "restartPolicy", Value: "Never"}, {Key: "automountServiceAccountToken", Value: false},
		{Key: "securityContext", Value: pyjson.Object{{Key: "runAsNonRoot", Value: true}, {Key: "runAsUser", Value: int64(scriptUID)},
			{Key: "runAsGroup", Value: int64(scriptUID)}, {Key: "seccompProfile", Value: pyjson.Object{{Key: "type", Value: "RuntimeDefault"}}}}},
		{Key: "containers", Value: []any{container}}}
	if img.Arch != "" {
		pod = append(pod, pyjson.KV{Key: "nodeSelector", Value: pyjson.Object{{Key: "kubernetes.io/arch", Value: img.Arch}}})
	}
	pod = append(pod, pyjson.KV{Key: "volumes", Value: []any{pyjson.Object{{Key: "name", Value: "code"}, {Key: "configMap",
		Value: pyjson.Object{{Key: "name", Value: codeName}, {Key: "items", Value: items}}}},
		pyjson.Object{{Key: "name", Value: "tmp"}, {Key: "emptyDir", Value: pyjson.Object{{Key: "sizeLimit", Value: modelTmpLimit(img)}}}}}})
	if js.registryPullSecret != "" {
		pod = append(pod, pyjson.KV{Key: "imagePullSecrets", Value: []any{pyjson.Object{{Key: "name", Value: js.registryPullSecret}}}})
	}
	pod = net.applyPod(pod)
	return jobManifestOf(name, withInternet(labels, false), timeout, js.jobTTL, pod)
}

// modelAttempts is the attempts' loop of a model step (attempts, jobs_scripts.go): a refusal
// of the runner (exit 3) ends it at once with its sentence — the same rows would be refused
// again. Before an attempt after the first, the run must still be open and the group still
// usable by the person (enabled, ready, allowed: as «Before anything», checkModelGroup), and the
// files an attempt writes (attemptFiles: its output, results and artifacts, never its input) are
// deleted, so that the next one's import never reads what an earlier one left. (succeeded, the
// refusal's sentence, the last reason it failed).
func (q *queue) modelAttempts(ctx context.Context, user *auth.User, child *childRun, s *modelStep, runID int64, storage *scriptStorage,
	attemptFiles []string, manifest func(string, int64) pyjson.Object, code, secret pyjson.Object, ns string, js jobSettings) (bool, string, string, error) {
	var logs []string
	reason := ""
	for attempt := int64(1); attempt <= s.retries+1; attempt++ {
		name := fmt.Sprintf("tabularia-model-%d-a%d", child.id, attempt)
		m := manifest(name, attempt)
		child.info = child.info.Set("attempts", attempt)
		if err := child.save(ctx); err != nil {
			return false, "", "", err
		}
		ok, log, exitCode, why, err := modelAttemptJob(ctx, kubeAPI(), name, m, code, secret, s.timeout, ns, js.pollSeconds, js.logMaxBytes)
		if err != nil {
			return false, "", "", err
		}
		reason = why
		if s.retries > 0 {
			logs = append(logs, fmt.Sprintf("── attempt %d ──\n%s", attempt, log))
		} else {
			logs = append(logs, log)
		}
		child.info = child.info.Set("log", kube.Tail(strings.Join(logs, "\n"), js.logMaxBytes)).Set("exit_code", exitCode)
		if err := child.save(ctx); err != nil {
			return false, "", "", err
		}
		if ok {
			return true, "", "", nil
		}
		if n, isInt := pyInt(exitCode); isInt && exitCode != nil && n == modelgroups.ExitRefused {
			if e, found := modelgroups.ErrorFromLog(log); found {
				child.info = child.info.Set("refusal", pyjson.Object{{Key: "code", Value: e.Code}, {Key: "message", Value: e.Message}})
				return false, e.Message, "", child.save(ctx)
			}
			return false, "the runner refused the data and did not say why (its log may)", "", nil
		}
		if attempt <= s.retries {
			if !jobs.Sleep(ctx, time.Duration(s.delay)*time.Second) {
				return false, "", "", ctx.Err()
			}
			if !q.d.runStillOpen(ctx, runID) {
				return false, "", "", &scriptFailed{fmt.Sprintf("%s failed: %s (not run again: its run was closed)", s.what(), reason)}
			}
			if _, err := checkModelGroup(ctx, q.d, user, s.group); err != nil {
				var off *scriptFailed
				if errors.As(err, &off) { // disabled, being rebuilt, or no longer the person's to use
					return false, "", "", &scriptFailed{fmt.Sprintf("%s failed: %s (not run again: %s)", s.what(), reason, off.msg)}
				}
				return false, "", "", err
			}
			q.d.deleteObjects(ctx, storage, attemptFiles)
		}
	}
	return false, "", reason, nil
}

// importModel is «The import»: the results checked and copied, the model card kept, the rows
// copied when the node has some to give; the run closed SUCCESS with its keys.
func (q *queue) importModel(ctx context.Context, r *scriptNodeRun, s *modelStep, child *childRun, storage *scriptStorage,
	resultsKey, outKey, artifacts, engineResultsKey, engineOutKey string, writeOutput bool) (*flowresolver.Source, error) {
	d := q.d
	bucket := d.Config.EngineBucket
	cctx, cancel := context.WithTimeout(ctx, 10*time.Minute)
	res, raw, err := d.Engine.JSON(cctx, http.MethodPost, "/scripts/results/import", pyjson.Object{{Key: "connection", Value: storage.payload()},
		{Key: "key", Value: resultsKey}, {Key: "bucket", Value: bucket}, {Key: "output_key", Value: engineResultsKey},
		{Key: "model", Value: s.model}, {Key: "params", Value: pyjsonOf(s.checked)}, {Key: "card_key", Value: artifacts + "model.json"}})
	cancel()
	if err != nil {
		return nil, err
	}
	if res.StatusCode >= 400 {
		return nil, &scriptFailed{fmt.Sprintf("model «%s»: its results were not accepted, a fault of the runner and not of the data: %s",
			s.label, engineDetail(raw))}
	}
	v, err := pyjson.Loads(string(raw))
	if err != nil {
		return nil, &pyJSONError{err}
	}
	got := pyjson.Obj(v)
	if !pyjson.Truthy(got.Value("found")) {
		return nil, &scriptFailed{fmt.Sprintf("model «%s» wrote no results: a fault of the runner (its log may say more)", s.label)}
	}
	rows, err := intColumn(got.Value("rows"))
	if err != nil {
		return nil, err
	}
	var card any
	if c, isText := got.Value("card").(string); isText && c != "" {
		card = c
	}
	dataKey, dataRows := engineResultsKey, rows
	if writeOutput {
		cctx, cancel := context.WithTimeout(ctx, 10*time.Minute)
		res, raw, err := d.Engine.JSON(cctx, http.MethodPost, "/scripts/output/import", pyjson.Object{{Key: "connection", Value: storage.payload()},
			{Key: "key", Value: outKey}, {Key: "bucket", Value: bucket}, {Key: "output_key", Value: engineOutKey}})
		cancel()
		if err != nil {
			return nil, err
		}
		if res.StatusCode >= 400 {
			return nil, &scriptFailed{fmt.Sprintf("model «%s»: its rows could not be read: %s", s.label, engineDetail(raw))}
		}
		v, err := pyjson.Loads(string(raw))
		if err != nil {
			return nil, &pyJSONError{err}
		}
		out := pyjson.Obj(v)
		if !pyjson.Truthy(out.Value("found")) {
			return nil, &scriptFailed{fmt.Sprintf("model «%s» did not write the %s the nodes after it read: a fault of the runner", s.label, s.output)}
		}
		if dataRows, err = intColumn(out.Value("rows")); err != nil {
			return nil, err
		}
		dataKey = engineOutKey
	}
	child.info = child.info.Set("results_rows", rows)
	if err := child.save(ctx, "model_results_key", engineResultsKey, "model_card", card, "output_bucket", bucket, "output_key", dataKey,
		"rows_written", dataRows); err != nil {
		return nil, err
	}
	if s.mlflow != nil && r.trial == 0 {
		if err := q.logModelMLflow(ctx, r, s, child, modelFiles{storage: storage, results: resultsKey, artifacts: artifacts,
			engineBucket: bucket, engineResultsKey: engineResultsKey}); err != nil {
			return nil, err
		}
	}
	if err := child.save(ctx, "status", "SUCCESS", "finished_at", jobs.Now()); err != nil {
		return nil, err
	}
	return &flowresolver.Source{Bucket: bucket, Key: dataKey}, nil
}

// upperFirst is a sentence of a step's failure as a route's answer: its first letter capital.
func upperFirst(s string) string {
	r := []rune(s)
	if len(r) > 0 {
		r[0] = []rune(strings.ToUpper(string(r[0])))[0]
	}
	return string(r)
}
