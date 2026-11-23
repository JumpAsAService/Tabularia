package bayesgraph

import (
	"math"
	"sort"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/bayescatalog"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/bayesexpr"
)

// This file is the validator's entry point and the rules about nodes, data, priors and bounds
// (docs/specs/bayes-model-graph.md, «Validation»): graph.node_plate, graph.data_type,
// graph.standardize, graph.prior, graph.bounds, with each parameter's range and truncation. The
// rules of the slots (graph.slot, expr.*, graph.slot_shape, graph.slot_interval, graph.slot_maybe,
// graph.constant, graph.prior_data) are in slots.go.

// SlotKey names a slot: the node it belongs to ("<node>.<component>" for a component's,
// "<node>.correlation" for the correlation's) and the slot ("prior.<arg>", "init.<arg>",
// "args.<arg>", "value", "intercept", "terms[<i>].coef", "scale", "mean", "eta").
type SlotKey struct {
	Node string
	Slot string
}

// Param is what the validator knows of a parameter whose prior and bounds have no finding: the
// prior's support, the range (the support ∩ the bounds), whether the prior is truncated (the range
// narrower than the support) and whether the program needs T[…] (truncated, and an argument of the
// prior is not a constant: rule 4 of the catalogue's «Bounds and truncation»).
//
// The bounds are open ends: every prior is continuous, so an end carries no mass, and Stan's
// transform for <lower=L> never reaches L. A range reduced to a point is therefore empty.
type Param struct {
	Dist              string
	Support           bayesexpr.Interval
	Range             bayesexpr.Interval
	Truncated         bool
	TruncateInProgram bool
}

// Validate checks a graph: CheckStructure first, then the rules of nodes, data, priors, bounds and
// slots. Every finding is reported, sorted by (node, slot, code); nothing is repaired. When the
// structure has graph.id or graph.rows findings, the ids or the rows plate cannot be trusted, and
// only the structural findings are returned.
func Validate(g *Graph) []Finding { return validate(g).out }

// validation is one run of the validator: its findings and what it learnt (the typed slots, the
// parameters), which the validated model is made of.
type validation struct {
	g      *Graph
	lim    bayescatalog.Limits
	env    *graphEnv
	specs  []*slotSpec
	slots  map[SlotKey]TypedSlot
	params map[string]Param
	out    []Finding
}

func validate(g *Graph) *validation {
	v := &validation{g: g, lim: bayescatalog.GetLimits(), slots: map[SlotKey]TypedSlot{}, params: map[string]Param{}}
	v.out = CheckStructure(g)
	for _, f := range v.out {
		if f.Code == "graph.id" || f.Code == "graph.rows" {
			return v
		}
	}
	v.env = newGraphEnv(g, nil)
	for _, n := range v.nodeList() {
		v.nodePlate(n)
		if n.Kind == "data" {
			v.dataType(n)
			v.standardize(n)
		}
	}
	v.collectSlots()
	ranges := map[string]bayesexpr.Interval{}
	for _, n := range v.nodeList() {
		if n.Kind == "parameter" {
			if p, ok := v.parameter(n); ok {
				v.params[n.ID] = p
				ranges[n.ID] = p.Range
			}
		}
	}
	v.env = newGraphEnv(g, ranges)
	v.typeSlots()
	v.priorData()
	SortFindings(v.out)
	return v
}

// nodeList is the graph's nodes, each id once (its first occurrence), by id.
func (v *validation) nodeList() []*Node {
	l := make([]*Node, 0, len(v.env.nodes))
	for _, n := range v.env.nodes {
		l = append(l, n)
	}
	sort.Slice(l, func(i, j int) bool { return l[i].ID < l[j].ID })
	return l
}

func (v *validation) add(sev Severity, code, node, slot string, args map[string]any) {
	v.out = append(v.out, Finding{Code: code, Severity: sev, Node: node, Slot: slot, Args: args})
}

func (v *validation) error(code, node, slot string, args map[string]any) {
	v.add(Error, code, node, slot, args)
}

