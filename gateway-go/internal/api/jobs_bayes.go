package api

// A Bayesian model node (`bayes_model`) as a step of a flow run or of a trial (docs/specs/bayes-flow-node.md, «From the
// node to the Job»), in the shape of a model node's (jobs_models.go):
//
//  1. Before anything (planBayes): the models' machinery on, the group bayes usable by the run's authority (the person
//     who ran the flow, or the schedule's author), the pinned model visible and runnable by that person, the version
//     read, every variable bound, the sampler, the output and the limits in range; then the compile asked for
//     (Binaries.Need, with the run's authority). Otherwise the step fails with the reason and creates nothing.
//  2. The input: the chain to the node, ended by the `select` of the bound columns (and the series' time) and, for a
//     series, the engine's `sort` by the series' unit and its time (the runner only checks the order).
//  3. The binary: waited for every 2 seconds while the step's timeout lasts (the run page says so), then placed in the
//     step's prefix; one altered or gone is asked for again. The wait is deducted from the Job's deadline and from
//     time_budget_seconds.
//  4. The task (task.json's `bayes` from the stored version: never regenerated), checked against the contract before
//     anything leaves the gateway; the sampling Job (bayesJobManifest); the import of the results, checked by the engine
//     against the catalogue's bayes_model.
//  5. MLflow, for a flow run of a node that names a connection (docs/specs/bayes-mlflow.md): the plan checked before the
//     compile is asked, `task.mlflow` so that the runner writes MLmodel, the record made by the gateway after the import
//     (jobs_bayes_mlflow.go: the summary, never a draw), a FAILED run for a step that fails after its Job ran. Never a trial.
//  6. The step's files leave the scripts bucket by exact key, the binary's included.

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"math"
	"slices"
	"strings"
	"time"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/auth"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/bayeschecks"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/bayescompile"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/bayescontract"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/bayesgraph"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/bayesmodels"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/flowresolver"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/jobs"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/modelcatalog"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/modelgroups"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/perm"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/scriptnodes"
)

// bayesNodeType is the node's type; the runs it makes have the kind of the models' (`model`), so that
// the run page, the closing of orphans, the report and ModelRunDetail read them as they are.
const bayesNodeType = "bayes_model"

// bayesRunKind is the kind of the run a node of this type makes, as it is written in `runs.kind`.
func bayesRunKind(nodeType string) string {
	if nodeType == bayesNodeType {
		return "model"
	}
	return nodeType
}

// The wait for the binary: a question every bayesWaitTick, on bayesNow's clock. Variables: the tests play the time.
var (
	bayesWaitTick = 2 * time.Second
	bayesSleep    = jobs.Sleep
	bayesNow      = time.Now
)

// bayesStep is what planBayes made of a Bayesian model node: the model step's (the label, the group, the output, the
// image, the timeout and retries, the parameters the engine's import checks) and everything the task needs.
type bayesStep struct {
	*modelStep
	mode       string // bayescontract.ModeSample, or ModePriorPredictive for the check's trial (bayesCheckPrior)
	model      *bayesmodels.Model
	version    *bayesmodels.Version
	graph      *bayesgraph.Model
	stored     []bayescontract.DataEntry // the data manifest as the version stored it
	data       []bayescontract.DataEntry // the same with `column` written on the entries that read one
	quantities []bayescontract.Quantity
	mapping    []bayescontract.MappingEntry
	sampler    bayescontract.Sampler
	replicated *bayescontract.Replicated
	series     *bayescontract.Series
	columns    []string // the input's columns: the bound ones in the manifest's order, then the series' time
	sortBy     []string // a series' sort: the column of its unit (when it has one), then its time
	binaries   bayescompile.Binaries
	request    bayesmodels.CompileRequest
	binary     *bayescompile.Binary // ready when the step was planned, else nil (waited for in runBayes)
	compile    bayesmodels.CompileState
}

// bayesBinaries is the compiler's side of the runs (Deps.BayesCompiler, set at start-up where the models can run); nil
// where there is none.
func (d *Deps) bayesBinaries() bayescompile.Binaries {
	b, _ := d.BayesCompiler.(bayescompile.Binaries)
	return b
}

// wholeNumber is a node's integer setting: a JSON integer (or a float without a fraction), never a boolean or a text.
func wholeNumber(v any) (int64, bool) {
	switch x := v.(type) {
	case int64:
		return x, true
	case float64:
		if x == math.Trunc(x) && math.Abs(x) < 1<<53 {
			return int64(x), true
		}
	}
	return 0, false
}

// realNumber is a node's real setting: a JSON number, finite.
func realNumber(v any) (float64, bool) {
	switch x := v.(type) {
	case int64:
		return float64(x), true
	case float64:
		return x, !math.IsNaN(x) && !math.IsInf(x, 0)
	}
	return 0, false
}

