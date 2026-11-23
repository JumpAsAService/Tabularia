package bayescodegen

import (
	"strconv"
	"strings"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/bayescatalog"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/bayescontract"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/bayesexpr"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/bayesgraph"
)

// This file is the correlated effects and the latent states (docs/specs/bayes-stan-codegen.md,
// «Correlated effects», «Latent states»).
//
// Correlated effects (answer 2): K components per unit of a group plate, scalar means and scales,
// an LKJ(eta) correlation in Cholesky form. Non-centred (the plate's default): standard normals
// Z_<id> (a K × J matrix) and the Cholesky factor L_<id> are the parameters; in transformed
// parameters W_<id> = diag_pre_multiply(scales, L_<id>) * Z_<id> is a local of a nested block, and
// each component is its mean plus a row of W. Centred: V_<id>, an array over the units of K-vectors,
// with a multi_normal_cholesky statement, each component a column of it. No transpose is written
// (the program's alphabet has no '). The correlation matrix multiply_lower_tri_self_transpose(L) is a
// generated quantity, with the correlation's name.
//
// Latent states (a random walk on the rows of a series): x₁ ~ init and xₜ ~ normal(xₜ₋₁, scale)
// within each unit of the series, on the rows the runner gives: Rows_first (the first row of each
// unit), Rows_next (the others) and Prev (the row before each of Rows_next). Non-centred (the
// default, when init is location-scale): Z_<id> over the rows is the parameter, the state a
// transformed parameter set in the rows' order; centred: the state is the parameter, with two
// vectorized statements.

// Identifiers of the derived data of a series.
const (
	identNLag      = "N_lag"
	identRowsLag   = "Rows_lag"
	identLagPrefix = "Lag"
	identNFirst    = "N_first"
	identRowsFirst = "Rows_first"
	identNNext     = "N_next"
	identRowsNext  = "Rows_next"
	identPrev      = "Prev"
)

// effect is what a correlated-effects node declares.
type effect struct {
	plate      string
	k          int
	nonCentred bool
	z, l, w, v string   // Z_<id>, L_<id>, W_<id> (non-centred); V_<id> (centred)
	comps      []string // the components' ids, "<node>.<component>", in the node's order
	corr       string   // the correlation matrix: its name, or Omega_<id>
}

// state is what a latent state declares: Z_<id> when it is non-centred.
type state struct {
	z          string
	nonCentred bool
}

// indexEffectsAndStates names the components, the correlations and the internal variables of the
// correlated effects, and the standard normals of the non-centred states.
func (g *gen) indexEffectsAndStates() error {
	for _, id := range g.m.Order {
		n := g.node(id)
		switch n.Kind {
		case "correlated_effects":
			if err := g.indexEffect(n); err != nil {
				return err
			}
		case "state":
			info, ok := g.m.States[id]
			if !ok || n.Plate == nil || *n.Plate != g.rows.ID || g.rows.Series == nil || n.Process != "random_walk" ||
				n.Init == nil || n.Scale == nil {
				return bug("the state %q is not a random walk on the rows of a series", id)
			}
			s := &state{nonCentred: info.NonCentred}
			if s.nonCentred {
				d, ok := bayescatalog.LookupDistribution(n.Init.Dist)
				if !ok || d.LocationScale == nil {
					return bug("the non-centred state %q has the init %q, not location-scale", id, n.Init.Dist)
				}
				z, err := generated("Z", id)
				if err != nil {
					return err
				}
				s.z = z
			}
			g.states[id] = s
		}
	}
	return nil
}

