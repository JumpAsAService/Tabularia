package bayeschecks

import (
	"math"

	cg "github.com/JumpAsAService/Tabularia/gateway-go/internal/bayescodegen"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/bayescatalog"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/bayescontract"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/bayesgraph"
)

// This file is the ancestral draws («What the program holds», «Truncated priors»): in Order, for
// each quantity node,
//
//   - its prior marginal, Prior_m_<k> (k its place in Order; a component's Prior_m_<k>_<c>, the
//     correlation's Prior_m_<k>_rho), written in every run: a single value drawn from its parents'
//     marginals, a plate's representative unit (a parent over a group plate read by a value that
//     varies is a vector Prior_r_<k> of its representative, so that the reader's value is that of
//     its first unit), a correlation's Ω[2,1] from its LKJ marginal;
//   - on the prior's path (when the model has one), its ancestral draw: a single value is its
//     marginal (written again as the prior_draw Prior_d_<k>), a value over a plate the vector
//     Prior_p_<k>, sized by the mode, drawn unit by unit in a loop of the mode's size, the
//     arguments that vary computed in a loop of one iteration in the prior predictive and none
//     otherwise (a draw sized 0 is never indexed), correlated effects by the onion method, a state
//     along the series' rows the runner names.
//
// A truncated prior is drawn by inversion, u uniform between F(L) and F(U), x = Q(u), with the
// catalogue's quantile form; gamma and inv_gamma have none (no marginal, no prior predictive).

// builder writes the extension's declarations and statements, gathering the first errors of a
// chain of constructors.
type builder struct {
	s       cg.Scope
	x       *model
	a       Analysis
	errs    []error
	written []cg.Decl
	stmts   []cg.Stmt
	marg    map[string]cg.Decl // the marginals' single values, by quantity
	rep     map[string]cg.Decl // the representative vectors, by quantity over a group plate
	path    map[string]cg.Decl // the prior's path's identifiers, by quantity
	one     cg.Size            // Mode == 1 ? 1 : 0
	fixed   map[string]bool    // the constant arguments of the truncated draw being written
}

func (b *builder) fail(err error) {
	if err != nil {
		b.errs = append(b.errs, err)
	}
}

func (b *builder) e(e cg.Expr, err error) cg.Expr {
	b.fail(err)
	return e
}

func (b *builder) size(s cg.Size, err error) cg.Size {
	b.fail(err)
	return s
}

func (b *builder) num(v float64) cg.Expr           { return b.e(cg.Num(v)) }
func (b *builder) integer(n int) cg.Expr           { return b.e(cg.Integer(n)) }
func (b *builder) add(l, r cg.Expr) cg.Expr        { return b.e(cg.Add(l, r)) }
func (b *builder) mul(l, r cg.Expr) cg.Expr        { return b.e(cg.Mul(l, r)) }
func (b *builder) sub(l, r cg.Expr) cg.Expr        { return b.add(l, b.mul(b.num(-1), r)) }
func (b *builder) index(v, i cg.Expr) cg.Expr      { return b.e(cg.Index(v, i)) }
func (b *builder) at(d cg.Decl, i cg.Expr) cg.Expr { return b.index(d.Ref(), i) }
func (b *builder) call(name string, args ...cg.Expr) cg.Expr {
	return b.e(b.s.Call(name, args...))
}

func scalar(name string) cg.Decl { return cg.Decl{Name: name, Elem: cg.Real, Shape: cg.Scalar} }

func vector(name string, n cg.Size) cg.Decl {
	return cg.Decl{Name: name, Elem: cg.Real, Shape: cg.Vector, Size: n}
}

// tag is a quantity's part of a name: its place in Order, then its component's place among the
// node's components, or rho for the correlation.
func (b *builder) tag(id string) string {
	t := itoa(b.x.pos[owner(id)])
	if id == owner(id) {
		return t
	}
	n := b.x.node(id)
	for i, c := range n.Components {
		if id == n.ID+"."+c.ID {
			return t + "_" + itoa(i+1)
		}
	}
	return t + "_rho"
}

