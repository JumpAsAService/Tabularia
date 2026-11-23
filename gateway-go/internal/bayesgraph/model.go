package bayesgraph

import (
	"sort"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/bayescatalog"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/bayesexpr"
)

// This file is the validated model (docs/specs/bayes-model-graph.md, «Validated model»): the graph
// with its expressions typed, its parameters' ranges, roles and forms, its dependencies and their
// order, what the flow node must bind and what the report writes on both scales. stan-codegen writes
// the program from it, prior-preview draws the parameters in Order, flow-node lists Bindings,
// graph-editor draws Edges and Findings.

// Roles of a parameter.
const (
	RolePrior      = "prior"
	RoleHyperprior = "hyperprior"
)

// Plate kinds of a ModelPlate.
const (
	PlateRows   = "rows"
	PlateGroup  = "group"
	PlateLevels = "levels"
)

// ModelPlate is a plate of the model: the rows, a group plate, or the implicit levels plate of a
// category data node.
type ModelPlate struct {
	ID    string
	Kind  string // PlateRows, PlateGroup, PlateLevels
	Label string // the plate's, or the category node's for a levels plate
	// Parent and Depth are a group plate's: its parent ("" for none) and the number of group plates
	// from the root down to it, itself included (a root group plate is 1). Zero for the others.
	Parent string
	Depth  int
	// Key is a group plate's key (a group_index data node); Category a levels plate's category node.
	Key      string
	Category string
	// NonCentred is a group plate's form (the default).
	NonCentred bool
	// Series is the rows plate's, when the rows are a time series.
	Series *Series
}

// StateInfo is what a state needs beyond its slots: whether it is written in the non-centred form
// (its form is not "centred" and its init is location-scale: otherwise it stays centred).
type StateInfo struct {
	NonCentred bool
}

// EffectsInfo is what a correlated-effects node needs beyond its slots: the number of effects per
// unit and the form of its plate (the multivariate normal is non-centred when the plate is).
type EffectsInfo struct {
	Plate      string
	Components int
	NonCentred bool
}

// Binding is a data node the flow node binds to a column.
type Binding struct {
	Node        string
	Name        string // "" when the node has none
	Label       string
	Type        string // real, int, group_index, category, time
	Support     string // of an int: any, nonnegative, binary
	Plate       string
	Target      string // of a group_index: the plate it indexes
	Standardize bool   // real predictors standardized on the rows of the run
	Observed    bool   // a likelihood observes it
}

// ScalePair marks a coefficient reported on both scales: that of the standardized data, which the
// program samples, and on the column's own scale (coef / sd). Intercept is the pair of a linear
// predictor's intercept, which is shifted by the sum of coef × mean / sd (Term is -1 and Data
// empty); it exists for every linear predictor that has a standardized term, with or without an
// intercept slot (without one the intercept is zero on the standardized scale).
type ScalePair struct {
	Node      string // the linear predictor
	Term      int
	Data      string
	Intercept bool
}

// Model is the validated model. Its other fields are complete only when no finding is an error
// (Valid): with errors it holds what could be read (the order of a cyclic graph ends with the nodes
// of the cycles, a slot that does not type is missing from Slots), which is what the browser's
// previews and the editor's canvas need; no program is written from it.
type Model struct {
	Graph    *Graph
	Plates   []ModelPlate          // rows, the group plates by depth then id, the levels plates by their category's id
	Order    []string              // every node, a topological order (what a node reads comes first), ties by id
	Slots    map[SlotKey]TypedSlot // every slot that typed: its expression or ref, context plate, type, interval
	Params   map[string]Param      // by parameter id: range, truncation, role, form
	States   map[string]StateInfo  // by state id
	Effects  map[string]EffectsInfo
	Edges    []Edge // derived: who reads whom, sorted by (to, slot, from)
	Lifts    []bayesexpr.Lift
	Lags     []int // the distinct lags the series needs, ascending
	Bindings []Binding
	Pairs    []ScalePair
	Findings []Finding
}