func (g *gen) indexEffect(n *bayesgraph.Node) error {
	info, ok := g.m.Effects[n.ID]
	if !ok || n.Plate == nil || info.Plate != *n.Plate || g.plates[info.Plate].Kind != bayesgraph.PlateGroup ||
		info.Components != len(n.Components) || info.Components < 2 || n.Correlation == nil {
		return bug("the correlated effects %q are not K >= 2 components on a group plate with a correlation", n.ID)
	}
	e := &effect{plate: info.Plate, k: info.Components, nonCentred: info.NonCentred}
	var err error
	for _, p := range []struct {
		to     *string
		prefix string
	}{{&e.z, "Z"}, {&e.l, "L"}, {&e.w, "W"}, {&e.v, "V"}} {
		if *p.to, err = generated(p.prefix, n.ID); err != nil {
			return err
		}
	}
	for i := range n.Components {
		c := &n.Components[i]
		key := n.ID + "." + c.ID
		if _, dup := g.comps[key]; dup {
			return bug("the component %q twice in a valid model", key)
		}
		s, renamed, word, err := nameIdentifier(optName(c.Name), "V", n.ID, c.ID)
		if err != nil {
			return err
		}
		g.ident[key] = s
		if renamed != "" {
			g.renamed[key] = [2]string{renamed, word}
		}
		g.comps[key] = c
		e.comps = append(e.comps, key)
	}
	s, renamed, word, err := nameIdentifier(optName(n.Correlation.Name), "Omega", n.ID)
	if err != nil {
		return err
	}
	e.corr = s
	if renamed != "" {
		g.renamed[n.ID+".correlation"] = [2]string{renamed, word}
	}
	g.effects[n.ID] = e
	return nil
}

func optName(s string) *string {
	if s == "" {
		return nil
	}
	return &s
}

// componentEntry is the mapping entry of a component of correlated effects.
func (g *gen) componentEntry(n *bayesgraph.Node, key string) bayescontract.MappingEntry {
	c := g.comps[key]
	e := bayescontract.MappingEntry{Stan: g.ident[key], Block: blockTransformed, NodeID: strp(n.ID), Component: strp(c.ID),
		Name: optName(c.Name), Label: strp(c.Label), Kind: bayescontract.KindComponent, Plate: strp(*n.Plate), Role: strp(bayescontract.RoleQuantity)}
	if r, ok := g.renamed[key]; ok {
		e.Renamed = strp(r[0])
		e.Word = r[1]
	}
	return e
}

// writeEffectParameters declares an effects node's parameters: Z_<id> (K × J) or V_<id> (J
// K-vectors), and the Cholesky factor L_<id>.
func (g *gen) writeEffectParameters(n *bayesgraph.Node) {
	e := g.effects[n.ID]
	k, j := strconv.Itoa(e.k), g.size[e.plate]
	if e.nonCentred {
		g.w.line("matrix[" + k + ", " + j + "] " + e.z + ";")
		g.mapping = append(g.mapping, g.internal(n, e.z, blockParameters, bayescontract.RoleInternal))
		g.quantities = append(g.quantities, bayescontract.Quantity{Stan: e.z, Role: bayescontract.RoleInternal, Dims: []bayescontract.Dim{{Component: n.ID}, {Plate: e.plate}}})
	} else {
		g.w.line("array[" + j + "] vector[" + k + "] " + e.v + ";")
		g.mapping = append(g.mapping, g.internal(n, e.v, blockParameters, bayescontract.RoleInternal))
		g.quantities = append(g.quantities, bayescontract.Quantity{Stan: e.v, Role: bayescontract.RoleInternal, Dims: []bayescontract.Dim{{Plate: e.plate}, {Component: n.ID}}})
	}
	g.w.line("cholesky_factor_corr[" + k + "] " + e.l + ";")
	g.mapping = append(g.mapping, g.internal(n, e.l, blockParameters, bayescontract.RoleInternal))
	g.quantities = append(g.quantities, bayescontract.Quantity{Stan: e.l, Role: bayescontract.RoleInternal, Dims: []bayescontract.Dim{{Component: n.ID}, {Component: n.ID}}})
}

// effectSlots are the texts of the components' means and scales, in the node's order.
func (g *gen) effectSlots(e *effect) (means, scales []string, err error) {
	for _, key := range e.comps {
		for _, s := range []struct {
			slot string
			to   *[]string
		}{{"mean", &means}, {"scale", &scales}} {
			v, t, err := g.slotValue(bayesgraph.SlotKey{Node: key, Slot: s.slot}, nil)
			if err != nil {
				return nil, nil, err
			}
			if t.Plate != "" || t.Elem != bayesexpr.ElemReal {
				return nil, nil, bug("the %s of %q has the type %s", s.slot, key, t)
			}
			*s.to = append(*s.to, v)
		}
	}
	return means, scales, nil
}

// vectorOf is to_vector([a, b, …]): the row-vector literal made a vector.
func vectorOf(xs []string) (string, error) {
	return call("to_vector", "["+strings.Join(xs, ", ")+"]")
}

