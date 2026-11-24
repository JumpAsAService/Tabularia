// Package bayescontract is the protocol between the gateway and the runner of the group `bayes` (docs/specs/bayes-contract.md):
// the `bayes` object of task.json, the program's manifests, the columns of the outputs and of the draws parquet (always long),
// the rows of the ten-column table with their caps, the refusals and the card's `bayes` object. stan-codegen writes the
// manifests, stan-compile and flow-node write the task, bayes-mlflow reads the card; the runner (Python) reads the same JSON,
// and testdata/ holds the examples and the declaration both sides test against.
package bayescontract

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"math"
	"regexp"
	"slices"
)

// The group, its model and the protocol (tab_protocol's: task.json's common keys are modelgroups.Task's).
const (
	Group    = "bayes"
	Model    = "bayes_model"
	Protocol = 1
)

// Modes of the runner (task.json's `mode`). tab_protocol.MODES is fit, predict, selftest: the group has its own dispatcher.
const (
	ModeCompile         = "compile"
	ModePriorPredictive = "prior_predictive"
	ModeSample          = "sample"
	ModeSelftest        = "selftest"
)

// Modes are the four modes, in the spec's order.
var Modes = []string{ModeCompile, ModePriorPredictive, ModeSample, ModeSelftest}

// Outputs of a sampling step (task.json's `output`): the summary by default, the draws (long) or the replicated data on
// request, or the ten columns alone (answer 14). The prior predictive writes the ten columns alone.
const (
	OutputSummary    = "summary"
	OutputDraws      = "draws"
	OutputReplicated = "replicated"
	OutputResults    = "results"
)

// Outputs are the outputs of the mode sample, the default first.
var Outputs = []string{OutputSummary, OutputDraws, OutputReplicated, OutputResults}

// Task is task.json's `bayes` object: what a Job of the group reads beyond the common keys. The compile mode reads Model alone
// (Sampler nil, no manifest); prior_predictive and sample read everything; selftest has no `bayes` object. Labels and units
// travel as data (Mapping, Plates), never in a program or in CmdStan's arguments.
type Task struct {
	Model      ModelRef       `json:"model"`
	Sampler    *Sampler       `json:"sampler"`
	Plates     []Plate        `json:"plates"`
	Mapping    []MappingEntry `json:"mapping"`
	Data       []DataEntry    `json:"data"`
	Quantities []Quantity     `json:"quantities"`
	// Checks is what predictive-checks asks the runner to accumulate (its spec): null, or an object it defines.
	Checks json.RawMessage `json:"checks"`
	// Replicated is the thinning of the replicated output: set only when the output is `replicated`.
	Replicated *Replicated `json:"replicated"`
	// FiniteVariance is the `of` of every prior_marginal whose prior has a finite variance (bayeschecks.FiniteVariance on the
	// stored graph, written by flow-node): nil, absent, when the task does not say (every contraction NULL with its note); an
	// empty list when none has (omitzero writes it).
	FiniteVariance []string `json:"finite_variance,omitzero"`
	// ScalePairs are the original-scale pairs of model-graph's Model.Pairs as Stan identifiers (bayeschecks.ScalePairs, written
	// by flow-node from the stored graph and manifests); absent when there is none.
	ScalePairs []ScalePair `json:"scale_pairs,omitempty"`
	// Series is the rows' series when the stored graph's rows are one (flow-node writes it from the graph and the binding):
	// the runner checks the rows' order with it; absent when the model has no series.
	Series *Series `json:"series,omitempty"`
}

// Series names what the data entries of a series do not carry: the group plate whose units are the series (By, null for
// one series) and the input column of the series' time (Time, null when unchecked). The gateway sorts the input by the
// group index's column and the time before the Job; the runner only checks the order.
type Series struct {
	By   *string `json:"by"`
	Time *string `json:"time"`
}

// ScalePair is one pair of the original scale: a term (a coefficient and its standardized data) or a linear predictor's
// intercept, exactly one of the two («The original scale» of bayes-runner.md).
type ScalePair struct {
	Term      *TermPair
	Intercept *InterceptPair
}

