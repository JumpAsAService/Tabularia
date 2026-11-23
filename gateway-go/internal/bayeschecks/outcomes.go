// Package bayeschecks is the extension predictive-checks gives every stored program
// (docs/specs/bayes-predictive-checks.md): the posterior and prior predictive checks of the first
// likelihoods (statistics, bin counts and unit sums a draw, the replicated rows on request), the
// prior predictive's ancestral draws of the parameters (prior_draw) and one ancestral draw a draw of
// each quantity node (prior_marginal), truncated priors drawn by inversion. It writes no text: every
// declaration and statement is a typed value of bayescodegen's extension point, which checks it.
//
// One program serves every run, chosen by data the runner writes («The runs»): the rows of the
// posterior's and of the prior's paths, the flag of the statistics, the bins, the unit slots, the
// replicated rows. Nothing is drawn per row except on the rows the runner names.
package bayeschecks

import (
	"sort"
	"strconv"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/bayescatalog"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/bayesexpr"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/bayesgraph"
)

// The caps of the checks (the spec's «Decisions» and «The runner's side»), and bayes.checks' format.
const (
	Format       = 1
	MaxOutcomes  = 3    // max_check_outcomes: the first likelihoods in Order
	MaxUnits     = 8    // max_check_units: the units of an outcome's group plate with the most rows
	ValueBins    = 40   // the bins of a replicated outcome's density
	StatBins     = 30   // the bins of a statistic's histogram
	DensityDraws = 20   // the replicated draws whose densities are drawn
	PPCDraws     = 1000 // bayes_ppc_draws: the thinned draws of the posterior checks
)

// Checks is task.json's bayes.checks (the contract's place): the constants travel in the task, so
// the runner never guesses them; which columns are which is read from the manifests.
type Checks struct {
	Format       int  `json:"format"`
	On           bool `json:"on"`
	Outcomes     int  `json:"outcomes"`
	ValueBins    int  `json:"value_bins"`
	StatBins     int  `json:"stat_bins"`
	DensityDraws int  `json:"density_draws"`
	MaxUnits     int  `json:"max_units"`
	PPCDraws     int  `json:"ppc_draws"`
}

// TaskChecks is bayes.checks with this module's constants.
func TaskChecks(on bool) Checks {
	return Checks{Format: Format, On: on, Outcomes: MaxOutcomes, ValueBins: ValueBins, StatBins: StatBins,
		DensityDraws: DensityDraws, MaxUnits: MaxUnits, PPCDraws: PPCDraws}
}

// Kinds of an outcome, by its likelihood's outcome support.
const (
	KindContinuous = "continuous" // normal
	KindCounts     = "counts"     // poisson_log, neg_binomial_2_log, binomial_logit
	KindBinary     = "binary"     // bernoulli_logit
)

// Statistics of an outcome by kind, in the order they are written. q10, q50 and q90 are the type 7
// quantiles Stan's quantile computes; zeros is the number of replicated values at 0.
var Statistics = map[string][]string{
	KindContinuous: {"mean", "sd", "min", "max", "q10", "q50", "q90"},
	KindCounts:     {"mean", "sd", "max", "q50", "q90", "zeros"},
	KindBinary:     {"mean"},
}

// quantileOf is the probability of a quantile statistic.
var quantileOf = map[string]float64{"q10": 0.1, "q50": 0.5, "q90": 0.9}

// Reasons why a model has no prior predictive.
const (
	// NoQuantile: a prior is truncated inside its support and Stan has no quantile for it (gamma,
	// inv_gamma): it has no ancestral draw.
	NoQuantile = "truncated_without_quantile"
	// NoStructure: a quantity the likelihoods read has no ancestral draw this extension writes (a
	// state whose first value's or scale's arguments vary by row, a category's coefficients read
	// by a prior, a single value read from a plate…).
	NoStructure = "structure"
)