// writeEffectTransformed writes the components, each a vector over the units: non-centred, the
// mean plus a row of W = diag_pre_multiply(scales, L) * Z in a nested block; centred, a column of V.
func (g *gen) writeEffectTransformed(n *bayesgraph.Node) error {
	e := g.effects[n.ID]
	j := g.size[e.plate]
	if !e.nonCentred {
		for i, key := range e.comps {
			v, err := call("to_vector", e.v+"[:, "+strconv.Itoa(i+1)+"]")
			if err != nil {
				return err
			}
			g.w.line("vector[" + j + "] " + g.ident[key] + " = " + v + ";")
			g.componentWritten(n, key)
		}
		return nil
	}
	means, scales, err := g.effectSlots(e)
	if err != nil {
		return err
	}
	for _, key := range e.comps {
		g.w.line("vector[" + j + "] " + g.ident[key] + ";")
		g.componentWritten(n, key)
	}
	sv, err := vectorOf(scales)
	if err != nil {
		return err
	}
	scaled, err := call("diag_pre_multiply", sv, e.l)
	if err != nil {
		return err
	}
	g.w.openNested()
	g.w.line("matrix[" + strconv.Itoa(e.k) + ", " + j + "] " + e.w + " = " + scaled + " * " + e.z + ";")
	g.mapping = append(g.mapping, g.internal(n, e.w, blockTransformed, ""))
	for i, key := range e.comps {
		row, err := call("to_vector", e.w+"["+strconv.Itoa(i+1)+"]")
		if err != nil {
			return err
		}
		g.w.line(g.ident[key] + " = " + means[i] + " + " + row + ";")
	}
	g.w.close()
	return nil
}

func (g *gen) componentWritten(n *bayesgraph.Node, key string) {
	g.mapping = append(g.mapping, g.componentEntry(n, key))
	g.quantities = append(g.quantities, bayescontract.Quantity{Stan: g.ident[key], Role: bayescontract.RoleQuantity, Node: key, Dims: []bayescontract.Dim{{Plate: *n.Plate}}})
}

// writeEffectStatements writes the statements of the effects: the standard normals (non-centred)
// or the multivariate normal (centred), then the LKJ prior of the Cholesky factor.
func (g *gen) writeEffectStatements(n *bayesgraph.Node) error {
	e := g.effects[n.ID]
	var s string
	var err error
	if e.nonCentred {
		std, ok := bayescatalog.LookupDistribution("std_normal")
		if !ok {
			return bug("the catalogue has no std_normal")
		}
		z, err := call("to_vector", e.z)
		if err != nil {
			return err
		}
		if s, err = sampling(z, std, nil, ""); err != nil {
			return err
		}
	} else {
		mvn, ok := bayescatalog.LookupDistribution("multi_normal_cholesky")
		if !ok {
			return bug("the catalogue has no multi_normal_cholesky")
		}
		means, scales, err := g.effectSlots(e)
		if err != nil {
			return err
		}
		mv, err := vectorOf(means)
		if err != nil {
			return err
		}
		sv, err := vectorOf(scales)
		if err != nil {
			return err
		}
		cov, err := call("diag_pre_multiply", sv, e.l)
		if err != nil {
			return err
		}
		if s, err = sampling(e.v, mvn, []string{mv, cov}, ""); err != nil {
			return err
		}
	}
	g.w.line(s + ";")
	lkj, ok := bayescatalog.LookupDistribution("lkj_corr_cholesky")
	if !ok {
		return bug("the catalogue has no lkj_corr_cholesky")
	}
	eta, t, err := g.slotValue(bayesgraph.SlotKey{Node: n.ID + ".correlation", Slot: "eta"}, nil)
	if err != nil {
		return err
	}
	if t.Plate != "" || t.Elem != bayesexpr.ElemReal {
		return bug("the eta of %q has the type %s", n.ID, t)
	}
	if s, err = sampling(e.l, lkj, []string{eta}, ""); err != nil {
		return err
	}
	g.w.line(s + ";")
	return nil
}

