package bayesgraph

import (
	"strings"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/bayesexpr"
)

// This file is the expression environment of a slot: the Env of expression-language
// (docs/specs/bayes-expression-language.md, «The environment»), built once per graph and given to
// the checker with the context plate of each slot.
//
// The plates are the stored ones (rows, groups) and one implicit levels plate per category data
// node, id "levels:<node id>". A group plate encloses itself, the rows and the group plates nested
// in it (by parent); a levels plate encloses itself and the rows; the rows enclose only themselves.
//
// The names are those of the nodes, components and correlations that have one. Data nodes give
// their type (real, int, an index for group_index and category, time) and their support's interval
// (int nonnegative [0, ∞), binary [0, 1]); parameters their range (computed before the slots are
// typed); transformed, linear_predictor and state nodes are real values without a declared interval;
// a component is a real value over its node's plate. A named value that is not a number (a
// correlation, a likelihood, a correlated-effects node) is ElemNotNumber, so that writing it is
// expr.not_number. A name held twice (graph.name_taken) stands for its holder with the smallest id,
// so that the findings do not depend on the order of the nodes.

// levelsPrefix makes the id of a category's levels plate.
const levelsPrefix = "levels:"

// LevelsPlate is the id of the implicit levels plate of a category data node.
func LevelsPlate(category string) string { return levelsPrefix + category }

const kindLevels = "levels"

// envPlate is a plate as the environment sees it.
type envPlate struct {
	kind   string // rows, group, levels
	parent string // a group plate's parent ("" for none)
	series bool   // the rows of a series
	index  string // the id of the node whose units are this plate's (a group's key, a levels' category)
}

// graphEnv is what every slot of a graph shares: plates, names, and the nodes by id.
type graphEnv struct {
	plates map[bayesexpr.Plate]envPlate
	byName map[string]bayesexpr.Symbol
	// holder: the id (node, "<node>.<component>", "<node>.correlation") that holds each name.
	holder map[string]string
	// names: the name of each id that has one.
	names map[string]string
	nodes map[string]*Node
	// components: "<node>.<component>" → its component, and the node that holds it.
	components map[string]*Component
	compOwner  map[string]*Node
	ranges     map[string]bayesexpr.Interval
}

// newGraphEnv builds the environment of g; ranges are the parameters' ranges by node id (those
// that could be computed). g's ids are assumed unique and its rows plate single (the validator
// runs the slot rules only then); a duplicate is taken at its first occurrence.
func newGraphEnv(g *Graph, ranges map[string]bayesexpr.Interval) *graphEnv {
	e := &graphEnv{
		plates:     map[bayesexpr.Plate]envPlate{},
		byName:     map[string]bayesexpr.Symbol{},
		holder:     map[string]string{},
		names:      map[string]string{},
		nodes:      map[string]*Node{},
		components: map[string]*Component{},
		compOwner:  map[string]*Node{},
		ranges:     ranges,
	}
	for i := range g.Plates {
		p := &g.Plates[i]
		id := bayesexpr.Plate(p.ID)
		if _, dup := e.plates[id]; dup {
			continue
		}
		ep := envPlate{kind: p.Kind}
		switch p.Kind {
		case kindRows:
			ep.series = p.Series != nil
		case kindGrp:
			if p.Parent != nil {
				ep.parent = *p.Parent
			}
			ep.index = p.Key
		}
		e.plates[id] = ep
	}
	for i := range g.Nodes {
		n := &g.Nodes[i]
		if _, dup := e.nodes[n.ID]; dup {
			continue
		}
		e.nodes[n.ID] = n
		if n.Kind == "data" && n.Type == "category" {
			e.plates[bayesexpr.Plate(LevelsPlate(n.ID))] = envPlate{kind: kindLevels, index: n.ID}
		}
		for j := range n.Components {
			k := &n.Components[j]
			id := n.ID + "." + k.ID
			if _, dup := e.components[id]; !dup {
				e.components[id] = k
				e.compOwner[id] = n
			}
		}
	}
	for i := range g.Nodes {
		n := &g.Nodes[i]
		if e.nodes[n.ID] != n {
			continue
		}
		if n.Name != nil && *n.Name != "" {
			e.add(*n.Name, nodeSymbol(n, ranges))
		}
		plate := plateOf(n)
		for j := range n.Components {
			k := &n.Components[j]
			if k.Name != "" {
				e.add(k.Name, bayesexpr.Symbol{ID: n.ID + "." + k.ID, Role: bayesexpr.RoleComponent,
					Type: bayesexpr.Type{Elem: bayesexpr.ElemReal, Plate: plate}})
			}
		}
		if n.Correlation != nil && n.Correlation.Name != "" {
			e.add(n.Correlation.Name, bayesexpr.Symbol{ID: n.ID + ".correlation", Role: bayesexpr.RoleCorrelation,
				Type: bayesexpr.Type{Elem: bayesexpr.ElemNotNumber}})
		}
	}
	return e
}

// add gives the name to the symbol, unless a holder with a smaller id has it.
func (e *graphEnv) add(name string, s bayesexpr.Symbol) {
	e.names[s.ID] = name
	if h, taken := e.holder[name]; taken && h < s.ID {
		return
	}
	e.holder[name] = s.ID
	e.byName[name] = s
}