// Outcome is a checked likelihood.
type Outcome struct {
	K        int    `json:"k"`        // 1…MaxOutcomes, the names' o<K>
	Node     string `json:"node"`     // the likelihood
	Observed string `json:"observed"` // its observed data node (what the checks' of and the data's node name)
	Dist     string `json:"dist"`
	Kind     string `json:"kind"`
	// UnitIndex is the group index (a data node on the rows) of the outcome's first group plate,
	// UnitPlate that plate; empty when the outcome reads no group plate.
	UnitIndex string `json:"unit_index,omitempty"`
	UnitPlate string `json:"unit_plate,omitempty"`
}

// Analysis is what the extension writes for a model, computed from the validated model alone.
type Analysis struct {
	Outcomes    []Outcome
	Likelihoods int // all of the model's: a note says when Outcomes leaves some out
	// PriorPredictive says whether the program can run the prior predictive; NoPrior is why not.
	PriorPredictive bool
	NoPrior         string
	// Marginals are the quantities with a prior_marginal, in Order (a node, "<node>.<component>",
	// "<node>.correlation"); the other quantity nodes have none.
	Marginals []string
	// marg and path: a node with a marginal, a node with an ancestral draw on the prior's path.
	marg, path map[string]bool
}

// model is the validated model with what the extension reads of it.
type model struct {
	m      *bayesgraph.Model
	nodes  map[string]*bayesgraph.Node
	plates map[string]bayesgraph.ModelPlate
	pos    map[string]int                  // a node's place in Order, from 1
	reads  map[string][]string             // a node's (or a component's) reads, no index
	sreads map[bayesgraph.SlotKey][]string // a slot's reads, no index
	keys   map[string][]bayesgraph.SlotKey // the slots of a node or a component, sorted
	tdata  map[string]bool                 // transformed nodes that read no parameter
}

func newModel(m *bayesgraph.Model) *model {
	x := &model{m: m, nodes: map[string]*bayesgraph.Node{}, plates: map[string]bayesgraph.ModelPlate{},
		pos: map[string]int{}, reads: map[string][]string{}, sreads: map[bayesgraph.SlotKey][]string{}, keys: map[string][]bayesgraph.SlotKey{}, tdata: map[string]bool{}}
	for i := range m.Graph.Nodes {
		if _, dup := x.nodes[m.Graph.Nodes[i].ID]; !dup {
			x.nodes[m.Graph.Nodes[i].ID] = &m.Graph.Nodes[i]
		}
	}
	for _, p := range m.Plates {
		x.plates[p.ID] = p
	}
	for i, id := range m.Order {
		x.pos[id] = i + 1
	}
	seen := map[[2]string]bool{}
	for _, e := range m.Edges {
		if e.Kind == bayesgraph.EdgeIndex {
			continue
		}
		x.sreads[e.Slot] = append(x.sreads[e.Slot], e.From)
		// a component's or a correlation's slot is read by it; every slot is read by its node
		for _, to := range []string{e.Slot.Node, e.To} {
			if !seen[[2]string{to, e.From}] {
				seen[[2]string{to, e.From}] = true
				x.reads[to] = append(x.reads[to], e.From)
			}
		}
	}
	for k := range m.Slots {
		x.keys[k.Node] = append(x.keys[k.Node], k)
	}
	for _, ks := range x.keys {
		sort.Slice(ks, func(i, j int) bool { return ks[i].Slot < ks[j].Slot })
	}
	for _, id := range m.Order {
		if n := x.nodes[id]; n != nil && n.Kind == "transformed" && !x.readsParameter(id, map[string]bool{}) {
			x.tdata[id] = true
		}
	}
	return x
}

// owner is the node of an id ("<node>.<component>" is the node's).
func owner(id string) string {
	for i := 0; i < len(id); i++ {
		if id[i] == '.' {
			return id[:i]
		}
	}
	return id
}

func (x *model) node(id string) *bayesgraph.Node { return x.nodes[owner(id)] }

