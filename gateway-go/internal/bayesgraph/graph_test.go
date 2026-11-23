package bayesgraph

import (
	"bytes"
	"encoding/json"
	"fmt"
	"strings"
	"testing"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/bayescatalog"
)

// example is the spec's example: pupils' scores in schools.
const example = `{
  "format_version": 1,
  "plates": [
    {"id": "rows", "kind": "rows", "label": "Pupils"},
    {"id": "p1", "kind": "group", "label": "School", "parent": null, "key": "n3", "form": "non_centred"}
  ],
  "nodes": [
    {"id": "n1", "kind": "data", "name": "score", "label": "Test score", "plate": "rows", "type": "real"},
    {"id": "n2", "kind": "data", "name": "hours", "label": "Hours of study", "plate": "rows", "type": "real",
     "standardize": true},
    {"id": "n3", "kind": "data", "name": "school", "label": "School", "plate": "rows", "type": "group_index",
     "target": "p1"},
    {"id": "n4", "kind": "parameter", "name": "mu", "label": "Mean of the schools", "plate": null,
     "prior": {"dist": "normal", "args": {"mu": {"expr": "0"}, "sigma": {"expr": "10"}}}},
    {"id": "n5", "kind": "parameter", "name": "tau", "label": "Spread between schools", "plate": null,
     "prior": {"dist": "normal", "args": {"mu": {"expr": "0"}, "sigma": {"expr": "5"}}},
     "bounds": {"lower": 0}},
    {"id": "n6", "kind": "parameter", "name": "alpha_school", "label": "Mean of a school", "plate": "p1",
     "prior": {"dist": "normal", "args": {"mu": {"expr": "mu"}, "sigma": {"expr": "tau"}}}},
    {"id": "n7", "kind": "parameter", "name": "beta", "label": "Effect of one more hour", "plate": null,
     "prior": {"dist": "normal", "args": {"mu": {"expr": "0"}, "sigma": {"expr": "1"}}}},
    {"id": "n8", "kind": "parameter", "label": "Spread within a school", "plate": null,
     "prior": {"dist": "exponential", "args": {"beta": {"expr": "1"}}}},
    {"id": "n9", "kind": "likelihood", "label": "Scores", "observed": "n1", "dist": "normal",
     "args": {"mu": {"expr": "alpha_school[school] + beta * hours"}, "sigma": {"ref": "n8"}}}
  ],
  "layout": {"p1": {"x": 0, "y": 0, "w": 420, "h": 260}, "n6": {"x": 40, "y": 60}}
}`

func mustDecode(t *testing.T, s string) *Graph {
	t.Helper()
	g, fs := Decode([]byte(s))
	if g == nil {
		t.Fatalf("Decode: %+v", fs)
	}
	return g
}

func codes(fs []Finding) string {
	var c []string
	for _, f := range fs {
		c = append(c, f.Code+"@"+f.Node+"/"+f.Slot)
	}
	return strings.Join(c, " ")
}

func hasCode(fs []Finding, code, node string) bool {
	for _, f := range fs {
		if f.Code == code && (node == "" || f.Node == node) {
			return true
		}
	}
	return false
}

// withDoc edits the example as a generic tree and returns its JSON.
func withDoc(t *testing.T, edit func(m map[string]any)) string {
	t.Helper()
	var m map[string]any
	if err := json.Unmarshal([]byte(example), &m); err != nil {
		t.Fatal(err)
	}
	edit(m)
	b, err := json.Marshal(m)
	if err != nil {
		t.Fatal(err)
	}
	return string(b)
}

func node(m map[string]any, i int) map[string]any  { return m["nodes"].([]any)[i].(map[string]any) }
func plate(m map[string]any, i int) map[string]any { return m["plates"].([]any)[i].(map[string]any) }

func TestExampleDecodesAndIsClean(t *testing.T) {
	g := mustDecode(t, example)
	if fs := CheckStructure(g); len(fs) != 0 {
		t.Fatalf("findings on the spec's example: %+v", fs)
	}
	if len(g.Nodes) != 9 || len(g.Plates) != 2 || g.Nodes[7].Name != nil || str0(g.Nodes[8].Args["sigma"].Ref) != "n8" {
		t.Fatalf("unexpected decoding: %+v", g)
	}
}

func str0(p *string) string {
	if p == nil {
		return ""
	}
	return *p
}

