// Package bayesgraph is the model graph of the Bayesian editor: the canonical JSON of a model,
// its strict decoding, its canonical form and semantic hash, and the structural rules that
// make it a model Stan can sample (docs/specs/bayes-model-graph.md).
//
// This file holds the document's types, the findings, strict decoding, Canonical and
// SemanticHash. Versions are in version.go, the structural rules in structure.go.
package bayesgraph

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"sort"
	"strconv"
	"strings"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/bayescatalog"
)

// Severity of a finding.
type Severity string

const (
	// Error: no code can be generated, the model cannot be run.
	Error Severity = "error"
	// Warning: a fact the person should know; nothing is blocked.
	Warning Severity = "warning"
)

// Finding is one thing a rule found: a code (graph.*, expr.*), a severity, the node (or plate,
// or "<node id>.<component id>") and the slot it is about, and the arguments of its message
// (bayes.graph.<code>, in five languages, written elsewhere).
type Finding struct {
	Code     string         `json:"code"`
	Severity Severity       `json:"severity"`
	Node     string         `json:"node,omitempty"`
	Slot     string         `json:"slot,omitempty"`
	Args     map[string]any `json:"args,omitempty"`
}

// SortFindings orders findings by (node, slot, code), then by their arguments, so that a list
// of findings does not depend on the order in which the rules ran.
func SortFindings(fs []Finding) {
	key := func(f Finding) string {
		b, _ := json.Marshal(f.Args)
		return string(b)
	}
	sort.SliceStable(fs, func(i, j int) bool {
		a, b := fs[i], fs[j]
		if a.Node != b.Node {
			return a.Node < b.Node
		}
		if a.Slot != b.Slot {
			return a.Slot < b.Slot
		}
		if a.Code != b.Code {
			return a.Code < b.Code
		}
		return key(a) < key(b)
	})
}

// Slot is where a value is typed: one {"expr": "<text>"} (a typed expression; a number is an
// expression) or one {"ref": "<id>"} (a node or component without a name). That exactly one
// of the two is present is rule graph.slot (the expression lot), not decoding's.
type Slot struct {
	Expr *string `json:"expr,omitempty"`
	Ref  *string `json:"ref,omitempty"`
}

// Dist is a distribution with its arguments by name: a prior, a state's init.
type Dist struct {
	Dist string          `json:"dist"`
	Args map[string]Slot `json:"args"`
}

// Bounds are the numbers a parameter's range is cut with.
type Bounds struct {
	Lower *float64 `json:"lower,omitempty"`
	Upper *float64 `json:"upper,omitempty"`
}

// Term is one term of a linear predictor: coef × data.
type Term struct {
	Data string `json:"data"`
	Coef Slot   `json:"coef"`
}

// Component is one effect of a correlated-effects node.
type Component struct {
	ID    string `json:"id"`
	Name  string `json:"name,omitempty"`
	Label string `json:"label"`
	Mean  Slot   `json:"mean"`
	Scale Slot   `json:"scale"`
}

// Correlation is the LKJ correlation of a correlated-effects node.
type Correlation struct {
	Name  string `json:"name,omitempty"`
	Label string `json:"label"`
	Eta   Slot   `json:"eta"`
}

// Series makes the rows plate a time series: one series per unit of By (null: one series),
// ordered by Time (a time data node).
type Series struct {
	Time string  `json:"time"`
	By   *string `json:"by,omitempty"`
}

// Plate is a box: the rows, or a level of a hierarchy. Plate kinds: "rows", "group".
type Plate struct {
	ID     string  `json:"id"`
	Kind   string  `json:"kind"`
	Label  string  `json:"label"`
	Note   string  `json:"note,omitempty"`
	Series *Series `json:"series,omitempty"`
	Parent *string `json:"parent,omitempty"`
	Key    string  `json:"key,omitempty"`
	Form   string  `json:"form,omitempty"`
}

