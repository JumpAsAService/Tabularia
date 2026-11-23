package bayesgraph

import (
	"fmt"
	"strings"
	"testing"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/bayescatalog"
)

// check decodes the example edited by edit and returns the structural findings.
func check(t *testing.T, edit func(m map[string]any)) []Finding {
	t.Helper()
	return CheckStructure(mustDecode(t, withDoc(t, edit)))
}

func wantOnly(t *testing.T, name string, fs []Finding, code, node string) {
	t.Helper()
	if len(fs) == 0 || !hasCode(fs, code, node) {
		t.Errorf("%s: want %s on %q, got %s", name, code, node, codes(fs))
		return
	}
	for _, f := range fs {
		if f.Code != code && !strings.HasPrefix(name, "+") {
			t.Errorf("%s: unexpected %s on %q (all: %s)", name, f.Code, f.Node, codes(fs))
		}
	}
}

func TestIds(t *testing.T) {
	bad := []string{"N1", "1n", "n_1", "", "n1234567890123456", "é1", "n.1", "levels:n1"}
	for _, id := range bad {
		fs := check(t, func(m map[string]any) { node(m, 0)["id"] = id })
		if !hasCode(fs, "graph.id", id) {
			t.Errorf("id %q: %s", id, codes(fs))
		}
	}
	// just beside: 16 characters, digits inside
	if fs := check(t, func(m map[string]any) { node(m, 0)["id"] = "a234567890123456" }); hasCode(fs, "graph.id", "") {
		t.Errorf("a valid 16-character id was refused: %s", codes(fs))
	}
	// two nodes, one id: reported once, whatever their order
	fs := check(t, func(m map[string]any) { node(m, 1)["id"] = "n1" })
	if n := countCode(fs, "graph.id"); n != 1 {
		t.Errorf("duplicate id: %d findings: %s", n, codes(fs))
	}
	// a node and a plate share an id
	fs = check(t, func(m map[string]any) { node(m, 0)["id"] = "p1" })
	if !hasCode(fs, "graph.id", "p1") {
		t.Errorf("node and plate: %s", codes(fs))
	}
	// rows is the rows plate's alone
	fs = check(t, func(m map[string]any) { node(m, 0)["id"] = "rows" })
	if !hasCode(fs, "graph.id", "rows") {
		t.Errorf("a node called rows: %s", codes(fs))
	}
	fs = check(t, func(m map[string]any) { plate(m, 1)["id"] = "rows" })
	if !hasCode(fs, "graph.id", "rows") {
		t.Errorf("a group plate called rows: %s", codes(fs))
	}
	fs = check(t, func(m map[string]any) { plate(m, 0)["id"] = "base" })
	if !hasCode(fs, "graph.id", "base") {
		t.Errorf("a rows plate not called rows: %s", codes(fs))
	}
}

func countCode(fs []Finding, code string) int {
	n := 0
	for _, f := range fs {
		if f.Code == code {
			n++
		}
	}
	return n
}

func TestComponentIds(t *testing.T) {
	effects := func(a, b string) func(m map[string]any) {
		return func(m map[string]any) {
			m["nodes"] = append(m["nodes"].([]any), map[string]any{"id": "n10", "kind": "correlated_effects", "label": "E", "plate": "p1",
				"components": []any{
					map[string]any{"id": a, "name": "icpt", "label": "Intercept", "mean": map[string]any{"expr": "0"}, "scale": map[string]any{"expr": "1"}},
					map[string]any{"id": b, "name": "slope", "label": "Slope", "mean": map[string]any{"expr": "0"}, "scale": map[string]any{"expr": "1"}},
				}, "correlation": map[string]any{"name": "rho", "label": "Correlation", "eta": map[string]any{"expr": "2"}}})
		}
	}
	if fs := check(t, effects("c1", "c2")); len(fs) != 0 {
		t.Fatalf("clean effects: %s", codes(fs))
	}
	if fs := check(t, effects("c1", "c1")); !hasCode(fs, "graph.id", "n10.c1") {
		t.Errorf("duplicate component: %s", codes(fs))
	}
	if fs := check(t, effects("C1", "c2")); !hasCode(fs, "graph.id", "n10.C1") {
		t.Errorf("malformed component id: %s", codes(fs))
	}
}

