package bayesgraph

import (
	"fmt"
	"sort"
	"strings"
	"unicode"
	"unicode/utf8"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/bayescatalog"
)

// CheckStructure applies the rules that need no expression and no model: graph.too_big (nodes
// and plates), graph.id, graph.name, graph.name_taken, graph.name_function, graph.label,
// graph.note, graph.rows, graph.plate_parent, graph.plate_key and graph.series. It reports
// everything it finds, sorted by (node, slot, code); it never repairs. The graph may be any
// value a Go program built: a nil pointer inside it is not expected, an empty field is a
// finding.
func CheckStructure(g *Graph) []Finding {
	c := &checker{lim: bayescatalog.GetLimits(), functions: map[string]bool{}}
	for _, f := range bayescatalog.Declared().Language.Functions {
		c.functions[f.Name] = true
	}
	c.size(g)
	c.ids(g)
	c.names(g)
	c.texts(g)
	c.plates(g)
	SortFindings(c.out)
	return c.out
}

type checker struct {
	lim       bayescatalog.Limits
	functions map[string]bool
	out       []Finding
}

func (c *checker) add(code, node, slot string, args map[string]any) {
	c.out = append(c.out, Finding{Code: code, Severity: Error, Node: node, Slot: slot, Args: args})
}

func (c *checker) size(g *Graph) {
	if n := len(g.Nodes); n > c.lim.MaxNodes {
		c.out = append(c.out, tooBig("nodes", n, c.lim.MaxNodes)...)
	}
	if n := len(g.Plates); n > c.lim.MaxPlates {
		c.out = append(c.out, tooBig("plates", n, c.lim.MaxPlates)...)
	}
}

// validID is ^[a-z][a-z0-9]{0,15}$.
func validID(s string) bool {
	if len(s) == 0 || len(s) > 16 || s[0] < 'a' || s[0] > 'z' {
		return false
	}
	for i := 1; i < len(s); i++ {
		if !(s[i] >= 'a' && s[i] <= 'z' || s[i] >= '0' && s[i] <= '9') {
			return false
		}
	}
	return true
}

// validName is the catalogue's pattern [a-z][a-z0-9_]*.
func validName(s string) bool {
	if len(s) == 0 || s[0] < 'a' || s[0] > 'z' {
		return false
	}
	for i := 1; i < len(s); i++ {
		if !(s[i] >= 'a' && s[i] <= 'z' || s[i] >= '0' && s[i] <= '9' || s[i] == '_') {
			return false
		}
	}
	return true
}

// ids: well formed and unique across plates and nodes; "rows" is the rows plate's alone;
// component ids are well formed and unique within their node.
func (c *checker) ids(g *Graph) {
	seen := map[string]int{}
	for _, p := range g.Plates {
		seen[p.ID]++
		c.idShape(p.ID, "plate")
		c.idSuffix(p.ID, p.ID)
		if p.Kind == kindRows && p.ID != "rows" {
			c.add("graph.id", p.ID, "", map[string]any{"id": p.ID, "reason": "rows_plate_id"})
		}
		if p.Kind != kindRows && p.ID == "rows" {
			c.add("graph.id", p.ID, "", map[string]any{"id": p.ID, "reason": "rows_reserved"})
		}
	}
	for _, n := range g.Nodes {
		seen[n.ID]++
		c.idShape(n.ID, "node")
		c.idSuffix(n.ID, n.ID)
		if n.ID == "rows" {
			c.add("graph.id", n.ID, "", map[string]any{"id": n.ID, "reason": "rows_reserved"})
		}
		cs := map[string]int{}
		for _, k := range n.Components {
			cs[k.ID]++
			if !validID(k.ID) {
				c.add("graph.id", n.ID+"."+k.ID, "", map[string]any{"id": k.ID, "reason": "malformed"})
			}
			c.idSuffix(n.ID+"."+k.ID, k.ID)
		}
		for id, k := range cs {
			if k > 1 {
				c.add("graph.id", n.ID+"."+id, "", map[string]any{"id": id, "reason": "duplicate"})
			}
		}
	}
	for id, k := range seen {
		if k > 1 {
			c.add("graph.id", id, "", map[string]any{"id": id, "reason": "duplicate", "count": k})
		}
	}
}

func (c *checker) idShape(id, what string) {
	if !validID(id) {
		c.add("graph.id", id, "", map[string]any{"id": id, "reason": "malformed", "what": what})
	}
}

// idSuffix refuses an id that is exactly a suffix Stan reserves without its underscore (lpdf,
// cdf…): the generated identifiers V_<id>, J_<id>… would end with the reserved suffix (correction
// of 2026-11-24 from lot S0 of stan-codegen). Ids hold no underscore, so equality is the whole rule.
func (c *checker) idSuffix(where, id string) {
	for _, suffix := range bayescatalog.StanLists().ReservedSuffixes {
		if id == strings.TrimPrefix(suffix, "_") {
			c.add("graph.id", where, "", map[string]any{"id": id, "reason": "reserved_suffix"})
			return
		}
	}
}