// Node is any node. The fields a kind owns are listed in kindKeys; a field of another kind is
// refused when decoding (graph.format). Absent and null are the same thing (a nil pointer): a
// null Plate is a single value.
type Node struct {
	ID    string  `json:"id"`
	Kind  string  `json:"kind"`
	Name  *string `json:"name,omitempty"`
	Label string  `json:"label"`
	Plate *string `json:"plate,omitempty"`
	Note  string  `json:"note,omitempty"`

	// data
	Type        string `json:"type,omitempty"`
	Support     string `json:"support,omitempty"`
	Target      string `json:"target,omitempty"`
	Standardize bool   `json:"standardize,omitempty"`

	// parameter
	Prior  *Dist   `json:"prior,omitempty"`
	Bounds *Bounds `json:"bounds,omitempty"`

	// transformed
	Value *Slot `json:"value,omitempty"`

	// linear_predictor
	Intercept *Slot  `json:"intercept,omitempty"`
	Terms     []Term `json:"terms,omitempty"`

	// likelihood
	Observed string          `json:"observed,omitempty"`
	Dist     string          `json:"dist,omitempty"`
	Args     map[string]Slot `json:"args,omitempty"`

	// state
	Process string `json:"process,omitempty"`
	Init    *Dist  `json:"init,omitempty"`
	Scale   *Slot  `json:"scale,omitempty"`
	Form    string `json:"form,omitempty"`

	// correlated_effects
	Components  []Component  `json:"components,omitempty"`
	Correlation *Correlation `json:"correlation,omitempty"`
}

// Box is a position and a size on the canvas. Only the canvas reads it.
type Box struct {
	X float64  `json:"x"`
	Y float64  `json:"y"`
	W *float64 `json:"w,omitempty"`
	H *float64 `json:"h,omitempty"`
}

// Graph is the document.
type Graph struct {
	FormatVersion int            `json:"format_version"`
	Plates        []Plate        `json:"plates"`
	Nodes         []Node         `json:"nodes"`
	Layout        map[string]Box `json:"layout,omitempty"`
}

const (
	kindRows = "rows"
	kindGrp  = "group"
)

// The keys each object may have: nothing else is accepted, and a key is matched exactly (Go's
// decoder alone would match «Format_Version» to format_version).
var (
	graphKeys     = keys("format_version", "plates", "nodes", "layout")
	boxKeys       = keys("x", "y", "w", "h")
	plateCommon   = []string{"id", "kind", "label", "note"}
	plateKeys     = map[string]map[string]bool{kindRows: keys(append(plateCommon, "series")...), kindGrp: keys(append(plateCommon, "parent", "key", "form")...)}
	seriesKeys    = keys("time", "by")
	nodeCommon    = []string{"id", "kind", "label", "name", "plate", "note"}
	slotKeys      = keys("expr", "ref")
	distKeys      = keys("dist", "args")
	boundsKeys    = keys("lower", "upper")
	termKeys      = keys("data", "coef")
	componentKeys = keys("id", "name", "label", "mean", "scale")
	correlKeys    = keys("name", "label", "eta")
	nodeKeys      = map[string]map[string]bool{
		"data":               keys(append(nodeCommon, "type", "support", "target", "standardize")...),
		"parameter":          keys(append(nodeCommon, "prior", "bounds")...),
		"transformed":        keys(append(nodeCommon, "value")...),
		"linear_predictor":   keys(append(nodeCommon, "intercept", "terms")...),
		"likelihood":         keys(append(nodeCommon, "observed", "dist", "args")...),
		"state":              keys(append(nodeCommon, "process", "init", "scale", "form")...),
		"correlated_effects": keys(append(nodeCommon, "components", "correlation")...),
	}
)

func keys(ks ...string) map[string]bool {
	m := make(map[string]bool, len(ks))
	for _, k := range ks {
		m[k] = true
	}
	return m
}

// formatFinding is the graph.format finding for a document that is not one.
func formatFinding(format string, a ...any) []Finding {
	return []Finding{{Code: "graph.format", Severity: Error, Args: map[string]any{"detail": fmt.Sprintf(format, a...)}}}
}

