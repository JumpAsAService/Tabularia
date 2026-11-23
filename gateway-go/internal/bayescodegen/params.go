package bayescodegen

import (
	"math"
	"strings"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/bayescatalog"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/bayesgraph"
)

// This file is the parameters (docs/specs/bayes-stan-codegen.md, «Parameters, bounds and
// truncation»): each parameter declared with its range, its prior statement with every argument
// emitted, and the truncation of the catalogue's «Bounds and truncation» as the validated model
// flags it (Param.TruncateInProgram). Lot S0 writes the parameters that are single values; the
// plates' forms (affine, explicit non-centred, centred, categories, the truncation loop) are S1's.

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

func (g *gen) writeParameters() error {
	w := &g.w
	w.open(blockParameters)
	for _, id := range g.m.Order {
		n := g.node(id)
		if n.Kind != "parameter" {
			continue
		}
		if n.Plate != nil {
			return unsupported("the parameter %q on the plate %q (lot S1)", id, *n.Plate)
		}
		p, _, err := g.param(id)
		if err != nil {
			return err
		}
		bounds, err := rangeConstraint(p.Range.Lo, p.Range.Hi)
		if err != nil {
			return err
		}
		x := g.ident[id]
		w.line("real" + bounds + " " + x + ";")
		g.mapping = append(g.mapping, g.entry(n, blockParameters, RoleQuantity))
		g.quantities = append(g.quantities, Quantity{Stan: x, Role: RoleQuantity, Node: id, Dims: []Dim{}})
	}
	w.close()
	return nil
}

// prior is a parameter's statement: "x ~ dist(args)", with " T[L, U]" when the program truncates.
func (g *gen) prior(n *bayesgraph.Node) (string, error) {
	p, d, err := g.param(n.ID)
	if err != nil {
		return "", err
	}
	args := make([]string, 0, len(d.Params))
	for _, a := range d.Params {
		v, _, err := g.slotValue(bayesgraph.SlotKey{Node: n.ID, Slot: "prior." + a.Name})
		if err != nil {
			return "", err
		}
		args = append(args, v)
	}
	trunc := ""
	if p.TruncateInProgram {
		if trunc, err = truncation(p, d); err != nil {
			return "", err
		}
	}
	return sampling(g.ident[n.ID], d, args, trunc)
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
