package bayesgraph

import (
	"bytes"
	"encoding/json"
	"flag"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"testing"
)

// The corpus (docs/specs/bayes-model-graph.md, «Testing strategy»): every rule of «Validation» and
// every expr.* code a slot reports has at least two cases, one that trips it and one just beside it
// that does not; the map's four families are clean graphs; hostile documents. The cases are written
// here, the files of testdata/corpus/ hold each case's input and the findings Go gives, and go test
// fails when a committed file differs from what the case and the validator give now. Regenerate
// with: go test -run TestCorpus -update-corpus ./internal/bayesgraph/

var updateCorpus = flag.Bool("update-corpus", false, "rewrite testdata/corpus/")

// corpusCase is a case of the corpus. Expect: "trips" (the finding of Rule is there, on Node when
// given), "clean" (none of Rule), "valid" (no finding at all).
type corpusCase struct {
	Name   string
	Rule   string
	Expect string
	Node   string
	Raw    bool // the input is text, not a JSON tree
	Stored bool // the input is written in the file (a big one is built by the test instead)
	Build  func() []byte
}

// corpusFile is a file of testdata/corpus/.
type corpusFile struct {
	Name      string          `json:"name"`
	Rule      string          `json:"rule"`
	Expect    string          `json:"expect"`
	Node      string          `json:"node,omitempty"`
	Input     json.RawMessage `json:"input,omitempty"`
	Raw       *string         `json:"raw,omitempty"`
	Generated bool            `json:"generated,omitempty"`
	Findings  []Finding       `json:"findings"`
}

// cpFindings is what the gateway says of a document: Decode's findings, or the validator's.
func cpFindings(data []byte) []Finding {
	g, fs := Decode(data)
	if g == nil {
		return fs
	}
	out := Validate(g)
	if out == nil {
		out = []Finding{}
	}
	return out
}

func cpTree(src string, fn func(m map[string]any)) map[string]any {
	var m map[string]any
	if err := json.Unmarshal([]byte(src), &m); err != nil {
		panic(err)
	}
	if fn != nil {
		fn(m)
	}
	return m
}

func cpDoc(src string, fn func(m map[string]any)) []byte {
	b, err := json.Marshal(cpTree(src, fn))
	if err != nil {
		panic(err)
	}
	return b
}

// cpNested: pupils in schools in districts, a category, a series, a district-level datum, a linear
// predictor: nothing unused.
const cpNested = `{
  "format_version": 1,
  "plates": [
    {"id": "rows", "kind": "rows", "label": "Pupils", "series": {"time": "d4", "by": null}},
    {"id": "p1", "kind": "group", "label": "District", "key": "d1"},
    {"id": "p2", "kind": "group", "label": "School", "parent": "p1", "key": "d2"}
  ],
  "nodes": [
    {"id": "d1", "kind": "data", "name": "district", "label": "District", "plate": "rows", "type": "group_index", "target": "p1"},
    {"id": "d2", "kind": "data", "name": "school", "label": "School", "plate": "rows", "type": "group_index", "target": "p2"},
    {"id": "d3", "kind": "data", "name": "region", "label": "Region", "plate": "rows", "type": "category"},
    {"id": "d4", "kind": "data", "name": "day", "label": "Day", "plate": "rows", "type": "time"},
    {"id": "d5", "kind": "data", "name": "score", "label": "Score", "plate": "rows", "type": "real"},
    {"id": "d6", "kind": "data", "name": "size", "label": "District size", "plate": "p1", "type": "real", "standardize": true},
    {"id": "d7", "kind": "data", "name": "hours", "label": "Hours", "plate": "rows", "type": "real", "standardize": true},
    {"id": "d8", "kind": "data", "name": "count", "label": "Count", "plate": "rows", "type": "int", "support": "nonnegative"},
    {"id": "m1", "kind": "parameter", "name": "mu", "label": "Mean", "prior": {"dist": "normal", "args": {"mu": {"expr": "0"}, "sigma": {"expr": "1"}}}},
    {"id": "m2", "kind": "parameter", "name": "mu_district", "label": "District mean", "plate": "p1",
     "prior": {"dist": "normal", "args": {"mu": {"expr": "mu"}, "sigma": {"expr": "1"}}}},
    {"id": "m3", "kind": "parameter", "name": "mu_school", "label": "School mean", "plate": "p2",
     "prior": {"dist": "normal", "args": {"mu": {"expr": "mu_district[district]"}, "sigma": {"expr": "1"}}}},
    {"id": "m4", "kind": "parameter", "name": "b_region", "label": "Region effect", "plate": "levels:d3",
     "prior": {"dist": "normal", "args": {"mu": {"expr": "0"}, "sigma": {"expr": "1"}}}},
    {"id": "m5", "kind": "parameter", "name": "sigma", "label": "Noise", "prior": {"dist": "exponential", "args": {"beta": {"expr": "1"}}}},
    {"id": "m6", "kind": "parameter", "name": "b_hours", "label": "Hours effect", "prior": {"dist": "normal", "args": {"mu": {"expr": "0"}, "sigma": {"expr": "1"}}}},
    {"id": "m7", "kind": "parameter", "name": "b_count", "label": "Count effect", "prior": {"dist": "normal", "args": {"mu": {"expr": "0"}, "sigma": {"expr": "1"}}}},
    {"id": "t1", "kind": "transformed", "name": "trend", "label": "Trend", "plate": "rows", "value": {"expr": "lag(score, 1) + lag(score, 2)"}},
    {"id": "t2", "kind": "transformed", "name": "dsize", "label": "Size doubled", "plate": "p1", "value": {"expr": "size * 2"}},
    {"id": "q1", "kind": "linear_predictor", "name": "eta", "label": "Predictor", "plate": "rows",
     "terms": [{"data": "d7", "coef": {"expr": "b_hours"}}, {"data": "d8", "coef": {"ref": "m7"}}]},
    {"id": "l1", "kind": "likelihood", "label": "Scores", "observed": "d5", "dist": "normal",
     "args": {"mu": {"expr": "mu_school[school] + b_region[region] + trend + eta + dsize[district]"}, "sigma": {"expr": "sigma"}}}
  ]
}`