func tooBig(what string, got, max int) []Finding {
	return []Finding{{Code: "graph.too_big", Severity: Error,
		Args: map[string]any{"what": what, "got": got, "max": max}}}
}

// Decode reads a graph strictly: the document within max_graph_bytes (checked before anything
// is parsed), well-formed JSON without a duplicate key, a known format_version (older ones
// upgraded, newer refused), no unknown key (keys match exactly, a key of another kind of
// node is unknown), the right JSON types, at most max_nodes nodes and max_plates plates.
// It returns the graph, or nil and the findings (graph.format, graph.too_big). It does not
// check the rules of the model: CheckStructure and the validator do.
func Decode(data []byte) (*Graph, []Finding) {
	lim := bayescatalog.GetLimits()
	if len(data) > lim.MaxGraphBytes {
		return nil, tooBig("bytes", len(data), lim.MaxGraphBytes)
	}
	tree, err := parseTree(data)
	if err != nil {
		return nil, formatFinding("%v", err)
	}
	obj, ok := tree.(map[string]any)
	if !ok {
		return nil, formatFinding("the document must be a JSON object")
	}
	ver, err := versionOf(obj)
	if err != nil {
		return nil, formatFinding("%v", err)
	}
	if ver > FormatVersion {
		return nil, []Finding{{Code: "graph.format", Severity: Error,
			Args: map[string]any{"detail": fmt.Sprintf("format_version %d is newer than the %d this gateway reads", ver, FormatVersion), "version": ver}}}
	}
	if ver < FormatVersion {
		obj, err = upgrade(obj, ver, FormatVersion, upgrades)
		if err != nil {
			return nil, formatFinding("upgrading from format_version %d: %v", ver, err)
		}
		if data, err = json.Marshal(obj); err != nil {
			return nil, formatFinding("upgrading from format_version %d: %v", ver, err)
		}
	}
	if err := checkKeys(obj); err != nil {
		return nil, formatFinding("%v", err)
	}
	if n := listLen(obj["nodes"]); n > lim.MaxNodes {
		return nil, tooBig("nodes", n, lim.MaxNodes)
	}
	if n := listLen(obj["plates"]); n > lim.MaxPlates {
		return nil, tooBig("plates", n, lim.MaxPlates)
	}
	var g Graph
	dec := json.NewDecoder(bytes.NewReader(data))
	dec.DisallowUnknownFields()
	if err := dec.Decode(&g); err != nil {
		return nil, formatFinding("%v", err)
	}
	return &g, nil
}

func listLen(v any) int {
	l, _ := v.([]any)
	return len(l)
}

const maxDepth = 64

// parseTree parses a JSON document into maps, slices, strings, json.Number, bool and nil,
// refusing a duplicate key, trailing content and nesting deeper than maxDepth.
func parseTree(data []byte) (any, error) {
	dec := json.NewDecoder(bytes.NewReader(data))
	dec.UseNumber()
	v, err := parseValue(dec, 0)
	if err != nil {
		return nil, err
	}
	if _, err := dec.Token(); err != io.EOF {
		return nil, fmt.Errorf("unexpected content after the document")
	}
	return v, nil
}

func parseValue(dec *json.Decoder, depth int) (any, error) {
	if depth > maxDepth {
		return nil, fmt.Errorf("nested more than %d levels", maxDepth)
	}
	tok, err := dec.Token()
	if err != nil {
		return nil, fmt.Errorf("invalid JSON: %v", err)
	}
	d, isDelim := tok.(json.Delim)
	if !isDelim {
		return tok, nil
	}
	if d == '{' {
		m := map[string]any{}
		for dec.More() {
			kt, err := dec.Token()
			if err != nil {
				return nil, fmt.Errorf("invalid JSON: %v", err)
			}
			k, _ := kt.(string)
			if _, dup := m[k]; dup {
				return nil, fmt.Errorf("duplicate key %q", k)
			}
			if m[k], err = parseValue(dec, depth+1); err != nil {
				return nil, err
			}
		}
		if _, err := dec.Token(); err != nil {
			return nil, fmt.Errorf("invalid JSON: %v", err)
		}
		return m, nil
	}
	l := []any{}
	for dec.More() {
		e, err := parseValue(dec, depth+1)
		if err != nil {
			return nil, err
		}
		l = append(l, e)
	}
	if _, err := dec.Token(); err != nil {
		return nil, fmt.Errorf("invalid JSON: %v", err)
	}
	return l, nil
}