// plateKinds are the plates each node kind may be on ("" a single value); a likelihood is on its
// observed node's plate.
var plateKinds = map[string][]string{
	"data":               {kindRows, kindGrp},
	"parameter":          {"", kindGrp, kindLevels},
	"transformed":        {"", kindRows, kindGrp},
	"linear_predictor":   {kindRows, kindGrp},
	"state":              {kindRows},
	"correlated_effects": {kindGrp},
}

// nodePlate: graph.node_plate, the node's plate exists and is allowed for its kind (table «Nodes»).
func (v *validation) nodePlate(n *Node) {
	p := plateOf(n)
	kind := ""
	if p != "" {
		ep, ok := v.env.plates[p]
		if !ok {
			v.error("graph.node_plate", n.ID, "plate", map[string]any{"reason": "unknown_plate", "plate": string(p)})
			return
		}
		kind = ep.kind
	}
	if n.Kind == "likelihood" {
		if obs, ok := v.env.nodes[n.Observed]; ok && obs.Kind == "data" && p != "" && p != plateOf(obs) {
			v.error("graph.node_plate", n.ID, "plate", map[string]any{"reason": "not_observed_plate", "plate": string(p), "observed": n.Observed})
		}
		return
	}
	allowed, known := plateKinds[n.Kind]
	if !known {
		return
	}
	for _, k := range allowed {
		if k == kind {
			return
		}
	}
	args := map[string]any{"reason": "not_allowed", "kind": n.Kind}
	if p != "" {
		args["plate"] = string(p)
	}
	v.error("graph.node_plate", n.ID, "plate", args)
}

var (
	dataTypes    = keys("real", "int", "group_index", "category", "time")
	dataSupports = keys("any", "nonnegative", "binary")
	rowsOnly     = keys("group_index", "category", "time")
)

// dataType: graph.data_type, a known type and support; group_index, category and time only on
// rows. (A data node on no plate, or on a levels plate, is graph.node_plate's.)
func (v *validation) dataType(n *Node) {
	if !dataTypes[n.Type] {
		v.error("graph.data_type", n.ID, "type", map[string]any{"reason": "unknown_type", "type": n.Type})
	}
	switch {
	case n.Type == "int" && n.Support == "":
		v.error("graph.data_type", n.ID, "support", map[string]any{"reason": "missing_support"})
	case n.Type == "int" && !dataSupports[n.Support]:
		v.error("graph.data_type", n.ID, "support", map[string]any{"reason": "unknown_support", "support": n.Support})
	case n.Type != "int" && n.Support != "":
		v.error("graph.data_type", n.ID, "support", map[string]any{"reason": "support_not_int", "support": n.Support})
	}
	if n.Target != "" && n.Type != "group_index" {
		v.error("graph.data_type", n.ID, "target", map[string]any{"reason": "target_not_group_index"})
	}
	if rowsOnly[n.Type] {
		if ep, ok := v.env.plates[plateOf(n)]; ok && ep.kind == kindGrp {
			v.error("graph.data_type", n.ID, "plate", map[string]any{"reason": "not_on_rows", "type": n.Type, "plate": string(plateOf(n))})
		}
	}
}

// standardize: graph.standardize, only on a real data node that no likelihood observes.
func (v *validation) standardize(n *Node) {
	if !n.Standardize {
		return
	}
	if n.Type != "real" {
		v.error("graph.standardize", n.ID, "standardize", map[string]any{"reason": "not_real", "type": n.Type})
		return
	}
	by := ""
	for _, l := range v.nodeList() {
		if l.Kind == "likelihood" && l.Observed == n.ID {
			by = l.ID
			break
		}
	}
	if by != "" {
		v.error("graph.standardize", n.ID, "standardize", map[string]any{"reason": "observed", "likelihood": by})
	}
}

