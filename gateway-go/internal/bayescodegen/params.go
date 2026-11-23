package bayescodegen

import (
	"math"
	"strconv"
	"strings"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/bayescatalog"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/bayesexpr"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/bayesgraph"
)

// This file is the parameters (docs/specs/bayes-stan-codegen.md, «Parameters, bounds and
// truncation»): each parameter declared with its range, its prior statement with every argument
// emitted, and the truncation of the catalogue's «Bounds and truncation» as the validated model
// flags it (Param.TruncateInProgram). A parameter on a group plate is a vector over its units, in
// one of three forms (answer 4): the affine non-centred form (<offset=…, multiplier=…>) when its
// location and scale read only what the parameters block has already declared, the explicit
// non-centred form (standard normals Z_<id> and the parameter a transformed parameter) when they
// read a transformed parameter, the centred form otherwise (the plate's switch, a prior that is not
// location-scale, a bounded parameter: Param.NonCentred). A parameter on a category's levels has
// K - 1 coefficients, the reference level none. A truncated statement on a plate is written for one
// unit, in a loop.

// Quantity is one variable the program writes, in the order of the CSV's columns (bayes-contract,
// «The quantity manifest»). Dims is never null: [] for a single value.
type Quantity struct {
	Stan     string `json:"stan"`
	Role     string `json:"role"`
	Node     string `json:"node,omitempty"`
	Dims     []Dim  `json:"dims"`
	Triangle string `json:"triangle,omitempty"`
	Check    string `json:"check,omitempty"`
	Of       string `json:"of,omitempty"`
}

// Dim is one dimension of a written variable: over a plate's units, a size, or a node's components.
type Dim struct {
	Plate     string `json:"plate,omitempty"`
	Size      string `json:"size,omitempty"`
	Component string `json:"component,omitempty"`
}

// rangeConstraint is a range as Stan's bounds, "<lower=…, upper=…>", an unbounded side left out;
// "" for the real line.
func rangeConstraint(lo, hi float64) (string, error) {
	var parts []string
	if !math.IsInf(lo, -1) {
		s, err := realLiteral(lo)
		if err != nil {
			return "", err
		}
		parts = append(parts, "lower="+s)
	}
	if !math.IsInf(hi, 1) {
		s, err := realLiteral(hi)
		if err != nil {
			return "", err
		}
		parts = append(parts, "upper="+s)
	}
	if len(parts) == 0 {
		return "", nil
	}
	return "<" + strings.Join(parts, ", ") + ">", nil
}

// param is the validated model's Param of a parameter node, with its distribution.
func (g *gen) param(id string) (bayesgraph.Param, bayescatalog.Distribution, error) {
	p, ok := g.m.Params[id]
	if !ok {
		return p, bayescatalog.Distribution{}, bug("the parameter %q has no Param in a valid model", id)
	}
	d, ok := bayescatalog.LookupDistribution(p.Dist)
	if !ok || !d.HasUse(bayescatalog.Prior) {
		return p, d, bug("the parameter %q has the prior %q", id, p.Dist)
	}
	return p, d, nil
}

// indexForms chooses the non-centred form of each parameter that has one (Param.NonCentred): the
// explicit form when its location or its scale reads a value the parameters block has not declared
// before it (a transformed parameter: a transformed node that depends on a parameter, a linear
// predictor on a group plate, a parameter itself explicit, a component of correlated effects, which
// is a transformed parameter in both of their forms), the affine form otherwise (data, transformed
// data, the parameters of the affine or centred forms).
func (g *gen) indexForms() error {
	declared := func(id string) bool {
		n := g.node(id)
		if n == nil {
			return false // a component of correlated effects: a transformed parameter
		}
		switch n.Kind {
		case "data":
			return true
		case "transformed":
			return g.tdata[id]
		case "parameter":
			_, explicit := g.explicit[id]
			return !explicit
		}
		return false
	}
	for _, id := range g.m.Order {
		n := g.node(id)
		if n.Kind != "parameter" || !g.m.Params[id].NonCentred {
			continue
		}
		p, d, err := g.param(id)
		if err != nil {
			return err
		}
		if n.Plate == nil || g.plates[*n.Plate].Kind != bayesgraph.PlateGroup || d.LocationScale == nil ||
			!math.IsInf(p.Range.Lo, -1) || !math.IsInf(p.Range.Hi, 1) {
			return bug("the parameter %q is non-centred off a group plate, without a location-scale prior or with bounds", id)
		}
		explicit := false
		for _, a := range []string{d.LocationScale.Location, d.LocationScale.Scale} {
			t, ok := g.m.Slots[bayesgraph.SlotKey{Node: id, Slot: "prior." + a}]
			if !ok {
				return bug("the slot %s.prior.%s did not type in a valid model", id, a)
			}
			for _, r := range t.Refs {
				if !declared(r) {
					explicit = true
				}
			}
		}
		if explicit {
			z, err := generated("Z", id)
			if err != nil {
				return err
			}
			g.explicit[id] = z
		}
	}
	return nil
}