// readsParameter reports a node that reads a parameter, a state or an effect, directly or through
// the transformed nodes it reads (the codegen's transformed data are the transformed nodes that do
// not).
func (x *model) readsParameter(id string, seen map[string]bool) bool {
	if seen[id] {
		return false
	}
	seen[id] = true
	for _, r := range x.reads[id] {
		switch n := x.node(r); {
		case n == nil:
		case n.Kind == "parameter" || n.Kind == "state" || n.Kind == "correlated_effects":
			return true
		case (n.Kind == "transformed" || n.Kind == "linear_predictor") && x.readsParameter(n.ID, seen):
			return true
		}
	}
	return false
}

// readsData reports a node that reads a data node, directly or through what it reads.
func (x *model) readsData(id string, seen map[string]bool) bool {
	if seen[id] {
		return false
	}
	seen[id] = true
	for _, r := range x.reads[id] {
		if n := x.node(r); n != nil && (n.Kind == "data" || x.readsData(r, seen)) {
			return true
		}
	}
	return false
}

// plateKind is the kind of a node's plate ("" for a single value).
func (x *model) plateKind(n *bayesgraph.Node) string {
	if n.Plate == nil {
		return ""
	}
	return x.plates[*n.Plate].Kind
}

// local reports a value per row (a transformed node or a linear predictor on the rows): the
// codegen rebuilds it on the rows a value is asked on.
func (x *model) local(id string) bool {
	n := x.node(id)
	return n != nil && (n.Kind == "transformed" || n.Kind == "linear_predictor") && !x.tdata[id] &&
		x.plateKind(n) == bayesgraph.PlateRows
}

// quantity reports a node the program writes as a quantity that an ancestral draw may stand for: a
// parameter, a transformed node or a linear predictor single or on a group plate that reads a
// parameter, a state.
func (x *model) quantity(id string) bool {
	n := x.node(id)
	if n == nil {
		return false
	}
	switch n.Kind {
	case "parameter", "state":
		return true
	case "transformed", "linear_predictor":
		k := x.plateKind(n)
		return !x.tdata[id] && (k == "" || k == bayesgraph.PlateGroup)
	}
	return false
}

// slotPlate is the plate a slot's value varies over ("" for a single value).
func (x *model) slotPlate(key bayesgraph.SlotKey) string {
	return string(x.m.Slots[key].Type.Plate)
}

// valueSlots are the slots of a transformed node's value or of a linear predictor's sum.
func (x *model) valueSlots(id string) []bayesgraph.SlotKey {
	return x.keys[id]
}

// singleValue reports a transformed node or a linear predictor whose value is one number for every
// unit of its plate (the codegen writes it as a single value), realValue one of reals.
func (x *model) singleValue(id string) bool {
	for _, k := range x.valueSlots(id) {
		if x.slotPlate(k) != "" {
			return false
		}
	}
	n := x.node(id)
	return n.Kind != "linear_predictor" || len(n.Terms) == 0
}

func (x *model) realValue(id string) bool {
	if x.node(id).Kind == "linear_predictor" {
		return true
	}
	t, ok := x.m.Slots[bayesgraph.SlotKey{Node: id, Slot: "value"}]
	return ok && t.Type.Elem == bayesexpr.ElemReal
}

// keyReads are what some slots read, no index.
func (x *model) keyReads(keys []bayesgraph.SlotKey) []string {
	var out []string
	for _, k := range keys {
		out = append(out, x.sreads[k]...)
	}
	return out
}

// Analyze is what the extension writes for a model: its checked outcomes, whether it can run the
// prior predictive, which quantities have a prior marginal. A model that is not valid has none.
func Analyze(m *bayesgraph.Model) Analysis {
	if m == nil || m.Graph == nil || !m.Valid() {
		return Analysis{}
	}
	return newModel(m).analyze()
}

