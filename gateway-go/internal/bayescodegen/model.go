package bayescodegen

import (
	"fmt"
	"sort"
	"strconv"
	"strings"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/bayescatalog"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/bayescontract"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/bayesexpr"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/bayesgraph"
)

// This file is the values and the statements (docs/specs/bayes-stan-codegen.md, «Linear predictors
// and transformed nodes», «Categories», «Likelihoods»): the EmitEnv every slot is emitted with,
// transformed data (values that depend on no parameter: computed once, never written), the
// transformed parameters (values computed from parameters, single or over a group plate, the
// explicit non-centred parameters, the non-centred states and the components of correlated
// effects: written), the model block's locals (the categories' Full_, the linear predictors and
// transformed nodes on the rows: never written), the priors (and the statements of states and
// effects) and the likelihoods in Order.
//
// lag: a statement or a value per row that uses lag, or reads a value per row that does, runs on
// the rows that have every lag the model uses (Rows_lag, which the runner builds within each unit
// of the series): a likelihood is written y[Rows_lag] ~ …, a value per row is declared over all the
// rows and assigned on Rows_lag only (x[Rows_lag] = …; the other rows are never read), and every
// per-row name in their expressions is taken on Rows_lag by Emit (RowsSubset), lag(x, k) being
// x[Lag<k>]. A lag of a value that is itself computed with lag would read rows where that value is
// not computed: refused (ErrUnsupported).

// emitEnv is the program's EmitEnv for one slot, whose context plate is ctx. A parameter on a
// category's levels read from another plate (per row, through the category) is its Full_<id>, the
// coefficients with the reference's 0; an index is its own array on its plate, its lift
// (Up_<index>_<plate>) on a plate it is lifted to; a lag is Lag<k> on the rows that have every lag
// (subset Rows_lag); subset is the rows the statement runs on (Rows_lag, Rows_first for a state's
// first values, "" for every row). ids, when set, gives an extension's identifiers in place of the
// program's (Scope.ValueWith); use, when set, is told of each node whose identifier is written
// (full: as its Full_), so that the extension's nested block rebuilds the locals it reads. sink is
// where the locals of a split expression go (lot S5; nil where no local can be written: a single
// value, the parameters block), rootLocal makes a split slot's whole value a local (a part of the
// generator's own forms), and locals are the names of the parts already written, by localKey.
// frame, when set, is the rows an extension's value is written on (Scope.SlotOn, ValueOn, or a
// value asked for with ids): every slot emitted in env, the value's own and those of the per-row
// values it rebuilds, is taken on frame's rows. inline, when set, answers for a per-row value that
// reads a node ids replaces: the name of a local holding that value rebuilt with ids on frame's
// rows (so a value of the prior's draws never reads the posterior's per-row values).
type emitEnv struct {
	g         *gen
	ctx       bayesexpr.Plate
	subset    string
	ids       func(node string) (string, bool)
	use       func(node string, full bool)
	sink      *splitSink
	rootLocal bool
	locals    map[string]string
	frame     *rowsFrame
	inline    func(node string) (string, bool)
}

// rowsFrame is the rows an extension's value is written on: the rows its statement runs on (base:
// every row "", Rows_lag, Rows_first) taken at the positions an array of the extension's data
// names among them (on: "" for all of them).
type rowsFrame struct{ base, on string }

// rows is the frame's rows as an index: Rows_lag[Ppc_o1_rows], Ppc_o1_rows, Rows_lag, or "" for
// every row.
func (f *rowsFrame) rows() string { return onRows(f.base, f.on) }

// onRows is base taken at the positions on: base[on], on alone over every row, base alone when on
// is "".
func onRows(base, on string) string {
	switch {
	case on == "":
		return base
	case base == "":
		return on
	}
	return base + "[" + on + "]"
}

