package bayeschecks

import (
	"math"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/bayescatalog"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/bayesexpr"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/bayesgraph"
)

// This file decides, from the stored graph, which prior marginals have a finite variance: the
// contraction 1 - Var_post/Var_prior is a number only for them (docs/specs/bayes-diagnostics.md,
// «Prior-to-posterior contraction»; docs/specs/bayes-runner.md, «The contraction and the prior's
// density»). flow-node writes the answer as task.json's bayes.finite_variance.
//
// A marginal has a finite variance when it is bounded (a parameter whose range is bounded: a
// truncation to a bounded range, or a bounded support such as the beta's), or when its
// distribution's rule (the catalogue's finite_variance) holds and its arguments allow it. The
// decision is prudent: a «yes» is a proof, a «no» may only mean «not proven» (the contraction is
// then NULL with its note, never an estimate of an infinite quantity).
//
//   - The rule: always, never, or above a bound on a constant argument (the Student-t's nu, the
//     inverse-gamma's alpha). That argument not a constant: not finite (a nu with mass at or
//     below 2 makes the mixture's variance infinite).
//   - Every other argument that is not a constant must be the location or the scale of a
//     location-scale distribution (normal, Student-t, Cauchy, the correlated effects' normal),
//     with a finite variance itself (X = loc + scale * Z: Var X is finite when Var loc, E[scale²]
//     and Var Z are). Any other argument that varies (a rate, a shape, the lognormal's mu or
//     sigma) is not finite: its effect on the variance is not bounded by the argument's variance
//     (an exponential whose rate has a gamma(1, 1) prior has an infinite variance).
//   - A value (an argument's expression, a transformed node's value, a linear predictor's
//     intercept) has a finite variance when it is bounded (its static interval), a constant, a
//     node that has one, or an affine combination of these: sums, differences, a negation, a
//     product by a constant, a quotient by a constant. A function, a power, a product or a quotient
//     of two values that vary is not finite unless bounded.
//   - A correlation's Ω[2,1] is in [-1, 1]: always finite.

// FiniteVariance returns the `of` of every prior_marginal of the model's program whose prior has a
// finite variance, in the order of Analysis.Marginals; an empty list (not nil) when none has. A
// model that is not valid has no marginal: nil.
func FiniteVariance(m *bayesgraph.Model) []string {
	if m == nil || m.Graph == nil || !m.Valid() {
		return nil
	}
	x := newModel(m)
	a := x.analyze()
	v := variance{x: x, finite: map[string]bool{}}
	for _, id := range m.Order {
		v.node(id)
	}
	out := []string{}
	for _, q := range a.Marginals {
		if v.finite[q] {
			out = append(out, q)
		}
	}
	return out
}

// variance holds the decision of each node (and component, and correlation) seen so far, in Order.
type variance struct {
	x      *model
	finite map[string]bool
}

func (v *variance) node(id string) {
	n := v.x.nodes[id]
	if n == nil {
		return
	}
	m := v.x.m
	switch n.Kind {
	case "parameter":
		if n.Prior == nil {
			return
		}
		if p, ok := m.Params[id]; ok && bounded(p.Range) {
			v.finite[id] = true
			return
		}
		v.finite[id] = v.dist(id, "prior.", n.Prior.Dist)
	case "state":
		// its marginal is its first value's, the init's draw
		if n.Init != nil {
			v.finite[id] = v.dist(id, "init.", n.Init.Dist)
		}
	case "transformed":
		v.finite[id] = v.slot(bayesgraph.SlotKey{Node: id, Slot: "value"})
	case "linear_predictor":
		// a linear predictor with a marginal has no term (its terms read data)
		v.finite[id] = len(n.Terms) == 0 && n.Intercept != nil && v.slot(bayesgraph.SlotKey{Node: id, Slot: "intercept"})
	case "correlated_effects":
		for _, c := range n.Components {
			cid := id + "." + c.ID
			v.finite[cid] = v.slot(bayesgraph.SlotKey{Node: cid, Slot: "mean"}) && v.slot(bayesgraph.SlotKey{Node: cid, Slot: "scale"})
		}
		if n.Correlation != nil {
			v.finite[id+".correlation"] = true
		}
	}
}