func TestStrictDecoding(t *testing.T) {
	cases := map[string]string{
		"unknown top key":      withDoc(t, func(m map[string]any) { m["extra"] = 1 }),
		"unknown node key":     withDoc(t, func(m map[string]any) { node(m, 0)["colour"] = "red" }),
		"unknown plate key":    withDoc(t, func(m map[string]any) { plate(m, 0)["colour"] = "red" }),
		"unknown slot key":     withDoc(t, func(m map[string]any) { node(m, 8)["args"].(map[string]any)["mu"].(map[string]any)["x"] = 1 }),
		"unknown layout key":   withDoc(t, func(m map[string]any) { m["layout"].(map[string]any)["p1"].(map[string]any)["z"] = 1 }),
		"unknown prior key":    withDoc(t, func(m map[string]any) { node(m, 3)["prior"].(map[string]any)["x"] = 1 }),
		"field of other kind":  withDoc(t, func(m map[string]any) { node(m, 3)["standardize"] = false }),
		"group key on rows":    withDoc(t, func(m map[string]any) { plate(m, 0)["parent"] = nil }),
		"unknown node kind":    withDoc(t, func(m map[string]any) { node(m, 0)["kind"] = "magic" }),
		"unknown plate kind":   withDoc(t, func(m map[string]any) { plate(m, 0)["kind"] = "magic" }),
		"wrong type label":     withDoc(t, func(m map[string]any) { node(m, 0)["label"] = 5 }),
		"wrong type nodes":     withDoc(t, func(m map[string]any) { m["nodes"] = "x" }),
		"wrong type standard.": withDoc(t, func(m map[string]any) { node(m, 1)["standardize"] = "yes" }),
		"wrong type bound":     withDoc(t, func(m map[string]any) { node(m, 4)["bounds"].(map[string]any)["lower"] = "0" }),
		"version missing":      withDoc(t, func(m map[string]any) { delete(m, "format_version") }),
		"version string":       withDoc(t, func(m map[string]any) { m["format_version"] = "1" }),
		"version fractional":   `{"format_version": 1.5, "plates": [], "nodes": []}`,
		"version zero":         `{"format_version": 0, "plates": [], "nodes": []}`,
		"newer version":        withDoc(t, func(m map[string]any) { m["format_version"] = 2 }),
		"case-folded key":      strings.Replace(example, `"format_version"`, `"Format_Version"`, 1),
		"duplicate key":        strings.Replace(example, `"plates": [`, `"plates": [], "plates": [`, 1),
		"trailing content":     example + ` {}`,
		"trailing brace":       example + `}`,
		"not an object":        `[]`,
		"null":                 `null`,
		"empty":                ``,
		"too deep":             `{"format_version":1,"layout":` + strings.Repeat(`{"a":`, 80) + `1` + strings.Repeat(`}`, 80) + `}`,
	}
	for name, doc := range cases {
		g, fs := Decode([]byte(doc))
		if g != nil || len(fs) != 1 || fs[0].Code != "graph.format" || fs[0].Severity != Error {
			t.Errorf("%s: graph %v, findings %+v", name, g != nil, fs)
		}
	}
}

// TestDecodeDetailIsTheDocuments: a value of the wrong JSON type is told by its path and the
// JSON type it must be, as the TypeScript mirror tells it, never by the decoder's message with
// Go's types (E4, finding 6).
func TestDecodeDetailIsTheDocuments(t *testing.T) {
	node := func(fields string) string {
		return `{"format_version":1,"plates":[],"nodes":[{"id":"a1","kind":"parameter","label":"A"` + fields + `}]}`
	}
	cases := map[string]string{
		`{"format_version":1,"plates":[],"nodes":[{"id":"a1","kind":"data","label":5}]}`: "nodes[0].label: must be a string",
		node(`,"bounds":{"lower":"0"}`):                              "nodes[0].bounds.lower: must be a number",
		node(`,"bounds":{"upper":1e999}`):                            "nodes[0].bounds.upper: must be a number",
		node(`,"bounds":{"lower":[1]}`):                              "nodes[0].bounds.lower: must be a number",
		node(`,"name":true`):                                         "nodes[0].name: must be a string",
		node(`,"prior":{"dist":"normal","args":{"mu":{"expr":{}}}}`): "nodes[0].prior.args.mu.expr: must be a string",
		`{"format_version":1,"plates":[],"nodes":[{"id":"a1","kind":"data","label":"A","standardize":"yes"}]}`: "nodes[0].standardize: must be a boolean",
		`{"format_version":1,"plates":[],"nodes":[],"layout":{"a1":{"x":"1","y":0}}}`:                          "layout.a1.x: must be a number",
	}
	for doc, want := range cases {
		g, fs := Decode([]byte(doc))
		if g != nil || len(fs) != 1 || fs[0].Code != "graph.format" {
			t.Errorf("%s: graph %v, findings %+v", doc, g != nil, fs)
			continue
		}
		got, _ := fs[0].Args["detail"].(string)
		if got != want {
			t.Errorf("%s: detail %q, want %q", doc, got, want)
		}
		for _, goWord := range []string{"json:", "Go ", "struct", "float64", "bool ", "Graph", "unmarshal"} {
			if strings.Contains(got, goWord) {
				t.Errorf("%s: detail %q names %q", doc, got, goWord)
			}
		}
	}
}

