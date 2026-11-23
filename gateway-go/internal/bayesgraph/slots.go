package bayesgraph

import (
	"errors"
	"sort"
	"strconv"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/bayescatalog"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/bayesexpr"
)

// This file is the rules of the slots (docs/specs/bayes-model-graph.md, «Validation»):
// graph.slot, expr.* (the expression's own error, with the node and the slot), graph.slot_shape,
// graph.slot_interval, graph.slot_maybe, graph.constant, graph.prior_data, and graph.too_big for
// max_total_expression_nodes.
//
// The slots and their context plate:
//
//	parameter          prior.<arg>          the parameter's plate
//	transformed        value (required)     the node's plate
//	linear_predictor   intercept (or null), terms[<i>].coef
//	likelihood         args.<arg>           the observed data node's plate (the likelihood's own when
//	                                        it observes no data node: graph.likelihood says why)
//	state              init.<arg>, scale (required, a single value)
//	correlated_effects <node>.<component>: mean, scale (single values); <node>.correlation: eta (a
//	                   single constant value)
//
// What an argument needs is its catalogue constraint: positive is (0, ∞), a non-negative integer
// [0, ∞), real nothing; an argument the catalogue marks constant (uniform's alpha and beta, LKJ's
// eta) must be a constant: a number, possibly negated (one or more unary minus). A function's
// argument needs the function's domain. A value whose interval is disjoint from what it needs is
// graph.slot_interval (an error), one whose interval overlaps it without being inside is
// graph.slot_maybe (a warning).

// TypedSlot is a slot that was typed: its context plate, its checked expression (nil for a ref) or
// the id it refers to, its type, its static interval, its value when it is a constant, the ids of
// the values it reads (names, x of x[g], lag's x, the ref) and of the indices it indexes with
// (g of x[g]), each sorted and distinct.
type TypedSlot struct {
	Context  bayesexpr.Plate
	Expr     *bayesexpr.Checked
	Ref      string
	Type     bayesexpr.Type
	Interval bayesexpr.Interval
	Constant *float64
	Refs     []string
	Indices  []string
}

// slotSpec is a slot as the validator finds it in the graph, with what it needs.
type slotSpec struct {
	key      SlotKey
	owner    *Node
	slot     *Slot // nil: a required slot that is missing
	ctx      bayesexpr.Plate
	scalar   bool   // a single value is needed
	need     string // the catalogue interval the value must be in ("" for none)
	constant bool   // a constant is needed
	prior    bool   // a prior's slot: it must not reach data
	tree     *bayesexpr.Node
	ok       bool // one expr that parses, or one ref
}

func constraintNeed(c bayescatalog.Constraint) string {
	switch c {
	case bayescatalog.Positive:
		return "positive"
	case bayescatalog.NonNegInt:
		return "non_negative"
	}
	return ""
}

// collectSlots lists every slot of the graph and reads it: graph.slot for a missing, empty or
// double slot, the syntax's expr.* for an expression that does not parse, graph.too_big when all
// the syntax trees together exceed max_total_expression_nodes.
func (v *validation) collectSlots() {
	for _, n := range v.nodeList() {
		ctx := plateOf(n)
		switch n.Kind {
		case "parameter":
			if n.Prior != nil {
				v.distSlots(n, "prior.", n.Prior.Dist, n.Prior.Args, ctx, true)
			}
		case "transformed":
			v.spec(&slotSpec{key: SlotKey{n.ID, "value"}, owner: n, slot: n.Value, ctx: ctx})
		case "linear_predictor":
			if n.Intercept != nil {
				v.spec(&slotSpec{key: SlotKey{n.ID, "intercept"}, owner: n, slot: n.Intercept, ctx: ctx})
			}
			for i := range n.Terms {
				v.spec(&slotSpec{key: SlotKey{n.ID, "terms[" + strconv.Itoa(i) + "].coef"}, owner: n, slot: &n.Terms[i].Coef, ctx: ctx})
			}
		case "likelihood":
			if obs, ok := v.env.nodes[n.Observed]; ok && obs.Kind == "data" {
				ctx = plateOf(obs)
			}
			v.distSlots(n, "args.", n.Dist, n.Args, ctx, false)
		case "state":
			if n.Init != nil {
				v.distSlots(n, "init.", n.Init.Dist, n.Init.Args, ctx, true)
			}
			v.spec(&slotSpec{key: SlotKey{n.ID, "scale"}, owner: n, slot: n.Scale, ctx: ctx, scalar: true, need: "positive", prior: true})
		case "correlated_effects":
			for i := range n.Components {
				k := &n.Components[i]
				id := n.ID + "." + k.ID
				v.spec(&slotSpec{key: SlotKey{id, "mean"}, owner: n, slot: &k.Mean, ctx: ctx, scalar: true, prior: true})
				v.spec(&slotSpec{key: SlotKey{id, "scale"}, owner: n, slot: &k.Scale, ctx: ctx, scalar: true, need: "positive", prior: true})
			}
			if n.Correlation != nil {
				s := &slotSpec{key: SlotKey{n.ID + ".correlation", "eta"}, owner: n, slot: &n.Correlation.Eta, ctx: ctx, scalar: true}
				if d, ok := bayescatalog.LookupDistribution("lkj_corr_cholesky"); ok {
					for _, p := range d.Params {
						if p.Name == "eta" {
							s.need, s.constant = constraintNeed(p.Constraint), p.Constant
						}
					}
				}
				v.spec(s)
			}
		}
	}
	total := 0
	for _, s := range v.specs {
		total += countNodes(s.tree)
	}
	if total > v.lim.MaxTotalExpressionNode {
		v.out = append(v.out, tooBig("expression_nodes", total, v.lim.MaxTotalExpressionNode)...)
	}
}