// checkKeys walks the document and refuses any key the schema does not have for that object.
func checkKeys(root map[string]any) error {
	if err := exact("the document", root, graphKeys); err != nil {
		return err
	}
	if v, ok := root["plates"]; ok {
		l, ok := v.([]any)
		if !ok && v != nil {
			return fmt.Errorf("plates: must be an array")
		}
		for i, e := range l {
			p := fmt.Sprintf("plates[%d]", i)
			o, ok := e.(map[string]any)
			if !ok {
				return fmt.Errorf("%s: must be an object", p)
			}
			kind, _ := o["kind"].(string)
			allowed, known := plateKeys[kind]
			if !known {
				return fmt.Errorf("%s: unknown kind %v", p, o["kind"])
			}
			if err := exact(p, o, allowed); err != nil {
				return err
			}
			if s, ok := o["series"].(map[string]any); ok {
				if err := exact(p+".series", s, seriesKeys); err != nil {
					return err
				}
			} else if o["series"] != nil {
				return fmt.Errorf("%s.series: must be an object", p)
			}
		}
	}
	if v, ok := root["nodes"]; ok {
		l, ok := v.([]any)
		if !ok && v != nil {
			return fmt.Errorf("nodes: must be an array")
		}
		for i, e := range l {
			if err := checkNode(fmt.Sprintf("nodes[%d]", i), e); err != nil {
				return err
			}
		}
	}
	if v, ok := root["layout"]; ok && v != nil {
		m, ok := v.(map[string]any)
		if !ok {
			return fmt.Errorf("layout: must be an object")
		}
		for id, b := range m {
			o, ok := b.(map[string]any)
			if !ok {
				return fmt.Errorf("layout.%s: must be an object", id)
			}
			if err := exact("layout."+id, o, boxKeys); err != nil {
				return err
			}
		}
	}
	return nil
}

func checkNode(p string, e any) error {
	o, ok := e.(map[string]any)
	if !ok {
		return fmt.Errorf("%s: must be an object", p)
	}
	kind, _ := o["kind"].(string)
	allowed, known := nodeKeys[kind]
	if !known {
		return fmt.Errorf("%s: unknown kind %v", p, o["kind"])
	}
	if err := exact(p+" ("+kind+")", o, allowed); err != nil {
		return err
	}
	for _, k := range []string{"value", "intercept", "scale"} {
		if err := slotAt(p+"."+k, o[k]); err != nil {
			return err
		}
	}
	for _, k := range []string{"prior", "init"} {
		if err := distAt(p+"."+k, o[k]); err != nil {
			return err
		}
	}
	if err := argsAt(p+".args", o["args"]); err != nil {
		return err
	}
	if err := objAt(p+".bounds", o["bounds"], boundsKeys, nil); err != nil {
		return err
	}
	if err := listAt(p+".terms", o["terms"], func(q string, t map[string]any) error {
		if err := exact(q, t, termKeys); err != nil {
			return err
		}
		return slotAt(q+".coef", t["coef"])
	}); err != nil {
		return err
	}
	if err := listAt(p+".components", o["components"], func(q string, c map[string]any) error {
		if err := exact(q, c, componentKeys); err != nil {
			return err
		}
		for _, k := range []string{"mean", "scale"} {
			if err := slotAt(q+"."+k, c[k]); err != nil {
				return err
			}
		}
		return nil
	}); err != nil {
		return err
	}
	return objAt(p+".correlation", o["correlation"], correlKeys, func(q string, c map[string]any) error {
		return slotAt(q+".eta", c["eta"])
	})
}