func TestNewerVersionSaysWhich(t *testing.T) {
	_, fs := Decode([]byte(withDoc(t, func(m map[string]any) { m["format_version"] = 7 })))
	if len(fs) != 1 || fs[0].Args["version"] != 7 {
		t.Fatalf("%+v", fs)
	}
}

func TestEmptyAndNullCollectionsDecode(t *testing.T) {
	for _, d := range []string{
		`{"format_version":1,"plates":null,"nodes":null}`,
		`{"format_version":1,"plates":[],"nodes":[],"layout":{}}`,
		`{"format_version":1}`,
	} {
		if g, fs := Decode([]byte(d)); g == nil {
			t.Errorf("%s: %+v", d, fs)
		}
	}
}

func TestTooBigBodyIsRefusedBeforeDecoding(t *testing.T) {
	max := bayescatalog.GetLimits().MaxGraphBytes
	// not even JSON: if it were decoded the finding would be graph.format
	g, fs := Decode(bytes.Repeat([]byte("x"), max+1))
	if g != nil || len(fs) != 1 || fs[0].Code != "graph.too_big" || fs[0].Args["what"] != "bytes" {
		t.Fatalf("%+v", fs)
	}
	// exactly max_graph_bytes is read (and found not to be JSON)
	_, fs = Decode(bytes.Repeat([]byte("x"), max))
	if len(fs) != 1 || fs[0].Code != "graph.format" {
		t.Fatalf("%+v", fs)
	}
	// a valid document padded by a long label up to the limit and beyond
	pad := strings.Repeat("a", max)
	_, fs = Decode([]byte(`{"format_version":1,"plates":[{"id":"rows","kind":"rows","label":"` + pad + `"}]}`))
	if len(fs) != 1 || fs[0].Code != "graph.too_big" {
		t.Fatalf("%+v", fs)
	}
}

func TestTooManyNodesAndPlates(t *testing.T) {
	lim := bayescatalog.GetLimits()
	nodes := make([]string, lim.MaxNodes+1)
	for i := range nodes {
		nodes[i] = fmt.Sprintf(`{"id":"n%d","kind":"transformed","label":"x"}`, i)
	}
	doc := `{"format_version":1,"nodes":[` + strings.Join(nodes, ",") + `]}`
	if len(doc) > lim.MaxGraphBytes {
		t.Skip("limits changed: the test document is too big")
	}
	g, fs := Decode([]byte(doc))
	if g != nil || fs[0].Code != "graph.too_big" || fs[0].Args["what"] != "nodes" {
		t.Fatalf("%+v", fs)
	}
	plates := make([]string, lim.MaxPlates+1)
	for i := range plates {
		plates[i] = fmt.Sprintf(`{"id":"p%d","kind":"group","label":"x"}`, i)
	}
	g, fs = Decode([]byte(`{"format_version":1,"plates":[` + strings.Join(plates, ",") + `]}`))
	if g != nil || fs[0].Code != "graph.too_big" || fs[0].Args["what"] != "plates" {
		t.Fatalf("%+v", fs)
	}
	// the same rule on a graph built in Go
	big := &Graph{FormatVersion: 1, Nodes: make([]Node, lim.MaxNodes+1)}
	for i := range big.Nodes {
		big.Nodes[i] = Node{ID: fmt.Sprintf("n%d", i), Kind: "transformed", Label: "x"}
	}
	if !hasCode(CheckStructure(big), "graph.too_big", "") {
		t.Fatal("CheckStructure misses too many nodes")
	}
}

func TestCanonicalRoundTrip(t *testing.T) {
	g := mustDecode(t, example)
	c1, err := Canonical(g)
	if err != nil {
		t.Fatal(err)
	}
	g2, fs := Decode(c1)
	if g2 == nil {
		t.Fatalf("canonical form does not decode: %+v", fs)
	}
	c2, err := Canonical(g2)
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(c1, c2) {
		t.Fatalf("not idempotent:\n%s\n%s", c1, c2)
	}
	if !bytes.Contains(c1, []byte(`"layout"`)) {
		t.Fatal("Canonical dropped the layout")
	}
	if bytes.ContainsAny(c1, "\n ") && !bytes.Contains(c1, []byte("Test score")) {
		t.Fatal("not compact")
	}
}

func TestCanonicalIgnoresOrderOfPlatesAndNodes(t *testing.T) {
	g := mustDecode(t, example)
	h := mustDecode(t, example)
	h.Plates[0], h.Plates[1] = h.Plates[1], h.Plates[0]
	for i, j := 0, len(h.Nodes)-1; i < j; i, j = i+1, j-1 {
		h.Nodes[i], h.Nodes[j] = h.Nodes[j], h.Nodes[i]
	}
	a, _ := Canonical(g)
	b, _ := Canonical(h)
	if !bytes.Equal(a, b) {
		t.Fatalf("order changes the canonical form")
	}
	if !bytes.HasPrefix(a, []byte(`{"format_version":1,"plates":[{"id":"rows"`)) {
		t.Fatalf("rows is not first: %s", a[:80])
	}
	// the input is not modified
	if h.Plates[0].ID != "p1" {
		t.Fatal("Canonical reordered its argument")
	}
}

