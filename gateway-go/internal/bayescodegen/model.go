package bayescodegen

import (
	"fmt"
	"sort"
	"strings"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/bayescatalog"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/bayesexpr"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/bayesgraph"
)

// This file is the values and the statements (docs/specs/bayes-stan-codegen.md, «Linear predictors
// and transformed nodes», «Categories», «Likelihoods»): the EmitEnv every slot is emitted with, the
// transformed parameters (values computed from parameters, single or over a group plate, and the
// explicit non-centred parameters: written), the model block's locals (the categories' Full_, the
// linear predictors and transformed nodes on the rows: never written), the priors and the
// likelihoods in Order.

// emitEnv is the program's EmitEnv for one slot, whose context plate is ctx. A parameter on a
// category's levels read from another plate (per row, through the category) is its Full_<id>, the
// coefficients with the reference's 0; an index is its own array on its plate, its lift
// (Up_<index>_<plate>) on a plate it is lifted to. Lot S2 answers lags and subsets of the rows:
// until then they answer "", which Emit refuses (a model that needs them is refused before,
// ErrUnsupported).
type emitEnv struct {
	g   *gen
	ctx bayesexpr.Plate
}

func (e emitEnv) Identifier(id string) string {
	if f, ok := e.g.full[id]; ok {
		if p, _ := e.g.levelsOf(id); bayesexpr.Plate(p.ID) != e.ctx {
			return f
		}
	}
	return e.g.ident[id]
}

func (e emitEnv) IndexArray(id string, context bayesexpr.Plate) string {
	n := e.g.node(id)
	if n == nil || n.Plate == nil {
		return ""
	}
	if bayesexpr.Plate(*n.Plate) == context {
		return e.g.ident[id]
	}
	return e.g.lift[liftKey(id, context)]
}

func (e emitEnv) LagArray(_ int) string { return "" }
func (e emitEnv) RowsSubset() string    { return "" }

// slotValue is a slot's text: its expression through Emit, or the identifier of the node it refers
// to; with the value's type.
func (g *gen) slotValue(key bayesgraph.SlotKey) (string, bayesexpr.Type, error) {
	t, ok := g.m.Slots[key]
	if !ok {
		return "", bayesexpr.Type{}, bug("the slot %s.%s did not type in a valid model", key.Node, key.Slot)
	}
	if t.Expr != nil {
		s, err := bayesexpr.Emit(t.Expr, emitEnv{g, t.Context})
		if err != nil {
			return "", t.Type, fmt.Errorf("slot %s.%s: %w", key.Node, key.Slot, err)
		}
		return s, t.Type, nil
	}
	x, ok := g.ident[t.Ref]
	if !ok || t.Ref == "" {
		return "", t.Type, bug("the slot %s.%s refers to %q, which has no identifier", key.Node, key.Slot, t.Ref)
	}
	return x, t.Type, nil
}

// overPlate is a value made a vector over a plate (the rows or a group plate): a vector as it is,
// an integer array through to_vector, a single real repeated over the units.
func (g *gen) overPlate(text string, t bayesexpr.Type, plate string) (string, error) {
	switch {
	case t.Plate == "" && t.Elem == bayesexpr.ElemReal:
		return call("rep_vector", text, g.size[plate])
	case t.Plate == bayesexpr.Plate(plate) && t.Elem == bayesexpr.ElemReal:
		return text, nil
	case t.Plate == bayesexpr.Plate(plate) && t.Elem == bayesexpr.ElemInt:
		return call("to_vector", text)
	}
	return "", bug("a value of type %s where a vector over %q is needed", t, plate)
}

// readsParameter reports whether a node's value depends on a parameter: a slot of it reads a
// parameter, or a transformed node or linear predictor that does.
func (g *gen) readsParameter(id string, seen map[string]bool) bool {
	if seen[id] {
		return false
	}
	seen[id] = true
	for _, r := range g.reads[id] {
		if strings.IndexByte(r, '.') >= 0 {
			return true // a component of correlated effects (lot S2)
		}
		n := g.node(r)
		if n == nil {
			continue
		}
		switch n.Kind {
		case "parameter", "state", "correlated_effects":
			return true
		case "transformed", "linear_predictor":
			if g.readsParameter(r, seen) {
				return true
			}
		}
	}
	return false
}