// plateOf is the node's plate ("" for a single value).
func plateOf(n *Node) bayesexpr.Plate {
	if n.Plate == nil {
		return ""
	}
	return bayesexpr.Plate(*n.Plate)
}

// dataInterval is the interval of a data node's support: int nonnegative [0, ∞), binary [0, 1];
// nil (unbounded) otherwise.
func dataInterval(n *Node) *bayesexpr.Interval {
	if n.Type != "int" {
		return nil
	}
	switch n.Support {
	case "nonnegative":
		i := bayesexpr.NamedInterval("non_negative")
		return &i
	case "binary":
		i := bayesexpr.Interval{Lo: 0, Hi: 1}
		return &i
	}
	return nil
}

// nodeSymbol is the symbol a named node stands for.
func nodeSymbol(n *Node, ranges map[string]bayesexpr.Interval) bayesexpr.Symbol {
	s := bayesexpr.Symbol{ID: n.ID, Role: bayesexpr.Role(n.Kind),
		Type: bayesexpr.Type{Elem: bayesexpr.ElemReal, Plate: plateOf(n)}}
	switch n.Kind {
	case "data":
		s.Role = bayesexpr.RoleData
		switch n.Type {
		case "int":
			s.Type.Elem = bayesexpr.ElemInt
		case "group_index":
			s.Type = bayesexpr.Type{Elem: bayesexpr.ElemIndex, Plate: plateOf(n), Target: bayesexpr.Plate(n.Target)}
		case "category":
			s.Type = bayesexpr.Type{Elem: bayesexpr.ElemIndex, Plate: plateOf(n), Target: bayesexpr.Plate(LevelsPlate(n.ID))}
		case "time":
			s.Type.Elem = bayesexpr.ElemTime
		}
		// real, and an unknown type (graph.data_type), are real values.
		s.Interval = dataInterval(n)
	case "parameter":
		if r, ok := ranges[n.ID]; ok {
			s.Interval = &r
		}
	case "transformed", "linear_predictor", "state":
	default:
		// a likelihood, a correlated-effects node: named, not a value.
		s.Type = bayesexpr.Type{Elem: bayesexpr.ElemNotNumber}
	}
	return s
}

// lookupID is the symbol of an id (a node, "<node>.<component>", "<node>.correlation"), named or
// not: what a ref slot stands for.
func (e *graphEnv) lookupID(id string) (bayesexpr.Symbol, bool) {
	if n, ok := e.nodes[id]; ok {
		return nodeSymbol(n, e.ranges), true
	}
	if _, ok := e.components[id]; ok {
		return bayesexpr.Symbol{ID: id, Role: bayesexpr.RoleComponent,
			Type: bayesexpr.Type{Elem: bayesexpr.ElemReal, Plate: plateOf(e.compOwner[id])}}, true
	}
	if owner, ok := strings.CutSuffix(id, ".correlation"); ok {
		if n, ok := e.nodes[owner]; ok && n.Correlation != nil {
			return bayesexpr.Symbol{ID: id, Role: bayesexpr.RoleCorrelation,
				Type: bayesexpr.Type{Elem: bayesexpr.ElemNotNumber}}, true
		}
	}
	return bayesexpr.Symbol{}, false
}

// encloses: reflexive; a group plate encloses the rows and its descendants; a levels plate the
// rows; the rows nothing else. A cycle of parents (graph.plate_parent) ends the walk.
func (e *graphEnv) encloses(outer, inner bayesexpr.Plate) bool {
	if outer == inner {
		return true
	}
	o, ok := e.plates[outer]
	if !ok {
		return false
	}
	in, ok := e.plates[inner]
	if !ok {
		return false
	}
	switch o.kind {
	case kindGrp:
		if in.kind == kindRows {
			return true
		}
		if in.kind != kindGrp {
			return false
		}
		seen := map[bayesexpr.Plate]bool{inner: true}
		for cur := in; cur.parent != ""; {
			p := bayesexpr.Plate(cur.parent)
			if p == outer {
				return true
			}
			if seen[p] {
				return false
			}
			seen[p] = true
			next, ok := e.plates[p]
			if !ok || next.kind != kindGrp {
				return false
			}
			cur = next
		}
		return false
	case kindLevels:
		return in.kind == kindRows
	}
	return false
}

// slotEnv is the Env of one slot: the graph's with the slot's context plate.
type slotEnv struct {
	*graphEnv
	ctx bayesexpr.Plate
}

var _ bayesexpr.Env = slotEnv{}

func (s slotEnv) Lookup(name string) (bayesexpr.Symbol, bool) {
	sym, ok := s.byName[name]
	return sym, ok
}

func (s slotEnv) Context() bayesexpr.Plate { return s.ctx }

func (s slotEnv) Encloses(outer, inner bayesexpr.Plate) bool { return s.encloses(outer, inner) }

func (s slotEnv) IsSeries(p bayesexpr.Plate) bool {
	ep, ok := s.plates[p]
	return ok && ep.kind == kindRows && ep.series
}

// IndexOf names the node whose units are the target's: a group plate's key, a levels plate's
// category; false when it has no name.
func (s slotEnv) IndexOf(target bayesexpr.Plate) (string, bool) {
	ep, ok := s.plates[target]
	if !ok || ep.index == "" {
		return "", false
	}
	name, ok := s.names[ep.index]
	return name, ok
}
