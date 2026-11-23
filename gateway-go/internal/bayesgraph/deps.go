package bayesgraph

import (
	"container/heap"
	"sort"
	"strconv"
	"strings"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/bayesexpr"
)

// This file is the dependencies of a graph (docs/specs/bayes-model-graph.md, «Edges»): what each
// node reads, derived from its slots, never stored. They give the edges the canvas draws, graph.cycle,
// graph.self_predictor, graph.unused_parameter, the topological order and the roles of the
// parameters.

// Edge kinds.
const (
	EdgeRead     = "read"     // a name or a ref in a slot
	EdgeIndex    = "index"    // g of x[g]
	EdgeLag      = "lag"      // x of lag(x, k)
	EdgeTerm     = "term"     // the data of a linear predictor's term
	EdgeObserved = "observed" // the data a likelihood observes
)

// Edge says that a slot of node To reads From (a node id, or "<node>.<component>" or
// "<node>.correlation" for a component or a correlation). Slot is the slot's key: the node of a
// component's slot is "<node>.<component>", while To is always the node. A value that is read twice
// by the same slot, in the same way, is one edge.
type Edge struct {
	From string
	To   string
	Slot SlotKey
	Kind string
}

// ownerOf is the node an id belongs to: a node id is itself, "<node>.<component>" and
// "<node>.correlation" are the node's.
func ownerOf(id string) string {
	if i := strings.IndexByte(id, '.'); i >= 0 {
		return id[:i]
	}
	return id
}

// readSets are the ids a slot reads: plain (a name, x of x[g], a ref), lagged (x of lag(x, k)) and
// indices (g of x[g]), each sorted and distinct. A name that is not in the environment is left
// out, whether or not the slot types.
type readSets struct {
	plain, lag, index []string
}

func (v *validation) readSets(s *slotSpec) readSets {
	if !s.ok {
		return readSets{}
	}
	if s.slot.Ref != nil {
		if _, ok := v.env.lookupID(*s.slot.Ref); ok {
			return readSets{plain: []string{*s.slot.Ref}}
		}
		return readSets{}
	}
	plain, lag, index := map[string]bool{}, map[string]bool{}, map[string]bool{}
	var walk func(n *bayesexpr.Node)
	walk = func(n *bayesexpr.Node) {
		switch n.Kind {
		case bayesexpr.KindName, bayesexpr.KindIndex, bayesexpr.KindLag:
			if sym, ok := v.env.byName[n.Name]; ok {
				if n.Kind == bayesexpr.KindLag {
					lag[sym.ID] = true
				} else {
					plain[sym.ID] = true
				}
			}
			if n.Kind == bayesexpr.KindIndex {
				if sym, ok := v.env.byName[n.By]; ok {
					index[sym.ID] = true
				}
			}
		}
		for _, a := range n.Args {
			walk(a)
		}
	}
	walk(s.tree)
	return readSets{sortedKeys(plain), sortedKeys(lag), sortedKeys(index)}
}

// deps is the graph's dependencies: the edges, and by node the nodes it reads (any way, itself
// included when it reads itself) and the nodes whose value it reads in the same row (plain reads
// and terms: no lag, no index).
type deps struct {
	edges []Edge
	reads map[string][]string
	plain map[string][]string
}

// dependencies derives the edges from the specs, the terms and the observed nodes.
func (v *validation) dependencies() *deps {
	seen := map[Edge]bool{}
	var edges []Edge
	add := func(from, to string, slot SlotKey, kind string) {
		e := Edge{From: from, To: to, Slot: slot, Kind: kind}
		if !seen[e] {
			seen[e] = true
			edges = append(edges, e)
		}
	}
	for _, s := range v.specs {
		r := v.readSets(s)
		for _, id := range r.plain {
			add(id, s.owner.ID, s.key, EdgeRead)
		}
		for _, id := range r.lag {
			add(id, s.owner.ID, s.key, EdgeLag)
		}
		for _, id := range r.index {
			add(id, s.owner.ID, s.key, EdgeIndex)
		}
	}
	for _, n := range v.nodeList() {
		switch n.Kind {
		case "linear_predictor":
			for i, t := range n.Terms {
				if _, ok := v.env.nodes[t.Data]; ok {
					add(t.Data, n.ID, SlotKey{n.ID, "terms[" + strconv.Itoa(i) + "].data"}, EdgeTerm)
				}
			}
		case "likelihood":
			if o, ok := v.env.nodes[n.Observed]; ok && o.Kind == "data" {
				add(o.ID, n.ID, SlotKey{n.ID, "observed"}, EdgeObserved)
			}
		}
	}
	sort.Slice(edges, func(i, j int) bool {
		a, b := edges[i], edges[j]
		switch {
		case a.To != b.To:
			return a.To < b.To
		case a.Slot.Node != b.Slot.Node:
			return a.Slot.Node < b.Slot.Node
		case a.Slot.Slot != b.Slot.Slot:
			return a.Slot.Slot < b.Slot.Slot
		case a.From != b.From:
			return a.From < b.From
		}
		return a.Kind < b.Kind
	})
	d := &deps{edges: edges, reads: map[string][]string{}, plain: map[string][]string{}}
	all, pl := map[string]map[string]bool{}, map[string]map[string]bool{}
	put := func(m map[string]map[string]bool, to, from string) {
		if m[to] == nil {
			m[to] = map[string]bool{}
		}
		m[to][from] = true
	}
	for _, e := range edges {
		from := ownerOf(e.From)
		if _, ok := v.env.nodes[from]; !ok {
			continue
		}
		put(all, e.To, from)
		if e.Kind == EdgeRead || e.Kind == EdgeTerm {
			put(pl, e.To, from)
		}
	}
	for to, m := range all {
		d.reads[to] = sortedKeys(m)
	}
	for to, m := range pl {
		d.plain[to] = sortedKeys(m)
	}
	return d
}

