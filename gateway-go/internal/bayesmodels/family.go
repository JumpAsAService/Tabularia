package bayesmodels

import (
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/bayesexpr"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/bayesgraph"
)

// The families a model is listed under (graph-editor's words: hierarchical, series, GLM, free).
const (
	FamilyHierarchical = "hierarchical"
	FamilySeries       = "series"
	FamilyGLM          = "glm"
	FamilyFree         = "free"
)

// glmLocation is the location argument of each of the catalogue's GLM likelihoods.
var glmLocation = map[string]string{
	"normal":             "mu",
	"bernoulli_logit":    "alpha",
	"binomial_logit":     "alpha",
	"poisson_log":        "alpha",
	"neg_binomial_2_log": "eta",
}

// Family is the family of a validated model, derived from its graph and never from the template
// it was opened from (the spec, «The family»), the first rule that holds:
//
//  1. a group plate holds a node: hierarchical;
//  2. the rows plate is a series: series;
//  3. there is a likelihood, and every likelihood is one of the GLM likelihoods with its location
//     argument a linear predictor node (a ref to it, or its name, indexed or not): glm;
//  4. free.
func Family(m *bayesgraph.Model) string {
	g := m.Graph
	groups := map[string]bool{}
	for _, p := range g.Plates {
		if p.Kind == bayesgraph.PlateGroup {
			groups[p.ID] = true
		}
	}
	kind := map[string]string{}
	for _, n := range g.Nodes {
		kind[n.ID] = n.Kind
		if n.Plate != nil && groups[*n.Plate] {
			return FamilyHierarchical
		}
	}
	for _, p := range g.Plates {
		if p.Kind == bayesgraph.PlateRows && p.Series != nil {
			return FamilySeries
		}
	}
	likelihoods := 0
	for _, n := range g.Nodes {
		if n.Kind != "likelihood" {
			continue
		}
		likelihoods++
		arg, ok := glmLocation[n.Dist]
		if !ok {
			return FamilyFree
		}
		s, ok := m.Slots[bayesgraph.SlotKey{Node: n.ID, Slot: "args." + arg}]
		if !ok || kind[locationNode(s)] != "linear_predictor" {
			return FamilyFree
		}
	}
	if likelihoods == 0 {
		return FamilyFree
	}
	return FamilyGLM
}

// locationNode is the node a slot is exactly (a ref, a name, a name indexed by a group index), or
// "" when it is any other expression.
func locationNode(s bayesgraph.TypedSlot) string {
	if s.Ref != "" {
		return s.Ref
	}
	if s.Expr == nil || s.Expr.Tree == nil || s.Expr.Tree.Node == nil || s.Expr.Tree.Sym == nil {
		return ""
	}
	switch s.Expr.Tree.Node.Kind {
	case bayesexpr.KindName, bayesexpr.KindIndex:
		return s.Expr.Tree.Sym.ID
	}
	return ""
}