func cpNode(m map[string]any, id string) map[string]any { return nodeByID(m, id) }

// cpX adds a transformed node x1 (a scalar when plate is nil) with the expression under test.
func cpX(text string, plate any) func(m map[string]any) {
	return func(m map[string]any) {
		addNodes(m, map[string]any{"id": "x1", "kind": "transformed", "label": "Under test", "plate": plate, "value": expr(text)})
	}
}

// vectorFill is transformed nodes on the rows plate whose expressions (data + data + …) hold n
// nodes together, each at most 255 (max_expression_nodes 256, odd sizes): the vector nodes of
// max_program_vector_nodes, added to a graph's own.
func vectorFill(data string, n int) []map[string]any {
	var sizes []int
	for ; n > 255; n -= 255 {
		sizes = append(sizes, 255)
	}
	if n%2 == 0 && n > 0 {
		sizes = append(sizes, 1)
		n--
	}
	if n > 0 {
		sizes = append(sizes, n)
	}
	var out []map[string]any
	for i, k := range sizes {
		out = append(out, transformed(fmt.Sprintf("v%d", i+1), "", cpMany((k+1)/2, data, "+"), "rows"))
	}
	return out
}

func cpMany(n int, word, sep string) string {
	return strings.TrimSuffix(strings.Repeat(word+sep, n), sep)
}

func cpOn(src string, fn func(m map[string]any)) func() []byte {
	return func() []byte { return cpDoc(src, fn) }
}

func cpRaw(s string) func() []byte { return func() []byte { return []byte(s) } }