func (e emitEnv) Identifier(id string) string {
	if s, ok := e.locals[id]; ok {
		return s
	}
	if e.ids != nil {
		if s, ok := e.ids(id); ok {
			return s
		}
	}
	if e.inline != nil {
		if s, ok := e.inline(id); ok {
			return s
		}
	}
	if f, ok := e.g.full[id]; ok {
		if p, _ := e.g.levelsOf(id); bayesexpr.Plate(p.ID) != e.ctx {
			if e.use != nil {
				e.use(id, true)
			}
			return f
		}
	}
	if e.use != nil {
		e.use(id, false)
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

// LagArray is Lag<k> on the rows that have every lag, for a lag the model declares (taken at the
// frame's positions among those rows: Lag<k>[Ppc_o1_rows]); "" otherwise (Emit refuses it).
func (e emitEnv) LagArray(k int) string {
	on := ""
	switch {
	case e.frame != nil:
		if e.frame.base != identRowsLag {
			return ""
		}
		on = e.frame.on
	case e.subset != identRowsLag:
		return ""
	}
	for _, l := range e.g.m.Lags {
		if l == k {
			return onRows(identLagPrefix+strconv.Itoa(k), on)
		}
	}
	return ""
}

func (e emitEnv) RowsSubset() string { return e.subset }

// subsetOf is the rows a slot's statement runs on: Rows_first for a state's init, Rows_lag for a
// lagged value or likelihood, every row ("") otherwise.
func (g *gen) subsetOf(key bayesgraph.SlotKey) string {
	if n := g.node(key.Node); n != nil && n.Kind == "state" && strings.HasPrefix(key.Slot, "init.") {
		return identRowsFirst
	}
	if g.lagged[key.Node] {
		return identRowsLag
	}
	return ""
}

// slotValue is a slot's text: its expression through Emit, or the identifier of the node it refers
// to; with the value's type. It is written on the rows its statement runs on (subsetOf); the locals
// of a large vector expression go to sink (nil: none may be written, a bug if one is needed).
func (g *gen) slotValue(key bayesgraph.SlotKey, sink *splitSink) (string, bayesexpr.Type, error) {
	return g.emitSlot(key, emitEnv{g: g, sink: sink})
}

// emitSlot is a slot's text in env, whose context and subset it sets.
func (g *gen) emitSlot(key bayesgraph.SlotKey, env emitEnv) (string, bayesexpr.Type, error) {
	t, ok := g.m.Slots[key]
	if !ok {
		return "", bayesexpr.Type{}, bug("the slot %s.%s did not type in a valid model", key.Node, key.Slot)
	}
	env.ctx, env.subset = t.Context, g.subsetOf(key)
	if env.frame != nil {
		// A value on the rows that have every lag is never read on other rows (the lag rule of
		// indexLags): a frame of every row cannot hold it.
		if env.subset == identRowsLag && env.frame.base != identRowsLag || env.subset == identRowsFirst && env.frame.base != identRowsFirst {
			return "", t.Type, bug("the slot %s.%s on %s taken on the rows %q", key.Node, key.Slot, env.subset, env.frame.rows())
		}
		env.subset = env.frame.rows()
	}
	if t.Expr != nil {
		s, err := g.emitSplit(key, t.Expr, env)
		if err != nil {
			return "", t.Type, fmt.Errorf("slot %s.%s: %w", key.Node, key.Slot, err)
		}
		return s, t.Type, nil
	}
	if t.Ref == "" {
		return "", t.Type, bug("the slot %s.%s has neither an expression nor a ref", key.Node, key.Slot)
	}
	x := env.Identifier(t.Ref)
	if x == "" {
		return "", t.Type, bug("the slot %s.%s refers to %q, which has no identifier", key.Node, key.Slot, t.Ref)
	}
	if t.Type.Plate != "" && env.subset != "" {
		x += "[" + env.subset + "]"
	}
	return x, t.Type, nil
}

// overPlate is a value made a vector of size over a plate (the rows or a group plate): a vector as
// it is, an integer array through to_vector, a single real repeated size times.
func (g *gen) overPlate(text string, t bayesexpr.Type, plate, size string) (string, error) {
	switch {
	case t.Plate == "" && t.Elem == bayesexpr.ElemReal:
		return call("rep_vector", text, size)
	case t.Plate == bayesexpr.Plate(plate) && t.Elem == bayesexpr.ElemReal:
		return text, nil
	case t.Plate == bayesexpr.Plate(plate) && t.Elem == bayesexpr.ElemInt:
		return call("to_vector", text)
	}
	return "", bug("a value of type %s where a vector over %q is needed", t, plate)
}

// readsParameter reports whether a node's value depends on a parameter: a slot of it reads a
// parameter, a state, a component of correlated effects, a linear predictor (always placed after
// the parameters), or a transformed node that does.
func (g *gen) readsParameter(id string, seen map[string]bool) bool {
	if seen[id] {
		return false
	}
	seen[id] = true
	for _, r := range g.reads[id] {
		if strings.IndexByte(r, '.') >= 0 {
			return true // a component of correlated effects
		}
		n := g.node(r)
		if n == nil {
			continue
		}
		switch n.Kind {
		case "parameter", "state", "correlated_effects", "linear_predictor":
			return true
		case "transformed":
			if g.readsParameter(r, seen) {
				return true
			}
		}
	}
	return false
}

// indexReads gathers what each node's slots read (the validated model's Refs) and its slots, by
// node (a component's and a correlation's slots are their node's).
func (g *gen) indexReads() {
	g.reads = map[string][]string{}
	g.slotKeys = map[string][]bayesgraph.SlotKey{}
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
		g.slotKeys[owner] = append(g.slotKeys[owner], k)
	}
}

// indexTransformedData marks the transformed nodes that depend on no parameter: transformed data.
func (g *gen) indexTransformedData() {
	for _, id := range g.m.Order {
		if n := g.node(id); n.Kind == "transformed" && !g.readsParameter(id, map[string]bool{}) {
			g.tdata[id] = true
		}
	}
}

// refsOf adds the nodes a typed tree reads as values (plain) and through lag (lagged).
func refsOf(t *bayesexpr.Typed, plain, lagged map[string]bool) {
	if t == nil || t.Node == nil {
		return
	}
	if t.Sym != nil {
		if t.Node.Kind == bayesexpr.KindLag {
			lagged[t.Sym.ID] = true
		} else {
			plain[t.Sym.ID] = true
		}
	}
	for _, a := range t.Args {
		refsOf(a, plain, lagged)
	}
}

func sortedKeys(m map[string]bool) []string {
	out := make([]string, 0, len(m))
	for k := range m {
		out = append(out, k)
	}
	sort.Strings(out)
	return out
}

// indexLags marks the lagged nodes (in Order, so what a node reads is marked before it): a value
// per row or a likelihood on the rows with lag in a slot, or reading a lagged value. A state's init
// that would run on Rows_lag, and the lag of a lagged value, are refused.
func (g *gen) indexLags() error {
	for _, id := range g.m.Order {
		n := g.node(id)
		lag := false
		plain, viaLag := map[string]bool{}, map[string]bool{}
		for _, k := range g.slotKeys[id] {
			t := g.m.Slots[k]
			if t.Expr == nil {
				plain[t.Ref] = true
				continue
			}
			if len(t.Expr.Lags) > 0 {
				lag = true
			}
			refsOf(t.Expr.Tree, plain, viaLag)
		}
		for _, r := range sortedKeys(viaLag) {
			if g.lagged[r] {
				return unsupported("the node %q takes the lag of %q, which is itself computed with lag", id, r)
			}
		}
		for _, r := range sortedKeys(plain) {
			if g.lagged[r] {
				lag = true
			}
		}
		if !lag {
			continue
		}
		if len(g.m.Lags) == 0 {
			return bug("the node %q uses lag in a model without lags", id)
		}
		switch {
		case (n.Kind == "transformed" || n.Kind == "linear_predictor") && n.Plate != nil && *n.Plate == g.rows.ID:
		case n.Kind == "likelihood" && g.node(n.Observed) != nil && g.node(n.Observed).Plate != nil &&
			*g.node(n.Observed).Plate == g.rows.ID:
		case n.Kind == "state":
			return unsupported("the init of the state %q uses lag", id)
		default:
			return bug("the %s %q uses lag off the rows", n.Kind, id)
		}
		g.lagged[id] = true
	}
	return nil
}

// placement is where a transformed node or a linear predictor is written.
type placement int

const (
	placeNone        placement = iota
	placeData                  // transformed data: a value that depends on no parameter, not written
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
	if g.tdata[n.ID] {
		return placeData, nil
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
	// graph.node_plate: a transformed node or a linear predictor is single, per row or on a group
	// plate, never on a category's levels.
	return placeNone, bug("the %s %q on the plate %q", n.Kind, n.ID, *n.Plate)
}

// writeTransformedData writes the values that depend on no parameter, computed once.
func (g *gen) writeTransformedData() error {
	for _, id := range g.m.Order {
		if !g.tdata[id] {
			continue
		}
		n := g.node(id)
		self := g.entry(n, blockTransformedData, "")
		if n.Plate != nil {
			if err := g.writeVectorValue(n, emitEnv{g: g}, blockTransformedData, &self); err != nil {
				return err
			}
			continue
		}
		if err := g.writeSingleValue(n); err != nil {
			return err
		}
		g.mapping = append(g.mapping, self)
	}
	return nil
}

// writeSingleValue writes "real x = …;": a single transformed value.
func (g *gen) writeSingleValue(n *bayesgraph.Node) error {
	v, t, err := g.slotValue(bayesgraph.SlotKey{Node: n.ID, Slot: "value"}, nil)
	if err != nil {
		return err
	}
	if t.Plate != "" || t.Elem != bayesexpr.ElemReal {
		return bug("the single transformed node %q has a value of type %s", n.ID, t)
	}
	g.w.line("real " + g.ident[n.ID] + " = " + v + ";")
	return nil
}

func (g *gen) writeTransformedParameters() error {
	for _, id := range g.m.Order {
		n := g.node(id)
		switch n.Kind {
		case "state":
			if g.states[id].nonCentred {
				if err := g.writeStateTransformed(n); err != nil {
					return err
				}
			}
			continue
		case "correlated_effects":
			if err := g.writeEffectTransformed(n); err != nil {
				return err
			}
			continue
		}
		p, err := g.place(n)
		if err != nil {
			return err
		}
		if p != placeTransformed {
			continue
		}
		self := g.entry(n, blockTransformed, bayescontract.RoleQuantity)
		var dims []bayescontract.Dim
		switch {
		case n.Kind == "parameter":
			if err := g.writeExplicit(n, &self); err != nil {
				return err
			}
			dims = []bayescontract.Dim{{Plate: *n.Plate}}
		case n.Plate == nil:
			if err := g.writeSingleValue(n); err != nil {
				return err
			}
			g.mapping = append(g.mapping, self)
			dims = []bayescontract.Dim{}
		default:
			if err := g.writeVectorValue(n, emitEnv{g: g}, blockTransformed, &self); err != nil {
				return err
			}
			dims = []bayescontract.Dim{{Plate: *n.Plate}}
		}
		g.quantities = append(g.quantities, bayescontract.Quantity{Stan: g.ident[id], Role: bayescontract.RoleQuantity, Node: id, Dims: dims})
	}
	return nil
}

// writeVectorValue writes a value over a plate (the rows or a group plate): a transformed node's
// value, or a linear predictor's sum, declared in block with its mapping entry self. A lagged value
// is declared over every row and assigned on Rows_lag only. A value split into locals (lot S5) has
// them before it in the model block; in transformed data and transformed parameters, where a
// declaration of the block is a variable of the block, the value is declared, then assigned in a
// nested block that holds the locals. block "" is the extension's nested block, where the model
// block's per-row locals are written again with the same names (self nil: no entry).
func (g *gen) writeVectorValue(n *bayesgraph.Node, env emitEnv, block string, self *bayescontract.MappingEntry) error {
	sink := &splitSink{block: block, rebuild: block == ""}
	env.sink = sink
	v, t, err := g.vectorValue(n, env)
	if err != nil {
		return err
	}
	plate, x := *n.Plate, g.ident[n.ID]
	lagged := g.lagged[n.ID]
	over := g.size[plate]
	if lagged {
		over = identNLag
	}
	if v, err = g.overPlate(v, t, plate, over); err != nil {
		return err
	}
	decl := "vector[" + g.size[plate] + "] " + x
	target := x
	if lagged {
		target += "[" + identRowsLag + "]"
	}
	declared := func() {
		if self != nil {
			g.mapping = append(g.mapping, *self)
		}
	}
	if !sink.empty() && (block == blockTransformedData || block == blockTransformed) {
		g.w.line(decl + ";")
		declared()
		g.w.openNested()
		g.flush(sink)
		g.w.line(target + " = " + v + ";")
		g.w.close()
		return nil
	}
	g.flush(sink)
	if !lagged {
		g.w.line(decl + " = " + v + ";")
		declared()
		return nil
	}
	g.w.line(decl + ";")
	declared()
	g.w.line(target + " = " + v + ";")
	return nil
}

// vectorValue is the text of a transformed node's value or of a linear predictor's sum.
func (g *gen) vectorValue(n *bayesgraph.Node, env emitEnv) (string, bayesexpr.Type, error) {
	if n.Kind == "transformed" {
		return g.emitSlot(bayesgraph.SlotKey{Node: n.ID, Slot: "value"}, env)
	}
	return g.linearPredictor(n, env)
}

// linearPredictor is a linear predictor's sum, intercept first, then each term in the node's
// order, left-associated: a coefficient times a real data vector, an integer data through
// to_vector, a category's coefficients by its level (Full_<b>[c]); each data on Rows_lag when the
// predictor is lagged. A long sum is split as an expression is (lot S5): each term holds three nodes
// (the product, the coefficient, the data) and its addition one, a single-value intercept one, and
// when the next term would make the sum hold more than maxLocalNodes, the sum so far becomes a local
// of env.sink.
func (g *gen) linearPredictor(n *bayesgraph.Node, env emitEnv) (string, bayesexpr.Type, error) {
	plate := *n.Plate
	over := bayesexpr.Plate(plate)
	sum, typ := "", bayesexpr.Type{Elem: bayesexpr.ElemReal}
	rows := ""
	switch {
	case env.frame != nil:
		if g.lagged[n.ID] && env.frame.base != identRowsLag {
			return "", typ, bug("the lagged linear predictor %q taken on the rows %q", n.ID, env.frame.rows())
		}
		rows = env.frame.rows()
	case g.lagged[n.ID]:
		rows = identRowsLag
	}
	subset := ""
	if rows != "" {
		subset = "[" + rows + "]"
	}
	resid, locals := 0, 0
	if n.Intercept != nil {
		key := bayesgraph.SlotKey{Node: n.ID, Slot: "intercept"}
		v, t, err := g.emitSlot(key, env)
		if err != nil {
			return "", typ, err
		}
		if resid, _, err = g.slotResid(key); err != nil {
			return "", typ, err
		}
		resid = max(resid, 1)
		if t.Plate != "" {
			if t.Elem == bayesexpr.ElemInt {
				if v, err = call("to_vector", v); err != nil {
					return "", typ, err
				}
			}
			typ.Plate = over
		}
		sum = v
	}
	env.ctx = over
	for i, term := range n.Terms {
		key := bayesgraph.SlotKey{Node: n.ID, Slot: fmt.Sprintf("terms[%d].coef", i)}
		d := g.node(term.Data)
		if d == nil || d.Kind != "data" || d.Plate == nil || *d.Plate != plate {
			return "", typ, bug("term %d of %q is not a data node on its plate", i, n.ID)
		}
		x := env.Identifier(d.ID) + subset
		var product string
		switch d.Type {
		case "real", "int":
			coef, ct, err := g.emitSlot(key, env)
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
			if _, ok := g.full[b]; !ok {
				return "", typ, bug("the category term %d of %q reads %q, which has no Full_", i, n.ID, b)
			}
			product = "(" + env.Identifier(b) + "[" + x + "])"
		default:
			return "", typ, bug("term %d of %q reads a %s data node", i, n.ID, d.Type)
		}
		switch {
		case sum == "":
			sum, resid = product, 3
		default:
			if resid+4 > maxLocalNodes {
				if env.sink == nil {
					return "", typ, bug("the sum of %q needs a local where none can be written", n.ID)
				}
				ident, err := g.localName(env.sink, n.ID+"\x00#sum", n.ID, locals)
				if err != nil {
					return "", typ, err
				}
				locals++
				env.sink.declareLocal(ident, g.size[plate], rows, sum)
				sum, resid = ident+subset, 1
			}
			sum, resid = "("+sum+" + "+product+")", resid+4
		}
		typ.Plate = over
	}
	if sum == "" {
		return "", typ, bug("the linear predictor %q has neither an intercept nor a term", n.ID)
	}
	return sum, typ, nil
}

// likelihood is "y ~ dist(args)", vectorized over the observed node's plate (on Rows_lag when it
// is lagged: y[Rows_lag]), the arguments in Stan's order; the locals of a split argument go to sink.
func (g *gen) likelihood(n *bayesgraph.Node, sink *splitSink) (string, error) {
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
		v, _, err := g.slotValue(bayesgraph.SlotKey{Node: n.ID, Slot: "args." + a.Name}, sink)
		if err != nil {
			return "", err
		}
		args = append(args, v)
	}
	lhs := g.ident[obs.ID]
	if g.lagged[n.ID] {
		lhs += "[" + identRowsLag + "]"
	}
	return sampling(lhs, d, args, "")
}

// writeFull writes "vector[K_c] Full_<b> = append_row(0.0, b);".
func (g *gen) writeFull(id string) error {
	p, _ := g.levelsOf(id)
	v, err := call("append_row", "0.0", g.ident[id])
	if err != nil {
		return err
	}
	g.w.line("vector[" + g.size[p.ID] + "] " + g.full[id] + " = " + v + ";")
	return nil
}

// writeModel writes the model block: the categories' Full_ (the coefficients with the reference
// level's 0 in front), the per-row locals, the priors (with the statements of the states and the
// correlated effects) and the likelihoods, each in Order.
func (g *gen) writeModel() error {
	w := g.w
	w.open(blockModel)
	for _, id := range g.m.Order {
		if _, ok := g.full[id]; !ok {
			continue
		}
		if err := g.writeFull(id); err != nil {
			return err
		}
		g.mapping = append(g.mapping, g.internal(g.node(id), g.full[id], blockModel, ""))
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
		self := g.entry(n, blockModel, "")
		if err := g.writeVectorValue(n, emitEnv{g: g}, blockModel, &self); err != nil {
			return err
		}
	}
	for _, id := range g.m.Order {
		var err error
		switch n := g.node(id); n.Kind {
		case "parameter":
			err = g.writePrior(n)
		case "state":
			err = g.writeStateStatements(n)
		case "correlated_effects":
			err = g.writeEffectStatements(n)
		}
		if err != nil {
			return err
		}
	}
	for _, id := range g.m.Order {
		if n := g.node(id); n.Kind == "likelihood" {
			sink := &splitSink{block: blockModel}
			s, err := g.likelihood(n, sink)
			if err != nil {
				return err
			}
			g.flush(sink)
			w.line(s + ";")
		}
	}
	w.close()
	return nil
}