// distSlots lists a distribution's argument slots, in the order of their names, with what the
// catalogue says each needs (nothing for an argument it does not know: graph.prior,
// graph.likelihood or graph.state report it).
func (v *validation) distSlots(n *Node, prefix, dist string, args map[string]Slot, ctx bayesexpr.Plate, prior bool) {
	d, known := bayescatalog.LookupDistribution(dist)
	names := make([]string, 0, len(args))
	for a := range args {
		names = append(names, a)
	}
	sort.Strings(names)
	for _, a := range names {
		slot := args[a]
		s := &slotSpec{key: SlotKey{n.ID, prefix + a}, owner: n, slot: &slot, ctx: ctx, prior: prior}
		if known {
			for _, p := range d.Params {
				if p.Name == a {
					s.need, s.constant = constraintNeed(p.Constraint), p.Constant
				}
			}
		}
		v.spec(s)
	}
}

// spec reads one slot.
func (v *validation) spec(s *slotSpec) {
	v.specs = append(v.specs, s)
	switch {
	case s.slot == nil:
		v.error("graph.slot", s.key.Node, s.key.Slot, map[string]any{"reason": "missing"})
	case s.slot.Expr == nil && s.slot.Ref == nil:
		v.error("graph.slot", s.key.Node, s.key.Slot, map[string]any{"reason": "empty"})
	case s.slot.Expr != nil && s.slot.Ref != nil:
		v.error("graph.slot", s.key.Node, s.key.Slot, map[string]any{"reason": "both"})
	case s.slot.Ref != nil:
		s.ok = true
	default:
		tree, err := bayesexpr.Parse(*s.slot.Expr)
		if err != nil {
			v.exprError(s, err)
			return
		}
		s.tree, s.ok = tree, true
	}
}

// exprError is an expression's error as a finding: its code, the node and the slot, its args with
// its position (pos, len: code points of the slot's text).
func (v *validation) exprError(s *slotSpec, err error) {
	var e *bayesexpr.Error
	if !errors.As(err, &e) {
		v.error("graph.slot", s.key.Node, s.key.Slot, map[string]any{"reason": "unreadable"})
		return
	}
	args := map[string]any{"pos": e.Pos, "len": e.Len}
	for k, a := range e.Args {
		args[k] = a
	}
	v.error(string(e.Code), s.key.Node, s.key.Slot, args)
}

func countNodes(n *bayesexpr.Node) int {
	if n == nil {
		return 0
	}
	c := 1
	for _, a := range n.Args {
		c += countNodes(a)
	}
	return c
}

// constantValue is the value of a constant: a number under zero or more unary minus.
func constantValue(n *bayesexpr.Node) (float64, bool) {
	sign := 1.0
	for n != nil && n.Kind == bayesexpr.KindNeg {
		sign = -sign
		n = n.Args[0]
	}
	if n == nil || n.Kind != bayesexpr.KindNum {
		return 0, false
	}
	return sign * n.Num, true
}

