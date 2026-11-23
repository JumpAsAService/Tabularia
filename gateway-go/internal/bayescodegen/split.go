package bayescodegen

import (
	"strconv"
	"strings"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/bayescontract"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/bayesexpr"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/bayesgraph"
)

// This file is the splitting of large vector expressions (docs/specs/bayes-stan-codegen.md,
// «Correction of 2026-11-24 from E4», lot S5). A vector expression is compiled by Stan's C++ into
// one expression template that nests all its vector operations, and the compile time of that
// template grows faster than its nodes (the expression language's E4, finding 1: 249 nodes 106 s).
// Written as a chain of locals, each holding a bounded part of the tree, the same program compiles
// in a time about linear in the nodes. So a vector expression of more than maxLocalNodes vector
// nodes is written bottom-up as locals of the block its statement is in, E_<node id>_<k>, each
// holding at most maxLocalNodes vector nodes of the tree, and the statement holds the last part.
//
// What a part holds is counted in nodes of the expression template: each vector node (a node whose
// value is over the context plate) and each single-value operand of one, whatever its size (a single
// value inside a vector expression is computed at once, a scalar of Stan's autodiff that enters the
// template as one operand; it is never split). For the expression language's forms this is the
// tree's nodes (`a * x / (x + b)` is 7). The parts are the fewest a bound allows: bottom-up, while a
// node holds more than the bound, its largest vector operand becomes a local (Kundu and Misra's
// greedy partition of a tree, optimal in the number of parts), the first one on a tie. The same tree
// gives the same parts and the same names: nothing depends on the order of maps nor on the graph's
// layout.

// maxLocalNodes is the most nodes a local, or a statement, holds of a split expression: 64, from the
// measures (a local of 8 nodes or of 63 compiles in the same time; 64 bounds the expression templates
// without multiplying the locals; parts of 64 vector nodes and their scalar operands, about 82
// nodes, made the 1,497-node program 190 s instead of 133 s: lot S5's «As built»).
const maxLocalNodes = 64

// maxAffineNodes is the most nodes a location or a scale over the plate holds in the affine
// non-centred form; a larger one takes the explicit form. Measured (lot S5's «As built»): a location
// of 61 nodes costs 9.0 s of compile as an offset, 4.6 s in the explicit form, 3.2 s written once
// in the model block; the graph's budget counts it once. Up to 8 nodes (an indexing, a lift, a
// coefficient times a group's data) the difference is under a second.
const maxAffineNodes = 8

// splitPrefix is the prefix of the locals of a split expression, E_<node id>_<k>.
const splitPrefix = "E"

// wrapperNodes are the nodes the generator's own forms add around a location and a scale (the
// explicit non-centred form, a non-centred state's first values): the sum, the product and Z of
// (loc + (scale * Z)).
const wrapperNodes = 3

// splitPlan is how one tree is split: the nodes that become locals, children before their parents
// (and left before right), and the nodes the root keeps.
type splitPlan struct {
	cuts  []*bayesexpr.Typed
	resid int // the nodes left in the statement (0: a single value)
	total int // the tree's nodes, as a part counts them (0: a single value)
}

func vectorNode(t *bayesexpr.Typed) bool { return t != nil && t.Type.Plate != "" }

// planSplit is the partition of a tree into parts of at most maxLocalNodes nodes.
func planSplit(t *bayesexpr.Typed) splitPlan {
	var p splitPlan
	var walk func(t *bayesexpr.Typed) int
	walk = func(t *bayesexpr.Typed) int {
		if t == nil {
			return 0
		}
		rs := make([]int, len(t.Args))
		for i, a := range t.Args {
			rs[i] = walk(a)
		}
		if !vectorNode(t) {
			// A single value: its operands are single values too (the type rules bring every
			// value that is not a single one to the context), nothing of it is cut.
			return 0
		}
		p.total++
		sum := 1
		for i, r := range rs {
			if !vectorNode(t.Args[i]) {
				r, rs[i] = 1, 1 // a single-value operand: one node, never cut
				p.total++
			}
			sum += r
		}
		for sum > maxLocalNodes {
			best := -1
			for i, r := range rs {
				if r > 1 && (best < 0 || r > rs[best]) {
					best = i
				}
			}
			if best < 0 {
				break // operands that are all single nodes: never more than 1 + arity
			}
			p.cuts = append(p.cuts, t.Args[best])
			sum -= rs[best] - 1
			rs[best] = 1
		}
		return sum
	}
	p.resid = walk(t)
	// The cuts were taken parent by parent; a part is written after the parts it reads, so order
	// them as the tree's post-order.
	p.cuts = postOrder(t, p.cuts)
	return p
}