// Valid reports that the model has no error finding: a program can be written from it.
func (m *Model) Valid() bool {
	for _, f := range m.Findings {
		if f.Severity == Error {
			return false
		}
	}
	return true
}

// BuildModel validates the graph and gathers what the other modules read. It never returns nil.
// When the structure has graph.id or graph.rows findings, the ids or the rows plate cannot be
// trusted, and the model holds the findings alone.
func BuildModel(g *Graph) *Model {
	v := validate(g)
	m := &Model{Graph: g, Findings: v.out}
	if v.env == nil {
		return m
	}
	m.Slots = v.slots
	m.Edges = v.deps.edges
	m.Order = topological(v.sortedIDs(), v.deps.reads)
	m.Plates = v.modelPlates()
	m.Params = v.roles()
	m.States, m.Effects = v.stateAndEffects()
	m.Lifts, m.Lags = v.liftsAndLags()
	m.Bindings = v.bindings()
	m.Pairs = v.pairs()
	return m
}

// modelPlates: the rows, the group plates by depth then id, the levels plates by their category's
// id.
func (v *validation) modelPlates() []ModelPlate {
	byID := map[string]*Plate{}
	for i := range v.g.Plates {
		if _, dup := byID[v.g.Plates[i].ID]; !dup {
			byID[v.g.Plates[i].ID] = &v.g.Plates[i]
		}
	}
	depth := func(p *Plate) int {
		d, seen := 1, map[string]bool{p.ID: true}
		for cur := p; cur.Parent != nil; {
			next, ok := byID[*cur.Parent]
			if !ok || next.Kind != kindGrp || seen[next.ID] {
				break // graph.plate_parent says so
			}
			seen[next.ID] = true
			d++
			cur = next
		}
		return d
	}
	var rows, groups, levels []ModelPlate
	ids := make([]string, 0, len(byID))
	for id := range byID {
		ids = append(ids, id)
	}
	sort.Strings(ids)
	for _, id := range ids {
		p := byID[id]
		switch p.Kind {
		case kindRows:
			rows = append(rows, ModelPlate{ID: p.ID, Kind: PlateRows, Label: p.Label, Series: p.Series})
		case kindGrp:
			mp := ModelPlate{ID: p.ID, Kind: PlateGroup, Label: p.Label, Depth: depth(p), Key: p.Key, NonCentred: nonCentredForm(p.Form)}
			if p.Parent != nil {
				mp.Parent = *p.Parent
			}
			groups = append(groups, mp)
		}
	}
	sort.SliceStable(groups, func(i, j int) bool {
		if groups[i].Depth != groups[j].Depth {
			return groups[i].Depth < groups[j].Depth
		}
		return groups[i].ID < groups[j].ID
	})
	for _, n := range v.nodeList() {
		if n.Kind == "data" && n.Type == "category" {
			levels = append(levels, ModelPlate{ID: LevelsPlate(n.ID), Kind: PlateLevels, Label: n.Label, Category: n.ID})
		}
	}
	return append(append(rows, groups...), levels...)
}

// roles: the parameters' Params with their role. A parameter whose value feeds another prior (a
// parameter's prior argument, a state's init or scale, a component's mean or scale), directly or
// through transformed and linear-predictor nodes, is a hyperprior; the others are priors.
func (v *validation) roles() map[string]Param {
	hyper := map[string]bool{}
	var queue []string
	seen := map[string]bool{}
	push := func(id string) {
		id = ownerOf(id)
		if !seen[id] {
			seen[id] = true
			queue = append(queue, id)
		}
	}
	for _, s := range v.specs {
		if !s.prior {
			continue
		}
		r := v.readSets(s)
		for _, ids := range [][]string{r.plain, r.lag, r.index} {
			for _, id := range ids {
				push(id)
			}
		}
	}
	for len(queue) > 0 {
		id := queue[0]
		queue = queue[1:]
		n, ok := v.env.nodes[id]
		if !ok {
			continue
		}
		switch n.Kind {
		case "parameter":
			hyper[id] = true
		case "transformed", "linear_predictor":
			for _, w := range v.deps.reads[id] {
				push(w)
			}
		}
	}
	out := make(map[string]Param, len(v.params))
	for id, p := range v.params {
		p.Role = RolePrior
		if hyper[id] {
			p.Role = RoleHyperprior
		}
		out[id] = p
	}
	return out
}