// bayesCompileSentence is the state's sentence when the run cannot wait for the compile (stan-compile's words).
func bayesCompileSentence(s bayesmodels.CompileState) string {
	if s.Detail != "" {
		return s.Detail
	}
	if s.State == bayesmodels.CompileTimeout {
		return "the program did not compile within its limit: it is not compiled again with this limit. A simpler model, or a " +
			"longer limit set by an administrator, compiles anew"
	}
	return "the generated program did not compile: this is a fault of Tabularia, not of the model. The administrator's log says where"
}

// bayesCheckPrior is the trial's `check` that asks for the prior predictive check instead of the sampling (docs/specs/
// bayes-flow-node.md, «The prior predictive check»): a trial of the node, never its output.
const bayesCheckPrior = "prior_predictive"

// The prior predictive check's sampler: the checks spec's «The runs» (four chains, no warm-up, 250 draws, the node's seed).
const (
	priorChains = 4
	priorWarmup = 0
	priorDraws  = 250
)

// planBayes is «Before anything» for a Bayesian model node: the step, or why it cannot run (a *scriptFailed), before any
// row, input or Job exists. `user` is the run's authority: the person who runs the flow or tries the node, or the
// schedule's author; every right is checked again at every run. `check` is "" for the sampling, bayesCheckPrior for the
// prior predictive check (a trial's alone). `flowRun` is a flow run's step (not a trial): only then is the node's MLflow
// block read and checked (planMLflow), before the compile is asked.
func (q *queue) planBayes(ctx context.Context, user *auth.User, node pyjson.Object, check string, flowRun bool) (*bayesStep, error) {
	label := modelLabel(node)
	what := "Bayesian model «" + label + "»"
	fail := func(format string, a ...any) error { return &scriptFailed{what + ": " + fmt.Sprintf(format, a...)} }
	if why := modelsOff(q.sc); why != "" {
		return nil, &scriptFailed{why}
	}
	img, err := checkModelGroup(ctx, q.d, user, bayescontract.Group)
	if err != nil {
		return nil, err
	}
	data := nodeData(node)

	// the model, visible and runnable by the run's authority (one sentence whether it is absent or not seen), its version
	modelID, okM := wholeNumber(data.Value(bayesmodels.KeyModel))
	pinned, okV := wholeNumber(data.Value(bayesmodels.KeyVersion))
	if !okM || !okV || modelID <= 0 || pinned <= 0 {
		return nil, fail("choose a saved model in its panel")
	}
	unavailable := &scriptFailed{fmt.Sprintf("the Bayesian model of «%s» is not available to %s", label, user.Email)}
	m, err := bayesmodels.Get(ctx, q.d.DB, modelID)
	if err != nil {
		return nil, err
	}
	if m == nil {
		return nil, unavailable
	}
	// the group's use is checkModelGroup's answer above
	if ok, err := bayesmodels.CanRun(ctx, perm.For(q.d.DB, user), m, true); err != nil {
		return nil, err
	} else if !ok {
		return nil, unavailable
	}
	v, err := bayesmodels.GetVersion(ctx, q.d.DB, m, pinned)
	if err != nil {
		return nil, err
	}
	if v == nil {
		return nil, fail("the model or its version no longer exists: choose another")
	}
	s := &bayesStep{mode: bayescontract.ModeSample, model: m, version: v}
	if check != "" && check != bayesCheckPrior {
		return nil, fail("the check %q is not one it makes", check)
	}
	if check == bayesCheckPrior {
		s.mode = bayescontract.ModePriorPredictive
	}
	if err := s.read(); err != nil {
		return nil, err
	}
	s.label = label
	if !s.graph.Valid() {
		return nil, fail("its saved version does not validate in this version of Tabularia: open the model and save it again")
	}
	if s.mode == bayescontract.ModePriorPredictive && !s.hasPriorCheck() {
		return nil, fail("this version of the model has no prior predictive check (a truncated prior Stan cannot draw ancestrally, or a " +
			"version saved before the checks): open the model and save it again, or run the sampling")
	}

	// the bindings: every entry that reads a column, by its node's id, and the series' time
	if err := s.bind(data, fail); err != nil {
		return nil, err
	}
	// the sampler, the output, the limits; the prior predictive check keeps the node's seed alone (its settings are its own:
	// the output and the replicated data's thinning are not read, they belong to the sampling)
	if s.mode == bayescontract.ModePriorPredictive {
		data = withoutKeys(data, "output", "replicatedDraws")
	}
	if err := s.settings(data, fail); err != nil {
		return nil, err
	}
	if s.mode == bayescontract.ModePriorPredictive {
		s.sampler = bayescontract.Sampler{Chains: priorChains, Warmup: priorWarmup, Draws: priorDraws, Seed: s.sampler.Seed,
			AdaptDelta: bayescontract.DefaultAdaptDelta, MaxTreedepth: bayescontract.DefaultMaxTreedepth, Refresh: bayescontract.DefaultRefresh}
		s.modelStep.output, s.replicated = bayescontract.OutputResults, nil
	}
	// a program whose draws generate_quantities cannot reread (correlated effects: the CSV's 8 digits break the Cholesky factor's
	// check) has no replicated data: the panel keeps it off, and this is the second wall (owner's decision of 2026-11-24)
	if t, no := bayescontract.GQRereadRefusal(s.quantities); no && s.mode == bayescontract.ModeSample && s.output == bayescontract.OutputReplicated {
		return nil, fail("%s", gqRereadSentence(t, "the replicated data are not available"))
	}
	params := map[string]any{"mode": s.mode, "output": s.output}
	if s.mode == bayescontract.ModePriorPredictive {
		params = map[string]any{"mode": s.mode} // the catalogue's variant takes no output: it writes the results table alone
	}
	checked, err := modelcatalog.Check(bayescontract.Model, params)
	if err != nil {
		var ce *modelcatalog.Error
		if errors.As(err, &ce) {
			return nil, fail("%s", ce.Message)
		}
		return nil, err
	}
	s.checked = checked
	timeout, retries, delay, err := retrySettings(data)
	if err != nil {
		return nil, fail("its timeout and retries are whole numbers")
	}
	switch {
	case timeout < 1:
		return nil, fail("its timeout must be at least one second")
	case img.MaxTimeoutSeconds > 0 && timeout > img.MaxTimeoutSeconds:
		return nil, fail("its timeout (%d s) is longer than the group «%s» allows (%d s)", timeout, bayescontract.Group, img.MaxTimeoutSeconds)
	case retries < 0 || retries > scriptnodes.MaxRetries:
		return nil, fail("from 0 to %d retries", scriptnodes.MaxRetries)
	case delay < 0:
		delay = 0
	}
	budget, err := budgetSeconds(data)
	if err != nil {
		return nil, err
	}
	s.img, s.timeout, s.retries, s.delay, s.budget = img, timeout, retries, delay, budget

	// MLflow: a flow run's sampling only (a trial, the prior predictive check among them, logs nothing), the connection
	// checked with the run's authority before the compile is asked
	if flowRun && s.mode == bayescontract.ModeSample {
		if s.mlflow, err = q.planMLflow(ctx, user, node, label); err != nil {
			var sf *scriptFailed
			if errors.As(err, &sf) && strings.HasPrefix(sf.msg, "model «") {
				sf.msg = "Bayesian " + sf.msg // planMLflow names a model node's step; this one is s.what()
			}
			return nil, err
		}
	}

	// the compile, asked for now with the run's authority: the binary, or the state to wait on, or the refusal
	if s.binaries = q.d.bayesBinaries(); s.binaries == nil {
		return nil, &scriptFailed{"Bayesian models cannot be compiled on this installation"}
	}
	s.request = bayesmodels.CompileRequest{ModelID: m.ID, Version: v.Version, ProgramSHA256: v.ProgramSHA256, By: user.ID}
	s.binary, s.compile, err = s.binaries.Need(ctx, s.request)
	if errors.Is(err, bayescompile.ErrRefused) {
		return nil, fail("%s", bayesCompileSentence(s.compile))
	}
	if err != nil {
		return nil, err
	}
	return s, nil
}