type named struct{ node, name string }

// names: the node's name, a component's, the correlation's. Lexical rules per name; uniqueness
// across all of them, reported on every holder (so that the order of the nodes changes nothing).
func (c *checker) names(g *Graph) {
	var all []named
	for _, n := range g.Nodes {
		if n.Name != nil {
			all = append(all, named{n.ID, *n.Name})
			c.name(n.ID, *n.Name, true)
		}
		for _, k := range n.Components {
			if k.Name != "" {
				all = append(all, named{n.ID + "." + k.ID, k.Name})
				c.name(n.ID+"."+k.ID, k.Name, false)
			}
		}
		if n.Correlation != nil && n.Correlation.Name != "" {
			all = append(all, named{n.ID + ".correlation", n.Correlation.Name})
			c.name(n.ID+".correlation", n.Correlation.Name, false)
		}
	}
	by := map[string][]string{}
	for _, a := range all {
		by[a.name] = append(by[a.name], a.node)
	}
	for name, holders := range by {
		if len(holders) < 2 {
			continue
		}
		for _, h := range holders {
			c.add("graph.name_taken", h, "", map[string]any{"name": name, "count": len(holders)})
		}
	}
}

func (c *checker) name(node, name string, present bool) {
	switch {
	case name == "":
		c.add("graph.name", node, "", map[string]any{"name": name, "reason": "empty"})
	case !validName(name):
		c.add("graph.name", node, "", map[string]any{"name": name, "reason": "pattern"})
	case len(name) > c.lim.MaxNameBytes:
		c.add("graph.name", node, "", map[string]any{"name": name, "reason": "too_long", "max": c.lim.MaxNameBytes})
	case strings.HasSuffix(name, "__"):
		c.add("graph.name", node, "", map[string]any{"name": name, "reason": "double_underscore"})
	case c.functions[name]:
		c.add("graph.name_function", node, "", map[string]any{"name": name})
	}
}

// forbiddenRune is a character a label or a note may not hold, with the reason: a control
// character (C0, C1: category Cc), a direction control (U+202A-U+202E, U+2066-U+2069), U+FEFF.
// Tab and newline are allowed in a note only.
func forbiddenRune(r rune, note bool) (string, bool) {
	switch {
	case note && (r == '\n' || r == '\t'):
		return "", false
	case unicode.Is(unicode.Cc, r):
		return "control", true
	case r >= 0x202A && r <= 0x202E, r >= 0x2066 && r <= 0x2069:
		return "direction", true
	case r == 0xFEFF:
		return "bom", true
	}
	return "", false
}

func (c *checker) label(node, slot, s string, required bool) {
	n := utf8.RuneCountInString(s)
	if n == 0 {
		if required {
			c.add("graph.label", node, slot, map[string]any{"reason": "empty"})
		}
		return
	}
	if n > c.lim.MaxLabelChars {
		c.add("graph.label", node, slot, map[string]any{"reason": "too_long", "max": c.lim.MaxLabelChars, "got": n})
	}
	if why, bad := firstForbidden(s, false); bad {
		c.add("graph.label", node, slot, map[string]any{"reason": why})
	}
}

func (c *checker) note(node, s string) {
	if s == "" {
		return
	}
	if n := utf8.RuneCountInString(s); n > c.lim.MaxNoteChars {
		c.add("graph.note", node, "note", map[string]any{"reason": "too_long", "max": c.lim.MaxNoteChars, "got": n})
	}
	if why, bad := firstForbidden(s, true); bad {
		c.add("graph.note", node, "note", map[string]any{"reason": why})
	}
}

func firstForbidden(s string, note bool) (string, bool) {
	for _, r := range s {
		if why, bad := forbiddenRune(r, note); bad {
			return why, true
		}
	}
	return "", false
}

// texts: labels (required on plates, nodes and components; the correlation's too) and notes.
func (c *checker) texts(g *Graph) {
	for _, p := range g.Plates {
		c.label(p.ID, "label", p.Label, true)
		c.note(p.ID, p.Note)
	}
	for _, n := range g.Nodes {
		c.label(n.ID, "label", n.Label, true)
		c.note(n.ID, n.Note)
		for _, k := range n.Components {
			c.label(n.ID+"."+k.ID, "label", k.Label, true)
		}
		if n.Correlation != nil {
			c.label(n.ID+".correlation", "label", n.Correlation.Label, true)
		}
	}
}