// indexReads gathers what each node's slots read (the validated model's Refs), by node.
func (g *gen) indexReads() {
	g.reads = map[string][]string{}
	keys := make([]bayesgraph.SlotKey, 0, len(g.m.Slots))
	for k := range g.m.Slots {
		keys = append(keys, k)
	}
	sort.Slice(keys, func(i, j int) bool {
		if keys[i].Node != keys[j].Node {
			return keys[i].Node < keys[j].Node
		}
		return keys[i].Slot < keys[j].Slot
	})
	for _, k := range keys {
		owner := k.Node
		if i := strings.IndexByte(owner, '.'); i >= 0 {
			owner = owner[:i]
		}
		g.reads[owner] = append(g.reads[owner], g.m.Slots[k].Refs...)
	}
}

// placement is where a transformed node or a linear predictor is written.
type placement int

const (
	placeNone        placement = iota
	placeTransformed           // transformed parameters: single values computed from parameters, written
	placeLocal                 // a local of the model block: a value per row, not written
)

func (g *gen) place(n *bayesgraph.Node) (placement, error) {
	switch n.Kind {
	case "transformed", "linear_predictor":
	case "parameter":
		if _, ok := g.explicit[n.ID]; ok {
			return placeTransformed, nil // the explicit non-centred form
		}
		return placeNone, nil
	default:
		return placeNone, nil
	}
	if n.Kind == "transformed" && !g.readsParameter(n.ID, map[string]bool{}) {
		return placeNone, unsupported("the transformed node %q depends on no parameter: transformed data (lot S2)", n.ID)
	}
	if n.Plate == nil {
		if n.Kind == "transformed" {
			return placeTransformed, nil
		}
		return placeNone, bug("the %s %q on no plate", n.Kind, n.ID)
	}
	switch g.plates[*n.Plate].Kind {
	case bayesgraph.PlateRows:
		return placeLocal, nil
	case bayesgraph.PlateGroup:
		return placeTransformed, nil
	}
	return placeNone, unsupported("the %s %q on the plate %q (no lot writes it yet)", n.Kind, n.ID, *n.Plate)
}

func (g *gen) writeTransformedParameters() error {
	var lines []string
	for _, id := range g.m.Order {
		n := g.node(id)
		p, err := g.place(n)
		if err != nil {
			return err
		}
		if p != placeTransformed {
			continue
		}
		x := g.ident[id]
		var line string
		var dims []Dim
		switch {
		case n.Kind == "parameter":
			if line, err = g.explicitValue(n); err != nil {
				return err
			}
			dims = []Dim{{Plate: *n.Plate}}
		case n.Plate == nil:
			v, t, err := g.slotValue(bayesgraph.SlotKey{Node: id, Slot: "value"})
			if err != nil {
				return err
			}
			if t.Plate != "" || t.Elem != bayesexpr.ElemReal {
				return bug("the single transformed node %q has a value of type %s", id, t)
			}
			line, dims = "real "+x+" = "+v+";", []Dim{}
		default:
			if line, err = g.vectorValue(n); err != nil {
				return err
			}
			dims = []Dim{{Plate: *n.Plate}}
		}
		lines = append(lines, line)
		g.mapping = append(g.mapping, g.entry(n, blockTransformed, RoleQuantity))
		g.quantities = append(g.quantities, Quantity{Stan: x, Role: RoleQuantity, Node: id, Dims: dims})
	}
	if len(lines) == 0 {
		return nil
	}
	g.w.open(blockTransformed)
	for _, l := range lines {
		g.w.line(l)
	}
	g.w.close()
	return nil
}

// vectorValue is the declaration of a value over a plate (the rows or a group plate): a
// transformed node's value, or a linear predictor's sum, intercept first, then each term in the
// node's order, left-associated: a coefficient times a real data vector, an integer data through
// to_vector, a category's coefficients by its level (Full_<b>[c]).
func (g *gen) vectorValue(n *bayesgraph.Node) (string, error) {
	var v string
	var t bayesexpr.Type
	var err error
	if n.Kind == "transformed" {
		if v, t, err = g.slotValue(bayesgraph.SlotKey{Node: n.ID, Slot: "value"}); err != nil {
			return "", err
		}
	} else if v, t, err = g.linearPredictor(n); err != nil {
		return "", err
	}
	if v, err = g.overPlate(v, t, *n.Plate); err != nil {
		return "", err
	}
	return "vector[" + g.size[*n.Plate] + "] " + g.ident[n.ID] + " = " + v + ";", nil
}