// constantOf is the value of the slot when it is a constant expression.
func (v *validation) constantOf(key SlotKey) (float64, bool) {
	for _, s := range v.specs {
		if s.key == key {
			if !s.ok || s.tree == nil {
				return 0, false
			}
			return constantValue(s.tree)
		}
	}
	return 0, false
}

// reads are the ids a slot reads (values) and indexes with, from its syntax tree or its ref,
// whether or not it types: a name not in the environment is left out.
func (v *validation) reads(s *slotSpec) (refs, indices []string) {
	if !s.ok {
		return nil, nil
	}
	if s.slot.Ref != nil {
		return []string{*s.slot.Ref}, nil
	}
	seenR, seenI := map[string]bool{}, map[string]bool{}
	var walk func(n *bayesexpr.Node)
	walk = func(n *bayesexpr.Node) {
		switch n.Kind {
		case bayesexpr.KindName, bayesexpr.KindIndex, bayesexpr.KindLag:
			if sym, ok := v.env.byName[n.Name]; ok {
				seenR[sym.ID] = true
			}
			if n.Kind == bayesexpr.KindIndex {
				if sym, ok := v.env.byName[n.By]; ok {
					seenI[sym.ID] = true
				}
			}
		}
		for _, a := range n.Args {
			walk(a)
		}
	}
	walk(s.tree)
	return sortedKeys(seenR), sortedKeys(seenI)
}

func sortedKeys(m map[string]bool) []string {
	if len(m) == 0 {
		return nil
	}
	l := make([]string, 0, len(m))
	for k := range m {
		l = append(l, k)
	}
	sort.Strings(l)
	return l
}

// typeSlots types every well-formed slot against the environment: graph.slot for a ref to nothing
// or to a value that is not a number, the checker's expr.*, then graph.slot_shape, graph.constant,
// graph.slot_interval and graph.slot_maybe.
func (v *validation) typeSlots() {
	for _, s := range v.specs {
		if !s.ok {
			continue
		}
		t, ok := v.typeSlot(s)
		if !ok {
			continue
		}
		t.Refs, t.Indices = v.reads(s)
		v.slots[s.key] = t
		v.shape(s, t)
		if s.constant && t.Constant == nil {
			v.error("graph.constant", s.key.Node, s.key.Slot, nil)
		}
		if s.need != "" {
			v.within(s, t.Interval, bayesexpr.NamedInterval(s.need), map[string]any{"need": s.need})
		}
		if t.Expr != nil {
			v.domains(s, t.Expr.Tree)
		}
	}
}

func (v *validation) typeSlot(s *slotSpec) (TypedSlot, bool) {
	t := TypedSlot{Context: s.ctx}
	if s.slot.Ref != nil {
		id := *s.slot.Ref
		sym, found := v.env.lookupID(id)
		if !found {
			v.error("graph.slot", s.key.Node, s.key.Slot, map[string]any{"reason": "unknown_ref", "ref": id})
			return t, false
		}
		if sym.Type.Elem != bayesexpr.ElemReal && sym.Type.Elem != bayesexpr.ElemInt {
			v.error("graph.slot", s.key.Node, s.key.Slot, map[string]any{"reason": "not_number", "ref": id})
			return t, false
		}
		t.Ref, t.Type, t.Interval = id, sym.Type, bayesexpr.Unbounded()
		if sym.Interval != nil {
			t.Interval = *sym.Interval
		}
		return t, true
	}
	c, err := bayesexpr.Check(s.tree, slotEnv{graphEnv: v.env, ctx: s.ctx})
	if err != nil {
		v.exprError(s, err)
		return t, false
	}
	t.Expr, t.Type, t.Interval = c, c.Tree.Type, c.Interval()
	if k, ok := constantValue(s.tree); ok {
		t.Constant = &k
	}
	return t, true
}

// shape: graph.slot_shape, a single value where one is needed, otherwise a single value or one
// over the context plate. (A typed expression is always one of the two; a ref may be over any
// plate, and a ref cannot be indexed.)
func (v *validation) shape(s *slotSpec, t TypedSlot) {
	p := t.Type.Plate
	switch {
	case p == "":
	case s.scalar:
		v.error("graph.slot_shape", s.key.Node, s.key.Slot, map[string]any{"need": "scalar", "plate": string(p)})
	case p != s.ctx:
		args := map[string]any{"need": "context", "plate": string(p)}
		if s.ctx != "" {
			args["context"] = string(s.ctx)
		}
		v.error("graph.slot_shape", s.key.Node, s.key.Slot, args)
	}
}