// idsMarg answers a quantity with its marginal: a single value, or the representative vector of a
// value over a group plate.
func (b *builder) idsMarg(id string) (cg.Expr, bool) {
	if d, ok := b.rep[id]; ok {
		return d.Ref(), true
	}
	if d, ok := b.marg[id]; ok && !b.overPlate(id) {
		return d.Ref(), true
	}
	return cg.Expr{}, false
}

// idsPath answers a quantity with its ancestral draw on the prior's path.
func (b *builder) idsPath(id string) (cg.Expr, bool) {
	if d, ok := b.path[id]; ok {
		return d.Ref(), true
	}
	return cg.Expr{}, false
}

// overPlate reports a quantity over a plate (a component is over its node's).
func (b *builder) overPlate(id string) bool { return b.x.node(id).Plate != nil }

// plateOf is the plate of a quantity.
func (b *builder) plateOf(id string) string {
	if n := b.x.node(id); n.Plate != nil {
		return *n.Plate
	}
	return ""
}

// representative writes, for each quantity read that is over a group plate and has no
// representative vector yet, Prior_r_<k>: its marginal in each unit.
func (b *builder) representative(reads []string) {
	for _, r := range reads {
		if _, ok := b.rep[r]; ok {
			continue
		}
		m, ok := b.marg[r]
		if !ok || !b.overPlate(r) {
			continue
		}
		n := b.size(b.s.PlateSize(b.plateOf(r)))
		v := vector("Prior_r_"+b.tag(r), n)
		b.stmts = append(b.stmts, cg.Declare(v, cg.Expr{}), cg.For(n, cg.Assign(b.at(v, cg.LoopIndex()), m.Ref())))
		b.rep[r] = v
	}
}

// margArg is a slot's value for a marginal: a single value, or the value of the first unit of a
// value over a plate (its representative unit), computed into the vector Prior_v_<tag>_<j>.
func (b *builder) margArg(key bayesgraph.SlotKey, tag string, j int) cg.Expr {
	if b.x.slotPlate(key) == "" {
		return b.e(b.s.Slot(key, b.idsMarg))
	}
	b.representative(b.x.sreads[key])
	n := b.size(b.s.PlateSize(string(b.x.m.Slots[key].Context)))
	v := vector("Prior_v_"+tag+"_"+itoa(j), n)
	b.stmts = append(b.stmts, cg.Declare(v, b.e(b.s.Slot(key, b.idsMarg))))
	return b.at(v, b.integer(1))
}

// Never a refusal: an exception in the generated quantities makes CmdStan write every generated
// quantity of that draw as NaN (measured on the image: the parameters are kept), so a draw that
// throws would lose every marginal of the draw, not only its own, and bias them (a no-mass
// truncation is most likely where a parent's draw is extreme). Every ancestral draw is therefore
// written so that Stan never refuses it: a generator's arguments are clamped into its domain
// (fmax and fmin return the other argument for a NaN), and a draw whose argument is not finite is
// made NaN by adding 0 times it (the browser's rejected draw); a truncated draw is u uniform between
// F(L) and F(U) as L + (U - L) * uniform_rng(0, 1) (uniform_rng refuses equal bounds: a truncation
// with no mass at these arguments gives an infinite draw, not a refusal), with the checking
// functions' arguments clamped (Phi's to ±40, where it is 0 and 1; inc_beta's and inv_inc_beta's
// shapes to [1e-8, 1e8], their probabilities to [0, 1]).

const (
	hugeArg  = 1e300
	tinyArg  = 1e-300
	shapeLow = 1e-8
	shapeHi  = 1e8
)

func (b *builder) clamp(v cg.Expr, lo, hi float64) cg.Expr {
	return b.call("fmin", b.call("fmax", v, b.num(lo)), b.num(hi))
}

// unit is v clamped to [0, 1].
func (b *builder) unit(v cg.Expr) cg.Expr { return b.clamp(v, 0, 1) }

// shape is v clamped to [1e-8, 1e8], unless it is made of the constant arguments named.
func (b *builder) shape(v cg.Expr, of ...string) cg.Expr {
	if b.allFixed(of...) {
		return v
	}
	return b.clamp(v, shapeLow, shapeHi)
}

// allFixed reports arguments of the draw being written that are all constants.
func (b *builder) allFixed(names ...string) bool {
	for _, n := range names {
		if !b.fixed[n] {
			return false
		}
	}
	return true
}

