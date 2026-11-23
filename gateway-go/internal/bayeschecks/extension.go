package bayeschecks

import (
	"errors"

	cg "github.com/JumpAsAService/Tabularia/gateway-go/internal/bayescodegen"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/bayescatalog"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/bayescontract"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/bayesgraph"
)

// This file is the extension itself: its data (after Mode) and, in the generated quantities, the
// ancestral draws (priors.go) then, for each checked outcome k, the replicated outcome on the rows
// the runner names, from the posterior (Ppc_o<k>_rows) or from the prior's draws (Prior_o<k>_rows),
// its statistics in a loop of Ppc_on (0 or 1) iterations, its bins' counts and its units' sums in a
// loop over the rows binned, and the replicated rows on request («What the program holds»).
//
// The data the runner writes, by kind (each names its outcome, the observed data node, in node;
// unit_slot names the group index of the units it slots):
//
//	checks_on         Ppc_on: 1 when the statistics, bins and sums are computed (prior predictive, checks)
//	post_count/rows   Ppc_o<k>_n, Ppc_o<k>_rows: positions among the statement's rows replicated from the posterior
//	prior_count/rows  Prior_o<k>_n, Prior_o<k>_rows: the same from the prior's draws (only with a prior predictive)
//	replicated_count  Ppc_o<k>_m: the rows replicated in this run (one of the two above)
//	bin_slots         Ppc_o<k>_slots: the bins and the two outer slots, 0 when the checks are off
//	bin_scale/shift   Ppc_o<k>_scale, Ppc_o<k>_shift: slot = clamp(floor(y * scale + shift), 1, slots)
//	binned_count      Ppc_o<k>_binned: the rows binned and summed (Ppc_o<k>_m when the checks are on, else 0)
//	unit_count        Ppc_o<k>_units: the checked units and the slot of the others (with a group plate only)
//	unit_slot         Ppc_o<k>_slot: each replicated row's unit slot (node: the group index)
//	replicated_rows   Ppc_o<k>_rep: the replicated rows written (the output replicated), 0 otherwise
//	first_count, prior_first, next_count, prior_next, prior_previous
//	                  Prior_first_n, Prior_first, Prior_next_n, Prior_next, Prior_prev: a state's series
//	                  (the program's N_first, Rows_first, N_next, Rows_next, Prev) in the prior predictive,
//	                  0 and empty otherwise (with a state and a prior predictive only)

var errNoQuantile = errors.New("bayeschecks: a truncated prior without a quantile")

// Extension is predictive-checks' extension of a program: every program a save writes holds it
// (bayesmodels, Generate(m, Extension{})).
type Extension struct{}

var _ cg.Extension = Extension{}

// checksOn is Ppc_on.
var checksOn = cg.Decl{Name: "Ppc_on", Elem: cg.Int, Shape: cg.Scalar, NonNegative: true, Kind: "checks_on"}

// outcomeData are an outcome's data, in the order Data declares them.
type outcomeData struct {
	n, rows, pn, prows, m, slots, scale, shift, binned, units, slot, rep cg.Decl
}

func outcomeDecls(o Outcome, prior bool) (outcomeData, []cg.Decl) {
	ppc, pri := "Ppc_o"+itoa(o.K)+"_", "Prior_o"+itoa(o.K)+"_"
	count := func(name, kind string) cg.Decl {
		return cg.Decl{Name: name, Elem: cg.Int, Shape: cg.Scalar, NonNegative: true, Kind: kind, Of: o.Observed}
	}
	array := func(name, kind string, n cg.Decl, of string) cg.Decl {
		return cg.Decl{Name: name, Elem: cg.Int, Shape: cg.Array, Size: n.AsSize(), NonNegative: true, Kind: kind, Of: of}
	}
	number := func(name, kind string) cg.Decl {
		return cg.Decl{Name: name, Elem: cg.Real, Shape: cg.Scalar, Kind: kind, Of: o.Observed}
	}
	var d outcomeData
	d.n = count(ppc+"n", "post_count")
	d.rows = array(ppc+"rows", "post_rows", d.n, o.Observed)
	d.pn = count(pri+"n", "prior_count")
	d.prows = array(pri+"rows", "prior_rows", d.pn, o.Observed)
	d.m = count(ppc+"m", "replicated_count")
	d.slots = count(ppc+"slots", "bin_slots")
	d.scale, d.shift = number(ppc+"scale", "bin_scale"), number(ppc+"shift", "bin_shift")
	d.binned = count(ppc+"binned", "binned_count")
	d.units = count(ppc+"units", "unit_count")
	d.slot = array(ppc+"slot", "unit_slot", d.binned, o.UnitIndex)
	d.rep = count(ppc+"rep", "replicated_rows")
	out := []cg.Decl{d.n, d.rows}
	if prior {
		out = append(out, d.pn, d.prows)
	}
	out = append(out, d.m, d.slots, d.scale, d.shift, d.binned)
	if o.UnitIndex != "" {
		out = append(out, d.units, d.slot)
	}
	return d, append(out, d.rep)
}