// stateAndEffects: the form of each state and of each correlated-effects node.
func (v *validation) stateAndEffects() (map[string]StateInfo, map[string]EffectsInfo) {
	states, effects := map[string]StateInfo{}, map[string]EffectsInfo{}
	groupForm := map[string]string{}
	for _, p := range v.g.Plates {
		if p.Kind == kindGrp {
			if _, dup := groupForm[p.ID]; !dup {
				groupForm[p.ID] = p.Form
			}
		}
	}
	for _, n := range v.nodeList() {
		switch n.Kind {
		case "state":
			nc := false
			if n.Init != nil && nonCentredForm(n.Form) {
				if d, ok := bayescatalog.LookupDistribution(n.Init.Dist); ok && d.HasUse(bayescatalog.Prior) && d.LocationScale != nil {
					nc = true
				}
			}
			states[n.ID] = StateInfo{NonCentred: nc}
		case "correlated_effects":
			effects[n.ID] = EffectsInfo{Plate: string(plateOf(n)), Components: len(n.Components),
				NonCentred: nonCentredForm(groupForm[string(plateOf(n))])}
		}
	}
	return states, effects
}

// liftsAndLags: the group indices lifted to a plate (the data arrays the runner builds) and the lags
// the series needs, each distinct across all the slots.
func (v *validation) liftsAndLags() ([]bayesexpr.Lift, []int) {
	lifts := map[bayesexpr.Lift]bool{}
	lags := map[int]bool{}
	for _, t := range v.slots {
		if t.Expr == nil {
			continue
		}
		for _, l := range t.Expr.Lifts {
			lifts[l] = true
		}
		for _, k := range t.Expr.Lags {
			lags[k] = true
		}
	}
	var ls []bayesexpr.Lift
	for l := range lifts {
		ls = append(ls, l)
	}
	sort.Slice(ls, func(i, j int) bool {
		a, b := ls[i], ls[j]
		switch {
		case a.ID != b.ID:
			return a.ID < b.ID
		case a.Context != b.Context:
			return a.Context < b.Context
		}
		return a.Rows < b.Rows
	})
	var ks []int
	for k := range lags {
		ks = append(ks, k)
	}
	sort.Ints(ks)
	return ls, ks
}

// bindings: the data nodes the flow node binds, by id.
func (v *validation) bindings() []Binding {
	observed := map[string]bool{}
	for _, n := range v.nodeList() {
		if n.Kind == "likelihood" {
			observed[n.Observed] = true
		}
	}
	var out []Binding
	for _, n := range v.nodeList() {
		if n.Kind != "data" {
			continue
		}
		b := Binding{Node: n.ID, Label: n.Label, Type: n.Type, Support: n.Support, Plate: string(plateOf(n)),
			Target: n.Target, Standardize: n.Standardize && n.Type == "real", Observed: observed[n.ID]}
		if n.Name != nil {
			b.Name = *n.Name
		}
		out = append(out, b)
	}
	return out
}

// pairs: a coefficient on both scales for each term of a linear predictor whose data is a
// standardized real, and the intercept's.
func (v *validation) pairs() []ScalePair {
	var out []ScalePair
	for _, n := range v.nodeList() {
		if n.Kind != "linear_predictor" {
			continue
		}
		standardized := false
		for i, t := range n.Terms {
			if d, ok := v.env.nodes[t.Data]; ok && d.Kind == "data" && d.Type == "real" && d.Standardize {
				out = append(out, ScalePair{Node: n.ID, Term: i, Data: d.ID})
				standardized = true
			}
		}
		if standardized {
			out = append(out, ScalePair{Node: n.ID, Term: -1, Intercept: true})
		}
	}
	return out
}