// arguments are a distribution's arguments as a draw reads them: the constants as they are, the
// others held in locals Prior_g_<tag>_<j> (each is read more than once, and a split value is
// written once), declared by the returned statements.
func (b *builder) arguments(vals []cg.Expr, constant []bool, tag string) ([]cg.Expr, []cg.Stmt) {
	refs := make([]cg.Expr, len(vals))
	var out []cg.Stmt
	for j, v := range vals {
		if constant[j] {
			refs[j] = v
			continue
		}
		d := scalar("Prior_g_" + tag + "_" + itoa(j+1))
		out = append(out, cg.Declare(d, v))
		refs[j] = d.Ref()
	}
	return refs, out
}

// nanOf is 0 times the sum of the arguments that are not constants: 0 when they are finite, NaN
// otherwise (none when every argument is a constant).
func (b *builder) nanOf(args []cg.Expr, constant []bool) (cg.Expr, bool) {
	var sum cg.Expr
	some := false
	for j, a := range args {
		if constant[j] {
			continue
		}
		if !some {
			sum, some = a, true
		} else {
			sum = b.add(sum, a)
		}
	}
	if !some {
		return cg.Expr{}, false
	}
	return b.mul(b.num(0), sum), true
}

// draw is the statements that assign target a draw of the distribution with these arguments (a
// constant one as it is), truncated to the parameter's range by inversion when it is; tag names
// its locals (Prior_g_<tag>_<j>, Prior_u_<tag>, Prior_s_<tag>, Prior_q_<tag>).
func (b *builder) draw(target cg.Expr, d bayescatalog.Distribution, vals []cg.Expr, constant []bool, held bool, p bayesgraph.Param, tag string) []cg.Stmt {
	if d.Stan.Quantile == bayescatalog.QuantileLinear {
		// uniform: its arguments are constants, its range within them
		if p.Truncated {
			return []cg.Stmt{cg.Assign(target, b.call("uniform_rng", b.num(p.Range.Lo), b.num(p.Range.Hi)))}
		}
		return []cg.Stmt{cg.Assign(target, b.call("uniform_rng", vals...))}
	}
	args, out := vals, []cg.Stmt(nil)
	if !held {
		args, out = b.arguments(vals, constant, tag)
	}
	if !p.Truncated {
		clamped := make([]cg.Expr, len(args))
		for j, a := range args {
			switch {
			case constant[j]:
				clamped[j] = a
			case d.Params[j].Constraint == bayescatalog.Positive:
				clamped[j] = b.clamp(a, tinyArg, hugeArg)
			default:
				clamped[j] = b.clamp(a, -hugeArg, hugeArg)
			}
		}
		v := b.call(d.Stan.RNG, clamped...)
		if nan, ok := b.nanOf(args, constant); ok {
			v = b.add(v, nan)
		}
		return append(out, cg.Assign(target, v))
	}
	arg := map[string]cg.Expr{}
	b.fixed = map[string]bool{}
	for j, prm := range d.Params {
		arg[prm.Name] = args[j]
		b.fixed[prm.Name] = constant[j]
	}
	lo, hi := b.num(0), b.num(1)
	if p.Range.Lo > p.Support.Lo {
		lo = b.cdf(d.Stan.Quantile, arg, p.Range.Lo)
	}
	if p.Range.Hi < p.Support.Hi {
		hi = b.cdf(d.Stan.Quantile, arg, p.Range.Hi)
	}
	u := scalar("Prior_u_" + tag)
	out = append(out, cg.Declare(u, b.add(lo, b.mul(b.sub(hi, lo), b.call("uniform_rng", b.num(0), b.num(1))))))
	if d.Stan.Quantile == bayescatalog.QuantileIncBeta {
		// inv_inc_beta reads its shapes clamped: a shape that is not finite makes the draw NaN
		v := b.call("inv_inc_beta", b.shape(arg["alpha"], "alpha"), b.shape(arg["beta"], "beta"), b.unit(u.Ref()))
		if nan, ok := b.nanOf(args, constant); ok {
			v = b.add(v, nan)
		}
		return append(out, cg.Assign(target, v))
	}
	return append(out, b.quantile(target, d.Stan.Quantile, arg, u.Ref(), tag)...)
}

