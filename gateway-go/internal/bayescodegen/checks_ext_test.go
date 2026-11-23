package bayescodegen

import (
	"errors"
	"math"
	"strconv"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/bayescatalog"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/bayescontract"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/bayesexpr"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/bayesgraph"
)

// checksExtension is the test extension of lot P1 (docs/specs/bayes-predictive-checks.md, «What the
// program holds»): the shape predictive-checks gives a program, written with the extension point's
// primitives only, so that the golden cases extension_checks* and the stanc check pin them before
// lot P2 writes the real extension. It is not that extension: its statistics, its names and its
// choices of what to draw are a test's.
//
// For each of the first three likelihoods in Order: the replicated outcome drawn on the rows the
// runner names, from the posterior (SlotOn without ids) and, when every parameter of the model has
// an ancestral draw, from the prior (SlotOn with the draws in place of the parameters: a value per
// row that reads them is rebuilt by the codegen); the statistics in a loop of Ppc_on (0 or 1)
// iterations; the bins without a branch, clamp(floor(y * scale + shift), 1, slots); each unit
// slot's sum; the replicated rows on request. The ancestral draws: a scalar parameter in Prior_m_<i>
// (a truncated normal by inversion: u ~ uniform(Phi(zL), Phi(zU)), mu + sigma * inv_Phi(u)), a
// parameter on a group plate whose prior's arguments are single values in the vector Prior_p_<i>,
// sized by the mode and drawn in a loop.
type checksExtension struct{}

// checksMaxOutcomes is how many likelihoods the extension checks (max_check_outcomes).
const checksMaxOutcomes = 3

// checkedOutcomes are the likelihoods the extension checks, in Order.
func checkedOutcomes(m *bayesgraph.Model) []*bayesgraph.Node {
	var out []*bayesgraph.Node
	for _, id := range m.Order {
		for i := range m.Graph.Nodes {
			if n := &m.Graph.Nodes[i]; n.ID == id && n.Kind == "likelihood" && len(out) < checksMaxOutcomes {
				out = append(out, n)
			}
		}
	}
	return out
}

// outcomeData are an outcome's data, in the order Data declares them.
type outcomeData struct {
	n, rows, pn, prows, m, slots, scale, shift, units, slot, binned, rep Decl
}

func outcomeDecls(k int) outcomeData {
	ppc, prior := "Ppc_o"+strconv.Itoa(k)+"_", "Prior_o"+strconv.Itoa(k)+"_"
	size := func(name string) Decl {
		return Decl{Name: name, Elem: Int, Shape: Scalar, NonNegative: true, Kind: "number"}
	}
	var d outcomeData
	d.n, d.pn, d.m = size(ppc+"n"), size(prior+"n"), size(ppc+"m")
	d.rows = Decl{Name: ppc + "rows", Elem: Int, Shape: Array, Size: d.n.AsSize(), NonNegative: true, Kind: "rows"}
	d.prows = Decl{Name: prior + "rows", Elem: Int, Shape: Array, Size: d.pn.AsSize(), NonNegative: true, Kind: "rows"}
	d.slots, d.units, d.binned, d.rep = size(ppc+"slots"), size(ppc+"units"), size(ppc+"binned"), size(ppc+"rep")
	d.scale = Decl{Name: ppc + "scale", Elem: Real, Shape: Scalar, Kind: "scale"}
	d.shift = Decl{Name: ppc + "shift", Elem: Real, Shape: Scalar, Kind: "shift"}
	d.slot = Decl{Name: ppc + "slot", Elem: Int, Shape: Array, Size: d.m.AsSize(), NonNegative: true, Kind: "unit_slot"}
	return d
}

var checksOn = Decl{Name: "Ppc_on", Elem: Int, Shape: Scalar, NonNegative: true, Kind: "flag"}

func (checksExtension) Data(m *bayesgraph.Model) []Decl {
	outcomes := checkedOutcomes(m)
	if len(outcomes) == 0 {
		return nil
	}
	out := []Decl{checksOn}
	for k := range outcomes {
		d := outcomeDecls(k + 1)
		out = append(out, d.n, d.rows, d.pn, d.prows, d.m, d.slots, d.scale, d.shift, d.units, d.slot, d.binned, d.rep)
	}
	return out
}