func TestNames(t *testing.T) {
	long := "a" + strings.Repeat("b", bayescatalog.GetLimits().MaxNameBytes)
	cases := []struct {
		name, code string
	}{
		{"Score", "graph.name"}, {"1x", "graph.name"}, {"a-b", "graph.name"}, {"é", "graph.name"}, {"a b", "graph.name"},
		{"_x", "graph.name"}, {"", "graph.name"}, {long, "graph.name"}, {"x__", "graph.name"}, {"a___", "graph.name"},
		{"exp", "graph.name_function"}, {"log", "graph.name_function"}, {"lag", "graph.name_function"},
	}
	for _, c := range cases {
		fs := check(t, func(m map[string]any) { node(m, 0)["name"] = c.name })
		if !hasCode(fs, c.code, "n1") {
			t.Errorf("name %q: want %s, got %s", c.name, c.code, codes(fs))
		}
	}
	// just beside
	ok := []string{"a", "x_", "x_y", "a1", "a__b", "lower", "model", "target", "int", "y_lpdf", "exps", strings.Repeat("a", bayescatalog.GetLimits().MaxNameBytes)}
	for _, n := range ok {
		if fs := check(t, func(m map[string]any) { node(m, 0)["name"] = n }); len(fs) != 0 {
			t.Errorf("name %q refused: %s", n, codes(fs))
		}
	}
}

func TestNameTakenIsReportedOnEveryHolder(t *testing.T) {
	fs := check(t, func(m map[string]any) { node(m, 1)["name"] = "score" })
	if countCode(fs, "graph.name_taken") != 2 || !hasCode(fs, "graph.name_taken", "n1") || !hasCode(fs, "graph.name_taken", "n2") {
		t.Fatalf("%s", codes(fs))
	}
	// no name is not a name: two unnamed nodes are fine (the example has one; add another)
	fs = check(t, func(m map[string]any) { delete(node(m, 0), "name") })
	if len(fs) != 0 {
		t.Fatalf("%s", codes(fs))
	}
}

func TestNameTakenAcrossComponentsAndCorrelation(t *testing.T) {
	fs := check(t, func(m map[string]any) {
		m["nodes"] = append(m["nodes"].([]any), map[string]any{"id": "n10", "kind": "correlated_effects", "label": "E", "plate": "p1",
			"components":  []any{map[string]any{"id": "c1", "name": "mu", "label": "I"}},
			"correlation": map[string]any{"name": "beta", "label": "R"}})
	})
	if !hasCode(fs, "graph.name_taken", "n4") || !hasCode(fs, "graph.name_taken", "n10.c1") ||
		!hasCode(fs, "graph.name_taken", "n7") || !hasCode(fs, "graph.name_taken", "n10.correlation") {
		t.Fatalf("%s", codes(fs))
	}
}

func TestEmptyNamePresentIsAnError(t *testing.T) {
	fs := check(t, func(m map[string]any) { node(m, 0)["name"] = "" })
	if !hasCode(fs, "graph.name", "n1") {
		t.Fatalf("%s", codes(fs))
	}
}

func TestLabels(t *testing.T) {
	lim := bayescatalog.GetLimits()
	bad := map[string]string{
		"empty":     "",
		"too long":  strings.Repeat("é", lim.MaxLabelChars+1),
		"newline":   "a\nb",
		"tab":       "a\tb",
		"NUL":       "a\x00b",
		"C1":        "a\u0085b",
		"DEL C1":    "a\u009fb",
		"LRE":       "a‪b",
		"RLO":       "a‮b",
		"LRI":       "a⁦b",
		"PDI":       "a⁩b",
		"BOM":       "a\ufeffb",
		"BOM first": "\ufeffab",
	}
	for name, l := range bad {
		fs := check(t, func(m map[string]any) { node(m, 0)["label"] = l })
		if !hasCode(fs, "graph.label", "n1") {
			t.Errorf("%s: %s", name, codes(fs))
		}
		fs = check(t, func(m map[string]any) { plate(m, 0)["label"] = l })
		if !hasCode(fs, "graph.label", "rows") {
			t.Errorf("plate, %s: %s", name, codes(fs))
		}
	}
	// just beside: length counted in code points, any language, a joiner, an emoji
	good := map[string]string{
		"max in code points": strings.Repeat("é", lim.MaxLabelChars),
		"cjk":                strings.Repeat("語", lim.MaxLabelChars),
		"emoji":              strings.Repeat("😀", lim.MaxLabelChars),
		"arabic":             "عدد الساعات",
		"zwj":                "a‍b",
		"U+2065":             "a⁥b",
		"U+202F":             "a b",
		"U+2070":             "a⁰b",
	}
	for name, l := range good {
		if fs := check(t, func(m map[string]any) { node(m, 0)["label"] = l }); len(fs) != 0 {
			t.Errorf("%s refused: %s", name, codes(fs))
		}
	}
	// bytes do not count: 200 four-byte characters are 800 bytes and are accepted (above)
}