// supportInterval is a prior's support as an interval: the real line, (0, ∞), (0, 1), and the
// uniform's (alpha, beta) when both are constants with alpha < beta. ok is false when it cannot be
// told (a uniform whose bounds are not constants or not ordered; a support that is not a set of
// reals).
func supportInterval(d bayescatalog.Distribution, consts map[string]float64) (bayesexpr.Interval, bool) {
	switch d.Support {
	case bayescatalog.RealLine, bayescatalog.PositiveRay, bayescatalog.UnitRange:
		return bayesexpr.NamedInterval(string(d.Support)), true
	case bayescatalog.Bounded:
		if len(d.Params) != 2 {
			return bayesexpr.Interval{}, false
		}
		a, okA := consts[d.Params[0].Name]
		b, okB := consts[d.Params[1].Name]
		if !okA || !okB || !(a < b) {
			return bayesexpr.Interval{}, false
		}
		return bayesexpr.Interval{Lo: a, Hi: b, LoOpen: true, HiOpen: true}, true
	}
	return bayesexpr.Interval{}, false
}

// intersect is a ∩ b; at an equal end, the end is open when either is.
func intersect(a, b bayesexpr.Interval) bayesexpr.Interval {
	r := a
	if b.Lo > r.Lo || (b.Lo == r.Lo && b.LoOpen) {
		r.Lo, r.LoOpen = b.Lo, b.LoOpen || (b.Lo == a.Lo && a.LoOpen)
	}
	if b.Hi < r.Hi || (b.Hi == r.Hi && b.HiOpen) {
		r.Hi, r.HiOpen = b.Hi, b.HiOpen || (b.Hi == a.Hi && a.HiOpen)
	}
	return r
}

// emptyInterval: no real is in it (a point with an open end included).
func emptyInterval(i bayesexpr.Interval) bool {
	return i.Lo > i.Hi || (i.Lo == i.Hi && (i.LoOpen || i.HiOpen))
}

func sameInterval(a, b bayesexpr.Interval) bool {
	return a.Lo == b.Lo && a.Hi == b.Hi && a.LoOpen == b.LoOpen && a.HiOpen == b.HiOpen
}

// boundsInterval is the bounds as an interval with open ends.
func boundsInterval(b *Bounds) bayesexpr.Interval {
	i := bayesexpr.Unbounded()
	if b == nil {
		return i
	}
	if b.Lower != nil {
		i.Lo, i.LoOpen = *b.Lower, true
	}
	if b.Upper != nil {
		i.Hi, i.HiOpen = *b.Upper, true
	}
	return i
}

// TruncationRange is the range a support and bounds make: the support ∩ the bounds (open ends),
// whether it is empty, and whether it is narrower than the support. The catalogue's rule 1 and 2.
func TruncationRange(support bayesexpr.Interval, b *Bounds) (r bayesexpr.Interval, empty, truncated bool) {
	r = intersect(support, boundsInterval(b))
	if emptyInterval(r) {
		return r, true, false
	}
	return r, false, !sameInterval(r, support)
}

// paramRange is the range of a parameter whose prior d has the support, cut by the bounds, and
// whether it is truncated; reason is graph.bounds' when there is none: "empty" (rule of
// model-graph), "not_truncatable" (rule 5: the bounds cut a support the distribution has no lcdf
// and lccdf for).
func paramRange(d bayescatalog.Distribution, support bayesexpr.Interval, b *Bounds) (r bayesexpr.Interval, truncated bool, reason string) {
	r, empty, truncated := TruncationRange(support, b)
	switch {
	case empty:
		return r, false, "empty"
	case truncated && !d.Truncatable():
		return r, false, "not_truncatable"
	}
	return r, truncated, ""
}