// read decodes the stored version: its manifests, its mapping and its graph, validated as stan-codegen saw it.
func (s *bayesStep) read() error {
	v := s.version
	for _, part := range []struct {
		text string
		into any
	}{{v.DataManifest, &s.stored}, {v.QuantityManifest, &s.quantities}, {v.Mapping, &s.mapping}} {
		if err := json.Unmarshal([]byte(part.text), part.into); err != nil {
			return fmt.Errorf("the Bayesian model %d v%d: its stored manifests do not read: %w", v.ModelID, v.Version, err)
		}
	}
	g, findings := bayesgraph.Decode([]byte(v.Graph))
	if g == nil {
		return fmt.Errorf("the Bayesian model %d v%d: its stored graph does not decode: %v", v.ModelID, v.Version, findings)
	}
	s.graph = bayesgraph.BuildModel(g)
	s.modelStep = &modelStep{model: bayescontract.Model, group: bayescontract.Group, bayes: true}
	s.modelStep.bayesRun = s
	return nil
}

// bind writes the node's bindings into the data manifest: `column` on each entry that reads one (taken by the entry's
// node), and the series' time; the input's columns and a series' sort.
func (s *bayesStep) bind(data pyjson.Object, fail func(string, ...any) error) error {
	raw := pyjson.Or(data.Value("bindings"), pyjson.Object{})
	bindings, isObj := raw.(pyjson.Object)
	if !isObj {
		return fail("its bindings are not an object")
	}
	names := map[string]string{}
	for _, b := range s.graph.Bindings {
		names[b.Node] = b.Label
		if names[b.Node] == "" {
			names[b.Node] = b.Name
		}
		if names[b.Node] == "" {
			names[b.Node] = b.Node
		}
	}
	column := func(node string) (string, error) {
		c, ok := bindings.Value(node).(string)
		if !ok || c == "" {
			name := names[node]
			if name == "" {
				name = node
			}
			return "", fail("bind «%s» to a column", name)
		}
		return c, nil
	}
	add := func(c string) {
		if !slices.Contains(s.columns, c) {
			s.columns = append(s.columns, c)
		}
	}
	s.data = slices.Clone(s.stored)
	indexColumn := map[string]string{} // a group plate's id → the column of its group index
	for i := range s.data {
		e := &s.data[i]
		if !bayescontract.DataReadsColumn(e.Kind) {
			continue
		}
		c, err := column(e.Node)
		if err != nil {
			return err
		}
		e.Column = c
		add(c)
		if e.Kind == bayescontract.DataGroupIndex {
			indexColumn[e.Target] = c
		}
	}
	for _, p := range s.graph.Plates {
		if p.Kind != bayesgraph.PlateRows || p.Series == nil {
			continue
		}
		t, err := column(p.Series.Time)
		if err != nil {
			return err
		}
		add(t)
		s.series = &bayescontract.Series{By: p.Series.By, Time: &t}
		if p.Series.By != nil {
			unit, ok := indexColumn[*p.Series.By]
			if !ok {
				return fmt.Errorf("the Bayesian model %d v%d: the series' plate %q has no group index in the stored manifest",
					s.version.ModelID, s.version.Version, *p.Series.By)
			}
			s.sortBy = append(s.sortBy, unit)
		}
		s.sortBy = append(s.sortBy, t)
	}
	return nil
}