// writeCorrelation writes an effects node's correlation matrix, a generated quantity whose strict
// lower triangle is the person's (bayes-contract).
func (g *gen) writeCorrelation(n *bayesgraph.Node) error {
	e := g.effects[n.ID]
	v, err := call("multiply_lower_tri_self_transpose", e.l)
	if err != nil {
		return err
	}
	k := strconv.Itoa(e.k)
	g.w.line("matrix[" + k + ", " + k + "] " + e.corr + " = " + v + ";")
	entry := bayescontract.MappingEntry{Stan: e.corr, Block: blockGenerated, NodeID: strp(n.ID), Name: optName(n.Correlation.Name),
		Label: strp(n.Correlation.Label), Kind: bayescontract.KindCorrelation, Role: strp(bayescontract.RoleQuantity)}
	if r, ok := g.renamed[n.ID+".correlation"]; ok {
		entry.Renamed = strp(r[0])
		entry.Word = r[1]
	}
	g.mapping = append(g.mapping, entry)
	g.quantities = append(g.quantities, bayescontract.Quantity{Stan: e.corr, Role: bayescontract.RoleQuantity, Node: n.ID + ".correlation",
		Dims: []bayescontract.Dim{{Component: n.ID}, {Component: n.ID}}, Triangle: "strict_lower"})
	return nil
}

// writeStateParameter declares a state's parameter: its standard normals over the rows
// (non-centred) or the state itself (centred).
func (g *gen) writeStateParameter(n *bayesgraph.Node) {
	s := g.states[n.ID]
	if s.nonCentred {
		g.w.line("vector[" + identRows + "] " + s.z + ";")
		g.mapping = append(g.mapping, g.internal(n, s.z, blockParameters, bayescontract.RoleInternal))
		g.quantities = append(g.quantities, bayescontract.Quantity{Stan: s.z, Role: bayescontract.RoleInternal, Dims: []bayescontract.Dim{{Plate: g.rows.ID}}})
		return
	}
	g.w.line("vector[" + identRows + "] " + g.ident[n.ID] + ";")
	g.mapping = append(g.mapping, g.entry(n, blockParameters, bayescontract.RoleQuantity))
	g.quantities = append(g.quantities, bayescontract.Quantity{Stan: g.ident[n.ID], Role: bayescontract.RoleQuantity, Node: n.ID, Dims: []bayescontract.Dim{{Plate: g.rows.ID}}})
}

// initDist is a state's init distribution.
func (g *gen) initDist(n *bayesgraph.Node) (bayescatalog.Distribution, error) {
	d, ok := bayescatalog.LookupDistribution(n.Init.Dist)
	if !ok || !d.HasUse(bayescatalog.Prior) {
		return d, bug("the state %q has the init %q", n.ID, n.Init.Dist)
	}
	return d, nil
}

// initArgs are the texts of a state's init arguments, on the first rows of the series, for the
// arguments want keeps ("" for the others); the locals of a split argument go to sink.
func (g *gen) initArgs(n *bayesgraph.Node, d bayescatalog.Distribution, sink *splitSink, want func(name string) bool) ([]string, error) {
	args := make([]string, len(d.Params))
	for i, a := range d.Params {
		if !want(a.Name) {
			continue
		}
		v, _, err := g.slotValue(bayesgraph.SlotKey{Node: n.ID, Slot: "init." + a.Name}, sink)
		if err != nil {
			return nil, err
		}
		args[i] = v
	}
	return args, nil
}

// stepScale is the text of a state's scale, a single value.
func (g *gen) stepScale(n *bayesgraph.Node) (string, error) {
	v, t, err := g.slotValue(bayesgraph.SlotKey{Node: n.ID, Slot: "scale"}, nil)
	if err != nil {
		return "", err
	}
	if t.Plate != "" || t.Elem != bayesexpr.ElemReal {
		return "", bug("the scale of the state %q has the type %s", n.ID, t)
	}
	return v, nil
}