func corpusCases() []corpusCase {
	var cs []corpusCase
	add := func(name, rule, expect, node string, build func() []byte) {
		cs = append(cs, corpusCase{Name: name, Rule: rule, Expect: expect, Node: node, Stored: true, Build: build})
	}
	raw := func(name, rule, expect, node, text string) {
		cs = append(cs, corpusCase{Name: name, Rule: rule, Expect: expect, Node: node, Raw: true, Stored: true, Build: cpRaw(text)})
	}
	big := func(name, rule, expect, node string, build func() []byte) {
		cs = append(cs, corpusCase{Name: name, Rule: rule, Expect: expect, Node: node, Build: build})
	}
	trips := func(name, rule, node, src string, fn func(m map[string]any)) {
		add(name, rule, "trips", node, cpOn(src, fn))
	}
	clean := func(name, rule, src string, fn func(m map[string]any)) {
		add(name, rule, "clean", "", cpOn(src, fn))
	}
	set := func(id, key string, v any) func(m map[string]any) {
		return func(m map[string]any) { nodeByID(m, id)[key] = v }
	}
	fam := func(name, src string) { add("family-"+name, "family", "valid", "", cpOn(src, nil)) }

	// the four families, the spec's example, the nested one
	fam("regression", regression)
	fam("logistic-correlated", logistic)
	fam("ar2", ar2)
	fam("local-level", locallevel)
	fam("spec-example", example)
	fam("nested", cpNested)

	// graph.format
	trips("format-unknown-key", "graph.format", "", regression, func(m map[string]any) { m["bogus"] = 1 })
	clean("format-layout-key", "graph.format", regression, func(m map[string]any) {
		m["layout"] = map[string]any{"d1": map[string]any{"x": 1, "y": 2}}
	})
	raw("format-duplicate-key", "graph.format", "trips", "", `{"format_version":1,"format_version":1,"plates":[],"nodes":[]}`)
	raw("format-newer-version", "graph.format", "trips", "", `{"format_version":2,"plates":[],"nodes":[]}`)
	raw("format-case-folded-key", "graph.format", "trips", "", `{"Format_Version":1,"plates":[],"nodes":[]}`)
	raw("format-trailing-content", "graph.format", "trips", "", `{"format_version":1,"plates":[],"nodes":[]} {}`)
	raw("format-not-an-object", "graph.format", "trips", "", `[1,2,3]`)
	raw("format-nesting", "graph.format", "trips", "", `{"format_version":1,"plates":[],"nodes":[],"layout":`+strings.Repeat(`{"a":`, 70)+`1`+strings.Repeat(`}`, 70)+`}`)
	trips("format-key-of-another-kind", "graph.format", "", regression, set("d1", "bounds", map[string]any{"lower": 0}))
	trips("format-wrong-json-type", "graph.format", "", regression, set("d1", "label", 5))

	// graph.too_big
	trips("too-big-nodes", "graph.too_big", "", regression, func(m map[string]any) {
		for i := 0; i < 192; i++ {
			addNodes(m, transformed(fmt.Sprintf("z%d", i), "", "1", nil))
		}
	})
	clean("too-big-200-nodes", "graph.too_big", regression, func(m map[string]any) {
		for i := 0; i < 191; i++ {
			addNodes(m, transformed(fmt.Sprintf("z%d", i), "", "1", nil))
		}
	})
	trips("too-big-plates", "graph.too_big", "", regression, func(m map[string]any) {
		for i := 0; i < 8; i++ {
			m["plates"] = append(m["plates"].([]any), map[string]any{"id": fmt.Sprintf("g%d", i), "kind": "group", "label": "G"})
		}
	})
	clean("too-big-8-plates", "graph.too_big", regression, func(m map[string]any) {
		for i := 0; i < 7; i++ {
			m["plates"] = append(m["plates"].([]any), map[string]any{"id": fmt.Sprintf("g%d", i), "kind": "group", "label": "G"})
		}
	})
	trips("too-big-expression-nodes", "graph.too_big", "", regression, func(m map[string]any) {
		for i := 0; i < 20; i++ {
			addNodes(m, transformed(fmt.Sprintf("z%d", i), "", cpMany(120, "1", "+"), nil))
		}
	})
	// max_program_vector_nodes (1,500; the regression's own: 8, its linear predictor 1 + 3 × 2 and its mean)
	trips("too-big-vector-nodes", "graph.too_big", "", regression, func(m map[string]any) {
		addNodes(m, vectorFill("area", 1501-8)...)
	})
	clean("too-big-vector-nodes-1500", "graph.too_big", regression, func(m map[string]any) {
		addNodes(m, vectorFill("area", 1500-8)...)
	})
	big("too-big-bytes", "graph.too_big", "trips", "", cpOn(regression, func(m map[string]any) {
		node(m, 0)["note"] = strings.Repeat("a", 300000)
	}))
	big("hostile-10000-nodes", "graph.too_big", "trips", "", cpOn(regression, func(m map[string]any) {
		for i := 0; i < 10000; i++ {
			addNodes(m, transformed(fmt.Sprintf("z%d", i), "", "1", nil))
		}
	}))
	big("hostile-1mb-label", "graph.too_big", "trips", "", cpOn(regression, func(m map[string]any) {
		node(m, 0)["label"] = strings.Repeat("é", 1<<19)
	}))

	// graph.id
	trips("id-malformed", "graph.id", "", regression, set("d1", "id", "Bad Id"))
	clean("id-well-formed", "graph.id", regression, set("p1", "id", "p1x"))
	trips("id-duplicate", "graph.id", "", example, set("n2", "id", "n1"))
	trips("id-rows-on-a-node", "graph.id", "", regression, set("d1", "id", "rows"))
	trips("id-reserved-suffix-lpdf", "graph.id", "", regression, set("d1", "id", "lpdf"))
	trips("id-reserved-suffix-lccdf", "graph.id", "", regression, set("d1", "id", "lccdf"))
	clean("id-near-reserved-suffix", "graph.id", regression, set("d1", "id", "lpdfs"))
	trips("id-confusable", "graph.id", "", regression, set("d1", "id", "dı"))

	// graph.name, name_taken, name_function
	trips("name-pattern", "graph.name", "d1", regression, set("d1", "name", "Bad Name"))
	clean("name-good", "graph.name", regression, set("d1", "name", "good_name"))
	trips("name-ends-in-double-underscore", "graph.name", "d1", regression, set("d1", "name", "price__"))
	trips("name-too-long", "graph.name", "d1", regression, set("d1", "name", strings.Repeat("a", 41)))
	trips("name-taken", "graph.name_taken", "d1", regression, set("d1", "name", "area"))
	clean("name-distinct", "graph.name_taken", regression, set("d1", "name", "cost"))
	trips("name-function", "graph.name_function", "d1", regression, set("d1", "name", "exp"))
	clean("name-near-function", "graph.name_function", regression, set("d1", "name", "expo"))
	clean("name-stan-reserved-accepted", "graph.name", regression, set("d1", "name", "model"))

	// graph.label, graph.note
	trips("label-empty", "graph.label", "d1", regression, set("d1", "label", ""))
	clean("label-other-script", "graph.label", regression, set("d1", "label", "Цена 価格"))
	trips("label-direction-control", "graph.label", "d1", regression, set("d1", "label", "a‮b"))
	trips("label-control-character", "graph.label", "d1", regression, set("d1", "label", "a\u0007b"))
	trips("label-too-long", "graph.label", "d1", regression, set("d1", "label", strings.Repeat("é", 201)))
	trips("note-too-long", "graph.note", "d1", regression, set("d1", "note", strings.Repeat("a", 2001)))
	clean("note-at-the-limit", "graph.note", regression, set("d1", "note", strings.Repeat("a", 2000)))
	trips("note-direction-control", "graph.note", "d1", regression, set("d1", "note", "a⁦b"))

	// graph.rows
	trips("rows-two", "graph.rows", "", regression, func(m map[string]any) {
		m["plates"] = append(m["plates"].([]any), map[string]any{"id": "r2", "kind": "rows", "label": "More"})
	})
	trips("rows-none", "graph.rows", "", regression, func(m map[string]any) { m["plates"] = []any{} })
	clean("rows-one", "graph.rows", regression, nil)

	// graph.plate_parent
	trips("plate-parent-is-rows", "graph.plate_parent", "", cpNested, func(m map[string]any) { plate(m, 2)["parent"] = "rows" })
	trips("plate-parent-cycle", "graph.plate_parent", "", cpNested, func(m map[string]any) { plate(m, 1)["parent"] = "p2" })
	trips("plate-parent-too-deep", "graph.plate_parent", "", cpNested, func(m map[string]any) {
		prev := "p2"
		for i := 0; i < 3; i++ {
			id := fmt.Sprintf("g%d", i)
			m["plates"] = append(m["plates"].([]any), map[string]any{"id": id, "kind": "group", "label": "G", "parent": prev, "key": "k" + id})
			addNodes(m, map[string]any{"id": "k" + id, "kind": "data", "label": "K", "plate": "rows", "type": "group_index", "target": id})
			prev = id
		}
	})
	clean("plate-parent-nested", "graph.plate_parent", cpNested, nil)

	// graph.plate_key
	trips("plate-key-missing-node", "graph.plate_key", "", logistic, func(m map[string]any) { plate(m, 1)["key"] = "zz" })
	trips("plate-key-wrong-target", "graph.plate_key", "", cpNested, func(m map[string]any) { cpNode(m, "d2")["target"] = "p1" })
	trips("plate-key-two-indices", "graph.plate_key", "", logistic, func(m map[string]any) {
		addNodes(m, map[string]any{"id": "d9", "kind": "data", "name": "school2", "label": "School again", "plate": "rows", "type": "group_index", "target": "p1"})
	})
	clean("plate-key-ok", "graph.plate_key", logistic, nil)

	// graph.series
	trips("series-time-not-time", "graph.series", "rows", ar2, func(m map[string]any) { plate(m, 0)["series"] = map[string]any{"time": "d2"} })
	trips("series-time-unknown", "graph.series", "rows", ar2, func(m map[string]any) { plate(m, 0)["series"] = map[string]any{"time": "zz"} })
	trips("series-by-rows", "graph.series", "rows", ar2, func(m map[string]any) { plate(m, 0)["series"] = map[string]any{"time": "d1", "by": "rows"} })
	clean("series-ok", "graph.series", ar2, nil)

	// graph.plate_empty
	trips("plate-empty", "graph.plate_empty", "p2", logistic, func(m map[string]any) {
		m["plates"] = append(m["plates"].([]any), map[string]any{"id": "p2", "kind": "group", "label": "Empty", "key": "d9"})
		addNodes(m, map[string]any{"id": "d9", "kind": "data", "name": "empty_key", "label": "Key", "plate": "rows", "type": "group_index", "target": "p2"})
	})
	clean("plate-empty-none", "graph.plate_empty", logistic, nil)

	// graph.node_plate
	trips("node-plate-parameter-on-rows", "graph.node_plate", "m1", regression, set("m1", "plate", "rows"))
	trips("node-plate-unknown", "graph.node_plate", "m1", regression, set("m1", "plate", "zz"))
	trips("node-plate-likelihood-elsewhere", "graph.node_plate", "l1", logistic, set("l1", "plate", "p1"))
	clean("node-plate-scalar", "graph.node_plate", regression, set("m1", "plate", nil))
	trips("node-plate-state-not-on-rows", "graph.node_plate", "x1", locallevel, func(m map[string]any) {
		m["plates"] = append(m["plates"].([]any), map[string]any{"id": "p9", "kind": "group", "label": "G", "key": "d9"})
		s := state("p9")
		s["id"] = "x1"
		addNodes(m, s, map[string]any{"id": "d9", "kind": "data", "label": "K", "plate": "rows", "type": "group_index", "target": "p9"})
		cpNode(m, "s1")["scale"] = expr("sigma_level + sigma_obs")
	})

	// graph.data_type
	trips("data-type-int-without-support", "graph.data_type", "d3", regression, func(m map[string]any) { delete(cpNode(m, "d3"), "support") })
	clean("data-type-int-with-support", "graph.data_type", regression, nil)
	trips("data-type-unknown-type", "graph.data_type", "d3", regression, set("d3", "type", "text"))
	trips("data-type-support-on-real", "graph.data_type", "d1", regression, set("d1", "support", "binary"))
	trips("data-type-time-on-group", "graph.data_type", "d6", cpNested, set("d6", "type", "time"))
	trips("data-type-target-on-real", "graph.data_type", "d1", regression, set("d1", "target", "p1"))

	// graph.standardize
	trips("standardize-observed", "graph.standardize", "d1", regression, set("d1", "standardize", true))
	clean("standardize-predictor", "graph.standardize", regression, nil)
	trips("standardize-int", "graph.standardize", "d3", regression, set("d3", "standardize", true))

	// graph.prior
	trips("prior-unknown-dist", "graph.prior", "m1", regression, func(m map[string]any) { cpNode(m, "m1")["prior"].(map[string]any)["dist"] = "bogus" })
	clean("prior-normal", "graph.prior", regression, nil)
	trips("prior-missing-arg", "graph.prior", "m1", regression, func(m map[string]any) {
		delete(cpNode(m, "m1")["prior"].(map[string]any)["args"].(map[string]any), "sigma")
	})
	trips("prior-unknown-arg", "graph.prior", "m1", regression, func(m map[string]any) {
		cpNode(m, "m1")["prior"].(map[string]any)["args"].(map[string]any)["nu"] = expr("1")
	})
	trips("prior-a-likelihood", "graph.prior", "m1", regression, func(m map[string]any) {
		cpNode(m, "m1")["prior"] = map[string]any{"dist": "poisson_log", "args": map[string]any{"alpha": expr("0")}}
	})
	trips("prior-missing", "graph.prior", "m1", regression, func(m map[string]any) { delete(cpNode(m, "m1"), "prior") })
	trips("prior-lkj-alone", "graph.prior", "m1", regression, func(m map[string]any) {
		cpNode(m, "m1")["prior"] = map[string]any{"dist": "lkj_corr_cholesky", "args": map[string]any{"eta": expr("2")}}
	})

	// graph.slot
	trips("slot-both", "graph.slot", "m1", regression, func(m map[string]any) {
		cpNode(m, "m1")["prior"].(map[string]any)["args"].(map[string]any)["mu"] = map[string]any{"expr": "0", "ref": "m2"}
	})
	clean("slot-ref", "graph.slot", regression, func(m map[string]any) {
		cpNode(m, "m1")["prior"].(map[string]any)["args"].(map[string]any)["mu"] = ref("m2")
	})
	trips("slot-unknown-ref", "graph.slot", "m1", regression, func(m map[string]any) {
		cpNode(m, "m1")["prior"].(map[string]any)["args"].(map[string]any)["mu"] = ref("zz")
	})
	trips("slot-neither", "graph.slot", "m1", regression, func(m map[string]any) {
		cpNode(m, "m1")["prior"].(map[string]any)["args"].(map[string]any)["mu"] = map[string]any{}
	})
	trips("slot-ref-not-a-number", "graph.slot", "t2", logistic, func(m map[string]any) {
		addNodes(m, map[string]any{"id": "t2", "kind": "transformed", "label": "T", "plate": "rows", "value": ref("d3")})
	})
	trips("slot-value-missing", "graph.slot", "t1", ar2, func(m map[string]any) { delete(cpNode(m, "t1"), "value") })

	// graph.slot_shape
	trips("slot-shape-component-mean-not-scalar", "graph.slot_shape", "e1.c1", logistic, func(m map[string]any) {
		addNodes(m, transformed("t2", "", "hours", "rows"))
		cpNode(m, "e1")["components"].([]any)[0].(map[string]any)["mean"] = ref("t2")
	})
	clean("slot-shape-component-mean-scalar", "graph.slot_shape", logistic, nil)
	trips("slot-shape-prior-on-a-wrong-plate", "graph.slot_shape", "m1", regression, func(m map[string]any) {
		addNodes(m, transformed("t2", "", "1", "rows"))
		cpNode(m, "m1")["prior"].(map[string]any)["args"].(map[string]any)["mu"] = ref("t2")
	})

	// graph.slot_interval, slot_maybe
	trips("slot-interval-negative-sigma", "graph.slot_interval", "m4", regression, func(m map[string]any) {
		cpNode(m, "m4")["prior"].(map[string]any)["args"].(map[string]any)["beta"] = expr("-1")
	})
	clean("slot-interval-positive-sigma", "graph.slot_interval", regression, nil)
	trips("slot-interval-uniform-order", "graph.slot_interval", "m1", regression, func(m map[string]any) {
		cpNode(m, "m1")["prior"] = map[string]any{"dist": "uniform", "args": map[string]any{"alpha": expr("2"), "beta": expr("1")}}
	})
	trips("slot-maybe-real-for-sigma", "graph.slot_maybe", "m2", regression, func(m map[string]any) {
		cpNode(m, "m2")["prior"].(map[string]any)["args"].(map[string]any)["sigma"] = expr("alpha")
	})
	clean("slot-maybe-exp-sigma", "graph.slot_maybe", regression, func(m map[string]any) {
		cpNode(m, "m2")["prior"].(map[string]any)["args"].(map[string]any)["sigma"] = expr("exp(alpha)")
	})

	// graph.prior_data
	trips("prior-data-direct", "graph.prior_data", "m2", cpNested, func(m map[string]any) {
		cpNode(m, "m2")["prior"].(map[string]any)["args"].(map[string]any)["mu"] = expr("mu + size")
	})
	trips("prior-data-through-two-transformed", "graph.prior_data", "m2", cpNested, func(m map[string]any) {
		addNodes(m, transformed("t3", "u3", "size", "p1"), transformed("t4", "u4", "u3 + 1", "p1"))
		cpNode(m, "m2")["prior"].(map[string]any)["args"].(map[string]any)["mu"] = expr("u4")
	})
	clean("prior-data-parameters-only", "graph.prior_data", cpNested, nil)
	clean("prior-data-lifted-index", "graph.prior_data", cpNested, func(m map[string]any) {})

	// graph.constant
	trips("constant-uniform-expression", "graph.constant", "m1", regression, func(m map[string]any) {
		cpNode(m, "m1")["prior"] = map[string]any{"dist": "uniform", "args": map[string]any{"alpha": expr("-1"), "beta": expr("1 + 1")}}
	})
	clean("constant-uniform-numbers", "graph.constant", regression, func(m map[string]any) {
		cpNode(m, "m1")["prior"] = map[string]any{"dist": "uniform", "args": map[string]any{"alpha": expr("-1"), "beta": expr("2")}}
	})
	trips("constant-eta-a-parameter", "graph.constant", "e1.correlation", logistic, func(m map[string]any) {
		cpNode(m, "e1")["correlation"].(map[string]any)["eta"] = expr("tau_a")
	})

	// graph.bounds
	trips("bounds-order", "graph.bounds", "m1", regression, set("m1", "bounds", map[string]any{"lower": 5, "upper": 1}))
	clean("bounds-ordered", "graph.bounds", regression, set("m1", "bounds", map[string]any{"lower": -5, "upper": 5}))
	trips("bounds-too-large", "graph.bounds", "m1", regression, set("m1", "bounds", map[string]any{"lower": -1e16}))
	trips("bounds-empty-range", "graph.bounds", "m4", regression, set("m4", "bounds", map[string]any{"upper": -1}))
	trips("bounds-a-point", "graph.bounds", "m1", regression, set("m1", "bounds", map[string]any{"lower": 1, "upper": 1}))
	trips("bounds-outside-the-support", "graph.bounds", "m4", regression, set("m4", "bounds", map[string]any{"upper": -2}))

	// graph.cycle
	trips("cycle-two-transformed", "graph.cycle", "t2", regression, func(m map[string]any) {
		addNodes(m, transformed("t2", "ta", "tb", "rows"), transformed("t3", "tb", "ta", "rows"))
	})
	clean("cycle-chain", "graph.cycle", regression, func(m map[string]any) {
		addNodes(m, transformed("t2", "ta", "tb", "rows"), transformed("t3", "tb", "alpha", "rows"))
	})
	trips("cycle-through-lag", "graph.cycle", "t1", ar2, func(m map[string]any) {
		cpNode(m, "t1")["value"] = expr("lag(mean_sales, 1)")
	})
	trips("cycle-self", "graph.cycle", "t1", ar2, func(m map[string]any) { cpNode(m, "t1")["value"] = expr("mean_sales + 1") })

	// graph.likelihood
	trips("likelihood-none", "graph.likelihood", "", regression, func(m map[string]any) { removeNode(m, "l1") })
	clean("likelihood-one", "graph.likelihood", regression, nil)
	trips("likelihood-observed-twice", "graph.likelihood", "l2", regression, func(m map[string]any) {
		addNodes(m, map[string]any{"id": "l2", "kind": "likelihood", "label": "Again", "observed": "d1", "dist": "normal",
			"args": map[string]any{"mu": expr("eta"), "sigma": expr("sigma")}})
	})
	trips("likelihood-observed-not-data", "graph.likelihood", "l1", regression, set("l1", "observed", "m1"))
	trips("likelihood-unknown-dist", "graph.likelihood", "l1", regression, set("l1", "dist", "bogus"))
	trips("likelihood-outcome-int-for-normal", "graph.likelihood", "l1", regression, set("l1", "observed", "d3"))
	trips("likelihood-outcome-real-for-poisson", "graph.likelihood", "l1", regression, func(m map[string]any) {
		cpNode(m, "l1")["dist"] = "poisson_log"
		cpNode(m, "l1")["args"] = map[string]any{"alpha": expr("eta")}
	})
	clean("likelihood-outcome-count-for-poisson", "graph.likelihood", regression, func(m map[string]any) {
		cpNode(m, "d1")["type"] = "int"
		cpNode(m, "d1")["support"] = "nonnegative"
		cpNode(m, "l1")["dist"] = "poisson_log"
		cpNode(m, "l1")["args"] = map[string]any{"alpha": expr("eta")}
		cpNode(m, "p1")["terms"] = []any{map[string]any{"data": "d2", "coef": expr("b_area")}}
		removeNode(m, "d3")
		removeNode(m, "m3")
	})
	trips("likelihood-missing-arg", "graph.likelihood", "l1", regression, func(m map[string]any) {
		delete(cpNode(m, "l1")["args"].(map[string]any), "sigma")
	})
	trips("likelihood-binomial-n-literal", "graph.likelihood", "l1", logistic, func(m map[string]any) {
		cpNode(m, "l1")["dist"] = "binomial_logit"
		cpNode(m, "l1")["args"] = map[string]any{"N": expr("10"), "alpha": expr("logit_p")}
	})

	// graph.self_predictor
	trips("self-predictor-direct", "graph.self_predictor", "l1", regression, func(m map[string]any) {
		cpNode(m, "l1")["args"].(map[string]any)["mu"] = expr("eta + price")
	})
	clean("self-predictor-lag", "graph.self_predictor", ar2, nil)
	trips("self-predictor-through-node", "graph.self_predictor", "l1", ar2, set("t1", "value", expr("c + phi1 * sales")))

	// graph.unused_data, unused_parameter
	trips("unused-data", "graph.unused_data", "d9", regression, func(m map[string]any) { addNodes(m, dataNode("d9", "extra", "real", "rows")) })
	clean("unused-data-none", "graph.unused_data", regression, nil)
	clean("unused-data-key-and-time-count", "graph.unused_data", cpNested, nil)
	trips("unused-parameter", "graph.unused_parameter", "m9", regression, func(m map[string]any) { addNodes(m, param("m9", "extra", nil)) })
	clean("unused-parameter-none", "graph.unused_parameter", regression, nil)
	clean("unused-parameter-without-likelihood", "graph.unused_parameter", regression, func(m map[string]any) { removeNode(m, "l1") })

	// graph.form_centred
	trips("form-centred-bounded", "graph.form_centred", "n6", example, set("n6", "bounds", map[string]any{"lower": -50}))
	clean("form-centred-centred-plate", "graph.form_centred", example, func(m map[string]any) {
		plate(m, 1)["form"] = "centred"
		cpNode(m, "n6")["bounds"] = map[string]any{"lower": -50}
	})
	trips("form-centred-not-location-scale", "graph.form_centred", "n6", example, func(m map[string]any) {
		cpNode(m, "n6")["prior"] = map[string]any{"dist": "exponential", "args": map[string]any{"beta": expr("tau")}}
	})
	trips("form-centred-state-init", "graph.form_centred", "s1", locallevel, func(m map[string]any) {
		cpNode(m, "s1")["init"] = map[string]any{"dist": "exponential", "args": map[string]any{"beta": expr("1")}}
	})

	// graph.effects
	trips("effects-one-component", "graph.effects", "e1", logistic, func(m map[string]any) {
		cpNode(m, "e1")["components"] = cpNode(m, "e1")["components"].([]any)[:1]
	})
	clean("effects-two-components", "graph.effects", logistic, nil)
	trips("effects-too-many", "graph.effects", "e1", logistic, func(m map[string]any) {
		cs := cpNode(m, "e1")["components"].([]any)
		for i := 0; i < 9; i++ {
			cs = append(cs, map[string]any{"id": fmt.Sprintf("c%d", i+3), "label": "C", "mean": expr("0"), "scale": expr("tau_a")})
		}
		cpNode(m, "e1")["components"] = cs
	})
	trips("effects-no-correlation", "graph.effects", "e1", logistic, func(m map[string]any) { delete(cpNode(m, "e1"), "correlation") })

	// graph.state
	trips("state-unknown-process", "graph.state", "s1", locallevel, set("s1", "process", "ar1"))
	clean("state-random-walk", "graph.state", locallevel, nil)
	trips("state-not-a-series", "graph.state", "s1", locallevel, func(m map[string]any) { delete(plate(m, 0), "series") })
	trips("state-init-missing", "graph.state", "s1", locallevel, func(m map[string]any) { delete(cpNode(m, "s1"), "init") })
	trips("state-init-not-a-prior", "graph.state", "s1", locallevel, func(m map[string]any) {
		cpNode(m, "s1")["init"] = map[string]any{"dist": "poisson_log", "args": map[string]any{"alpha": expr("0")}}
	})
	trips("state-bad-form", "graph.state", "s1", locallevel, set("s1", "form", "weird"))

	// graph.terms
	trips("terms-duplicate-data", "graph.terms", "p1", regression, func(m map[string]any) {
		cpNode(m, "p1")["terms"] = []any{map[string]any{"data": "d2", "coef": expr("b_area")}, map[string]any{"data": "d2", "coef": expr("b_rooms")}}
	})
	clean("terms-two-data", "graph.terms", regression, nil)
	trips("terms-data-not-data", "graph.terms", "p1", regression, func(m map[string]any) {
		cpNode(m, "p1")["terms"] = []any{map[string]any{"data": "m1", "coef": expr("b_area")}}
	})
	trips("terms-coef-not-a-parameter", "graph.terms", "p1", regression, func(m map[string]any) {
		cpNode(m, "p1")["terms"] = []any{map[string]any{"data": "d2", "coef": expr("2")}}
	})
	trips("terms-category-needs-levels", "graph.terms", "q1", cpNested, func(m map[string]any) {
		cpNode(m, "q1")["terms"] = []any{map[string]any{"data": "d3", "coef": expr("b_hours")}}
	})
	clean("terms-category-levels", "graph.terms", cpNested, func(m map[string]any) {
		cpNode(m, "q1")["terms"] = []any{map[string]any{"data": "d3", "coef": expr("b_region")}}
	})
	trips("terms-time-data", "graph.terms", "q1", cpNested, func(m map[string]any) {
		cpNode(m, "q1")["terms"] = []any{map[string]any{"data": "d4", "coef": expr("b_hours")}}
	})

	// expr.*: the expression's own errors, reported by the slot (a transformed node x1 holds the text)
	ex := func(code, tripName, tripText string, tripPlate any, cleanText string, cleanPlate any) {
		short := strings.TrimPrefix(code, "expr.")
		trips("expr-"+short+"-trips", code, "x1", cpNested, cpX(tripText, tripPlate))
		clean("expr-"+short+"-beside", code, cpNested, cpX(cleanText, cleanPlate))
		_ = tripName
	}
	ex("expr.arity", "", "exp(sigma, 2)", "rows", "exp(sigma)", "rows")
	ex("expr.call_empty", "", "exp()", "rows", "exp(sigma)", "rows")
	ex("expr.comment", "", "mu // 2", "rows", "mu / 2", "rows")
	ex("expr.control", "", "mu\u0001", "rows", "mu", "rows")
	ex("expr.empty", "", "   ", "rows", "mu", "rows")
	ex("expr.hash", "", "mu # 2", "rows", "mu", "rows")
	ex("expr.hint", "", "mu ** 2", "rows", "mu ^ 2", "rows")
	ex("expr.index_alone", "", "school", "rows", "mu_school[school]", "rows")
	ex("expr.index_context", "", "mu_district[district]", nil, "mu_district[district]", "rows")
	ex("expr.index_scalar", "", "mu[district]", "rows", "mu_district[district]", "rows")
	ex("expr.index_target", "", "mu_district[school]", "rows", "mu_district[district]", "rows")
	ex("expr.lag_argument", "", "lag(score)", "rows", "lag(score, 1)", "rows")
	ex("expr.lag_k", "", "lag(score, 0)", "rows", "lag(score, 1)", "rows")
	ex("expr.lag_not_series", "", "lag(mu_district, 1)", "p1", "lag(score, 1)", "rows")
	ex("expr.name_case", "", "Mu", "rows", "mu", "rows")
	ex("expr.name_long", "", strings.Repeat("a", 41), "rows", "mu", "rows")
	ex("expr.name_reserved", "", "mu__", "rows", "mu", "rows")
	ex("expr.needs_index", "", "mu_district", "rows", "mu_district[district]", "rows")
	ex("expr.non_ascii", "", "а + mu", "rows", "mu", "rows")
	ex("expr.not_function", "", "score(1)", "rows", "exp(mu)", "rows")
	ex("expr.not_index", "", "mu_district[mu]", "rows", "mu_district[district]", "rows")
	trips("expr-not_number-trips", "expr.not_number", "x1", logistic, cpX("omega", "rows"))
	clean("expr-not_number-beside", "expr.not_number", logistic, cpX("icpt[school]", "rows"))
	ex("expr.number_form", "", "2i", "rows", "2", "rows")
	ex("expr.number_range", "", "1e400", "rows", "1e15", "rows")
	ex("expr.number_underflow", "", "1e-400", "rows", "1e-300", "rows")
	ex("expr.symbol", "", "mu @ 2", "rows", "mu", "rows")
	ex("expr.text", "", `"a"`, "rows", "mu", "rows")
	ex("expr.time_value", "", "day", "rows", "score", "rows")
	ex("expr.too_big", "", cpMany(200, "mu", "+"), "rows", cpMany(100, "mu", "+"), "rows")
	ex("expr.too_deep", "", strings.Repeat("(", 40)+"mu"+strings.Repeat(")", 40), "rows", strings.Repeat("(", 10)+"mu"+strings.Repeat(")", 10), "rows")
	ex("expr.too_long", "", strings.Repeat("mu + ", 250)+"mu", "rows", "mu + mu", "rows")
	ex("expr.unclosed", "", "(mu + 1", "rows", "(mu + 1)", "rows")
	ex("expr.unexpected", "", "mu mu", "rows", "mu + mu", "rows")
	ex("expr.unknown_function", "", "print(mu)", "rows", "exp(mu)", "rows")
	ex("expr.unknown_name", "", "nobody", "rows", "mu", "rows")
	ex("expr.wrong_plate", "", "score", nil, "score", "rows")

	// hostile documents
	trips("hostile-cycle-through-transformed-and-lag", "graph.cycle", "t1", ar2, func(m map[string]any) {
		cpNode(m, "t1")["value"] = expr("c + lag(u1, 1)")
		addNodes(m, transformed("t2", "u1", "mean_sales", "rows"))
	})
	trips("hostile-prior-reaches-data-through-two-nodes", "graph.prior_data", "m1", regression, func(m map[string]any) {
		addNodes(m, transformed("t1", "u1", "price", "rows"), transformed("t2", "u2", "u1", "rows"))
		cpNode(m, "m1")["prior"].(map[string]any)["args"].(map[string]any)["mu"] = expr("u2")
	})
	trips("hostile-confusable-name", "graph.name", "d1", regression, set("d1", "name", "prіce"))
	trips("hostile-direction-control-label", "graph.label", "d1", regression, set("d1", "label", "Price‮"))
	return cs
}