// checksBuilder gathers the first error of a chain of constructors.
type checksBuilder struct{ errs []error }

func (b *checksBuilder) e(e Expr, err error) Expr {
	if err != nil {
		b.errs = append(b.errs, err)
	}
	return e
}

func (b *checksBuilder) size(s Size, err error) Size {
	if err != nil {
		b.errs = append(b.errs, err)
	}
	return s
}

func (b *checksBuilder) num(v float64) Expr { return b.e(Num(v)) }
func (b *checksBuilder) integer(n int) Expr { return b.e(Integer(n)) }

func (checksExtension) Generated(m *bayesgraph.Model, s Scope) ([]Decl, []Stmt, error) {
	outcomes := checkedOutcomes(m)
	if len(outcomes) == 0 {
		return nil, nil, nil
	}
	b := &checksBuilder{}
	call := func(name string, args ...Expr) Expr { return b.e(s.Call(name, args...)) }
	var written []Decl
	var stmts []Stmt

	// the ancestral draws, in Order, when every parameter of the model has one (a prior's path
	// that read one posterior value would be refused by the codegen)
	drawn := map[string]Decl{}
	ids := func(id string) (Expr, bool) {
		if d, ok := drawn[id]; ok {
			return d.Ref(), true
		}
		return Expr{}, false
	}
	plates := map[string]bayesgraph.ModelPlate{}
	for _, p := range m.Plates {
		plates[p.ID] = p
	}
	every := allDrawable(m, plates)
	for i, id := range m.Order {
		n := nodeOf(m, id)
		if !every {
			break
		}
		switch n.Kind {
		case "transformed", "linear_predictor":
			// a single value or a value over a group plate, with the draws (a value per row is
			// rebuilt by the codegen)
			if n.Plate != nil && plates[*n.Plate].Kind != bayesgraph.PlateGroup {
				continue
			}
			x := Decl{Name: "Prior_t_" + strconv.Itoa(i+1), Elem: Real, Shape: Scalar}
			if n.Plate != nil {
				x.Shape, x.Size = Vector, b.size(s.PlateSize(*n.Plate))
			}
			stmts = append(stmts, Declare(x, b.e(s.ValueWith(id, ids))))
			drawn[id] = x
			continue
		case "parameter":
		default:
			continue
		}
		d, _ := bayescatalog.LookupDistribution(n.Prior.Dist)
		param := m.Params[id]
		slot := func(name string) Expr {
			return b.e(s.Slot(bayesgraph.SlotKey{Node: id, Slot: "prior." + name}, ids))
		}
		draw := func() Expr {
			vals := make([]Expr, len(d.Params))
			for j, p := range d.Params {
				vals[j] = slot(p.Name)
			}
			return call(d.Stan.RNG, vals...)
		}
		name := "Prior_m_" + strconv.Itoa(i+1)
		switch {
		case n.Plate == nil && param.Truncated:
			// a normal by inversion: u between Phi((L - mu) / sigma) and Phi((U - mu) / sigma)
			x := Decl{Name: name, Elem: Real, Shape: Scalar, Role: bayescontract.RolePriorDraw, Of: id}
			u := Decl{Name: "Prior_u_" + strconv.Itoa(i+1), Elem: Real, Shape: Scalar}
			cdf := func(bound float64, inf float64) Expr {
				if math.IsInf(bound, 0) {
					return b.num(inf)
				}
				z := b.e(Mul(b.e(Add(b.num(bound), b.e(Mul(b.num(-1), slot("mu"))))), call("inv", slot("sigma"))))
				return call("Phi", z)
			}
			stmts = append(stmts,
				Declare(u, call("uniform_rng", cdf(param.Range.Lo, 0), cdf(param.Range.Hi, 1))),
				Assign(x.Ref(), b.e(Add(slot("mu"), b.e(Mul(slot("sigma"), call("inv_Phi", u.Ref())))))))
			written = append(written, x)
			drawn[id] = x
		case n.Plate == nil:
			x := Decl{Name: name, Elem: Real, Shape: Scalar, Role: bayescontract.RolePriorDraw, Of: id}
			stmts = append(stmts, Assign(x.Ref(), draw()))
			written = append(written, x)
			drawn[id] = x
		default:
			size := b.size(s.PlateSize(*n.Plate)).WhenPrior()
			// a vector, as the parameter it stands for (an array would not add to a vector)
			x := Decl{Name: "Prior_p_" + strconv.Itoa(i+1), Elem: Real, Shape: Vector, Size: size, Role: bayescontract.RolePriorDraw, Of: id}
			stmts = append(stmts, For(size, Assign(b.e(Index(x.Ref(), LoopIndex())), draw())))
			written = append(written, x)
			drawn[id] = x
		}
	}

	on := checksOn.AsSize()
	for k, lik := range outcomes {
		dd := outcomeDecls(k + 1)
		d, _ := bayescatalog.LookupDistribution(lik.Dist)
		elem := Real
		count := d.Outcome != bayescatalog.RealLine
		if count {
			elem = Int
		}
		ppc := "Ppc_o" + strconv.Itoa(k+1) + "_"
		stat := func(name, check string) Decl {
			return Decl{Name: ppc + name, Elem: Real, Shape: Array, Size: on, Role: bayescontract.RoleCheck, Check: check}
		}
		stats := []struct {
			d  Decl
			fn string
		}{{stat("mean", "mean"), "mean"}, {stat("sd", "sd"), "sd"}, {stat("min", "min"), "min"}, {stat("max", "max"), "max"},
			{stat("low", "low_quantile"), "quantile"}, {stat("median", "median"), "quantile"}}
		zeros := stat("zeros", "zeros")
		bins := Decl{Name: ppc + "count", Elem: Int, Shape: Array, Size: dd.slots.AsSize(), Role: bayescontract.RoleCheck, Check: "bin_count"}
		usum := Decl{Name: ppc + "usum", Elem: Real, Shape: Vector, Size: dd.units.AsSize(), Role: bayescontract.RoleCheck, Check: "unit_sum"}
		yrep := Decl{Name: ppc + "yrep", Elem: Real, Shape: Array, Size: dd.rep.AsSize(), Role: bayescontract.RoleCheck, Check: "replicated"}
		for _, st := range stats {
			written = append(written, st.d)
		}
		if count {
			written = append(written, zeros)
		}
		written = append(written, bins, usum, yrep)

		// the replicated outcome: the generator's arguments on the rows named, a single value
		// repeated when no argument is one a row
		replicate := func(ids func(string) (Expr, bool), rows, n Decl) Expr {
			vals := make([]Expr, len(d.Params))
			perRow := false
			for j, p := range d.Params {
				key := bayesgraph.SlotKey{Node: lik.ID, Slot: "args." + p.Name}
				vals[j] = b.e(s.SlotOn(key, ids, rows.Ref()))
				perRow = perRow || m.Slots[key].Type.Plate != ""
			}
			if !perRow {
				vals[0] = call("rep_vector", vals[0], n.Ref())
			}
			if d.Stan.RNGInverseLink != "" {
				vals[len(vals)-1] = call(d.Stan.RNGInverseLink, vals[len(vals)-1])
			}
			return call(d.Stan.RNG, vals...)
		}
		post := Decl{Name: ppc + "post", Elem: elem, Shape: Array, Size: dd.n.AsSize()}
		draw := Decl{Name: "Prior_o" + strconv.Itoa(k+1) + "_draw", Elem: elem, Shape: Array, Size: dd.pn.AsSize()}
		y := Decl{Name: ppc + "y", Elem: Real, Shape: Vector, Size: dd.m.AsSize()}
		bin := Decl{Name: ppc + "b", Elem: Int, Shape: Scalar}
		stmts = append(stmts, Declare(post, replicate(nil, dd.rows, dd.n)))
		if every {
			stmts = append(stmts, Declare(draw, replicate(ids, dd.prows, dd.pn)))
		} else {
			stmts = append(stmts, Declare(draw, Expr{}))
		}
		stmts = append(stmts,
			Declare(y, call("to_vector", call("append_array", post.Ref(), draw.Ref()))),
			Assign(bins.Ref(), call("rep_array", b.integer(0), dd.slots.Ref())),
			Assign(usum.Ref(), call("rep_vector", b.num(0), dd.units.Ref())))
		i := LoopIndex()
		var each []Stmt
		for _, st := range stats {
			args := []Expr{y.Ref()}
			switch st.d.Check {
			case "low_quantile":
				args = append(args, b.num(0.1))
			case "median":
				args = append(args, b.num(0.5))
			}
			each = append(each, Assign(b.e(Index(st.d.Ref(), i)), call(st.fn, args...)))
		}
		if count {
			each = append(each, Assign(b.e(Index(zeros.Ref(), i)), b.num(0)))
		}
		stmts = append(stmts, For(on, each...))
		yi := b.e(Index(y.Ref(), i))
		slotOf := b.e(Index(usum.Ref(), b.e(Index(dd.slot.Ref(), i))))
		binOf := b.e(Index(bins.Ref(), bin.Ref()))
		scaled := b.e(Add(b.e(Mul(yi, dd.scale.Ref())), dd.shift.Ref()))
		binned := []Stmt{
			Declare(bin, call("to_int", call("fmin", call("fmax", call("floor", scaled), b.num(1)), dd.slots.Ref()))),
			Assign(binOf, b.e(Add(binOf, b.integer(1)))),
			Assign(slotOf, b.e(Add(slotOf, yi))),
		}
		if count {
			z := b.e(Index(zeros.Ref(), b.integer(1)))
			binned = append(binned, Assign(z, b.e(Add(z, b.e(Add(b.num(1), b.e(Mul(b.num(-1), call("fmin", yi, b.num(1))))))))))
		}
		stmts = append(stmts, For(dd.binned.AsSize(), binned...),
			For(dd.rep.AsSize(), Assign(b.e(Index(yrep.Ref(), i)), yi)))
	}
	return written, stmts, errors.Join(b.errs...)
}