func exact(where string, o map[string]any, allowed map[string]bool) error {
	var bad []string
	for k := range o {
		if !allowed[k] {
			bad = append(bad, k)
		}
	}
	if len(bad) > 0 {
		sort.Strings(bad)
		return fmt.Errorf("%s: unknown key %q", where, bad[0])
	}
	return nil
}

func objAt(p string, v any, allowed map[string]bool, more func(string, map[string]any) error) error {
	if v == nil {
		return nil
	}
	o, ok := v.(map[string]any)
	if !ok {
		return fmt.Errorf("%s: must be an object", p)
	}
	if err := exact(p, o, allowed); err != nil {
		return err
	}
	if more != nil {
		return more(p, o)
	}
	return nil
}

func slotAt(p string, v any) error { return objAt(p, v, slotKeys, nil) }

func distAt(p string, v any) error {
	return objAt(p, v, distKeys, func(q string, o map[string]any) error { return argsAt(q+".args", o["args"]) })
}

func argsAt(p string, v any) error {
	if v == nil {
		return nil
	}
	m, ok := v.(map[string]any)
	if !ok {
		return fmt.Errorf("%s: must be an object", p)
	}
	for k, s := range m {
		if err := slotAt(p+"."+k, s); err != nil {
			return err
		}
	}
	return nil
}

func listAt(p string, v any, each func(string, map[string]any) error) error {
	if v == nil {
		return nil
	}
	l, ok := v.([]any)
	if !ok {
		return fmt.Errorf("%s: must be an array", p)
	}
	for i, e := range l {
		q := p + "[" + strconv.Itoa(i) + "]"
		o, ok := e.(map[string]any)
		if !ok {
			return fmt.Errorf("%s: must be an object", q)
		}
		if err := each(q, o); err != nil {
			return err
		}
	}
	return nil
}

// Canonical writes the graph in its canonical form: compact JSON, plates (rows first, then by
// id), nodes by id, keys in the order of the document's tables (the order of the struct
// fields), maps by key, slots as typed, numbers as Go's encoder writes them (the shortest form
// that reads back to the same double), absent and null the same (omitted), layout last. The
// order of a node's own lists (terms, components) is part of the model and kept. The graph is
// not modified.
func Canonical(g *Graph) ([]byte, error) { return canonical(g, true) }

// SemanticHash is the sha256 (hex) of the canonical form without the layout: moving a box
// changes nothing, changing a label changes it.
func SemanticHash(g *Graph) (string, error) {
	b, err := canonical(g, false)
	if err != nil {
		return "", err
	}
	sum := sha256.Sum256(b)
	return hex.EncodeToString(sum[:]), nil
}

func canonical(g *Graph, withLayout bool) ([]byte, error) {
	c := *g
	c.Plates = append([]Plate{}, g.Plates...)
	sort.SliceStable(c.Plates, func(i, j int) bool {
		a, b := c.Plates[i], c.Plates[j]
		if (a.Kind == kindRows) != (b.Kind == kindRows) {
			return a.Kind == kindRows
		}
		return a.ID < b.ID
	})
	c.Nodes = append([]Node{}, g.Nodes...)
	sort.SliceStable(c.Nodes, func(i, j int) bool { return c.Nodes[i].ID < c.Nodes[j].ID })
	if !withLayout || len(g.Layout) == 0 {
		c.Layout = nil
	}
	var buf bytes.Buffer
	enc := json.NewEncoder(&buf)
	enc.SetEscapeHTML(false)
	if err := enc.Encode(&c); err != nil {
		return nil, err
	}
	return bytes.TrimRight(buf.Bytes(), "\n"), nil
}

// quote is strconv.Quote for the findings' details.
func quote(s string) string { return strings.TrimSpace(strconv.Quote(s)) }