// settings reads the sampler (the contract's ranges, the defaults for what the node does not hold; never the seed), the
// output and the replicated output's thinning.
func (s *bayesStep) settings(data pyjson.Object, fail func(string, ...any) error) error {
	raw := pyjson.Or(data.Value("sampler"), pyjson.Object{})
	sampler, isObj := raw.(pyjson.Object)
	if !isObj {
		return fail("its sampler's settings are not an object")
	}
	whole := func(key string, def, lo, hi int64) (int64, error) {
		v, has := sampler.Get(key)
		if !has || v == nil {
			return def, nil
		}
		n, ok := wholeNumber(v)
		if !ok || n < lo || n > hi {
			return 0, fail("%s: a whole number from %s to %s", key, thousands(lo), thousands(hi))
		}
		return n, nil
	}
	b := &s.sampler
	var err error
	var n int64
	if n, err = whole("chains", bayescontract.DefaultChains, bayescontract.MinChains, bayescontract.MaxChains); err != nil {
		return err
	}
	b.Chains = int(n)
	if n, err = whole("warmup", bayescontract.DefaultWarmup, bayescontract.MinWarmup, bayescontract.MaxWarmup); err != nil {
		return err
	}
	b.Warmup = int(n)
	if n, err = whole("draws", bayescontract.DefaultDraws, bayescontract.MinDraws, bayescontract.MaxDraws); err != nil {
		return err
	}
	b.Draws = int(n)
	if n, err = whole("max_treedepth", bayescontract.DefaultMaxTreedepth, bayescontract.MinMaxTreedepth, bayescontract.MaxMaxTreedepth); err != nil {
		return err
	}
	b.MaxTreedepth = int(n)
	if v, has := sampler.Get("seed"); !has || v == nil {
		return fail("its seed is missing: draw one in its panel")
	}
	if b.Seed, err = whole("seed", 0, 0, bayescontract.MaxSeed); err != nil {
		return err
	}
	b.AdaptDelta = bayescontract.DefaultAdaptDelta
	if v, has := sampler.Get("adapt_delta"); has && v != nil {
		x, ok := realNumber(v)
		if !ok || !(x > 0 && x < 1) {
			return fail("adapt_delta: a number between 0 and 1, both left out")
		}
		b.AdaptDelta = x
	}
	b.Refresh = bayescontract.DefaultRefresh
	if b.Init, err = s.initOf(sampler.Value("init"), fail); err != nil {
		return err
	}

	s.modelStep.output = bayescontract.OutputSummary
	if v, has := data.Get("output"); has && v != nil {
		o, _ := v.(string)
		if !slices.Contains(bayescontract.Outputs, o) {
			return fail("its output is the summary, the draws, the replicated data or the results table")
		}
		s.modelStep.output = o
	}
	if s.output == bayescontract.OutputReplicated {
		per := int64(bayescontract.DefaultDrawsPerChain)
		if v, has := data.Get("replicatedDraws"); has && v != nil {
			n, ok := wholeNumber(v)
			if !ok || n < 1 || n > int64(b.Draws) {
				return fail("the replicated data keep from 1 to %s draws a chain (its draws)", thousands(int64(b.Draws)))
			}
			per = n
		}
		s.replicated = &bayescontract.Replicated{DrawsPerChain: int(min(per, int64(b.Draws)))}
	}
	return nil
}