// constants say which slots are constants.
func (b *builder) constants(keys []bayesgraph.SlotKey) []bool {
	out := make([]bool, len(keys))
	for j, k := range keys {
		out[j] = b.x.m.Slots[k].Constant != nil
	}
	return out
}

// z is (x - mu) * inv(sigma).
func (b *builder) z(x cg.Expr, arg map[string]cg.Expr) cg.Expr {
	return b.mul(b.sub(x, arg["mu"]), b.call("inv", arg["sigma"]))
}

// phi is Phi(z), z clamped to ±40 unless mu and sigma are constants (Phi is 0 below -37.5 and 1
// above 8.25; it refuses a NaN).
func (b *builder) phi(z cg.Expr) cg.Expr {
	if b.allFixed("mu", "sigma") {
		return b.call("Phi", z)
	}
	return b.call("Phi", b.clamp(z, -40, 40))
}

// cdf is F at the bound v (bayescatalog.Quantile's forms).
func (b *builder) cdf(q bayescatalog.Quantile, arg map[string]cg.Expr, v float64) cg.Expr {
	switch q {
	case bayescatalog.QuantileInvPhi:
		return b.phi(b.z(b.num(v), arg))
	case bayescatalog.QuantileExpInvPhi:
		return b.phi(b.z(b.num(math.Log(v)), arg))
	case bayescatalog.QuantileTan:
		return b.add(b.num(0.5), b.mul(b.call("atan", b.z(b.num(v), arg)), b.num(1/math.Pi)))
	case bayescatalog.QuantileIncBetaT:
		// 0.5 + (step(z) - 0.5) * inc_beta(0.5, 0.5 * nu, z^2 / (nu + z^2))
		w := b.mul(b.call("square", b.z(b.num(v), arg)), b.call("inv", b.add(arg["nu"], b.call("square", b.z(b.num(v), arg)))))
		half := b.shape(b.mul(b.num(0.5), arg["nu"]), "nu")
		if !b.allFixed("nu", "mu", "sigma") {
			w = b.unit(w)
		}
		return b.add(b.num(0.5), b.mul(b.sub(b.call("step", b.z(b.num(v), arg)), b.num(0.5)), b.call("inc_beta", b.num(0.5), half, w)))
	case bayescatalog.QuantileIncBeta:
		return b.call("inc_beta", b.shape(arg["alpha"], "alpha"), b.shape(arg["beta"], "beta"), b.num(v))
	case bayescatalog.QuantileLog1m:
		return b.mul(b.num(-1), b.call("expm1", b.mul(b.num(-v), arg["beta"])))
	}
	b.fail(errNoQuantile)
	return cg.Expr{}
}

// quantile assigns target Q(u).
func (b *builder) quantile(target cg.Expr, q bayescatalog.Quantile, arg map[string]cg.Expr, u cg.Expr, tag string) []cg.Stmt {
	switch q {
	case bayescatalog.QuantileInvPhi:
		return []cg.Stmt{cg.Assign(target, b.add(arg["mu"], b.mul(arg["sigma"], b.call("inv_Phi", b.unit(u)))))}
	case bayescatalog.QuantileExpInvPhi:
		return []cg.Stmt{cg.Assign(target, b.call("exp", b.add(arg["mu"], b.mul(arg["sigma"], b.call("inv_Phi", b.unit(u))))))}
	case bayescatalog.QuantileTan:
		return []cg.Stmt{cg.Assign(target, b.add(arg["mu"], b.mul(arg["sigma"], b.call("tan", b.mul(b.num(math.Pi), b.sub(u, b.num(0.5)))))))}
	case bayescatalog.QuantileIncBetaT:
		// v = inv_inc_beta(0.5, 0.5 * nu, |2u - 1|); x = mu + sigma * (s * sqrt(nu * v * inv(1 - v))), s the sign of
		// u - 0.5 (Prior_s_<tag>), and |2u - 1| = s * (2u - 1): Stan 2.40 has no fabs (measured: stanc refuses it)
		s := scalar("Prior_s_" + tag)
		v := scalar("Prior_q_" + tag)
		half := b.shape(b.mul(b.num(0.5), arg["nu"]), "nu")
		root := b.call("sqrt", b.mul(b.mul(arg["nu"], v.Ref()), b.call("inv", b.sub(b.num(1), v.Ref()))))
		return []cg.Stmt{
			cg.Declare(s, b.sub(b.mul(b.num(2), b.call("step", b.sub(u, b.num(0.5)))), b.num(1))),
			cg.Declare(v, b.call("inv_inc_beta", b.num(0.5), half, b.unit(b.mul(s.Ref(), b.sub(b.mul(b.num(2), u), b.num(1)))))),
			cg.Assign(target, b.add(arg["mu"], b.mul(arg["sigma"], b.mul(s.Ref(), root)))),
		}
	case bayescatalog.QuantileLog1m:
		return []cg.Stmt{cg.Assign(target, b.mul(b.mul(b.num(-1), b.call("log1m", b.unit(u))), b.call("inv", arg["beta"])))}
	}
	b.fail(errNoQuantile)
	return nil
}