// units is the number of a parameter's values on its plate: J_<plate> on a group plate, K_<c> - 1
// on a category's levels (the reference level has no coefficient).
func (g *gen) units(n *bayesgraph.Node) (bayesgraph.ModelPlate, string, error) {
	plate, ok := g.plates[*n.Plate]
	if !ok {
		return plate, "", bug("the node %q on the plate %q, not a plate of the model", n.ID, *n.Plate)
	}
	switch plate.Kind {
	case bayesgraph.PlateGroup:
		return plate, g.size[plate.ID], nil
	case bayesgraph.PlateLevels:
		return plate, g.size[plate.ID] + " - 1", nil
	}
	// graph.node_plate: a parameter is single, on a group plate or on a category's levels.
	return plate, "", bug("the parameter %q on the plate %q", n.ID, plate.ID)
}

// internal is the mapping entry of an identifier the program declares for a node of its own (Z_,
// Full_, Arg<k>_): the node, no name, no label.
func (g *gen) internal(n *bayesgraph.Node, ident, block string, role string) MappingEntry {
	e := MappingEntry{Stan: ident, Block: block, NodeID: strp(n.ID), Kind: KindInternal, Role: optp(role)}
	if n.Plate != nil {
		e.Plate = strp(*n.Plate)
	}
	return e
}

func (g *gen) writeParameters() error {
	w := g.w
	w.open(blockParameters)
	for _, id := range g.m.Order {
		n := g.node(id)
		switch n.Kind {
		case "state":
			g.writeStateParameter(n)
			continue
		case "correlated_effects":
			g.writeEffectParameters(n)
			continue
		case "parameter":
		default:
			continue
		}
		p, d, err := g.param(id)
		if err != nil {
			return err
		}
		bounds, err := rangeConstraint(p.Range.Lo, p.Range.Hi)
		if err != nil {
			return err
		}
		x := g.ident[id]
		if n.Plate == nil {
			w.line("real" + bounds + " " + x + ";")
			g.mapping = append(g.mapping, g.entry(n, blockParameters, RoleQuantity))
			g.quantities = append(g.quantities, Quantity{Stan: x, Role: RoleQuantity, Node: id, Dims: []Dim{}})
			continue
		}
		plate, size, err := g.units(n)
		if err != nil {
			return err
		}
		if z, ok := g.explicit[id]; ok {
			// The explicit form: the standard normals are the parameter; the person's value is a
			// transformed parameter (writeTransformedParameters).
			w.line("vector[" + size + "] " + z + ";")
			g.mapping = append(g.mapping, g.internal(n, z, blockParameters, RoleInternal))
			g.quantities = append(g.quantities, Quantity{Stan: z, Role: RoleInternal, Dims: []Dim{{Plate: plate.ID}}})
			continue
		}
		if p.NonCentred {
			// The affine form: Stan samples the standardized values and writes the person's.
			loc, scale, err := g.locationScale(n, d)
			if err != nil {
				return err
			}
			if bounds != "" {
				return bug("the non-centred parameter %q has bounds", id)
			}
			bounds = "<offset=" + loc + ", multiplier=" + scale + ">"
		}
		w.line("vector" + bounds + "[" + size + "] " + x + ";")
		g.mapping = append(g.mapping, g.entry(n, blockParameters, RoleQuantity))
		g.quantities = append(g.quantities, Quantity{Stan: x, Role: RoleQuantity, Node: id, Dims: []Dim{{Plate: plate.ID}}})
	}
	w.close()
	return nil
}

// locationScale is the text of a location-scale prior's location and scale.
func (g *gen) locationScale(n *bayesgraph.Node, d bayescatalog.Distribution) (loc, scale string, err error) {
	if d.LocationScale == nil {
		return "", "", bug("the prior of %q is not location-scale", n.ID)
	}
	if loc, _, err = g.slotValue(bayesgraph.SlotKey{Node: n.ID, Slot: "prior." + d.LocationScale.Location}); err != nil {
		return "", "", err
	}
	if scale, _, err = g.slotValue(bayesgraph.SlotKey{Node: n.ID, Slot: "prior." + d.LocationScale.Scale}); err != nil {
		return "", "", err
	}
	return loc, scale, nil
}

// explicitValue is the declaration of an explicit non-centred parameter in transformed parameters:
// its location plus its scale times its standard normals, elementwise when the scale is a vector.
func (g *gen) explicitValue(n *bayesgraph.Node) (string, error) {
	_, d, err := g.param(n.ID)
	if err != nil {
		return "", err
	}
	loc, scale, err := g.locationScale(n, d)
	if err != nil {
		return "", err
	}
	st := g.m.Slots[bayesgraph.SlotKey{Node: n.ID, Slot: "prior." + d.LocationScale.Scale}]
	op := " * "
	if st.Type.Plate != "" {
		op = " .* "
	}
	_, size, err := g.units(n)
	if err != nil {
		return "", err
	}
	return "vector[" + size + "] " + g.ident[n.ID] + " = (" + loc + " + (" + scale + op + g.explicit[n.ID] + "));", nil
}