func (g *gen) linearPredictor(n *bayesgraph.Node) (string, bayesexpr.Type, error) {
	plate := *n.Plate
	over := bayesexpr.Plate(plate)
	sum, typ := "", bayesexpr.Type{Elem: bayesexpr.ElemReal}
	if n.Intercept != nil {
		v, t, err := g.slotValue(bayesgraph.SlotKey{Node: n.ID, Slot: "intercept"})
		if err != nil {
			return "", typ, err
		}
		if t.Plate != "" {
			if v, err = g.overPlate(v, t, plate); err != nil {
				return "", typ, err
			}
			typ.Plate = over
		}
		sum = v
	}
	for i, term := range n.Terms {
		key := bayesgraph.SlotKey{Node: n.ID, Slot: fmt.Sprintf("terms[%d].coef", i)}
		d := g.node(term.Data)
		if d == nil || d.Kind != "data" || d.Plate == nil || *d.Plate != plate {
			return "", typ, bug("term %d of %q is not a data node on its plate", i, n.ID)
		}
		x := g.ident[d.ID]
		var product string
		switch d.Type {
		case "real", "int":
			coef, ct, err := g.slotValue(key)
			if err != nil {
				return "", typ, err
			}
			if ct.Plate != "" || ct.Elem != bayesexpr.ElemReal {
				return "", typ, bug("the coefficient of term %d of %q has the type %s", i, n.ID, ct)
			}
			if d.Type == "int" {
				if x, err = call("to_vector", x); err != nil {
					return "", typ, err
				}
			}
			product = "(" + coef + " * " + x + ")"
		case "category":
			b, err := g.coefficient(key)
			if err != nil {
				return "", typ, err
			}
			full, ok := g.full[b]
			if !ok {
				return "", typ, bug("the category term %d of %q reads %q, which has no Full_", i, n.ID, b)
			}
			product = "(" + full + "[" + x + "])"
		default:
			return "", typ, bug("term %d of %q reads a %s data node", i, n.ID, d.Type)
		}
		if sum == "" {
			sum = product
		} else {
			sum = "(" + sum + " + " + product + ")"
		}
		typ.Plate = over
	}
	if sum == "" {
		return "", typ, bug("the linear predictor %q has neither an intercept nor a term", n.ID)
	}
	return sum, typ, nil
}

// likelihood is "y ~ dist(args)", vectorized over the observed node's plate, the arguments in
// Stan's order.
func (g *gen) likelihood(n *bayesgraph.Node) (string, error) {
	d, ok := bayescatalog.LookupDistribution(n.Dist)
	if !ok || !d.HasUse(bayescatalog.Likelihood) {
		return "", bug("the likelihood %q has the distribution %q", n.ID, n.Dist)
	}
	obs := g.node(n.Observed)
	if obs == nil || obs.Kind != "data" || obs.Plate == nil || g.plates[*obs.Plate].Kind == bayesgraph.PlateLevels {
		return "", bug("the likelihood %q observes %q, not a data node on the rows or a group plate", n.ID, n.Observed)
	}
	args := make([]string, 0, len(d.Params))
	for _, a := range d.Params {
		v, _, err := g.slotValue(bayesgraph.SlotKey{Node: n.ID, Slot: "args." + a.Name})
		if err != nil {
			return "", err
		}
		args = append(args, v)
	}
	return sampling(g.ident[obs.ID], d, args, "")
}

// writeModel writes the model block: the categories' Full_ (the coefficients with the reference
// level's 0 in front), the per-row locals, the priors and the likelihoods, each in Order.
func (g *gen) writeModel() error {
	w := &g.w
	w.open(blockModel)
	for _, id := range g.m.Order {
		f, ok := g.full[id]
		if !ok {
			continue
		}
		p, _ := g.levelsOf(id)
		v, err := call("append_row", "0.0", g.ident[id])
		if err != nil {
			return err
		}
		w.line("vector[" + g.size[p.ID] + "] " + f + " = " + v + ";")
		g.mapping = append(g.mapping, g.internal(g.node(id), f, blockModel, ""))
	}
	for _, id := range g.m.Order {
		n := g.node(id)
		p, err := g.place(n)
		if err != nil {
			return err
		}
		if p != placeLocal {
			continue
		}
		l, err := g.vectorValue(n)
		if err != nil {
			return err
		}
		w.line(l)
		g.mapping = append(g.mapping, g.entry(n, blockModel, ""))
	}
	for _, id := range g.m.Order {
		if n := g.node(id); n.Kind == "parameter" {
			if err := g.writePrior(n); err != nil {
				return err
			}
		}
	}
	for _, id := range g.m.Order {
		if n := g.node(id); n.Kind == "likelihood" {
			s, err := g.likelihood(n)
			if err != nil {
				return err
			}
			w.line(s + ";")
		}
	}
	w.close()
	return nil
}