// drawsOfNode writes a node's marginals and, when the model has a prior's path, its ancestral draws.
func (b *builder) drawsOfNode(id string) {
	n := b.x.nodes[id]
	switch n.Kind {
	case "parameter":
		b.parameter(n)
	case "transformed", "linear_predictor":
		b.transformed(n)
	case "correlated_effects":
		b.effects(n)
	case "state":
		b.state(n)
	}
}

func (b *builder) marginal(id string) cg.Decl {
	d := cg.Decl{Name: "Prior_m_" + b.tag(id), Elem: cg.Real, Shape: cg.Scalar, Role: bayescontract.RolePriorMarginal, Of: id}
	b.written = append(b.written, d)
	b.marg[id] = d
	return d
}

// priorDraw declares the prior_draw of a quantity: Prior_d_<tag> for a single value, Prior_p_<tag>
// sized by the mode over a plate.
func (b *builder) priorDraw(id string, n cg.Size, single bool) cg.Decl {
	if single {
		d := cg.Decl{Name: "Prior_d_" + b.tag(id), Elem: cg.Real, Shape: cg.Scalar, Role: bayescontract.RolePriorDraw, Of: id}
		b.written = append(b.written, d)
		return d
	}
	d := cg.Decl{Name: "Prior_p_" + b.tag(id), Elem: cg.Real, Shape: cg.Vector, Size: n.WhenPrior(), Role: bayescontract.RolePriorDraw, Of: id}
	b.written = append(b.written, d)
	return d
}

// pathArgs are a slot's values on the prior's path for a loop over the units: a single value in a
// local Prior_a_<tag>_<j> (every run, a few numbers), a value over the plate in a vector sized by the
// mode, filled in a loop of the mode (one iteration in the prior predictive), read at the loop's
// index.
func (b *builder) pathArgs(keys []bayesgraph.SlotKey, tag string, n cg.Size) []cg.Expr {
	out := make([]cg.Expr, len(keys))
	var fill []cg.Stmt
	for j, k := range keys {
		if b.x.slotPlate(k) == "" {
			d := scalar("Prior_a_" + tag + "_" + itoa(j+1))
			b.stmts = append(b.stmts, cg.Declare(d, b.e(b.s.Slot(k, b.idsPath))))
			out[j] = d.Ref()
			continue
		}
		d := vector("Prior_a_"+tag+"_"+itoa(j+1), n.WhenPrior())
		b.stmts = append(b.stmts, cg.Declare(d, cg.Expr{}))
		fill = append(fill, cg.Assign(d.Ref(), b.e(b.s.Slot(k, b.idsPath))))
		out[j] = b.at(d, cg.LoopIndex())
	}
	if len(fill) > 0 {
		b.stmts = append(b.stmts, cg.For(b.one, fill...))
	}
	return out
}