// initOf reads the initial values: null (CmdStan's), a radius ≥ 0, or values by a quantity's Stan identifier (a number for
// a single value, a list for a vector), as the contract's Init.
func (s *bayesStep) initOf(v any, fail func(string, ...any) error) (*bayescontract.Init, error) {
	if v == nil {
		return nil, nil
	}
	if r, ok := realNumber(v); ok {
		if r < 0 {
			return nil, fail("the initial values' radius is a number of at least 0")
		}
		return &bayescontract.Init{Radius: &r}, nil
	}
	values, isObj := v.(pyjson.Object)
	if !isObj || len(values) == 0 {
		return nil, fail("the initial values are a radius, or a value for each parameter given")
	}
	out := &bayescontract.Init{Values: map[string]bayescontract.InitValue{}}
	for _, kv := range values {
		k := slices.IndexFunc(s.quantities, func(q bayescontract.Quantity) bool {
			return q.Stan == kv.Key && q.Role == bayescontract.RoleQuantity
		})
		if k < 0 {
			return nil, fail("the initial value of «%s»: not a parameter of the pinned version", kv.Key)
		}
		single := len(s.quantities[k].Dims) == 0
		if x, ok := realNumber(kv.Value); ok && single {
			out.Values[kv.Key] = bayescontract.InitValue{Number: &x}
			continue
		}
		list, isList := kv.Value.([]any)
		if single || !isList {
			return nil, fail("the initial value of «%s»: a number for a single value, a list of numbers for a vector", kv.Key)
		}
		xs := make([]float64, 0, len(list))
		for _, e := range list {
			x, ok := realNumber(e)
			if !ok {
				return nil, fail("the initial value of «%s»: a list of numbers", kv.Key)
			}
			xs = append(xs, x)
		}
		out.Values[kv.Key] = bayescontract.InitValue{List: xs}
	}
	return out, nil
}

// sortOps is the engine's `sort` after the input's `select` for a series (nil for every other model).
func (s *bayesStep) sortOps() []any {
	if len(s.sortBy) == 0 {
		return nil
	}
	return []any{pyjson.Object{{Key: "type", Value: "sort"}, {Key: "params", Value: pyjson.Object{{Key: "by", Value: stringsAny(s.sortBy)}}}}}
}

// gqRereadSentence says why a model with a constrained Stan type cannot give its draws back to the checks, and what the person can do.
func gqRereadSentence(stanType, what string) string {
	return what + " for this model: it has correlated effects, and Stan reads the draws back with a check on their correlations that " +
		"the 8 digits the sampler writes cannot meet (" + stanType + "); run it with the summary, the draws or the results table"
}

// hasPriorCheck: the stored program holds the predictive checks and the prior's draws (the entries of kind prior_count: a
// truncated gamma or inverse-gamma prior has none, and the check is not offered).
func (s *bayesStep) hasPriorCheck() bool {
	return s.hasChecks() && slices.ContainsFunc(s.stored, func(e bayescontract.DataEntry) bool { return e.Kind == "prior_count" })
}

// hasChecks: the stored program holds the predictive checks (a data entry of kind checks_on: generator 2 on).
func (s *bayesStep) hasChecks() bool {
	return slices.ContainsFunc(s.stored, func(e bayescontract.DataEntry) bool { return e.Kind == "checks_on" })
}

// bayesTaskLimits are the limits a sampling task carries before the run's own (time, memory, /tmp, start): the results'
// and the charts' (the catalogue's, MODELS__MAX_CHART_POINTS), the group's and the contract's constants with their
// defaults, the report's caps (the catalogue's).
func bayesTaskLimits() map[string]int {
	limits := map[string]int{"max_result_rows": modelcatalog.Limit("max_result_rows"), "max_chart_points": modelcatalog.Limit("max_chart_points")}
	if n := modelMaxChartPoints(); n > 0 {
		limits["max_chart_points"] = n
	}
	for _, l := range bayescontract.Limits {
		v := int(l.Default)
		if c := modelcatalog.Limit(l.Key); l.Where == "catalogue" && c > 0 {
			v = c
		}
		limits[l.Key] = v
	}
	return limits
}

// task is task.json for the step (the run's own limits set by the caller): the common keys and `bayes` from the stored
// version, the binding, the sampler and the placed binary's hash.
func (s *bayesStep) task(binarySHA256 string, writeOutput bool, rowsFrom string) (modelgroups.Task, error) {
	g, ok := modelgroups.Get(bayescontract.Group)
	if !ok {
		return modelgroups.Task{}, errors.New("there is no model group bayes")
	}
	v := s.version
	plates := make([]bayescontract.Plate, 0, len(s.graph.Plates))
	for _, p := range s.graph.Plates {
		plates = append(plates, bayescontract.Plate{ID: p.ID, Kind: p.Kind, Label: p.Label})
	}
	sampler := s.sampler
	b := &bayescontract.Task{
		Model: bayescontract.ModelRef{ID: v.ModelID, Version: int(v.Version), SemanticHash: v.SemanticHash, ProgramSHA256: v.ProgramSHA256,
			BinarySHA256: binarySHA256, CodegenVersion: v.CodegenVersion, CmdStan: bayescompile.CmdStanOf(g.Recipe),
			ProgramBytes: int64(len(v.Program)), Image: s.img.Image},
		Sampler: &sampler, Plates: plates, Mapping: s.mapping, Data: s.data, Quantities: s.quantities, Replicated: s.replicated,
		// passed as they come: nil is «not decided», [] decided and empty (the runner reads the difference)
		FiniteVariance: bayeschecks.FiniteVariance(s.graph),
		ScalePairs:     bayeschecks.ScalePairs(s.graph, s.stored, s.quantities),
		Series:         s.series,
	}
	if s.hasChecks() {
		// the posterior checks run generate_quantities on the draws: off for a program that cannot reread them (the prior
		// predictive check, fixed_param, does not read draws back and runs as it does)
		_, noReread := bayescontract.GQRereadRefusal(s.quantities)
		checks, err := json.Marshal(bayeschecks.TaskChecks(!noReread))
		if err != nil {
			return modelgroups.Task{}, err
		}
		b.Checks = checks
	}
	variant := ""
	if vr := modelcatalog.VariantOf(bayescontract.Model, s.checked); vr != nil {
		variant = vr.ID
	}
	return modelgroups.Task{Protocol: bayescontract.Protocol, Mode: s.mode, Group: bayescontract.Group, GroupVersion: s.img.Version,
		Model: bayescontract.Model, Variant: variant, Params: s.checked, Output: s.output, WriteOutput: writeOutput, RowsFrom: rowsFrom,
		Limits: bayesTaskLimits(), RecipeHash: g.RecipeHash, RunnerHash: g.RunnerHash, Bayes: b}, nil
}