func cpIndent(raw []byte) json.RawMessage {
	var b bytes.Buffer
	if err := json.Indent(&b, raw, "  ", "  "); err != nil {
		panic(err)
	}
	return b.Bytes()
}

// cpFile is the file a case is written as.
func cpFileOf(c corpusCase) corpusFile {
	data := c.Build()
	f := corpusFile{Name: c.Name, Rule: c.Rule, Expect: c.Expect, Node: c.Node, Findings: cpFindings(data)}
	switch {
	case !c.Stored:
		f.Generated = true
	case c.Raw:
		s := string(data)
		f.Raw = &s
	default:
		f.Input = cpIndent(data)
	}
	return f
}

func cpMarshal(f corpusFile) []byte {
	b, err := json.MarshalIndent(f, "", "  ")
	if err != nil {
		panic(err)
	}
	return append(b, '\n')
}

func TestCorpus(t *testing.T) {
	cases := corpusCases()
	dir := "testdata/corpus"
	if *updateCorpus {
		if err := os.MkdirAll(dir, 0o755); err != nil {
			t.Fatal(err)
		}
		old, _ := filepath.Glob(filepath.Join(dir, "*.json"))
		for _, p := range old {
			os.Remove(p)
		}
		for _, c := range cases {
			if err := os.WriteFile(filepath.Join(dir, c.Name+".json"), cpMarshal(cpFileOf(c)), 0o644); err != nil {
				t.Fatal(err)
			}
		}
	}
	seen := map[string]bool{}
	for _, c := range cases {
		if seen[c.Name] {
			t.Fatalf("two cases are called %s", c.Name)
		}
		seen[c.Name] = true
		t.Run(c.Name, func(t *testing.T) {
			f := cpFileOf(c)
			want := cpMarshal(f)
			got, err := os.ReadFile(filepath.Join(dir, c.Name+".json"))
			if err != nil {
				t.Fatalf("no file for the case (go test -update-corpus): %v", err)
			}
			if !bytes.Equal(got, want) {
				t.Errorf("the committed file differs from what the case and the validator give now (go test -update-corpus):\n%s\n---\n%s", got, want)
			}
			switch c.Expect {
			case "trips":
				if !hasCode(f.Findings, c.Rule, c.Node) {
					t.Errorf("want %s on %q, got %s", c.Rule, c.Node, codes(f.Findings))
				}
			case "clean":
				if hasCode(f.Findings, c.Rule, "") {
					t.Errorf("unexpected %s: %s", c.Rule, codes(f.Findings))
				}
			case "valid":
				if len(f.Findings) != 0 {
					t.Errorf("want no finding, got %s", codes(f.Findings))
				}
			default:
				t.Fatalf("expect %q", c.Expect)
			}
			// a document the decoder takes builds a model without a panic, valid when it has no error
			if g, _ := Decode(c.Build()); g != nil {
				m := BuildModel(g)
				if m == nil {
					t.Fatal("BuildModel returned nil")
				}
				if c.Expect == "valid" && !m.Valid() {
					t.Errorf("the model of a valid case is not valid: %s", codes(m.Findings))
				}
			}
		})
	}
	files, _ := filepath.Glob(filepath.Join(dir, "*.json"))
	for _, p := range files {
		if !seen[strings.TrimSuffix(filepath.Base(p), ".json")] {
			t.Errorf("%s is no case of the table", p)
		}
	}
}