func (b *builder) parameter(n *bayesgraph.Node) {
	id := n.ID
	d, _ := bayescatalog.LookupDistribution(n.Prior.Dist)
	p := b.x.m.Params[id]
	keys := b.x.distKeys(id, "prior.", d)
	tag := b.tag(id)
	if b.a.marg[id] {
		args := make([]cg.Expr, len(keys))
		for j, k := range keys {
			args[j] = b.margArg(k, tag, j+1)
		}
		m := b.marginal(id)
		b.stmts = append(b.stmts, b.draw(m.Ref(), d, args, b.constants(keys), false, p, "m"+tag)...)
	}
	if !b.a.PriorPredictive || !b.a.path[id] {
		return
	}
	if n.Plate == nil {
		b.single(id)
		return
	}
	plate := *n.Plate
	units := b.size(b.s.PlateSize(plate))
	draw := b.priorDraw(id, units, false)
	args := b.pathArgs(keys, "p"+tag, units)
	i := cg.LoopIndex()
	b.stmts = append(b.stmts, cg.For(units.WhenPrior(), b.draw(b.at(draw, i), d, args, b.constants(keys), true, p, "p"+tag)...))
	b.path[id] = draw
	if b.x.plates[plate].Kind == bayesgraph.PlateLevels {
		// the coefficients with the reference's 0 in front, as a value per row reads them (Full_)
		full := vector("Prior_f_"+tag, b.size(b.s.Levels(plate)).WhenPrior())
		b.stmts = append(b.stmts, cg.Declare(full, cg.Expr{}),
			cg.For(b.one, cg.Assign(full.Ref(), b.call("append_row", b.num(0), draw.Ref()))))
		b.path[id] = full
	}
}

// single is a single value's ancestral draw: its marginal, written again as its prior_draw.
func (b *builder) single(id string) {
	m := b.marg[id]
	d := b.priorDraw(id, cg.Size{}, true)
	b.stmts = append(b.stmts, cg.Assign(d.Ref(), m.Ref()))
	b.path[id] = m
}

func (b *builder) transformed(n *bayesgraph.Node) {
	id := n.ID
	if !b.x.quantity(id) {
		return
	}
	tag := b.tag(id)
	single := b.x.singleValue(id)
	if b.a.marg[id] {
		b.representative(b.x.reads[id])
		m := b.marginal(id)
		if n.Plate == nil || single {
			b.stmts = append(b.stmts, cg.Assign(m.Ref(), b.e(b.s.ValueWith(id, b.idsMarg))))
		} else {
			r := vector("Prior_r_"+tag, b.size(b.s.PlateSize(*n.Plate)))
			b.stmts = append(b.stmts, cg.Declare(r, b.e(b.s.ValueWith(id, b.idsMarg))), cg.Assign(m.Ref(), b.at(r, b.integer(1))))
			b.rep[id] = r
		}
	}
	if !b.a.PriorPredictive || !b.a.path[id] {
		return
	}
	if n.Plate == nil {
		b.single(id)
		return
	}
	units := b.size(b.s.PlateSize(*n.Plate))
	draw := b.priorDraw(id, units, false)
	if single {
		v := scalar("Prior_a_p" + tag + "_1")
		b.stmts = append(b.stmts, cg.Declare(v, b.e(b.s.ValueWith(id, b.idsPath))),
			cg.For(units.WhenPrior(), cg.Assign(b.at(draw, cg.LoopIndex()), v.Ref())))
	} else {
		b.stmts = append(b.stmts, cg.For(b.one, cg.Assign(draw.Ref(), b.e(b.s.ValueWith(id, b.idsPath)))))
	}
	b.path[id] = draw
}