func TestCanonicalIgnoresOrderOfJSONKeys(t *testing.T) {
	reordered := `{"nodes":[{"label":"L","kind":"transformed","id":"n1"}],"plates":[{"label":"R","kind":"rows","id":"rows"}],"format_version":1}`
	ordered := `{"format_version":1,"plates":[{"id":"rows","kind":"rows","label":"R"}],"nodes":[{"id":"n1","kind":"transformed","label":"L"}]}`
	a, _ := Canonical(mustDecode(t, reordered))
	if string(a) != ordered {
		t.Fatalf("%s", a)
	}
}

func TestHashIgnoresLayoutButNotLabels(t *testing.T) {
	g := mustDecode(t, example)
	h0, err := SemanticHash(g)
	if err != nil {
		t.Fatal(err)
	}
	g.Layout["p1"] = Box{X: 999, Y: -3}
	g.Layout["new"] = Box{}
	if h, _ := SemanticHash(g); h != h0 {
		t.Fatal("moving a box changed the hash")
	}
	g.Layout = nil
	if h, _ := SemanticHash(g); h != h0 {
		t.Fatal("dropping the layout changed the hash")
	}
	if len(h0) != 64 {
		t.Fatalf("hash %q", h0)
	}
	g.Nodes[0].Label = "Test scores"
	if h, _ := SemanticHash(g); h == h0 {
		t.Fatal("changing a label did not change the hash")
	}
	g = mustDecode(t, example)
	g.Plates[1].Label = "Schools"
	if h, _ := SemanticHash(g); h == h0 {
		t.Fatal("changing a plate's label did not change the hash")
	}
	g = mustDecode(t, example)
	e := "tau + 1"
	g.Nodes[8].Args["mu"] = Slot{Expr: &e}
	if h, _ := SemanticHash(g); h == h0 {
		t.Fatal("changing an expression did not change the hash")
	}
}

func TestCanonicalNumbers(t *testing.T) {
	doc := func(lower string) string {
		return `{"format_version":1,"plates":[{"id":"rows","kind":"rows","label":"R"}],"nodes":[{"id":"n1","kind":"parameter","label":"P","bounds":{"lower":` + lower + `}}]}`
	}
	for _, in := range []string{"0.1", "1e-7", "123456789.123456789", "1e21", "-0.5", "2.5E3", "0.30000000000000004"} {
		g := mustDecode(t, doc(in))
		c, err := Canonical(g)
		if err != nil {
			t.Fatal(err)
		}
		g2 := mustDecode(t, string(c))
		if *g2.Nodes[0].Bounds.Lower != *g.Nodes[0].Bounds.Lower {
			t.Errorf("%s: %v != %v", in, *g2.Nodes[0].Bounds.Lower, *g.Nodes[0].Bounds.Lower)
		}
	}
	g := mustDecode(t, doc("2.50"))
	c, _ := Canonical(g)
	if !bytes.Contains(c, []byte(`"lower":2.5}`)) {
		t.Fatalf("not the shortest form: %s", c)
	}
	// the same number written two ways has one hash
	a, _ := SemanticHash(mustDecode(t, doc("0.10")))
	b, _ := SemanticHash(mustDecode(t, doc("1e-1")))
	if a != b {
		t.Fatal("equal doubles, different hashes")
	}
}

func TestCanonicalRefusesWhatJSONCannotWrite(t *testing.T) {
	g := mustDecode(t, example)
	nan := 0.0
	nan = nan / nan
	g.Nodes[4].Bounds.Lower = &nan
	if _, err := Canonical(g); err == nil {
		t.Fatal("NaN written")
	}
}

func TestCanonicalDoesNotEscapeHTML(t *testing.T) {
	g := mustDecode(t, example)
	g.Nodes[0].Label = "a<b & c>d"
	c, _ := Canonical(g)
	if !bytes.Contains(c, []byte("a<b & c>d")) {
		t.Fatalf("escaped: %s", c)
	}
}

func TestFindingsAreSorted(t *testing.T) {
	fs := []Finding{{Code: "b", Node: "n2"}, {Code: "z", Node: "n1", Slot: "b"}, {Code: "a", Node: "n1", Slot: "b"}, {Code: "c", Node: "n1"}}
	SortFindings(fs)
	if got := codes(fs); got != "c@n1/ a@n1/b z@n1/b b@n2/" {
		t.Fatal(got)
	}
}