func TestComponentAndCorrelationLabels(t *testing.T) {
	fs := check(t, func(m map[string]any) {
		m["nodes"] = append(m["nodes"].([]any), map[string]any{"id": "n10", "kind": "correlated_effects", "label": "E", "plate": "p1",
			"components":  []any{map[string]any{"id": "c1", "label": ""}},
			"correlation": map[string]any{"label": "a‮b"}})
	})
	if !hasCode(fs, "graph.label", "n10.c1") || !hasCode(fs, "graph.label", "n10.correlation") {
		t.Fatalf("%s", codes(fs))
	}
}

func TestNotes(t *testing.T) {
	lim := bayescatalog.GetLimits()
	bad := map[string]string{
		"too long": strings.Repeat("x", lim.MaxNoteChars+1),
		"NUL":      "a\x00",
		"C1":       "a\u0085",
		"CR":       "a\rb",
		"RLE":      "a‫b",
		"BOM":      "\ufeff",
		"FSI":      "a⁨b",
	}
	for name, s := range bad {
		fs := check(t, func(m map[string]any) { node(m, 0)["note"] = s })
		if !hasCode(fs, "graph.note", "n1") {
			t.Errorf("%s: %s", name, codes(fs))
		}
		fs = check(t, func(m map[string]any) { plate(m, 1)["note"] = s })
		if !hasCode(fs, "graph.note", "p1") {
			t.Errorf("plate, %s: %s", name, codes(fs))
		}
	}
	for name, s := range map[string]string{
		"max":       strings.Repeat("é", lim.MaxNoteChars),
		"multiline": "line one\n\tline two",
		"empty":     "",
	} {
		if fs := check(t, func(m map[string]any) { node(m, 0)["note"] = s }); len(fs) != 0 {
			t.Errorf("%s refused: %s", name, codes(fs))
		}
	}
}

func TestRows(t *testing.T) {
	fs := check(t, func(m map[string]any) { m["plates"] = m["plates"].([]any)[1:] })
	if !hasCode(fs, "graph.rows", "rows") {
		t.Errorf("no rows plate: %s", codes(fs))
	}
	fs = check(t, func(m map[string]any) {
		m["plates"] = append(m["plates"].([]any), map[string]any{"id": "r2", "kind": "rows", "label": "More"})
	})
	if !hasCode(fs, "graph.rows", "rows") || !hasCode(fs, "graph.rows", "r2") || !hasCode(fs, "graph.id", "r2") {
		t.Errorf("two rows plates: %s", codes(fs))
	}
	if fs := CheckStructure(&Graph{FormatVersion: 1}); !hasCode(fs, "graph.rows", "rows") {
		t.Errorf("empty graph: %s", codes(fs))
	}
}