// parameter: graph.prior (a prior of the catalogue with use prior, not LKJ, with exactly its
// arguments), graph.bounds (finite, within max_abs_literal, ordered, a range not empty, a
// truncation the distribution allows), and the parameter's Param when none of them is found.
func (v *validation) parameter(n *Node) (Param, bool) {
	ok := v.prior(n)
	bok := v.boundsShape(n)
	if n.Prior == nil {
		return Param{}, false
	}
	d, known := bayescatalog.LookupDistribution(n.Prior.Dist)
	if !known || !d.HasUse(bayescatalog.Prior) || d.Support == bayescatalog.CholeskyCorrelation {
		return Param{}, false
	}
	consts := map[string]float64{}
	allConst := true
	for _, p := range d.Params {
		c, isConst := v.constantOf(SlotKey{n.ID, "prior." + p.Name})
		if isConst {
			consts[p.Name] = c
		} else {
			allConst = false
		}
	}
	support, sok := supportInterval(d, consts)
	if !sok {
		v.uniformOrder(n, d, consts)
		return Param{}, false
	}
	if !bok {
		return Param{}, false
	}
	r, truncated, reason := paramRange(d, support, n.Bounds)
	switch {
	case reason != "":
		v.error("graph.bounds", n.ID, "bounds", map[string]any{"reason": reason, "dist": d.ID})
		return Param{}, false
	case !ok:
		return Param{}, false
	}
	return Param{Dist: d.ID, Support: support, Range: r, Truncated: truncated, TruncateInProgram: truncated && !allConst}, true
}

// uniformOrder: a bounded support (the uniform) whose two constants are not ordered is
// graph.slot_interval on the second argument (it must be above the first).
func (v *validation) uniformOrder(n *Node, d bayescatalog.Distribution, consts map[string]float64) {
	if d.Support != bayescatalog.Bounded || len(d.Params) != 2 {
		return
	}
	a, okA := consts[d.Params[0].Name]
	b, okB := consts[d.Params[1].Name]
	if okA && okB && !(a < b) {
		v.error("graph.slot_interval", n.ID, "prior."+d.Params[1].Name, map[string]any{"need": "above_" + d.Params[0].Name})
	}
}

// prior: graph.prior. It reports and returns whether the prior is one the catalogue allows.
func (v *validation) prior(n *Node) bool {
	if n.Prior == nil {
		v.error("graph.prior", n.ID, "prior", map[string]any{"reason": "missing"})
		return false
	}
	d, known := bayescatalog.LookupDistribution(n.Prior.Dist)
	switch {
	case !known:
		v.error("graph.prior", n.ID, "prior", map[string]any{"reason": "unknown_dist", "dist": n.Prior.Dist})
		return false
	case d.Support == bayescatalog.CholeskyCorrelation:
		v.error("graph.prior", n.ID, "prior", map[string]any{"reason": "effects_only", "dist": d.ID})
		return false
	case !d.HasUse(bayescatalog.Prior):
		v.error("graph.prior", n.ID, "prior", map[string]any{"reason": "not_prior", "dist": d.ID})
		return false
	}
	ok := true
	want := map[string]bool{}
	for _, p := range d.Params {
		want[p.Name] = true
		if _, has := n.Prior.Args[p.Name]; !has {
			v.error("graph.prior", n.ID, "prior."+p.Name, map[string]any{"reason": "missing_arg", "dist": d.ID, "arg": p.Name})
			ok = false
		}
	}
	for arg := range n.Prior.Args {
		if !want[arg] {
			v.error("graph.prior", n.ID, "prior."+arg, map[string]any{"reason": "unknown_arg", "dist": d.ID, "arg": arg})
			ok = false
		}
	}
	return ok
}

// boundsShape: graph.bounds on the numbers themselves: finite, within max_abs_literal, lower <
// upper. It returns whether they are well formed.
func (v *validation) boundsShape(n *Node) bool {
	b := n.Bounds
	if b == nil {
		return true
	}
	ok := true
	one := func(slot string, x *float64) {
		switch {
		case x == nil:
		case math.IsNaN(*x) || math.IsInf(*x, 0):
			v.error("graph.bounds", n.ID, slot, map[string]any{"reason": "not_finite"})
			ok = false
		case math.Abs(*x) > v.lim.MaxAbsLiteral:
			v.error("graph.bounds", n.ID, slot, map[string]any{"reason": "too_large", "max": v.lim.MaxAbsLiteral})
			ok = false
		}
	}
	one("bounds.lower", b.Lower)
	one("bounds.upper", b.Upper)
	if ok && b.Lower != nil && b.Upper != nil && !(*b.Lower < *b.Upper) {
		v.error("graph.bounds", n.ID, "bounds", map[string]any{"reason": "order"})
		ok = false
	}
	return ok
}