// TermPair is a term of a linear predictor whose data is a standardized real: the coefficient's quantity (a single value) and
// the data's Stan name. On the column's scale the coefficient is coef / sd.
type TermPair struct {
	Coef string `json:"coef"`
	Data string `json:"data"`
}

// InterceptPair is a linear predictor's intercept: its quantity (nil when the predictor has no intercept slot, 0 on the
// standardized scale; a quantity over a group plate has each element shifted), the coefficients of its standardized terms
// (LP) and their data, in the same order. On the columns' scale the intercept is intercept − Σ coef × mean / sd.
type InterceptPair struct {
	Intercept *string  `json:"intercept"`
	LP        []string `json:"lp"`
	Data      []string `json:"data"`
}

// MarshalJSON writes {"coef", "data"} for a term, {"intercept", "lp", "data"} for an intercept.
func (p ScalePair) MarshalJSON() ([]byte, error) {
	switch {
	case p.Term != nil && p.Intercept == nil:
		return json.Marshal(p.Term)
	case p.Intercept != nil && p.Term == nil:
		return json.Marshal(p.Intercept)
	}
	return nil, errors.New("a scale pair: a term or an intercept")
}

// UnmarshalJSON reads a term ({"coef", "data"}) or an intercept ({"intercept", "lp", "data"}).
func (p *ScalePair) UnmarshalJSON(b []byte) error {
	var keys map[string]json.RawMessage
	if err := json.Unmarshal(b, &keys); err != nil {
		return errors.New("a scale pair: an object")
	}
	p.Term, p.Intercept = nil, nil
	if _, ok := keys["lp"]; ok {
		p.Intercept = &InterceptPair{}
		return json.Unmarshal(b, p.Intercept)
	}
	p.Term = &TermPair{}
	return json.Unmarshal(b, p.Term)
}

// MarshalJSON writes every list as a list, never null, so that the runner reads the same keys in every mode.
func (t Task) MarshalJSON() ([]byte, error) {
	type plain Task
	p := plain(t)
	if p.Plates == nil {
		p.Plates = []Plate{}
	}
	if p.Mapping == nil {
		p.Mapping = []MappingEntry{}
	}
	if p.Data == nil {
		p.Data = []DataEntry{}
	}
	if p.Quantities == nil {
		p.Quantities = []Quantity{}
	}
	return json.Marshal(p)
}

// ModelRef is the saved model's pinned version (answer 7) and its hashes. BinarySHA256 is the one stan-compile recorded from the
// compile Job's termination message: empty in the compile mode, which makes the binary.
type ModelRef struct {
	ID             int64  `json:"id"`
	Version        int    `json:"version"`
	SemanticHash   string `json:"semantic_hash"`
	ProgramSHA256  string `json:"program_sha256"`
	BinarySHA256   string `json:"binary_sha256,omitempty"`
	CodegenVersion int    `json:"codegen_version"`
	CmdStan        string `json:"cmdstan"`
	// ProgramBytes and Image are the card's program.bytes and binary.image (flow-node writes them; the runner copies them).
	ProgramBytes int64  `json:"program_bytes,omitempty"`
	Image        string `json:"image,omitempty"`
}

// Sampler is the sampler's settings (answer 9): chains, warmup, draws and seed in view; AdaptDelta, MaxTreedepth and Init under
// «Advanced»; Refresh is the runner's (how often CmdStan prints its progress). The prior predictive runs fixed_param: no
// warm-up, and AdaptDelta and MaxTreedepth unread.
type Sampler struct {
	Chains       int     `json:"chains"`
	Warmup       int     `json:"warmup"`
	Draws        int     `json:"draws"`
	Seed         int64   `json:"seed"`
	AdaptDelta   float64 `json:"adapt_delta"`
	MaxTreedepth int     `json:"max_treedepth"`
	Init         *Init   `json:"init"`
	Refresh      int     `json:"refresh"`
}

// Init is the sampler's initial values: nil (JSON null) for CmdStan's default, a Radius (init=<radius>), or Values per Stan
// identifier of a quantity the parameters block declares (a number, or a list of numbers in CmdStan's order), which the runner
// writes as init.json.
type Init struct {
	Radius *float64
	Values map[string]InitValue
}