// bayesFingerprint is what a Bayesian model node's output is up to date with: the pinned model and version, the
// bindings, the sampler, the output (and the replicated data's thinning), and the chain before it; never its label, its
// timeout or its retries, which do not change the numbers (docs/specs/bayes-flow-node.md, «Not up to date»).
func bayesFingerprint(definition pyjson.Object, node pyjson.Object) string {
	data := nodeData(node)
	output := pyjson.Or(data.Value("output"), bayescontract.OutputSummary)
	own := pyjson.Object{{Key: "model", Value: data.Value(bayesmodels.KeyModel)}, {Key: "version", Value: data.Value(bayesmodels.KeyVersion)},
		{Key: "bindings", Value: pyjson.Or(data.Value("bindings"), pyjson.Object{})},
		{Key: "sampler", Value: pyjson.Or(data.Value("sampler"), pyjson.Object{})}, {Key: "output", Value: output}}
	if output == bayescontract.OutputReplicated {
		own = append(own, pyjson.KV{Key: "replicated_draws", Value: pyjson.Or(data.Value("replicatedDraws"), int64(bayescontract.DefaultDrawsPerChain))})
	}
	text, _ := pyjson.Dumps(own, pyjson.Options{SortKeys: true})
	return chainFingerprint(definition, node, text)
}

// bayesCompileInfo is the child run's `compile`, which the run page reads while the step waits and after.
func bayesCompileInfo(state bayesmodels.CompileState, waiting bool, seconds int64) pyjson.Object {
	o := pyjson.Object{{Key: "state", Value: state.State}, {Key: "detail", Value: state.Detail}, {Key: "waiting", Value: waiting}}
	if !waiting && seconds > 0 {
		o = append(o, pyjson.KV{Key: "seconds", Value: seconds})
	}
	return o
}

// bayesBinary waits for the binary while the step's timeout lasts (asking Need every bayesWaitTick, the child's `compile`
// updated when the state changes), then places it at key; an altered or gone binary is asked for again. The seconds it
// waited, which the Job's deadline and time_budget_seconds lose.
func (q *queue) bayesBinary(ctx context.Context, s *bayesStep, child *childRun, key string) (int64, error) {
	start := bayesNow()
	b, state := s.binary, s.compile
	waited := false
	shown := ""
	for {
		if b != nil {
			err := s.binaries.Place(ctx, b, key)
			if err == nil {
				break
			}
			if !errors.Is(err, bayescompile.ErrAltered) && !errors.Is(err, bayescompile.ErrGone) {
				return 0, err
			}
			// the stored binary is not the one recorded, or it went: the run compiles it again (stan-compile)
			b, state = nil, bayesmodels.CompileState{State: bayesmodels.CompileNone}
		}
		if spent := bayesNow().Sub(start); spent >= time.Duration(s.timeout)*time.Second {
			return 0, &scriptFailed{fmt.Sprintf("%s: the model was still compiling when the step's timeout (%d s) ended: a longer timeout, "+
				"or try again when it is compiled", s.what(), s.timeout)}
		}
		if state.State != shown {
			shown = state.State
			child.info = child.info.Set("compile", bayesCompileInfo(state, true, 0))
			if err := child.save(ctx); err != nil {
				return 0, err
			}
		}
		waited = true
		if !bayesSleep(ctx, bayesWaitTick) {
			return 0, ctx.Err()
		}
		var err error
		b, state, err = s.binaries.Need(ctx, s.request)
		if errors.Is(err, bayescompile.ErrRefused) {
			return 0, &scriptFailed{fmt.Sprintf("%s: %s", s.what(), bayesCompileSentence(state))}
		}
		if err != nil {
			return 0, err
		}
	}
	seconds := int64(0)
	if waited {
		seconds = int64(math.Ceil(bayesNow().Sub(start).Seconds()))
	}
	s.binary = b
	child.info = child.info.Set("compile", bayesCompileInfo(bayesmodels.CompileState{State: bayesmodels.CompileReady, Detail: state.Detail}, false, seconds))
	return seconds, child.save(ctx)
}