// hasState reports a state on the prior's path.
func hasState(m *bayesgraph.Model, a Analysis) bool {
	for _, n := range m.Graph.Nodes {
		if n.Kind == "state" && a.path[n.ID] {
			return true
		}
	}
	return false
}

// Data are the extension's data, after Mode.
func (Extension) Data(m *bayesgraph.Model) []cg.Decl {
	a := Analyze(m)
	if len(a.Outcomes) == 0 {
		return nil
	}
	out := []cg.Decl{checksOn}
	for _, o := range a.Outcomes {
		_, ds := outcomeDecls(o, a.PriorPredictive)
		out = append(out, ds...)
	}
	if a.PriorPredictive && hasState(m, a) {
		s := series()
		out = append(out, s.firstN, s.first, s.nextN, s.next, s.prev)
	}
	return out
}

// Generated are the ancestral draws, then each outcome's checks.
func (Extension) Generated(m *bayesgraph.Model, s cg.Scope) ([]cg.Decl, []cg.Stmt, error) {
	x := newModel(m)
	a := x.analyze()
	one, err := cg.Count(1)
	if err != nil {
		return nil, nil, err
	}
	b := &builder{s: s, x: x, a: a, marg: map[string]cg.Decl{}, rep: map[string]cg.Decl{}, path: map[string]cg.Decl{},
		one: one.WhenPrior()}
	for _, id := range m.Order {
		if x.nodes[id] != nil {
			b.drawsOfNode(id)
		}
	}
	for _, o := range a.Outcomes {
		b.outcome(o)
	}
	return b.written, b.stmts, errors.Join(b.errs...)
}