// within reports how a value's interval stands to what it needs: disjoint is graph.slot_interval,
// overlapping graph.slot_maybe.
func (v *validation) within(s *slotSpec, have, need bayesexpr.Interval, args map[string]any) {
	switch have.Within(need) {
	case bayesexpr.Disjoint:
		v.error("graph.slot_interval", s.key.Node, s.key.Slot, args)
	case bayesexpr.Overlap:
		v.add(Warning, "graph.slot_maybe", s.key.Node, s.key.Slot, args)
	}
}

// domains: every call's argument against the function's domain, once per function and relation.
func (v *validation) domains(s *slotSpec, t *bayesexpr.Typed) {
	seen := map[string]bool{}
	var walk func(t *bayesexpr.Typed)
	walk = func(t *bayesexpr.Typed) {
		for _, a := range t.Args {
			walk(a)
		}
		if t.Node.Kind != bayesexpr.KindCall || len(t.Args) != 1 {
			return
		}
		f, ok := bayescatalog.LookupFunction(t.Node.Name)
		if !ok || f.Domain == "" || f.Domain == string(bayescatalog.RealLine) {
			return
		}
		rel := t.Args[0].Interval().Within(bayesexpr.NamedInterval(f.Domain))
		key := f.Name + "/" + strconv.Itoa(int(rel))
		if rel == bayesexpr.Inside || seen[key] {
			return
		}
		seen[key] = true
		v.within(s, t.Args[0].Interval(), bayesexpr.NamedInterval(f.Domain), map[string]any{"need": f.Domain, "function": f.Name})
	}
	walk(t)
}

// priorData: graph.prior_data. A prior's argument, a state's init and scale, a component's mean
// and scale read parameters and values computed from parameters, never a data node: directly, or
// through transformed and linear_predictor nodes (their slots, and a linear predictor's terms'
// data). An index (g of x[g]) is the plates' structure, not data: a nested level's prior reads its
// parent's value through a lifted index. The finding names the data node with the smallest id the
// slot reaches and, when it is reached through a node, the node the slot reads it through (the
// smallest such id); a data node read directly is reported without one.
func (v *validation) priorData() {
	// what each transformed and linear_predictor node reads
	through := map[string][]string{}
	for _, s := range v.specs {
		if k := s.owner.Kind; k == "transformed" || k == "linear_predictor" {
			r, _ := v.reads(s)
			through[s.owner.ID] = append(through[s.owner.ID], r...)
		}
	}
	for _, n := range v.nodeList() {
		if n.Kind == "linear_predictor" {
			for _, t := range n.Terms {
				through[n.ID] = append(through[n.ID], t.Data)
			}
		}
	}
	isData := func(id string) bool { n, ok := v.env.nodes[id]; return ok && n.Kind == "data" }
	isVia := func(id string) bool {
		n, ok := v.env.nodes[id]
		return ok && (n.Kind == "transformed" || n.Kind == "linear_predictor")
	}
	// minData is the smallest data id reachable from a transformed or linear_predictor node.
	minData := func(start string) string {
		best, seen, queue := "", map[string]bool{start: true}, []string{start}
		for len(queue) > 0 {
			cur := queue[0]
			queue = queue[1:]
			for _, r := range through[cur] {
				switch {
				case isData(r):
					if best == "" || r < best {
						best = r
					}
				case isVia(r) && !seen[r]:
					seen[r] = true
					queue = append(queue, r)
				}
			}
		}
		return best
	}
	for _, s := range v.specs {
		if !s.prior {
			continue
		}
		refs, _ := v.reads(s)
		data, via := "", ""
		for _, r := range refs {
			d, w := "", ""
			switch {
			case isData(r):
				d = r
			case isVia(r):
				d, w = minData(r), r
			}
			if d != "" && (data == "" || d < data || (d == data && w < via)) {
				data, via = d, w
			}
		}
		if data == "" {
			continue
		}
		args := map[string]any{"data": data}
		if via != "" {
			args["through"] = via
		}
		v.error("graph.prior_data", s.key.Node, s.key.Slot, args)
	}
}