// InitValue is one variable's initial value: a single number, or a list.
type InitValue struct {
	Number *float64
	List   []float64
}

// MarshalJSON writes a number or an object.
func (i Init) MarshalJSON() ([]byte, error) {
	if i.Radius != nil {
		return json.Marshal(*i.Radius)
	}
	return json.Marshal(i.Values)
}

// UnmarshalJSON reads a number or an object.
func (i *Init) UnmarshalJSON(b []byte) error {
	b = bytes.TrimSpace(b)
	if len(b) > 0 && b[0] == '{' {
		i.Radius = nil
		return json.Unmarshal(b, &i.Values)
	}
	var r float64
	if err := json.Unmarshal(b, &r); err != nil {
		return errors.New("init: a number or an object of values")
	}
	i.Radius, i.Values = &r, nil
	return nil
}

// MarshalJSON writes a number or a list.
func (v InitValue) MarshalJSON() ([]byte, error) {
	if v.Number != nil {
		return json.Marshal(*v.Number)
	}
	if v.List == nil {
		return []byte("[]"), nil
	}
	return json.Marshal(v.List)
}

// UnmarshalJSON reads a number or a list of numbers.
func (v *InitValue) UnmarshalJSON(b []byte) error {
	b = bytes.TrimSpace(b)
	if len(b) > 0 && b[0] == '[' {
		v.Number = nil
		return json.Unmarshal(b, &v.List)
	}
	var x float64
	if err := json.Unmarshal(b, &x); err != nil {
		return errors.New("an initial value: a number or a list of numbers")
	}
	v.Number, v.List = &x, nil
	return nil
}

// Replicated is the thinning of the replicated output: the draws kept per chain, evenly spaced, the last included.
type Replicated struct {
	DrawsPerChain int `json:"draws_per_chain"`
}

// Plate is a plate the manifests name, with its label (data): the rows plate `rows`, a group plate (its id), a category's
// levels plate (`levels:<category>`). The `quantity` section's `plate_label` is this label.
type Plate struct {
	ID    string `json:"id"`
	Kind  string `json:"kind"`
	Label string `json:"label"`
}

// Kinds of a plate (model-graph's).
const (
	PlateRows   = "rows"
	PlateGroup  = "group"
	PlateLevels = "levels"
)

// LevelsPrefix starts the id of a category's levels plate.
const LevelsPrefix = "levels:"

// Ranges of the sampler's settings (answer 9; the node shows them, the gateway and the runner check them).
const (
	MinChains, MaxChains             = 1, 8
	MinWarmup, MaxWarmup             = 0, 10_000
	MinDraws, MaxDraws               = 1, 10_000
	MinMaxTreedepth, MaxMaxTreedepth = 1, 15
	MaxSeed                          = math.MaxUint32 // CmdStan's seed is an unsigned 32-bit integer
	MaxRefresh                       = 10_000
)

// Defaults of the sampler's settings.
const (
	DefaultChains        = 4
	DefaultWarmup        = 1000
	DefaultDraws         = 1000
	DefaultAdaptDelta    = 0.8
	DefaultMaxTreedepth  = 10
	DefaultRefresh       = 100
	DefaultDrawsPerChain = 50
)

var (
	sha256Hex  = regexp.MustCompile(`^[0-9a-f]{64}$`)
	cmdstanVer = regexp.MustCompile(`^[0-9]+\.[0-9]+\.[0-9]+$`)
)