// cycles: graph.cycle, one finding per set of nodes that depend on each other (and per node that
// reads itself), on the node with the smallest id, with the cycle in order: each node reads the
// next, the last reads the first.
func (v *validation) cycles(d *deps) {
	for _, comp := range stronglyConnected(v.sortedIDs(), d.reads) {
		start := comp[0]
		in := map[string]bool{}
		for _, id := range comp {
			in[id] = true
		}
		path := shortestCycle(start, d.reads, in)
		if path == nil {
			continue // a single node that does not read itself
		}
		names := make([]string, len(path))
		for i, id := range path {
			names[i] = v.nameOrLabel(id)
		}
		node, slot := start, ""
		for _, e := range d.edges {
			if e.To == path[0] && ownerOf(e.From) == path[1%len(path)] {
				node, slot = e.Slot.Node, e.Slot.Slot
				break
			}
		}
		v.error("graph.cycle", node, slot, map[string]any{"names": names, "cycle": path})
	}
}

// nameOrLabel is what a person calls the node: its name, else its label.
func (v *validation) nameOrLabel(id string) string {
	n, ok := v.env.nodes[id]
	if !ok {
		return id
	}
	if n.Name != nil && *n.Name != "" {
		return *n.Name
	}
	return n.Label
}

func (v *validation) sortedIDs() []string {
	ids := make([]string, 0, len(v.env.nodes))
	for id := range v.env.nodes {
		ids = append(ids, id)
	}
	sort.Strings(ids)
	return ids
}

// stronglyConnected are the strongly connected components of the graph whose nodes read each other
// (Tarjan), each sorted, the components by their smallest id; a single node only when it reads
// itself is left to the caller (it comes back as a one-node component either way).
func stronglyConnected(ids []string, reads map[string][]string) [][]string {
	index, low := map[string]int{}, map[string]int{}
	on := map[string]bool{}
	var stack []string
	var out [][]string
	next := 0
	var visit func(id string)
	visit = func(id string) {
		index[id], low[id] = next, next
		next++
		stack = append(stack, id)
		on[id] = true
		for _, w := range reads[id] {
			if _, seen := index[w]; !seen {
				visit(w)
				if low[w] < low[id] {
					low[id] = low[w]
				}
			} else if on[w] && index[w] < low[id] {
				low[id] = index[w]
			}
		}
		if low[id] == index[id] {
			var comp []string
			for {
				w := stack[len(stack)-1]
				stack = stack[:len(stack)-1]
				on[w] = false
				comp = append(comp, w)
				if w == id {
					break
				}
			}
			sort.Strings(comp)
			out = append(out, comp)
		}
	}
	for _, id := range ids {
		if _, seen := index[id]; !seen {
			visit(id)
		}
	}
	sort.Slice(out, func(i, j int) bool { return out[i][0] < out[j][0] })
	return out
}

// shortestCycle is the shortest way from start back to start along reads, inside the component,
// ties by the smallest id: start first, then each node it reads next; nil when there is none.
func shortestCycle(start string, reads map[string][]string, in map[string]bool) []string {
	parent := map[string]string{}
	queue := []string{start}
	seen := map[string]bool{}
	for len(queue) > 0 {
		cur := queue[0]
		queue = queue[1:]
		for _, w := range reads[cur] {
			if !in[w] {
				continue
			}
			if w == start {
				path := []string{cur}
				for cur != start {
					cur = parent[cur]
					path = append(path, cur)
				}
				for i, j := 0, len(path)-1; i < j; i, j = i+1, j-1 {
					path[i], path[j] = path[j], path[i]
				}
				return path
			}
			if !seen[w] {
				seen[w] = true
				parent[w] = cur
				queue = append(queue, w)
			}
		}
	}
	return nil
}

// idHeap is a min-heap of ids.
type idHeap []string

func (h idHeap) Len() int           { return len(h) }
func (h idHeap) Less(i, j int) bool { return h[i] < h[j] }
func (h idHeap) Swap(i, j int)      { h[i], h[j] = h[j], h[i] }
func (h *idHeap) Push(x any)        { *h = append(*h, x.(string)) }
func (h *idHeap) Pop() any {
	old := *h
	x := old[len(old)-1]
	*h = old[:len(old)-1]
	return x
}

// topological is a topological order of the nodes, what a node reads before the node, ties by
// the smallest id. The nodes of a cycle, and those that read them, are left at the end by id
// (graph.cycle says so).
func topological(ids []string, reads map[string][]string) []string {
	pending := map[string]int{}
	readers := map[string][]string{}
	for _, id := range ids {
		for _, w := range reads[id] {
			if w == id {
				pending[id]++ // a node that reads itself is never ready
				continue
			}
			pending[id]++
			readers[w] = append(readers[w], id)
		}
	}
	h := &idHeap{}
	for _, id := range ids {
		if pending[id] == 0 {
			heap.Push(h, id)
		}
	}
	out := make([]string, 0, len(ids))
	done := map[string]bool{}
	for h.Len() > 0 {
		id := heap.Pop(h).(string)
		out = append(out, id)
		done[id] = true
		for _, r := range readers[id] {
			if pending[r]--; pending[r] == 0 {
				heap.Push(h, r)
			}
		}
	}
	for _, id := range ids {
		if !done[id] {
			out = append(out, id)
		}
	}
	return out
}