func (c *checker) plates(g *Graph) {
	byID := map[string]Plate{}
	rows := 0
	for _, p := range g.Plates {
		if _, dup := byID[p.ID]; !dup {
			byID[p.ID] = p
		}
		if p.Kind == kindRows {
			rows++
		}
	}
	switch {
	case rows == 0:
		c.add("graph.rows", "rows", "", map[string]any{"count": 0})
	case rows > 1:
		for _, p := range g.Plates {
			if p.Kind == kindRows {
				c.add("graph.rows", p.ID, "", map[string]any{"count": rows})
			}
		}
	}
	nodes := map[string]Node{}
	for _, n := range g.Nodes {
		if _, dup := nodes[n.ID]; !dup {
			nodes[n.ID] = n
		}
	}
	for _, p := range g.Plates {
		switch p.Kind {
		case kindRows:
			c.series(p, byID, nodes)
		case kindGrp:
			c.parent(p, byID)
			if p.Form != "non_centred" && p.Form != "centred" && p.Form != "" {
				c.add("graph.format", p.ID, "form", map[string]any{"detail": fmt.Sprintf("form %s is not non_centred or centred", quote(p.Form))})
			}
			c.key(p, g, nodes)
		default:
			c.add("graph.format", p.ID, "kind", map[string]any{"detail": fmt.Sprintf("plate kind %s is unknown", quote(p.Kind))})
		}
	}
	// every group_index targets a group plate
	for _, n := range g.Nodes {
		if n.Kind != "data" || n.Type != "group_index" {
			continue
		}
		if t, ok := byID[n.Target]; !ok || t.Kind != kindGrp {
			c.add("graph.plate_key", n.ID, "target", map[string]any{"target": n.Target, "reason": "not_a_group_plate"})
		}
	}
}

// parent: a group plate's parent is a group plate, without a cycle, within max_plate_depth.
func (c *checker) parent(p Plate, byID map[string]Plate) {
	if p.Parent == nil {
		return
	}
	par, ok := byID[*p.Parent]
	if !ok || par.Kind != kindGrp {
		c.add("graph.plate_parent", p.ID, "parent", map[string]any{"parent": *p.Parent, "reason": "not_a_group_plate"})
		return
	}
	depth, seen := 1, map[string]bool{p.ID: true}
	for cur := p; cur.Parent != nil; {
		next, ok := byID[*cur.Parent]
		if !ok || next.Kind != kindGrp {
			return // reported on that plate
		}
		if seen[next.ID] {
			c.add("graph.plate_parent", p.ID, "parent", map[string]any{"parent": *p.Parent, "reason": "cycle"})
			return
		}
		seen[next.ID] = true
		depth++
		cur = next
	}
	if depth > c.lim.MaxPlateDepth {
		c.add("graph.plate_parent", p.ID, "parent", map[string]any{"reason": "too_deep", "depth": depth, "max": c.lim.MaxPlateDepth})
	}
}

// key: exactly one group_index data node on rows targets the plate, and it is the plate's key.
func (c *checker) key(p Plate, g *Graph, nodes map[string]Node) {
	var targeting []string
	for _, n := range g.Nodes {
		if n.Kind == "data" && n.Type == "group_index" && n.Target == p.ID {
			targeting = append(targeting, n.ID)
		}
	}
	sort.Strings(targeting)
	k, ok := nodes[p.Key]
	switch {
	case p.Key == "":
		c.add("graph.plate_key", p.ID, "key", map[string]any{"reason": "missing"})
	case !ok:
		c.add("graph.plate_key", p.ID, "key", map[string]any{"key": p.Key, "reason": "unknown_node"})
	case k.Kind != "data" || k.Type != "group_index":
		c.add("graph.plate_key", p.ID, "key", map[string]any{"key": p.Key, "reason": "not_a_group_index"})
	case k.Target != p.ID:
		c.add("graph.plate_key", p.ID, "key", map[string]any{"key": p.Key, "reason": "other_target"})
	case k.Plate == nil || *k.Plate != "rows":
		c.add("graph.plate_key", p.ID, "key", map[string]any{"key": p.Key, "reason": "not_on_rows"})
	}
	if len(targeting) > 1 {
		c.add("graph.plate_key", p.ID, "key", map[string]any{"reason": "several_keys", "nodes": strings.Join(targeting, ",")})
	}
}

// series: time is a time data node on rows; by a group plate or null.
func (c *checker) series(p Plate, byID map[string]Plate, nodes map[string]Node) {
	if p.Series == nil {
		return
	}
	t, ok := nodes[p.Series.Time]
	if !ok || t.Kind != "data" || t.Type != "time" || t.Plate == nil || *t.Plate != "rows" {
		c.add("graph.series", p.ID, "series.time", map[string]any{"time": p.Series.Time})
	}
	if p.Series.By != nil {
		if b, ok := byID[*p.Series.By]; !ok || b.Kind != kindGrp {
			c.add("graph.series", p.ID, "series.by", map[string]any{"by": *p.Series.By})
		}
	}
}