// Check checks the task of a Job of the given mode and output against the contract: what the mode reads is there and in range,
// what it does not read is absent, and the manifests hold together (CheckManifests). It is the gateway's check before a Job
// starts; the runner checks again what it depends on and refuses `task_value` (a fault of Tabularia).
func (t *Task) Check(mode, output string) error {
	fail := func(format string, a ...any) error { return fmt.Errorf("bayes."+format, a...) }
	if t == nil {
		return errors.New("bayes: missing")
	}
	m := t.Model
	switch {
	case m.ID <= 0 || m.Version <= 0:
		return fail("model: an id and a version (%d, %d)", m.ID, m.Version)
	case !sha256Hex.MatchString(m.SemanticHash) || !sha256Hex.MatchString(m.ProgramSHA256):
		return fail("model: the semantic hash and the program's sha256 are 64 hexadecimal digits")
	case m.CodegenVersion <= 0:
		return fail("model.codegen_version: %d", m.CodegenVersion)
	case !cmdstanVer.MatchString(m.CmdStan):
		return fail("model.cmdstan: %q is not a version", m.CmdStan)
	case m.ProgramBytes < 0:
		return fail("model.program_bytes: %d", m.ProgramBytes)
	}
	switch mode {
	case ModeCompile:
		if m.BinarySHA256 != "" || t.Sampler != nil || len(t.Plates)+len(t.Mapping)+len(t.Data)+len(t.Quantities) > 0 ||
			!isNull(t.Checks) || t.Replicated != nil || t.FiniteVariance != nil || t.ScalePairs != nil || t.Series != nil {
			return fail("the compile mode reads the model alone")
		}
		if output != "" && output != OutputResults {
			return fail("the compile mode has no output (%q)", output)
		}
		return nil
	case ModePriorPredictive, ModeSample:
	default:
		return fmt.Errorf("bayes: no task in the mode %q", mode)
	}
	if !sha256Hex.MatchString(m.BinarySHA256) {
		return fail("model.binary_sha256: 64 hexadecimal digits")
	}
	if err := t.Sampler.check(mode); err != nil {
		return err
	}
	if mode == ModePriorPredictive && output != OutputResults {
		return fail("the prior predictive writes the ten columns alone (output %q)", output)
	}
	if mode == ModeSample && !slices.Contains(Outputs, output) {
		return fail("output %q", output)
	}
	if (output == OutputReplicated) != (t.Replicated != nil) {
		return fail("replicated: set exactly when the output is replicated")
	}
	if r := t.Replicated; r != nil && (r.DrawsPerChain < 1 || r.DrawsPerChain > t.Sampler.Draws) {
		return fail("replicated.draws_per_chain: %d is not in 1…%d", r.DrawsPerChain, t.Sampler.Draws)
	}
	if !isNull(t.Checks) && (len(t.Checks) == 0 || t.Checks[0] != '{' || !json.Valid(t.Checks)) {
		return fail("checks: null or an object")
	}
	if err := CheckManifests(t.Plates, t.Mapping, t.Data, t.Quantities); err != nil {
		return err
	}
	if err := t.checkScale(); err != nil {
		return err
	}
	if err := t.checkSeries(); err != nil {
		return err
	}
	return t.Sampler.Init.check(t.Quantities)
}

// checkSeries checks bayes.series: its `by` a group plate that has a group index, its `time` a column's name when given
// (bayes_data._plan reads it so; a model whose data hold no series' array reads neither).
func (t *Task) checkSeries() error {
	s := t.Series
	if s == nil {
		return nil
	}
	indexed := map[string]bool{}
	for _, d := range t.Data {
		if d.Kind == DataGroupIndex {
			indexed[d.Target] = true
		}
	}
	switch {
	case s.By != nil && !indexed[*s.By]:
		return fmt.Errorf("bayes.series.by: %q is not a group plate with a group index", *s.By)
	case s.Time != nil && *s.Time == "":
		return errors.New("bayes.series.time: a column's name, or null")
	}
	return nil
}

// checkScale checks finite_variance (quantities' nodes, each once) and scale_pairs (quantities and standardized reals of the
// manifests: a term's coefficient and an intercept's terms single values).
func (t *Task) checkScale() error {
	nodes, single, named := map[string]bool{}, map[string]bool{}, map[string]bool{}
	for _, q := range t.Quantities {
		if q.Role == RoleQuantity {
			nodes[q.Node], named[q.Stan] = true, true
			if len(q.Dims) == 0 {
				single[q.Stan] = true
			}
		}
	}
	standardized := map[string]bool{}
	for _, d := range t.Data {
		if d.Kind == DataReal && d.Standardize {
			standardized[d.Stan] = true
		}
	}
	seen := map[string]bool{}
	for _, of := range t.FiniteVariance {
		if !nodes[of] || seen[of] {
			return fmt.Errorf("bayes.finite_variance: %q is not a quantity's node, or is listed twice", of)
		}
		seen[of] = true
	}
	for i, p := range t.ScalePairs {
		fail := fmt.Errorf("bayes.scale_pairs[%d]: a term {coef, data} or an intercept {intercept, lp, data} of the manifests' quantities and standardized reals", i)
		switch {
		case (p.Term == nil) == (p.Intercept == nil):
			return fail
		case p.Term != nil:
			if !single[p.Term.Coef] || !standardized[p.Term.Data] {
				return fail
			}
		default:
			ip := p.Intercept
			if ip.Intercept != nil && !named[*ip.Intercept] || len(ip.LP) == 0 || len(ip.LP) != len(ip.Data) {
				return fail
			}
			for j := range ip.LP {
				if !single[ip.LP[j]] || !standardized[ip.Data[j]] {
					return fail
				}
			}
		}
	}
	return nil
}