// runBayes is a flow's Bayesian model node as a step: where the nodes after it read, nil when it failed.
func (q *queue) runBayes(ctx context.Context, r *scriptNodeRun) (*flowresolver.Source, error) {
	d := q.d
	s, err := q.planBayes(ctx, r.user, r.node, r.check, r.trial == 0)
	if err != nil {
		return nil, err
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
	writeOutput := (hasNext || trial) && s.output != bayescontract.OutputResults
	ns := q.sc.Scripts.Namespace
	if trial && q.sc.Scripts.TrialNamespace != "" {
		ns = q.sc.Scripts.TrialNamespace
	}
	stepID := newUUIDText()
	prefix := fmt.Sprintf("env-%d/%s", s.img.EnvironmentID, stepID)
	inKey, outKey, resultsKey, artifacts, written := bayesStepKeys(prefix)
	binaryKey := written[len(written)-1]
	bucket := d.Config.EngineBucket
	engineResultsKey := "out/" + newHex() + ".parquet"
	dataKey := engineResultsKey
	engineOutKey := ""
	if writeOutput {
		engineOutKey = "out/" + newHex() + ".parquet"
		dataKey = engineOutKey
	}
	variant := ""
	if vr := modelcatalog.VariantOf(bayescontract.Model, s.checked); vr != nil {
		variant = vr.ID
	}
	info := pyjson.Object{{Key: "node_id", Value: nodeID}, {Key: "label", Value: s.label}, {Key: "model", Value: bayescontract.Model},
		{Key: "params", Value: pyjsonOf(s.checked)}, {Key: "variant", Value: variant}, {Key: "output_kind", Value: s.output},
		{Key: "group", Value: s.group}, {Key: "environment", Value: s.group}, {Key: "language", Value: modelgroupsLanguage(s.group)},
		{Key: "version", Value: s.img.Version}, {Key: "image", Value: s.img.Image}, {Key: "step_id", Value: stepID},
		{Key: "input", Value: true}, {Key: "output", Value: writeOutput}, {Key: "sample", Value: []any{}},
		{Key: "attempts", Value: int64(0)}, {Key: "exit_code", Value: nil}, {Key: "log", Value: ""}, {Key: "trial", Value: trial},
		{Key: "fingerprint", Value: bayesFingerprint(r.definition, r.node)}, {Key: "budget_seconds", Value: s.budget},
		{Key: "namespace", Value: ns}, {Key: "internet_access", Value: false}, {Key: "input_rows", Value: nil},
		{Key: "bayes_model", Value: pyjson.Object{{Key: "id", Value: s.model.ID}, {Key: "version", Value: s.version.Version}}},
		{Key: "compile", Value: bayesCompileInfo(s.compile, s.binary == nil, 0)}}
	if s.mode == bayescontract.ModePriorPredictive {
		// what keeps the run out of the node's outputs (runsOfNodeChecked) and gives it to the Report tab apart
		info = append(info, pyjson.KV{Key: "check", Value: bayesCheckPrior})
	}
	childID, err := r.startChild(ctx, d, "model", r.flow.ID, fmt.Sprintf("%s v%d", s.group, s.img.Version), bucket, dataKey, info)
	if err != nil {
		return nil, err
	}
	child := &childRun{d: d, id: childID, info: info}
	r.io()
	defer func() { d.deleteObjects(context.Background(), storage, written) }()
	out, err := func() (*flowresolver.Source, error) {
		in, err := modelPrepareInput(q, ctx, r, child, storage, inKey, s.columns, s.sortOps(), s.what())
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
		if most := modelMaxRowsOf(bayescontract.Model, s.checked, s.img.MaxRows); in.rows != nil && most > 0 && *in.rows > most {
			return nil, &scriptFailed{fmt.Sprintf("%s: the model would read %s rows; the group «%s» allows %s: filter or sample "+
				"before the node, or ask an administrator", s.what(), thousands(*in.rows), s.group, thousands(most))}
		}
		// the binary: waited for within the step's timeout, then placed in the step's prefix
		waited, err := q.bayesBinary(ctx, s, child, binaryKey)
		if err != nil {
			return nil, err
		}
		step := *s.modelStep
		if step.timeout -= waited; step.timeout < 1 {
			return nil, &scriptFailed{fmt.Sprintf("%s: the step's timeout (%d s) was spent waiting for the compile: a longer timeout",
				s.what(), s.timeout)}
		}
		rowsFrom := "run"
		if trial {
			rowsFrom = "trial"
		}
		task, err := s.task(s.binary.BinarySHA256, writeOutput, rowsFrom)
		if err != nil {
			return nil, err
		}
		task.MLflow = s.mlflow != nil // the runner writes MLmodel beside the card; the gateway logs them
		runID := r.orchestrationOf(childID)
		task.IDs = modelgroups.IDs{FlowID: r.flow.ID, NodeID: pyjson.Str(nodeID), RunID: runID, StepRunID: childID}
		if in.rows != nil {
			task.InputRows = *in.rows
		}
		tmp, tmpBytes, err := bayesSamplingTmp(ctx, d.DB)
		if err != nil {
			return nil, err
		}
		// the runner counts its time from the Job's creation, within the timeout the wait left
		bayesSetRunLimits(task.Limits, step.timeout, s.img, tmpBytes, bayesNow())
		if err := task.Bayes.Check(task.Mode, task.Output); err != nil {
			return nil, &scriptFailed{fmt.Sprintf("%s: its task is not the runner's contract, a fault of Tabularia: %v", s.what(), err)}
		}
		code, err := modelgroups.CodeFiles(s.group, task.JSON(), false)
		if err != nil {
			return nil, err
		}
		cm, _, items := modelgroups.ConfigMap(code)
		codeData := pyjson.Object{}
		for i := range code {
			k := fmt.Sprintf("f%d", i)
			codeData = append(codeData, pyjson.KV{Key: k, Value: cm[k]})
		}
		itemList := make([]any, len(items))
		for i, it := range items {
			itemList[i] = pyjson.Object{{Key: "key", Value: it["key"]}, {Key: "path", Value: it["path"]}}
		}
		url := func(key string) string { return "s3://" + storage.Bucket + "/" + key }
		threads := fmt.Sprint(threadsOf(s.img.CPULimit))
		env := pyjson.Object{{Key: modelgroups.EnvInput, Value: url(inKey)}}
		if writeOutput {
			env = append(env, pyjson.KV{Key: modelgroups.EnvOutput, Value: url(outKey)})
		}
		base := bayesEnvironment(merge(env, pyjson.Object{{Key: modelgroups.EnvResults, Value: url(resultsKey)},
			{Key: modelgroups.EnvArtifacts, Value: url(artifacts)},
			{Key: "TABULARIA_STEP_ID", Value: stepID}, {Key: "TABULARIA_RUN_ID", Value: fmt.Sprint(runID)},
			{Key: "TABULARIA_FLOW_ID", Value: fmt.Sprint(r.flow.ID)}, {Key: "TABULARIA_NODE_ID", Value: pyjson.Str(nodeID)},
			{Key: "OMP_NUM_THREADS", Value: threads}, {Key: "OPENBLAS_NUM_THREADS", Value: threads},
			{Key: "HOME", Value: "/tmp"}, {Key: "TMPDIR", Value: "/tmp"}}, storage.env()), storage.Bucket, prefix)
		key, err := d.Fernet.Decrypt(storage.SecretEnc)
		if err != nil {
			return nil, err
		}
		secret := pyjson.Object{{Key: "AWS_ACCESS_KEY_ID", Value: storage.AccessKey}, {Key: "AWS_SECRET_ACCESS_KEY", Value: key}}
		labels := pyjson.Object{{Key: "app.kubernetes.io/managed-by", Value: "tabularia"}, {Key: "app.kubernetes.io/component", Value: "models"},
			{Key: labelEnv, Value: fmt.Sprint(s.img.EnvironmentID)}, {Key: labelModelGroup, Value: s.group}, {Key: labelModel, Value: bayescontract.Model},
			{Key: labelScriptRun, Value: fmt.Sprint(childID)}, {Key: labelOrchestration, Value: fmt.Sprint(runID)},
			{Key: "tabularia.io/stage", Value: "run"}}
		offline := offlineNetwork(ctx, storage, d.Log)
		js := d.jobSettings()
		if js.logMaxBytes <= 0 { // the runner's refusal is in the log's last lines
			js.logMaxBytes = 256 * 1024
		}
		manifest := func(name string, attempt int64) pyjson.Object {
			return bayesJobManifest(js, name, s.img, tmp, merge(base, pyjson.Object{{Key: "TABULARIA_ATTEMPT", Value: fmt.Sprint(attempt)}}),
				labels, step.timeout, itemList, name+"-code", name+"-env", offline)
		}
		// an attempt's files are deleted before the next one: never its input, nor the binary it fetches
		attemptFiles := slices.DeleteFunc(slices.Clone(written[1:]), func(k string) bool { return k == binaryKey })
		ok, refused, reason, err := q.modelAttempts(ctx, r.user, child, &step, runID, storage, attemptFiles, manifest, codeData, secret, ns, js)
		if err != nil {
			return nil, err
		}
		if refused != "" {
			return nil, &scriptFailed{fmt.Sprintf("%s: %s", s.what(), refused)}
		}
		if !ok {
			return nil, &scriptFailed{fmt.Sprintf("%s failed: %s", s.what(), reason)}
		}
		return q.importModel(ctx, r, &step, child, storage, resultsKey, outKey, artifacts, engineResultsKey, engineOutKey, writeOutput)
	}()
	if err != nil {
		q.logFailedModel(ctx, r, s.modelStep, child, err)
	}
	return q.closeChild(ctx, child, out, err, s.what())
}