func TestPlateParent(t *testing.T) {
	grp := func(id, parent, key string) map[string]any {
		p := map[string]any{"id": id, "kind": "group", "label": id, "key": key}
		if parent != "" {
			p["parent"] = parent
		}
		return p
	}
	idx := func(id, target string) map[string]any {
		return map[string]any{"id": id, "kind": "data", "label": id, "plate": "rows", "type": "group_index", "target": target}
	}
	// a chain of four is allowed (max_plate_depth 4), five is not; each plate has its key
	chain := func(n int) func(m map[string]any) {
		return func(m map[string]any) {
			m["plates"] = m["plates"].([]any)[:1]
			m["nodes"] = []any{}
			parent := ""
			for i := 1; i <= n; i++ {
				id := fmt.Sprintf("p%d", i)
				m["plates"] = append(m["plates"].([]any), grp(id, parent, "k"+id))
				m["nodes"] = append(m["nodes"].([]any), idx("k"+id, id))
				parent = id
			}
		}
	}
	max := bayescatalog.GetLimits().MaxPlateDepth
	if fs := check(t, chain(max)); len(fs) != 0 {
		t.Errorf("depth %d: %s", max, codes(fs))
	}
	if fs := check(t, chain(max+1)); !hasCode(fs, "graph.plate_parent", fmt.Sprintf("p%d", max+1)) || countCode(fs, "graph.plate_parent") != 1 {
		t.Errorf("depth %d: %s", max+1, codes(fs))
	}
	// parent is the rows plate, a node, nothing
	for _, par := range []string{"rows", "n1", "zz"} {
		fs := check(t, func(m map[string]any) { plate(m, 1)["parent"] = par })
		if !hasCode(fs, "graph.plate_parent", "p1") {
			t.Errorf("parent %q: %s", par, codes(fs))
		}
	}
	// self and two-cycle
	fs := check(t, func(m map[string]any) { plate(m, 1)["parent"] = "p1" })
	if !hasCode(fs, "graph.plate_parent", "p1") {
		t.Errorf("self parent: %s", codes(fs))
	}
	fs = check(t, func(m map[string]any) {
		m["plates"] = append(m["plates"].([]any), grp("p2", "p1", "n10"))
		m["nodes"] = append(m["nodes"].([]any), idx("n10", "p2"))
		plate(m, 1)["parent"] = "p2"
	})
	if !hasCode(fs, "graph.plate_parent", "p1") || !hasCode(fs, "graph.plate_parent", "p2") {
		t.Errorf("cycle: %s", codes(fs))
	}
	// nested properly is fine
	fs = check(t, func(m map[string]any) {
		m["plates"] = append(m["plates"].([]any), grp("p2", "", "n10"))
		m["nodes"] = append(m["nodes"].([]any), idx("n10", "p2"))
		plate(m, 1)["parent"] = "p2"
	})
	if len(fs) != 0 {
		t.Errorf("nested: %s", codes(fs))
	}
}

func TestPlateForm(t *testing.T) {
	for _, f := range []string{"centred", "non_centred"} {
		if fs := check(t, func(m map[string]any) { plate(m, 1)["form"] = f }); len(fs) != 0 {
			t.Errorf("%s: %s", f, codes(fs))
		}
	}
	if fs := check(t, func(m map[string]any) { plate(m, 1)["form"] = "sideways" }); !hasCode(fs, "graph.format", "p1") {
		t.Errorf("%s", codes(fs))
	}
}

func TestPlateKey(t *testing.T) {
	cases := map[string]func(m map[string]any){
		"no key":              func(m map[string]any) { delete(plate(m, 1), "key") },
		"unknown key node":    func(m map[string]any) { plate(m, 1)["key"] = "zz" },
		"key is not index":    func(m map[string]any) { plate(m, 1)["key"] = "n1" },
		"key targets other":   func(m map[string]any) { node(m, 2)["target"] = "rows" },
		"key not on rows":     func(m map[string]any) { node(m, 2)["plate"] = "p1" },
		"key node scalar":     func(m map[string]any) { delete(node(m, 2), "plate") },
		"index no target":     func(m map[string]any) { delete(node(m, 2), "target") },
		"index on rows plate": func(m map[string]any) { node(m, 2)["target"] = "rows" },
	}
	for name, edit := range cases {
		fs := check(t, edit)
		if !hasCode(fs, "graph.plate_key", "") {
			t.Errorf("%s: %s", name, codes(fs))
		}
	}
	// two keys for one plate
	fs := check(t, func(m map[string]any) {
		m["nodes"] = append(m["nodes"].([]any), map[string]any{"id": "n10", "kind": "data", "label": "S2", "plate": "rows", "type": "group_index", "target": "p1"})
	})
	if !hasCode(fs, "graph.plate_key", "p1") {
		t.Errorf("two keys: %s", codes(fs))
	}
	// a group_index to a plate that is not a group plate, or does not exist
	for _, tgt := range []string{"rows", "zz", ""} {
		fs = check(t, func(m map[string]any) {
			m["nodes"] = append(m["nodes"].([]any), map[string]any{"id": "n10", "kind": "data", "label": "S2", "plate": "rows", "type": "group_index", "target": tgt})
		})
		if !hasCode(fs, "graph.plate_key", "n10") {
			t.Errorf("target %q: %s", tgt, codes(fs))
		}
	}
}

