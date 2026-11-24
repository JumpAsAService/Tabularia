package bayeschecks

import (
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/bayescontract"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/bayesexpr"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/bayesgraph"
)

// ScalePairs are the model's original-scale pairs (model-graph's Model.Pairs) as the Stan
// identifiers of the stored program's manifests, for task.json's bayes.scale_pairs
// (docs/specs/bayes-runner.md, «The original scale»): for each term of a linear predictor whose
// data is a standardized real, its coefficient's quantity and the data; for the predictor's
// intercept, the intercept's quantity (nil without an intercept slot: 0 on the standardized scale)
// with every such term's coefficient and data, which shift it by Σ coef × mean / sd.
//
// A coefficient is written when its slot is one quantity of a single value (a reference, or an
// expression that is only its name); an intercept when its slot is one quantity, a single value or
// a quantity over a group plate taken at the row's unit (a_g[g]: each of its elements is shifted).
// Anything else (an expression, a constant) has no column of its own: the pair is left out, and so
// is the intercept's when one of its terms is. An empty list (not nil) when no pair remains.
func ScalePairs(m *bayesgraph.Model, data []bayescontract.DataEntry, quantities []bayescontract.Quantity) []bayescontract.ScalePair {
	out := []bayescontract.ScalePair{}
	if m == nil || m.Graph == nil || !m.Valid() {
		return out
	}
	quantity := func(node string, single bool) (string, bool) {
		for _, q := range quantities {
			if q.Role == bayescontract.RoleQuantity && q.Node == node && (!single || len(q.Dims) == 0) {
				return q.Stan, true
			}
		}
		return "", false
	}
	standardized := func(node string) (string, bool) {
		for _, d := range data {
			if d.Kind == bayescontract.DataReal && d.Standardize && d.Node == node {
				return d.Stan, true
			}
		}
		return "", false
	}
	// slotQuantity is the node a slot is, alone: a reference, a name, or (indexed) a name taken at a
	// group index.
	slotQuantity := func(key bayesgraph.SlotKey, indexed bool) (string, bool) {
		t, ok := m.Slots[key]
		switch {
		case !ok || t.Constant != nil:
			return "", false
		case t.Ref != "":
			return t.Ref, true
		case t.Expr == nil || t.Expr.Tree == nil || t.Expr.Tree.Sym == nil:
			return "", false
		}
		switch t.Expr.Tree.Node.Kind {
		case bayesexpr.KindName:
			return t.Expr.Tree.Sym.ID, true
		case bayesexpr.KindIndex:
			return t.Expr.Tree.Sym.ID, indexed
		}
		return "", false
	}
	nodes := map[string]*bayesgraph.Node{}
	for i := range m.Graph.Nodes {
		nodes[m.Graph.Nodes[i].ID] = &m.Graph.Nodes[i]
	}
	type terms struct {
		lp, data []string
		missing  bool
	}
	byNode := map[string]*terms{}
	// Model.Pairs lists a predictor's terms, then its intercept
	for _, p := range m.Pairs {
		if p.Intercept {
			t := byNode[p.Node]
			if t == nil || t.missing || len(t.lp) == 0 {
				continue
			}
			ip := &bayescontract.InterceptPair{LP: t.lp, Data: t.data}
			if n := nodes[p.Node]; n != nil && n.Intercept != nil {
				node, ok := slotQuantity(bayesgraph.SlotKey{Node: p.Node, Slot: "intercept"}, true)
				stan, okq := quantity(node, false)
				if !ok || !okq {
					continue
				}
				ip.Intercept = &stan
			}
			out = append(out, bayescontract.ScalePair{Intercept: ip})
			continue
		}
		t := byNode[p.Node]
		if t == nil {
			t = &terms{}
			byNode[p.Node] = t
		}
		node, ok := slotQuantity(bayesgraph.SlotKey{Node: p.Node, Slot: "terms[" + itoa(p.Term) + "].coef"}, false)
		coef, okc := quantity(node, true)
		x, okx := standardized(p.Data)
		if !ok || !okc || !okx {
			t.missing = true
			continue
		}
		t.lp, t.data = append(t.lp, coef), append(t.data, x)
		out = append(out, bayescontract.ScalePair{Term: &bayescontract.TermPair{Coef: coef, Data: x}})
	}
	return out
}