// effects are correlated effects: each component's marginal is normal(mean, scale) (a correlation
// does not change it), the correlation's Ω[2,1] is 2 u - 1 with u ~ beta(b, b), b = η - 1 + K/2
// (the LKJ's marginal of one correlation); on the prior's path, the Cholesky factor by the onion
// method (Lewandowski, Kurowicka and Joe 2009, §3.2), then each unit's components
// mean + scale * (L z), z standard normal.
func (b *builder) effects(n *bayesgraph.Node) {
	id := n.ID
	k := len(n.Components)
	var corrKey bayesgraph.SlotKey
	if n.Correlation != nil {
		corrKey = bayesgraph.SlotKey{Node: id + ".correlation", Slot: "eta"}
	}
	betaOf := func(eta cg.Expr, shift float64) cg.Expr {
		if shift == 0 {
			return eta
		}
		return b.add(eta, b.num(shift))
	}
	for _, c := range n.Components {
		cid := id + "." + c.ID
		if !b.a.marg[cid] {
			continue
		}
		tag := b.tag(cid)
		keys := []bayesgraph.SlotKey{{Node: cid, Slot: "mean"}, {Node: cid, Slot: "scale"}}
		args := []cg.Expr{b.margArg(keys[0], tag, 1), b.margArg(keys[1], tag, 2)}
		m := b.marginal(cid)
		b.stmts = append(b.stmts, b.draw(m.Ref(), normal, args, b.constants(keys), false, bayesgraph.Param{}, "m"+tag)...)
	}
	if n.Correlation != nil && b.a.marg[id+".correlation"] {
		m := b.marginal(id + ".correlation")
		beta := betaOf(b.e(b.s.Slot(corrKey, b.idsMarg)), float64(k)/2-1)
		b.stmts = append(b.stmts, cg.Assign(m.Ref(), b.sub(b.mul(b.num(2), b.call("beta_rng", beta, beta)), b.num(1))))
	}
	if !b.a.PriorPredictive {
		return
	}
	for _, c := range n.Components {
		if !b.a.path[id+"."+c.ID] {
			return
		}
	}
	t := itoa(b.x.pos[id])
	// L[i][j], i > 1: the factor's rows below the first (L[1][1] = 1)
	l := make([][]cg.Decl, k+1)
	if n.Correlation != nil {
		var onion []cg.Stmt
		eta := scalar("Prior_oe_" + t)
		onion = append(onion, cg.Declare(eta, betaOf(b.e(b.s.Slot(corrKey, b.idsPath)), float64(k-2)/2)))
		for i := 2; i <= k; i++ {
			l[i] = make([]cg.Decl, i+1)
			for j := 1; j <= i; j++ {
				l[i][j] = scalar("Prior_l_" + t + "_" + itoa(i) + "_" + itoa(j))
				b.stmts = append(b.stmts, cg.Declare(l[i][j], cg.Expr{}))
			}
		}
		r := scalar("Prior_ob_" + t)
		onion = append(onion,
			cg.Declare(r, b.call("beta_rng", eta.Ref(), eta.Ref())),
			cg.Assign(l[2][1].Ref(), b.sub(b.mul(b.num(2), r.Ref()), b.num(1))),
			cg.Assign(l[2][2].Ref(), b.call("sqrt", b.sub(b.num(1), b.call("square", l[2][1].Ref())))))
		for m := 2; m <= k-1; m++ {
			y := scalar("Prior_oy_" + t + "_" + itoa(m))
			beta := b.sub(eta.Ref(), b.num(float64(m-1)/2))
			onion = append(onion, cg.Declare(y, b.call("beta_rng", b.num(float64(m)/2), beta)))
			zs := make([]cg.Decl, m+1)
			var norm cg.Expr
			for i := 1; i <= m; i++ {
				zs[i] = scalar("Prior_oz_" + t + "_" + itoa(m) + "_" + itoa(i))
				onion = append(onion, cg.Declare(zs[i], b.call("normal_rng", b.num(0), b.num(1))))
				sq := b.call("square", zs[i].Ref())
				if i == 1 {
					norm = sq
				} else {
					norm = b.add(norm, sq)
				}
			}
			s := scalar("Prior_os_" + t + "_" + itoa(m))
			onion = append(onion, cg.Declare(s, b.mul(b.call("sqrt", y.Ref()), b.call("inv", b.call("sqrt", norm)))))
			for i := 1; i <= m; i++ {
				onion = append(onion, cg.Assign(l[m+1][i].Ref(), b.mul(s.Ref(), zs[i].Ref())))
			}
			onion = append(onion, cg.Assign(l[m+1][m+1].Ref(), b.call("sqrt", b.sub(b.num(1), y.Ref()))))
		}
		b.stmts = append(b.stmts, cg.For(b.one, onion...))
	}
	units := b.size(b.s.PlateSize(*n.Plate))
	draws := make([]cg.Decl, k+1)
	args := make([][]cg.Expr, k+1)
	for c := 1; c <= k; c++ {
		cid := id + "." + n.Components[c-1].ID
		draws[c] = b.priorDraw(cid, units, false)
		args[c] = b.pathArgs([]bayesgraph.SlotKey{{Node: cid, Slot: "mean"}, {Node: cid, Slot: "scale"}}, "p"+b.tag(cid), units)
	}
	i := cg.LoopIndex()
	var each []cg.Stmt
	z := make([]cg.Decl, k+1)
	for c := 1; c <= k; c++ {
		z[c] = scalar("Prior_e_" + t + "_" + itoa(c))
		each = append(each, cg.Declare(z[c], b.call("normal_rng", b.num(0), b.num(1))))
	}
	for c := 1; c <= k; c++ {
		comb := z[c].Ref()
		if n.Correlation != nil && c > 1 {
			for j := 1; j <= c; j++ {
				term := b.mul(l[c][j].Ref(), z[j].Ref())
				if j == 1 {
					comb = term
				} else {
					comb = b.add(comb, term)
				}
			}
		}
		each = append(each, cg.Assign(b.at(draws[c], i), b.add(args[c][0], b.mul(args[c][1], comb))))
	}
	b.stmts = append(b.stmts, cg.For(units.WhenPrior(), each...))
	for c := 1; c <= k; c++ {
		b.path[id+"."+n.Components[c-1].ID] = draws[c]
	}
}