func nodeOf(m *bayesgraph.Model, id string) *bayesgraph.Node {
	for i := range m.Graph.Nodes {
		if m.Graph.Nodes[i].ID == id {
			return &m.Graph.Nodes[i]
		}
	}
	return nil
}

// allDrawable reports whether the test extension draws every parameter: no state, no correlated
// effects; each parameter single or on a group plate, its prior's arguments single values, a
// generator for it, truncated only when it is a single normal.
func allDrawable(m *bayesgraph.Model, plates map[string]bayesgraph.ModelPlate) bool {
	for _, n := range m.Graph.Nodes {
		switch n.Kind {
		case "state", "correlated_effects":
			return false
		case "transformed", "linear_predictor":
			if n.Plate != nil && plates[*n.Plate].Kind == bayesgraph.PlateGroup && !valueRealVector(m, &n) {
				return false
			}
			continue
		case "parameter":
		default:
			continue
		}
		d, ok := bayescatalog.LookupDistribution(n.Prior.Dist)
		if !ok || d.Stan.RNG == "" {
			return false
		}
		for _, p := range d.Params {
			t, has := m.Slots[bayesgraph.SlotKey{Node: n.ID, Slot: "prior." + p.Name}]
			if !has || t.Type.Plate != "" {
				return false
			}
		}
		param := m.Params[n.ID]
		if n.Plate != nil && (plates[*n.Plate].Kind != bayesgraph.PlateGroup || param.Truncated) ||
			n.Plate == nil && param.Truncated && d.ID != "normal" {
			return false
		}
	}
	return true
}

// valueRealVector reports whether a transformed node's value or a linear predictor's sum is a vector
// of reals as Stan types it (not a single value, not an array of integers).
func valueRealVector(m *bayesgraph.Model, n *bayesgraph.Node) bool {
	slot := "value"
	if n.Kind == "linear_predictor" {
		if len(n.Terms) > 0 {
			return true
		}
		slot = "intercept"
	}
	t := m.Slots[bayesgraph.SlotKey{Node: n.ID, Slot: slot}].Type
	return t.Plate != "" && t.Elem == bayesexpr.ElemReal
}