func (x *model) analyze() Analysis {
	a := Analysis{marg: map[string]bool{}, path: map[string]bool{}}
	for _, id := range x.m.Order {
		n := x.nodes[id]
		if n == nil || n.Kind != "likelihood" {
			continue
		}
		a.Likelihoods++
		if len(a.Outcomes) == MaxOutcomes {
			continue
		}
		d, ok := bayescatalog.LookupDistribution(n.Dist)
		if !ok || d.Stan.RNG == "" {
			continue
		}
		o := Outcome{K: len(a.Outcomes) + 1, Node: id, Observed: n.Observed, Dist: n.Dist}
		switch d.Outcome {
		case bayescatalog.RealLine:
			o.Kind = KindContinuous
		case bayescatalog.Binary:
			o.Kind = KindBinary
		default:
			o.Kind = KindCounts
		}
		o.UnitIndex, o.UnitPlate = x.unitPlate(id)
		a.Outcomes = append(a.Outcomes, o)
	}

	// the marginals and the prior's path, in Order: a node has one when what it reads has one
	quantile := true
	for _, id := range x.m.Order {
		n := x.nodes[id]
		if n == nil {
			continue
		}
		switch n.Kind {
		case "parameter":
			if n.Prior == nil {
				continue
			}
			p := x.m.Params[id]
			d, ok := bayescatalog.LookupDistribution(n.Prior.Dist)
			drawable := ok && d.Stan.RNG != "" && (!p.Truncated || d.Stan.Quantile != "")
			if ok && p.Truncated && d.Stan.Quantile == "" {
				quantile = false
			}
			keys := x.distKeys(id, "prior.", d)
			kind := x.plateKind(n)
			a.marg[id] = drawable && x.hasSlots(keys) && x.margReads(x.reads[id], a.marg) && (kind != bayesgraph.PlateLevels || allSingle(x, keys))
			a.path[id] = drawable && x.hasSlots(keys) && x.pathReads(x.reads[id], a.path) &&
				(kind == "" && a.marg[id] || kind == bayesgraph.PlateGroup || kind == bayesgraph.PlateLevels && allSingle(x, keys))
		case "transformed", "linear_predictor":
			if !x.quantity(id) {
				continue
			}
			ok := x.realValue(id)
			a.marg[id] = ok && !x.readsData(id, map[string]bool{}) && x.margReads(x.reads[id], a.marg)
			a.path[id] = ok && x.pathReads(x.reads[id], a.path) && (n.Plate != nil || a.marg[id])
		case "correlated_effects":
			for _, c := range n.Components {
				cid := id + "." + c.ID
				keys := []bayesgraph.SlotKey{{Node: cid, Slot: "mean"}, {Node: cid, Slot: "scale"}}
				a.marg[cid] = x.hasSlots(keys) && x.margReads(x.reads[cid], a.marg)
				a.path[cid] = x.hasSlots(keys) && x.pathReads(x.reads[cid], a.path)
			}
			if n.Correlation != nil {
				a.marg[id+".correlation"] = true
			}
		case "state":
			if n.Init == nil {
				continue
			}
			d, ok := bayescatalog.LookupDistribution(n.Init.Dist)
			keys := x.distKeys(id, "init.", d)
			single := ok && d.Stan.RNG != "" && allSingle(x, keys)
			scale := []bayesgraph.SlotKey{{Node: id, Slot: "scale"}}
			a.marg[id] = single && x.margReads(x.keyReads(keys), a.marg)
			a.path[id] = single && allSingle(x, scale) && x.pathReads(x.reads[id], a.path)
		}
	}
	for _, id := range x.m.Order {
		n := x.nodes[id]
		if n == nil {
			continue
		}
		ids := []string{id}
		if n.Kind == "correlated_effects" {
			ids = nil
			for _, c := range n.Components {
				ids = append(ids, id+"."+c.ID)
			}
			if n.Correlation != nil {
				ids = append(ids, id+".correlation")
			}
		}
		for _, q := range ids {
			if a.marg[q] {
				a.Marginals = append(a.Marginals, q)
			}
		}
	}

	a.PriorPredictive = len(a.Outcomes) > 0
	switch {
	case !quantile:
		a.PriorPredictive, a.NoPrior = false, NoQuantile
	case len(a.Outcomes) > 0:
		for _, o := range a.Outcomes {
			if !x.outcomePath(o.Node, a.path, map[string]bool{}) {
				a.PriorPredictive, a.NoPrior = false, NoStructure
			}
		}
	}
	return a
}