// writeStateTransformed writes a non-centred state: its first values from the init's location and
// scale, then each next value from the one before it, in the rows' order (the runner sorts the
// rows by unit and time, so Prev[I] < Rows_next[I] and every value is set before it is read). When
// the location and the scale together hold more than maxLocalNodes vector nodes, the larger becomes
// a local (lot S5), in a nested block with the first values' assignment.
func (g *gen) writeStateTransformed(n *bayesgraph.Node) error {
	s := g.states[n.ID]
	d, err := g.initDist(n)
	if err != nil {
		return err
	}
	if d.LocationScale == nil {
		return bug("the non-centred state %q has an init that is not location-scale", n.ID)
	}
	lk := bayesgraph.SlotKey{Node: n.ID, Slot: "init." + d.LocationScale.Location}
	sk := bayesgraph.SlotKey{Node: n.ID, Slot: "init." + d.LocationScale.Scale}
	locLocal, scaleLocal, err := g.wrapped(lk, sk)
	if err != nil {
		return err
	}
	sink := &splitSink{block: blockTransformed}
	loc, _, err := g.emitSlot(lk, emitEnv{g: g, sink: sink, rootLocal: locLocal})
	if err != nil {
		return err
	}
	scale, st, err := g.emitSlot(sk, emitEnv{g: g, sink: sink, rootLocal: scaleLocal})
	if err != nil {
		return err
	}
	op := " * "
	if st.Plate != "" {
		op = " .* "
	}
	step, err := g.stepScale(n)
	if err != nil {
		return err
	}
	x := g.ident[n.ID]
	g.w.line("vector[" + identRows + "] " + x + ";")
	g.mapping = append(g.mapping, g.entry(n, blockTransformed, bayescontract.RoleQuantity))
	g.quantities = append(g.quantities, bayescontract.Quantity{Stan: x, Role: bayescontract.RoleQuantity, Node: n.ID, Dims: []bayescontract.Dim{{Plate: g.rows.ID}}})
	first := x + "[" + identRowsFirst + "] = (" + loc + " + (" + scale + op + s.z + "[" + identRowsFirst + "]));"
	if sink.empty() {
		g.w.line(first)
	} else {
		g.w.openNested()
		g.flush(sink)
		g.w.line(first)
		g.w.close()
	}
	g.w.open("for (I in 1:" + identNNext + ")")
	g.w.line(x + "[" + identRowsNext + "[I]] = (" + x + "[" + identPrev + "[I]] + (" + step + " * " + s.z + "[" + identRowsNext + "[I]]));")
	g.w.close()
	return nil
}

// writeStateStatements writes a state's statements. Non-centred: its standard normals, all
// std_normal() for a normal init, otherwise the first rows from the init at location 0 and scale 1
// and the next ones std_normal(). Centred: the first values from the init, each next value normal
// around the one before it.
func (g *gen) writeStateStatements(n *bayesgraph.Node) error {
	s := g.states[n.ID]
	d, err := g.initDist(n)
	if err != nil {
		return err
	}
	// The arguments the statements read: all of them centred; non-centred, those that are not the
	// location nor the scale, and none for a normal init.
	want := func(name string) bool {
		if !s.nonCentred {
			return true
		}
		return d.ID != "normal" && d.LocationScale != nil && name != d.LocationScale.Location && name != d.LocationScale.Scale
	}
	sink := &splitSink{block: blockModel}
	args, err := g.initArgs(n, d, sink, want)
	if err != nil {
		return err
	}
	std, ok := bayescatalog.LookupDistribution("std_normal")
	if !ok {
		return bug("the catalogue has no std_normal")
	}
	normal, ok := bayescatalog.LookupDistribution("normal")
	if !ok {
		return bug("the catalogue has no normal")
	}
	var lines []string
	if s.nonCentred {
		if d.ID == "normal" {
			l, err := sampling(s.z, std, nil, "")
			if err != nil {
				return err
			}
			lines = append(lines, l)
		} else {
			standard := make([]string, len(args))
			for i, a := range d.Params {
				switch a.Name {
				case d.LocationScale.Location:
					standard[i] = "0.0"
				case d.LocationScale.Scale:
					standard[i] = "1.0"
				default:
					standard[i] = args[i]
				}
			}
			first, err := sampling(s.z+"["+identRowsFirst+"]", d, standard, "")
			if err != nil {
				return err
			}
			next, err := sampling(s.z+"["+identRowsNext+"]", std, nil, "")
			if err != nil {
				return err
			}
			lines = append(lines, first, next)
		}
	} else {
		x := g.ident[n.ID]
		step, err := g.stepScale(n)
		if err != nil {
			return err
		}
		first, err := sampling(x+"["+identRowsFirst+"]", d, args, "")
		if err != nil {
			return err
		}
		next, err := sampling(x+"["+identRowsNext+"]", normal, []string{x + "[" + identPrev + "]", step}, "")
		if err != nil {
			return err
		}
		lines = append(lines, first, next)
	}
	g.flush(sink)
	for _, l := range lines {
		g.w.line(l + ";")
	}
	return nil
}