// dist is the rule of a distribution with the arguments of the node's slots under prefix.
func (v *variance) dist(id, prefix, name string) bool {
	d, ok := bayescatalog.LookupDistribution(name)
	if !ok {
		return false
	}
	rule := d.FiniteVariance
	if rule.Rule == bayescatalog.VarianceNever {
		return false
	}
	for _, p := range d.Params {
		t, ok := v.x.m.Slots[bayesgraph.SlotKey{Node: id, Slot: prefix + p.Name}]
		if !ok {
			return false
		}
		if rule.Rule == bayescatalog.VarianceAbove && p.Name == rule.Param {
			if c, ok := constantValue(t); !ok || !rule.Holds(c) {
				return false
			}
			continue
		}
		if v.constantSlot(t) {
			continue
		}
		ls := d.LocationScale
		if ls == nil || (p.Name != ls.Location && p.Name != ls.Scale) || !v.typed(t) {
			return false
		}
	}
	return rule.Rule == bayescatalog.VarianceAlways || rule.Rule == bayescatalog.VarianceAbove
}

// constantValue is a slot's value when it is one number: its constant, or its static interval
// when that is a single point (an expression of numbers such as 1 / 300).
func constantValue(t bayesgraph.TypedSlot) (float64, bool) {
	if t.Constant != nil {
		return *t.Constant, true
	}
	if i := t.Interval; bounded(i) && i.Lo == i.Hi {
		return i.Lo, true
	}
	return 0, false
}

// constantSlot reports a slot that does not vary.
func (v *variance) constantSlot(t bayesgraph.TypedSlot) bool {
	if _, ok := constantValue(t); ok {
		return true
	}
	if t.Expr != nil {
		return v.constant(t.Expr.Tree)
	}
	return t.Ref != "" && v.x.tdata[t.Ref] && !v.x.readsData(t.Ref, map[string]bool{})
}

// slot is a slot's value.
func (v *variance) slot(key bayesgraph.SlotKey) bool {
	t, ok := v.x.m.Slots[key]
	return ok && v.typed(t)
}

func (v *variance) typed(t bayesgraph.TypedSlot) bool {
	switch {
	case t.Constant != nil, bounded(t.Interval):
		return true
	case t.Expr != nil:
		return v.affine(t.Expr.Tree)
	case t.Ref != "":
		return v.read(t.Ref)
	}
	return false
}

// read is a node a value reads: one with a finite variance, or a constant (a transformed node that
// reads neither a parameter nor data).
func (v *variance) read(id string) bool {
	return v.finite[id] || v.x.tdata[id] && !v.x.readsData(id, map[string]bool{})
}

// affine reports a value with a finite variance: bounded, a constant, a node that has one, or an
// affine combination of these.
func (v *variance) affine(t *bayesexpr.Typed) bool {
	if t == nil || t.Node == nil {
		return false
	}
	if bounded(t.Interval()) {
		return true
	}
	switch t.Node.Kind {
	case bayesexpr.KindNum:
		return true
	case bayesexpr.KindName, bayesexpr.KindIndex:
		return t.Sym != nil && v.read(t.Sym.ID)
	case bayesexpr.KindNeg:
		return v.affine(t.Args[0])
	case bayesexpr.KindBinary:
		a, b := t.Args[0], t.Args[1]
		switch t.Node.Op {
		case "+", "-":
			return v.affine(a) && v.affine(b)
		case "*":
			return v.constant(a) && v.affine(b) || v.constant(b) && v.affine(a)
		case "/":
			return v.constant(b) && v.affine(a)
		}
	}
	return false
}

// constant reports a value that does not vary: numbers, and transformed nodes that read neither a
// parameter nor data, under any operation or call.
func (v *variance) constant(t *bayesexpr.Typed) bool {
	if t == nil || t.Node == nil {
		return false
	}
	switch t.Node.Kind {
	case bayesexpr.KindNum:
		return true
	case bayesexpr.KindName:
		return t.Sym != nil && v.x.tdata[t.Sym.ID] && !v.x.readsData(t.Sym.ID, map[string]bool{})
	case bayesexpr.KindNeg, bayesexpr.KindBinary, bayesexpr.KindCall:
		for _, a := range t.Args {
			if !v.constant(a) {
				return false
			}
		}
		return true
	}
	return false
}

// bounded reports an interval with two finite ends.
func bounded(i bayesexpr.Interval) bool {
	return !math.IsInf(i.Lo, 0) && !math.IsInf(i.Hi, 0) && !math.IsNaN(i.Lo) && !math.IsNaN(i.Hi)
}