// writePrior writes a parameter's statement: "x ~ dist(args);", with " T[L, U]" when the program
// truncates. On a plate a truncated statement is written for one unit, in a loop over the units,
// each argument that varies over the plate first a local vector Arg<k>_<id>. A parameter in the
// explicit non-centred form has its standard normals' statement instead: std_normal() for a normal
// prior, the prior itself at location 0 and scale 1 otherwise.
func (g *gen) writePrior(n *bayesgraph.Node) error {
	w := g.w
	p, d, err := g.param(n.ID)
	if err != nil {
		return err
	}
	if z, ok := g.explicit[n.ID]; ok {
		s, err := g.standardPrior(n, z, d)
		if err != nil {
			return err
		}
		w.line(s + ";")
		return nil
	}
	args := make([]string, 0, len(d.Params))
	types := make([]bayesexpr.Type, 0, len(d.Params))
	for _, a := range d.Params {
		v, t, err := g.slotValue(bayesgraph.SlotKey{Node: n.ID, Slot: "prior." + a.Name})
		if err != nil {
			return err
		}
		args = append(args, v)
		types = append(types, t)
	}
	trunc := ""
	if p.TruncateInProgram {
		if trunc, err = truncation(p, d); err != nil {
			return err
		}
	}
	x := g.ident[n.ID]
	if trunc == "" || n.Plate == nil {
		s, err := sampling(x, d, args, trunc)
		if err != nil {
			return err
		}
		w.line(s + ";")
		return nil
	}
	_, size, err := g.units(n)
	if err != nil {
		return err
	}
	var locals []string
	for k, t := range types {
		if t.Plate == "" {
			continue
		}
		if t.Elem != bayesexpr.ElemReal {
			return bug("the argument %d of the prior of %q has the type %s", k+1, n.ID, t)
		}
		local, err := generated("Arg"+strconv.Itoa(k+1), n.ID)
		if err != nil {
			return err
		}
		locals = append(locals, "vector["+size+"] "+local+" = "+args[k]+";")
		g.mapping = append(g.mapping, g.internal(n, local, blockModel, ""))
		args[k] = local + "[I]"
	}
	s, err := sampling(x+"[I]", d, args, trunc)
	if err != nil {
		return err
	}
	if len(locals) > 0 {
		w.openNested()
		for _, l := range locals {
			w.line(l)
		}
	}
	bound := size
	if strings.ContainsRune(size, ' ') {
		bound = "(" + size + ")"
	}
	w.open("for (I in 1:" + bound + ")")
	w.line(s + ";")
	w.close()
	if len(locals) > 0 {
		w.close()
	}
	return nil
}

// standardPrior is the statement of an explicit non-centred parameter's standard normals.
func (g *gen) standardPrior(n *bayesgraph.Node, z string, d bayescatalog.Distribution) (string, error) {
	if d.LocationScale == nil {
		return "", bug("the prior of %q is not location-scale", n.ID)
	}
	if d.ID == "normal" {
		std, ok := bayescatalog.LookupDistribution("std_normal")
		if !ok {
			return "", bug("the catalogue has no std_normal")
		}
		return sampling(z, std, nil, "")
	}
	args := make([]string, 0, len(d.Params))
	for _, a := range d.Params {
		switch a.Name {
		case d.LocationScale.Location:
			args = append(args, "0.0")
		case d.LocationScale.Scale:
			args = append(args, "1.0")
		default:
			v, _, err := g.slotValue(bayesgraph.SlotKey{Node: n.ID, Slot: "prior." + a.Name})
			if err != nil {
				return "", err
			}
			args = append(args, v)
		}
	}
	return sampling(z, d, args, "")
}

// truncation is " T[L, U]", " T[L, ]" or " T[, U]": the sides on which the range is narrower than
// the prior's support (the catalogue's rule 4, applied as the validated model flags it). A
// distribution without lcdf and lccdf is never truncated (rule 5: model-graph refuses such bounds).
func truncation(p bayesgraph.Param, d bayescatalog.Distribution) (string, error) {
	if !d.Truncatable() {
		return "", bug("a truncation of %q, which has no lcdf and lccdf", d.ID)
	}
	var lo, hi string
	var err error
	if p.Range.Lo > p.Support.Lo {
		if lo, err = realLiteral(p.Range.Lo); err != nil {
			return "", err
		}
	}
	if p.Range.Hi < p.Support.Hi {
		if hi, err = realLiteral(p.Range.Hi); err != nil {
			return "", err
		}
	}
	if lo == "" && hi == "" {
		return "", bug("a truncation whose range is its support")
	}
	return " T[" + lo + ", " + hi + "]", nil
}