// postOrder is the nodes of set in the post-order of t (operands left to right, then the node).
func postOrder(t *bayesexpr.Typed, set []*bayesexpr.Typed) []*bayesexpr.Typed {
	if len(set) == 0 {
		return nil
	}
	in := map[*bayesexpr.Typed]bool{}
	for _, c := range set {
		in[c] = true
	}
	out := make([]*bayesexpr.Typed, 0, len(set))
	var walk func(t *bayesexpr.Typed)
	walk = func(t *bayesexpr.Typed) {
		if t == nil {
			return
		}
		for _, a := range t.Args {
			walk(a)
		}
		if in[t] {
			out = append(out, t)
		}
	}
	walk(t)
	return out
}

// localKey is the key a local stands under in a rebuilt tree: the name prefixed with a byte no
// node id has, so that an Identifier never confuses the two.
func localKey(ident string) string { return "#" + ident }

// replaced is t with every node of locals (not t itself) a name of its local: a new tree, the
// model's never changed.
func replaced(t *bayesexpr.Typed, locals map[*bayesexpr.Typed]string, top bool) *bayesexpr.Typed {
	if t == nil {
		return nil
	}
	if ident, ok := locals[t]; ok && !top {
		return &bayesexpr.Typed{
			Node: &bayesexpr.Node{Kind: bayesexpr.KindName, Name: ident},
			Type: t.Type,
			Sym:  &bayesexpr.Symbol{ID: localKey(ident), Role: bayesexpr.RoleTransformed, Type: t.Type},
		}
	}
	changed := false
	args := make([]*bayesexpr.Typed, len(t.Args))
	for i, a := range t.Args {
		args[i] = replaced(a, locals, false)
		changed = changed || args[i] != a
	}
	if !changed {
		return t
	}
	cp := *t
	cp.Args = args
	return &cp
}

// splitSink is where the locals of split expressions go before the statement that reads them: their
// lines, and their entries in the mapping table.
type splitSink struct {
	// block is the block the locals are declared in, for the mapping table.
	block string
	// rebuild: the locals are the model block's, written again in the extension's nested block
	// with the same names (not in the mapping table, as the per-row locals rebuilt there).
	rebuild bool
	// fresh: the locals of a value the extension asks for, numbered after the node's others.
	fresh   bool
	lines   []string
	entries []bayescontract.MappingEntry
}

func (s *splitSink) empty() bool { return s == nil || len(s.lines) == 0 }

// flush writes the sink's lines and their mapping entries, and empties it.
func (g *gen) flush(s *splitSink) {
	if s == nil {
		return
	}
	for _, l := range s.lines {
		g.w.line(l)
	}
	g.mapping = append(g.mapping, s.entries...)
	s.lines, s.entries = nil, nil
}

// splitIdent is E_<node id>_<k>.
func splitIdent(owner string, k int) (string, error) {
	if !graphID(owner) {
		return "", bug("the id %s of a split expression's node is not of the graph's form", strconv.Quote(owner))
	}
	return splitPrefix + "_" + owner + "_" + strconv.Itoa(k), nil
}

// ownerOf is the node a slot belongs to (a component's slots are their effects node's).
func ownerOf(node string) string {
	if i := strings.IndexByte(node, '.'); i >= 0 {
		return node[:i]
	}
	return node
}

// localName is the identifier of the i-th local of what memo names (a slot, a linear predictor's
// sum) of the node owner: numbered in the order the program writes them, E_<owner>_1 first; the
// same names again when the model block's locals are rebuilt; new numbers for the extension's.
func (g *gen) localName(s *splitSink, memo, owner string, i int) (string, error) {
	if s.rebuild {
		names := g.splitNames[memo]
		if i >= len(names) {
			return "", bug("a rebuilt local of %s that the model block did not write", memo)
		}
		return names[i], nil
	}
	if !s.fresh && i != len(g.splitNames[memo]) {
		return "", bug("the expression %s split twice in the program", memo)
	}
	g.splitCount[owner]++
	ident, err := splitIdent(owner, g.splitCount[owner])
	if err != nil {
		return "", err
	}
	if !s.fresh {
		g.splitNames[memo] = append(g.splitNames[memo], ident)
	}
	if s.block != "" {
		e := bayescontract.MappingEntry{Stan: ident, Block: s.block, NodeID: strp(owner), Kind: bayescontract.KindInternal}
		if n := g.node(owner); n != nil && n.Plate != nil {
			e.Plate = strp(*n.Plate)
		}
		s.entries = append(s.entries, e)
	}
	return ident, nil
}