// distKeys are the slots of a distribution's arguments under prefix.
func (x *model) distKeys(id, prefix string, d bayescatalog.Distribution) []bayesgraph.SlotKey {
	keys := make([]bayesgraph.SlotKey, len(d.Params))
	for i, p := range d.Params {
		keys[i] = bayesgraph.SlotKey{Node: id, Slot: prefix + p.Name}
	}
	return keys
}

func (x *model) hasSlots(keys []bayesgraph.SlotKey) bool {
	for _, k := range keys {
		if _, ok := x.m.Slots[k]; !ok {
			return false
		}
	}
	return true
}

func allSingle(x *model, keys []bayesgraph.SlotKey) bool {
	for _, k := range keys {
		if _, ok := x.m.Slots[k]; !ok || x.slotPlate(k) != "" {
			return false
		}
	}
	return true
}

// margReads reports a quantity whose reads all have a marginal: a single value read by a single
// value, a value over a group plate read by a value that varies (its representative vector), a
// constant transformed node; never data, a category's coefficients or a value per row.
func (x *model) margReads(reads []string, marg map[string]bool) bool {
	for _, r := range reads {
		n := x.node(r)
		switch {
		case n == nil:
			return false
		case x.tdata[r]:
			if x.readsData(r, map[string]bool{}) {
				return false
			}
		case !marg[r]:
			return false
		case x.plateKind(n) == bayesgraph.PlateLevels:
			return false
		}
	}
	return true
}

// pathReads reports a quantity whose reads all have an ancestral draw on the prior's path (or are
// data, transformed data); a category's coefficients are read only by values per row.
func (x *model) pathReads(reads []string, path map[string]bool) bool {
	for _, r := range reads {
		n := x.node(r)
		switch {
		case n == nil:
			return false
		case n.Kind == "data" || x.tdata[r]:
		case !path[r]:
			return false
		case x.plateKind(n) == bayesgraph.PlateLevels:
			return false
		}
	}
	return true
}

// outcomePath reports a likelihood (or a value per row it reads) whose every read has an ancestral
// draw on the prior's path.
func (x *model) outcomePath(id string, path, seen map[string]bool) bool {
	if seen[id] {
		return true
	}
	seen[id] = true
	for _, r := range x.reads[id] {
		n := x.node(r)
		switch {
		case n == nil:
			return false
		case n.Kind == "data" || x.tdata[r]:
		case x.local(r):
			if !x.outcomePath(r, path, seen) {
				return false
			}
		case !path[r]:
			return false
		}
	}
	return true
}

// unitPlate is the first group plate (in the model's plates' order) a likelihood reads through a
// group index on the rows, directly or through the values per row it reads: the group index and
// the plate.
func (x *model) unitPlate(lik string) (string, string) {
	indices := map[string]bool{}
	seen := map[string]bool{}
	var walk func(id string)
	walk = func(id string) {
		if seen[id] {
			return
		}
		seen[id] = true
		for _, e := range x.m.Edges {
			if e.To != id {
				continue
			}
			if e.Kind == bayesgraph.EdgeIndex {
				indices[e.From] = true
			} else if x.local(e.From) {
				walk(e.From)
			}
		}
	}
	walk(lik)
	for _, p := range x.m.Plates {
		if p.Kind != bayesgraph.PlateGroup {
			continue
		}
		var keys []string
		for id := range indices {
			if d := x.nodes[id]; d != nil && d.Kind == "data" && d.Type == "group_index" && d.Target == p.ID &&
				d.Plate != nil && x.plates[*d.Plate].Kind == bayesgraph.PlateRows {
				keys = append(keys, id)
			}
		}
		if len(keys) > 0 {
			sort.Strings(keys)
			return keys[0], p.ID
		}
	}
	return "", ""
}

func itoa(i int) string { return strconv.Itoa(i) }