func TestSeries(t *testing.T) {
	series := func(time string, by any) func(m map[string]any) {
		return func(m map[string]any) {
			m["nodes"] = append(m["nodes"].([]any), map[string]any{"id": "n10", "kind": "data", "label": "Day", "plate": "rows", "type": "time"})
			s := map[string]any{"time": time}
			if by != nil {
				s["by"] = by
			}
			plate(m, 0)["series"] = s
		}
	}
	for name, edit := range map[string]func(m map[string]any){
		"time only":   series("n10", nil),
		"by a group":  series("n10", "p1"),
		"by null":     series("n10", nil),
		"time absent": func(m map[string]any) { series("n10", nil)(m); delete(plate(m, 0)["series"].(map[string]any), "time") },
	} {
		fs := check(t, edit)
		if name == "time absent" {
			if !hasCode(fs, "graph.series", "rows") {
				t.Errorf("%s: %s", name, codes(fs))
			}
			continue
		}
		if len(fs) != 0 {
			t.Errorf("%s: %s", name, codes(fs))
		}
	}
	bad := map[string]func(m map[string]any){
		"time unknown":    series("zz", nil),
		"time is real":    series("n1", nil),
		"time on a plate": func(m map[string]any) { series("n10", nil)(m); node(m, 9)["plate"] = "p1" },
		"time is scalar":  func(m map[string]any) { series("n10", nil)(m); delete(node(m, 9), "plate") },
		"by unknown":      series("n10", "zz"),
		"by rows":         series("n10", "rows"),
		"by is a node":    series("n10", "n1"),
	}
	for name, edit := range bad {
		if fs := check(t, edit); !hasCode(fs, "graph.series", "rows") {
			t.Errorf("%s: %s", name, codes(fs))
		}
	}
	// a series on a group plate is another key: refused when decoding
	doc := withDoc(t, func(m map[string]any) { plate(m, 1)["series"] = map[string]any{"time": "n1"} })
	if g, fs := Decode([]byte(doc)); g != nil || fs[0].Code != "graph.format" {
		t.Errorf("series on a group plate: %+v", fs)
	}
}

func TestEveryFindingIsDeterministic(t *testing.T) {
	g := mustDecode(t, withDoc(t, func(m map[string]any) {
		node(m, 0)["label"] = ""
		node(m, 1)["name"] = "score"
		node(m, 2)["id"] = "n1"
		plate(m, 1)["parent"] = "p1"
	}))
	a := fmt.Sprintf("%+v", CheckStructure(g))
	for i := 0; i < 20; i++ {
		if b := fmt.Sprintf("%+v", CheckStructure(g)); a != b {
			t.Fatalf("findings change between runs:\n%s\n%s", a, b)
		}
	}
	// reversing nodes and plates gives the same findings
	h := mustDecode(t, withDoc(t, func(m map[string]any) {
		node(m, 0)["label"] = ""
		node(m, 1)["name"] = "score"
		node(m, 2)["id"] = "n1"
		plate(m, 1)["parent"] = "p1"
	}))
	for i, j := 0, len(h.Nodes)-1; i < j; i, j = i+1, j-1 {
		h.Nodes[i], h.Nodes[j] = h.Nodes[j], h.Nodes[i]
	}
	if b := fmt.Sprintf("%+v", CheckStructure(h)); a != b {
		t.Fatalf("findings depend on the order of nodes:\n%s\n%s", a, b)
	}
}