// vectorSize is the size of a vector over a context plate: N, J_<plate>, K_<c> - 1 on a category's
// levels (the coefficients after the reference).
func (g *gen) vectorSize(ctx bayesexpr.Plate) (string, error) {
	p, ok := g.plates[string(ctx)]
	if !ok {
		return "", bug("a vector over %q, not a plate of the model", ctx)
	}
	if p.Kind == bayesgraph.PlateLevels {
		return g.size[p.ID] + " - 1", nil
	}
	return g.size[p.ID], nil
}

// declareLocal adds a local's lines to the sink: "vector[size] E = value;", or, on a subset of the
// rows, the vector over every row and the value assigned on the subset (what reads it reads it on
// the same subset: Emit writes E[subset]).
func (s *splitSink) declareLocal(ident, size, subset, value string) {
	if subset == "" {
		s.lines = append(s.lines, "vector["+size+"] "+ident+" = "+value+";")
		return
	}
	s.lines = append(s.lines, "vector["+size+"] "+ident+";", ident+"["+subset+"] = "+value+";")
}

// emitSplit emits a slot's tree in env, split into locals when it holds more nodes than a
// statement may (or, with env.rootLocal, its whole value a local when it is not a single node):
// the locals go to env.sink, the text returned is the statement's part.
func (g *gen) emitSplit(key bayesgraph.SlotKey, c *bayesexpr.Checked, env emitEnv) (string, error) {
	plan := planSplit(c.Tree)
	cuts := plan.cuts
	if env.rootLocal && vectorNode(c.Tree) && len(c.Tree.Args) > 0 {
		cuts = append(cuts, c.Tree)
	}
	if len(cuts) == 0 {
		return bayesexpr.Emit(c, env)
	}
	if env.sink == nil {
		return "", bug("the slot %s.%s needs %d locals where none can be written", key.Node, key.Slot, len(cuts))
	}
	size, err := g.vectorSize(c.Context)
	if err != nil {
		return "", err
	}
	memo, owner := key.Node+"\x00"+key.Slot, ownerOf(key.Node)
	locals := map[*bayesexpr.Typed]string{}
	env.locals = map[string]string{}
	for i, cut := range cuts {
		ident, err := g.localName(env.sink, memo, owner, i)
		if err != nil {
			return "", err
		}
		part, err := bayesexpr.Emit(&bayesexpr.Checked{Tree: replaced(cut, locals, true), Context: c.Context}, env)
		if err != nil {
			return "", err
		}
		env.sink.declareLocal(ident, size, env.subset, part)
		locals[cut] = ident
		env.locals[localKey(ident)] = ident
	}
	if ident, ok := locals[c.Tree]; ok {
		if env.subset != "" {
			return ident + "[" + env.subset + "]", nil
		}
		return ident, nil
	}
	return bayesexpr.Emit(&bayesexpr.Checked{Tree: replaced(c.Tree, locals, true), Context: c.Context}, env)
}

// slotResid is the nodes a slot's text holds once split (a ref to a value over the plate is one
// node, a single value none), and its whole tree's.
func (g *gen) slotResid(key bayesgraph.SlotKey) (resid, total int, err error) {
	t, ok := g.m.Slots[key]
	if !ok {
		return 0, 0, bug("the slot %s.%s did not type in a valid model", key.Node, key.Slot)
	}
	if t.Type.Plate == "" {
		return 0, 0, nil
	}
	if t.Expr == nil {
		return 1, 1, nil
	}
	p := planSplit(t.Expr.Tree)
	return p.resid, p.total, nil
}

// wrapped decides which of a location and a scale a generator's form (loc + (scale * Z)) takes as
// a local: while the form would hold more than maxLocalNodes nodes, the larger of the two (the
// location on a tie) becomes a local, read as one node. A single value is one operand.
func (g *gen) wrapped(loc, scale bayesgraph.SlotKey) (locLocal, scaleLocal bool, err error) {
	rl, _, err := g.slotResid(loc)
	if err != nil {
		return false, false, err
	}
	rs, _, err := g.slotResid(scale)
	if err != nil {
		return false, false, err
	}
	rl, rs = max(rl, 1), max(rs, 1)
	for rl+rs+wrapperNodes > maxLocalNodes {
		switch {
		case rl > 1 && rl >= rs:
			locLocal, rl = true, 1
		case rs > 1:
			scaleLocal, rs = true, 1
		default:
			return locLocal, scaleLocal, nil
		}
	}
	return locLocal, scaleLocal, nil
}