// outcome writes an outcome's replicated values and their checks.
func (b *builder) outcome(o Outcome) {
	dd, _ := outcomeDecls(o, b.a.PriorPredictive)
	lik := b.x.nodes[o.Node]
	d, _ := bayescatalog.LookupDistribution(lik.Dist)
	elem := cg.Real
	if o.Kind != KindContinuous {
		elem = cg.Int
	}
	ppc := "Ppc_o" + itoa(o.K) + "_"
	on := checksOn.AsSize()
	check := func(name, stat string, e cg.Elem, shape cg.Shape, n cg.Size) cg.Decl {
		return cg.Decl{Name: ppc + name, Elem: e, Shape: shape, Size: n, Role: bayescontract.RoleCheck, Check: stat, Of: o.Observed}
	}
	stats := make([]cg.Decl, 0, len(Statistics[o.Kind]))
	for _, st := range Statistics[o.Kind] {
		stats = append(stats, check(st, st, cg.Real, cg.Array, on))
	}
	bins := check("count", "bin_count", cg.Int, cg.Array, dd.slots.AsSize())
	usum := check("usum", "unit_sum", cg.Real, cg.Vector, dd.units.AsSize())
	yrep := check("yrep", "replicated", cg.Real, cg.Array, dd.rep.AsSize())
	b.written = append(b.written, stats...)
	b.written = append(b.written, bins)
	if o.UnitIndex != "" {
		b.written = append(b.written, usum)
	}
	b.written = append(b.written, yrep)

	// the replicated outcome: the generator's arguments on the rows named (a single value repeated
	// when no argument is one a row), the binomial's probability through its inverse link
	replicate := func(ids func(string) (cg.Expr, bool), rows, n cg.Decl) cg.Expr {
		vals := make([]cg.Expr, len(d.Params))
		perRow := false
		for j, p := range d.Params {
			key := bayesgraph.SlotKey{Node: lik.ID, Slot: "args." + p.Name}
			vals[j] = b.e(b.s.SlotOn(key, ids, rows.Ref()))
			perRow = perRow || b.x.slotPlate(key) != ""
		}
		if !perRow {
			vals[0] = b.call("rep_vector", vals[0], n.Ref())
		}
		if d.Stan.RNGInverseLink != "" {
			vals[len(vals)-1] = b.call(d.Stan.RNGInverseLink, vals[len(vals)-1])
		}
		return b.call(d.Stan.RNG, vals...)
	}
	post := cg.Decl{Name: ppc + "post", Elem: elem, Shape: cg.Array, Size: dd.n.AsSize()}
	y := vector(ppc+"y", dd.m.AsSize())
	b.stmts = append(b.stmts, cg.Declare(post, replicate(nil, dd.rows, dd.n)))
	if b.a.PriorPredictive {
		draw := cg.Decl{Name: "Prior_o" + itoa(o.K) + "_draw", Elem: elem, Shape: cg.Array, Size: dd.pn.AsSize()}
		b.stmts = append(b.stmts, cg.Declare(draw, replicate(b.idsPath, dd.prows, dd.pn)),
			cg.Declare(y, b.call("to_vector", b.call("append_array", post.Ref(), draw.Ref()))))
	} else {
		b.stmts = append(b.stmts, cg.Declare(y, b.call("to_vector", post.Ref())))
	}
	b.stmts = append(b.stmts, cg.Assign(bins.Ref(), b.call("rep_array", b.integer(0), dd.slots.Ref())))
	if o.UnitIndex != "" {
		b.stmts = append(b.stmts, cg.Assign(usum.Ref(), b.call("rep_vector", b.num(0), dd.units.Ref())))
	}

	// the statistics, in a loop of Ppc_on (0 or 1) iterations
	i := cg.LoopIndex()
	var each []cg.Stmt
	var zeros cg.Decl
	for _, st := range stats {
		var v cg.Expr
		switch st.Check {
		case "mean", "sd", "min", "max":
			v = b.call(st.Check, y.Ref())
		case "zeros":
			zeros, v = st, b.num(0)
		default:
			v = b.call("quantile", y.Ref(), b.num(quantileOf[st.Check]))
		}
		each = append(each, cg.Assign(b.at(st, i), v))
	}
	b.stmts = append(b.stmts, cg.For(on, each...))

	// the bins without a branch, the units' sums, the zeros, over the rows binned
	yi := b.at(y, i)
	bin := cg.Decl{Name: ppc + "b", Elem: cg.Int, Shape: cg.Scalar}
	binOf := b.at(bins, bin.Ref())
	scaled := b.add(b.mul(yi, dd.scale.Ref()), dd.shift.Ref())
	binned := []cg.Stmt{
		cg.Declare(bin, b.call("to_int", b.call("fmin", b.call("fmax", b.call("floor", scaled), b.num(1)), dd.slots.Ref()))),
		cg.Assign(binOf, b.add(binOf, b.integer(1))),
	}
	if o.UnitIndex != "" {
		slotOf := b.index(usum.Ref(), b.at(dd.slot, i))
		binned = append(binned, cg.Assign(slotOf, b.add(slotOf, yi)))
	}
	if zeros.Name != "" {
		// counts are whole numbers at least 0: 1 - fmin(y, 1) is 1 at 0 and 0 elsewhere
		z := b.at(zeros, b.integer(1))
		binned = append(binned, cg.Assign(z, b.add(z, b.sub(b.num(1), b.call("fmin", yi, b.num(1))))))
	}
	b.stmts = append(b.stmts, cg.For(dd.binned.AsSize(), binned...),
		cg.For(dd.rep.AsSize(), cg.Assign(b.at(yrep, i), yi)))
}