func isNull(b json.RawMessage) bool { return len(b) == 0 || string(bytes.TrimSpace(b)) == "null" }

func (s *Sampler) check(mode string) error {
	fail := func(format string, a ...any) error { return fmt.Errorf("bayes.sampler."+format, a...) }
	switch {
	case s == nil:
		return errors.New("bayes.sampler: missing")
	case s.Chains < MinChains || s.Chains > MaxChains:
		return fail("chains: %d is not in %d…%d", s.Chains, MinChains, MaxChains)
	case s.Warmup < MinWarmup || s.Warmup > MaxWarmup:
		return fail("warmup: %d is not in %d…%d", s.Warmup, MinWarmup, MaxWarmup)
	case mode == ModePriorPredictive && s.Warmup != 0:
		return fail("warmup: the prior predictive draws without warm-up (%d)", s.Warmup)
	case s.Draws < MinDraws || s.Draws > MaxDraws:
		return fail("draws: %d is not in %d…%d", s.Draws, MinDraws, MaxDraws)
	case s.Seed < 0 || s.Seed > MaxSeed:
		return fail("seed: %d is not in 0…%d", s.Seed, int64(MaxSeed))
	case !(s.AdaptDelta > 0 && s.AdaptDelta < 1):
		return fail("adapt_delta: %v is not between 0 and 1", s.AdaptDelta)
	case s.MaxTreedepth < MinMaxTreedepth || s.MaxTreedepth > MaxMaxTreedepth:
		return fail("max_treedepth: %d is not in %d…%d", s.MaxTreedepth, MinMaxTreedepth, MaxMaxTreedepth)
	case s.Refresh < 1 || s.Refresh > MaxRefresh:
		return fail("refresh: %d is not in 1…%d", s.Refresh, MaxRefresh)
	}
	return nil
}

func (i *Init) check(quantities []Quantity) error {
	if i == nil {
		return nil
	}
	finite := func(x float64) bool { return !math.IsNaN(x) && !math.IsInf(x, 0) }
	if i.Radius != nil {
		if !finite(*i.Radius) || *i.Radius < 0 {
			return fmt.Errorf("bayes.sampler.init: the radius %v is not a number ≥ 0", *i.Radius)
		}
		return nil
	}
	if len(i.Values) == 0 {
		return errors.New("bayes.sampler.init: an empty object (null for CmdStan's default)")
	}
	for name, v := range i.Values {
		k := slices.IndexFunc(quantities, func(q Quantity) bool { return q.Stan == name && q.Role == RoleQuantity })
		if k < 0 {
			return fmt.Errorf("bayes.sampler.init: %q is not a quantity of the program", name)
		}
		if (v.Number == nil) == (v.List == nil) {
			return fmt.Errorf("bayes.sampler.init.%s: a number or a list", name)
		}
		if (v.Number != nil) != (len(quantities[k].Dims) == 0) {
			return fmt.Errorf("bayes.sampler.init.%s: a number for a single value, a list for a vector", name)
		}
		for _, x := range append(v.List, ptrOr(v.Number)...) {
			if !finite(x) {
				return fmt.Errorf("bayes.sampler.init.%s: %v is not a finite number", name, x)
			}
		}
	}
	return nil
}

func ptrOr(x *float64) []float64 {
	if x == nil {
		return nil
	}
	return []float64{*x}
}