// state is a random walk: its marginal is its first value's (the init's draw); on the prior's path
// it is drawn along the series' rows the runner names (Prior_first, then Prior_next from
// Prior_prev, the program's Rows_first, Rows_next and Prev in the prior predictive, empty
// otherwise).
func (b *builder) state(n *bayesgraph.Node) {
	id := n.ID
	d, _ := bayescatalog.LookupDistribution(n.Init.Dist)
	keys := b.x.distKeys(id, "init.", d)
	tag := b.tag(id)
	if b.a.marg[id] {
		args := make([]cg.Expr, len(keys))
		for j, k := range keys {
			args[j] = b.margArg(k, tag, j+1)
		}
		m := b.marginal(id)
		b.stmts = append(b.stmts, b.draw(m.Ref(), d, args, b.constants(keys), false, bayesgraph.Param{}, "m"+tag)...)
	}
	if !b.a.PriorPredictive || !b.a.path[id] {
		return
	}
	units := b.size(b.s.PlateSize(*n.Plate))
	draw := b.priorDraw(id, units, false)
	all := append(append([]bayesgraph.SlotKey(nil), keys...), bayesgraph.SlotKey{Node: id, Slot: "scale"})
	args := b.pathArgs(all, "p"+tag, units)
	consts := b.constants(all)
	i := cg.LoopIndex()
	sd := series()
	walk := []cg.Expr{b.index(draw.Ref(), b.at(sd.prev, i)), args[len(keys)]}
	b.stmts = append(b.stmts,
		cg.For(sd.firstN.AsSize(), b.draw(b.index(draw.Ref(), b.at(sd.first, i)), d, args[:len(keys)], consts[:len(keys)], true, bayesgraph.Param{}, "p"+tag)...),
		cg.For(sd.nextN.AsSize(), b.draw(b.index(draw.Ref(), b.at(sd.next, i)), normal, walk, []bool{false, consts[len(keys)]}, true, bayesgraph.Param{}, "w"+tag)...))
	b.path[id] = draw
}

// normal is the catalogue's normal: a component's and a random walk's step.
var normal, _ = bayescatalog.LookupDistribution("normal")

// seriesData are the series' arrays of the prior's path of a state.
type seriesData struct{ firstN, first, nextN, next, prev cg.Decl }

func series() seriesData {
	count := func(name, kind string) cg.Decl {
		return cg.Decl{Name: name, Elem: cg.Int, Shape: cg.Scalar, NonNegative: true, Kind: kind}
	}
	var s seriesData
	s.firstN, s.nextN = count("Prior_first_n", "first_count"), count("Prior_next_n", "next_count")
	s.first = cg.Decl{Name: "Prior_first", Elem: cg.Int, Shape: cg.Array, Size: s.firstN.AsSize(), NonNegative: true, Kind: "prior_first"}
	s.next = cg.Decl{Name: "Prior_next", Elem: cg.Int, Shape: cg.Array, Size: s.nextN.AsSize(), NonNegative: true, Kind: "prior_next"}
	s.prev = cg.Decl{Name: "Prior_prev", Elem: cg.Int, Shape: cg.Array, Size: s.nextN.AsSize(), NonNegative: true, Kind: "prior_previous"}
	return s
}