// Every rule of «Validation» and every expr.* code has a case that trips it and one beside it that
// does not.
func TestCorpusCoversEveryRule(t *testing.T) {
	rules := []string{
		"graph.format", "graph.too_big", "graph.id", "graph.name", "graph.name_taken", "graph.name_function",
		"graph.label", "graph.note", "graph.rows", "graph.plate_parent", "graph.plate_key", "graph.series",
		"graph.plate_empty", "graph.node_plate", "graph.data_type", "graph.standardize", "graph.prior",
		"graph.slot", "graph.slot_shape", "graph.slot_interval", "graph.slot_maybe", "graph.prior_data",
		"graph.constant", "graph.bounds", "graph.cycle", "graph.likelihood", "graph.self_predictor",
		"graph.unused_data", "graph.unused_parameter", "graph.form_centred", "graph.effects", "graph.state",
		"graph.terms",
	}
	for _, c := range []string{"arity", "call_empty", "comment", "control", "empty", "hash", "hint", "index_alone",
		"index_context", "index_scalar", "index_target", "lag_argument", "lag_k", "lag_not_series", "name_case",
		"name_long", "name_reserved", "needs_index", "non_ascii", "not_function", "not_index", "not_number",
		"number_form", "number_range", "number_underflow", "symbol", "text", "time_value", "too_big", "too_deep",
		"too_long", "unclosed", "unexpected", "unknown_function", "unknown_name", "wrong_plate"} {
		rules = append(rules, "expr."+c)
	}
	tr, cl := map[string]int{}, map[string]int{}
	fam := 0
	for _, c := range corpusCases() {
		switch c.Expect {
		case "trips":
			tr[c.Rule]++
		case "clean":
			cl[c.Rule]++
		case "valid":
			fam++
		}
	}
	var missing []string
	for _, r := range rules {
		if tr[r] == 0 || cl[r] == 0 {
			missing = append(missing, fmt.Sprintf("%s (trips %d, beside %d)", r, tr[r], cl[r]))
		}
	}
	sort.Strings(missing)
	if len(missing) > 0 {
		t.Errorf("rules without a pair of cases:\n%s", strings.Join(missing, "\n"))
	}
	if fam < 4 {
		t.Errorf("the four families are not all there: %d", fam)
	}
}

// corpusInputs reads the stored inputs of the corpus (for the properties and the fuzzer's seeds).
func corpusInputs(tb testing.TB) map[string][]byte {
	tb.Helper()
	files, _ := filepath.Glob("testdata/corpus/*.json")
	out := map[string][]byte{}
	for _, p := range files {
		b, err := os.ReadFile(p)
		if err != nil {
			tb.Fatal(err)
		}
		var f corpusFile
		if err := json.Unmarshal(b, &f); err != nil {
			tb.Fatal(err)
		}
		switch {
		case f.Raw != nil:
			out[f.Name] = []byte(*f.Raw)
		case len(f.Input) > 0:
			out[f.Name] = []byte(f.Input)
		}
	}
	return out
}
