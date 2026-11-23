package bayesgraph

import (
	"bytes"
	"encoding/json"
	"fmt"
	"math"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"testing"
)

// The mirror corpus: the documents the TypeScript mirror (frontend/composables/bayesGraph.ts) is
// tested on, with what the Go validator says of each (findings, and for a graph that decodes, its
// order, parameters, edges, scale pairs and lags). frontend/tests/bayes-graph.test.mjs reads
// testdata/mirror/cases.json and requires the mirror to give the same, in the same order.
//
// The cases are the four families, the spec's example and a richer hierarchy, an edit for each code
// and reason of the rules, and a deterministic mirrorSweep of single edits of those graphs kept when they
// bring a finding (code, slot, reason) not seen yet. go test fails when the committed file differs
// from what the cases and the validator give now. Regenerate with:
//
//	BAYES_MIRROR_WRITE=1 go test ./internal/bayesgraph -run TestMirrorCorpus

const mirrorFile = "testdata/mirror/cases.json"

// mnum is a number that may be infinite: written as a string then.
type mnum float64

func (n mnum) MarshalJSON() ([]byte, error) {
	switch {
	case math.IsInf(float64(n), 1):
		return []byte(`"Infinity"`), nil
	case math.IsInf(float64(n), -1):
		return []byte(`"-Infinity"`), nil
	}
	return json.Marshal(float64(n))
}

type mirrorInterval struct {
	Lo, Hi         mnum
	LoOpen, HiOpen bool
}

func (i mirrorInterval) MarshalJSON() ([]byte, error) {
	return json.Marshal([]any{i.Lo, i.Hi, i.LoOpen, i.HiOpen})
}

type mirrorParam struct {
	Dist              string         `json:"dist"`
	Support           mirrorInterval `json:"support"`
	Range             mirrorInterval `json:"range"`
	Truncated         bool           `json:"truncated"`
	TruncateInProgram bool           `json:"truncate_in_program"`
	Role              string         `json:"role"`
	NonCentred        bool           `json:"non_centred"`
}

type mirrorModel struct {
	Order  []string               `json:"order"`
	Params map[string]mirrorParam `json:"params"`
	Edges  [][5]string            `json:"edges"`
	Pairs  [][4]any               `json:"pairs"`
	Lags   []int                  `json:"lags"`
}

type mirrorCase struct {
	ID       string       `json:"id"`
	Input    string       `json:"input"`
	Decoded  bool         `json:"decoded"`
	Findings []Finding    `json:"findings"`
	Model    *mirrorModel `json:"model,omitempty"`
}

func mirrorOf(id, input string) mirrorCase {
	c := mirrorCase{ID: id, Input: input, Findings: []Finding{}}
	g, fs := Decode([]byte(input))
	if g == nil {
		c.Findings = fs
		return c
	}
	c.Decoded = true
	m := BuildModel(g)
	if m.Findings != nil {
		c.Findings = m.Findings
	}
	mm := &mirrorModel{Order: m.Order, Params: map[string]mirrorParam{}, Edges: [][5]string{}, Pairs: [][4]any{}, Lags: m.Lags}
	if mm.Order == nil {
		mm.Order = []string{}
	}
	if mm.Lags == nil {
		mm.Lags = []int{}
	}
	for id, p := range m.Params {
		mm.Params[id] = mirrorParam{Dist: p.Dist,
			Support:   mirrorInterval{mnum(p.Support.Lo), mnum(p.Support.Hi), p.Support.LoOpen, p.Support.HiOpen},
			Range:     mirrorInterval{mnum(p.Range.Lo), mnum(p.Range.Hi), p.Range.LoOpen, p.Range.HiOpen},
			Truncated: p.Truncated, TruncateInProgram: p.TruncateInProgram, Role: p.Role, NonCentred: p.NonCentred}
	}
	for _, e := range m.Edges {
		mm.Edges = append(mm.Edges, [5]string{e.From, e.To, e.Slot.Node, e.Slot.Slot, e.Kind})
	}
	for _, p := range m.Pairs {
		mm.Pairs = append(mm.Pairs, [4]any{p.Node, p.Term, p.Data, p.Intercept})
	}
	c.Model = mm
	return c
}

// richGraph is a hierarchy of districts and schools with a category predictor, a lifted index, a
// uniform and a beta prior, a bounded parameter and a binomial likelihood.
const richGraph = `{
  "format_version": 1,
  "plates": [
    {"id": "rows", "kind": "rows", "label": "Pupils"},
    {"id": "p1", "kind": "group", "label": "District", "key": "d4"},
    {"id": "p2", "kind": "group", "label": "School", "parent": "p1", "key": "d3", "form": "centred"}
  ],
  "nodes": [
    {"id": "d1", "kind": "data", "name": "score", "label": "Score", "plate": "rows", "type": "real"},
    {"id": "d2", "kind": "data", "name": "hours", "label": "Hours", "plate": "rows", "type": "real", "standardize": true},
    {"id": "d3", "kind": "data", "name": "school", "label": "School", "plate": "rows", "type": "group_index", "target": "p2"},
    {"id": "d4", "kind": "data", "name": "district", "label": "District", "plate": "rows", "type": "group_index", "target": "p1"},
    {"id": "d5", "kind": "data", "name": "track", "label": "Track", "plate": "rows", "type": "category"},
    {"id": "d6", "kind": "data", "name": "passed", "label": "Passed", "plate": "rows", "type": "int", "support": "nonnegative"},
    {"id": "d7", "kind": "data", "name": "trials", "label": "Trials", "plate": "rows", "type": "int", "support": "nonnegative"},
    {"id": "m1", "kind": "parameter", "name": "mu", "label": "Mean", "prior": {"dist": "normal", "args": {"mu": {"expr": "0"}, "sigma": {"expr": "10"}}}},
    {"id": "m2", "kind": "parameter", "name": "tau_d", "label": "District spread", "prior": {"dist": "normal", "args": {"mu": {"expr": "0"}, "sigma": {"expr": "5"}}}, "bounds": {"lower": 0}},
    {"id": "m3", "kind": "parameter", "name": "tau_s", "label": "School spread", "prior": {"dist": "exponential", "args": {"beta": {"expr": "tau_d"}}}},
    {"id": "m4", "kind": "parameter", "name": "mu_d", "label": "District mean", "plate": "p1", "prior": {"dist": "normal", "args": {"mu": {"expr": "mu"}, "sigma": {"expr": "tau_d"}}}},
    {"id": "m5", "kind": "parameter", "name": "a_s", "label": "School mean", "plate": "p2", "prior": {"dist": "normal", "args": {"mu": {"expr": "mu_d[district]"}, "sigma": {"expr": "tau_s"}}}},
    {"id": "m6", "kind": "parameter", "name": "b_track", "label": "Track effect", "plate": "levels:d5", "prior": {"dist": "student_t", "args": {"nu": {"expr": "3"}, "mu": {"expr": "0"}, "sigma": {"expr": "1"}}}},
    {"id": "m7", "kind": "parameter", "name": "b_hours", "label": "Hours effect", "prior": {"dist": "normal", "args": {"mu": {"expr": "0"}, "sigma": {"expr": "1"}}}},
    {"id": "m8", "kind": "parameter", "name": "sigma", "label": "Noise", "prior": {"dist": "exponential", "args": {"beta": {"expr": "1"}}}},
    {"id": "m9", "kind": "parameter", "name": "u", "label": "A share", "prior": {"dist": "uniform", "args": {"alpha": {"expr": "0"}, "beta": {"expr": "1"}}}},
    {"id": "m10", "kind": "parameter", "name": "pb", "label": "A probability", "prior": {"dist": "beta", "args": {"alpha": {"expr": "2"}, "beta": {"expr": "2"}}}, "bounds": {"upper": 0.5}},
    {"id": "p3", "kind": "linear_predictor", "name": "eta", "label": "Expected score", "plate": "rows", "intercept": {"expr": "a_s[school]"},
     "terms": [{"data": "d2", "coef": {"ref": "m7"}}, {"data": "d5", "coef": {"expr": "b_track"}}]},
    {"id": "t1", "kind": "transformed", "name": "logit_p", "label": "Log-odds", "plate": "rows", "value": {"expr": "mu_d[district] + u * pb"}},
    {"id": "l1", "kind": "likelihood", "label": "Scores", "observed": "d1", "dist": "normal", "args": {"mu": {"expr": "eta"}, "sigma": {"expr": "sigma"}}},
    {"id": "l2", "kind": "likelihood", "label": "Passes", "observed": "d6", "dist": "binomial_logit", "args": {"N": {"expr": "trials"}, "alpha": {"expr": "logit_p"}}}
  ]
}`

// effectsGraph is the logistic family with the correlated effects on a centred plate and a bounded
// location-scale parameter, to reach graph.form_centred and the states.
const stateGraph = `{
  "format_version": 1,
  "plates": [{"id": "rows", "kind": "rows", "label": "Days", "series": {"time": "d1", "by": null}}],
  "nodes": [
    {"id": "d1", "kind": "data", "name": "day", "label": "Day", "plate": "rows", "type": "time"},
    {"id": "d2", "kind": "data", "name": "flow", "label": "Flow", "plate": "rows", "type": "real"},
    {"id": "m1", "kind": "parameter", "name": "s1", "label": "Drift", "prior": {"dist": "exponential", "args": {"beta": {"expr": "1"}}}},
    {"id": "m2", "kind": "parameter", "name": "s2", "label": "Noise", "prior": {"dist": "gamma", "args": {"alpha": {"expr": "2"}, "beta": {"expr": "s1 + 1"}}}},
    {"id": "s1", "kind": "state", "name": "level", "label": "Level", "plate": "rows", "process": "random_walk",
     "init": {"dist": "gamma", "args": {"alpha": {"expr": "2"}, "beta": {"expr": "1"}}}, "scale": {"expr": "s1"}},
    {"id": "t1", "kind": "transformed", "name": "trend", "label": "Trend", "plate": "rows", "value": {"expr": "level + lag(level, 1) - lag(flow, 2)"}},
    {"id": "l1", "kind": "likelihood", "label": "Flow", "observed": "d2", "dist": "normal", "args": {"mu": {"expr": "trend"}, "sigma": {"expr": "s2"}}}
  ]
}`

func mirrorBases() map[string]string {
	return map[string]string{
		"regression": regression, "logistic": logistic, "ar2": ar2, "locallevel": locallevel,
		"example": example, "rich": richGraph, "state": stateGraph,
	}
}

type mtree = map[string]any

func mirrorParse(t *testing.T, s string) mtree {
	t.Helper()
	var m mtree
	if err := json.Unmarshal([]byte(s), &m); err != nil {
		t.Fatal(err)
	}
	return m
}

func mirrorDump(t *testing.T, m mtree) string {
	t.Helper()
	var buf bytes.Buffer
	enc := json.NewEncoder(&buf)
	enc.SetEscapeHTML(false)
	if err := enc.Encode(m); err != nil {
		t.Fatal(err)
	}
	return strings.TrimRight(buf.String(), "\n")
}

func mirrorNode(m mtree, id string) mtree {
	for _, n := range m["nodes"].([]any) {
		if o := n.(mtree); o["id"] == id {
			return o
		}
	}
	panic("no node " + id)
}

func mirrorPlate(m mtree, id string) mtree {
	for _, n := range m["plates"].([]any) {
		if o := n.(mtree); o["id"] == id {
			return o
		}
	}
	panic("no plate " + id)
}

func mirrorDrop(m mtree, id string) {
	var keep []any
	for _, n := range m["nodes"].([]any) {
		if n.(mtree)["id"] != id {
			keep = append(keep, n)
		}
	}
	m["nodes"] = keep
}

func mirrorAddNode(m mtree, n mtree)  { m["nodes"] = append(m["nodes"].([]any), n) }
func mirrorAddPlate(m mtree, p mtree) { m["plates"] = append(m["plates"].([]any), p) }

func mex(s string) mtree { return mtree{"expr": s} }
func mrf(s string) mtree { return mtree{"ref": s} }

// mirrorHand returns the edits: an id, the base and what to do to it.
func mirrorHand() []struct {
	id, base string
	edit     func(m mtree)
} {
	type c = struct {
		id, base string
		edit     func(m mtree)
	}
	n := func(m mtree, id string) mtree { return mirrorNode(m, id) }
	cases := []c{
		// ids, names, labels, notes
		{"id-malformed", "example", func(m mtree) { n(m, "n4")["id"] = "N4" }},
		{"id-duplicate", "example", func(m mtree) { n(m, "n5")["id"] = "n4" }},
		{"id-rows-node", "example", func(m mtree) { n(m, "n4")["id"] = "rows" }},
		{"id-reserved-suffix-node", "example", func(m mtree) { n(m, "n4")["id"] = "lpdf" }},
		{"id-reserved-suffix-plate", "example", func(m mtree) { mirrorPlate(m, "rows")["id"] = "r1"; n(m, "n4")["id"] = "cdf" }},
		{"id-reserved-suffix-component", "logistic", func(m mtree) { n(m, "e1")["components"].([]any)[0].(mtree)["id"] = "lupmf" }},
		{"id-rows-plate", "example", func(m mtree) { mirrorPlate(m, "rows")["id"] = "r1" }},
		{"id-component-malformed", "logistic", func(m mtree) { n(m, "e1")["components"].([]any)[0].(mtree)["id"] = "C 1" }},
		{"id-component-duplicate", "logistic", func(m mtree) { n(m, "e1")["components"].([]any)[1].(mtree)["id"] = "c1" }},
		{"name-empty", "example", func(m mtree) { n(m, "n4")["name"] = "" }},
		{"name-pattern", "example", func(m mtree) { n(m, "n4")["name"] = "Mu" }},
		{"name-too-long", "example", func(m mtree) { n(m, "n4")["name"] = strings.Repeat("a", 41) }},
		{"name-double-underscore", "example", func(m mtree) { n(m, "n4")["name"] = "mu__" }},
		{"name-function", "example", func(m mtree) { n(m, "n4")["name"] = "exp" }},
		{"name-taken", "example", func(m mtree) { n(m, "n4")["name"] = "tau" }},
		{"name-taken-component", "logistic", func(m mtree) { n(m, "e1")["components"].([]any)[0].(mtree)["name"] = "omega" }},
		{"name-component-bad", "logistic", func(m mtree) { n(m, "e1")["correlation"].(mtree)["name"] = "Bad Name" }},
		{"label-empty", "example", func(m mtree) { n(m, "n4")["label"] = "" }},
		{"label-too-long", "example", func(m mtree) { n(m, "n4")["label"] = strings.Repeat("é", 201) }},
		{"label-control", "example", func(m mtree) { n(m, "n4")["label"] = "a\u0007b" }},
		{"label-c1", "example", func(m mtree) { n(m, "n4")["label"] = "a\u0085b" }},
		{"label-direction", "example", func(m mtree) { n(m, "n4")["label"] = "a\u202eb" }},
		{"label-bom", "example", func(m mtree) { n(m, "n4")["label"] = "a\ufeffb" }},
		{"label-newline", "example", func(m mtree) { n(m, "n4")["label"] = "a\nb" }},
		{"label-other-script", "example", func(m mtree) { n(m, "n4")["label"] = "平均 μ 😀" }},
		{"label-component", "logistic", func(m mtree) { n(m, "e1")["components"].([]any)[0].(mtree)["label"] = "" }},
		{"label-correlation", "logistic", func(m mtree) { n(m, "e1")["correlation"].(mtree)["label"] = "" }},
		{"label-plate", "example", func(m mtree) { mirrorPlate(m, "p1")["label"] = "" }},
		{"note-ok", "example", func(m mtree) { n(m, "n4")["note"] = "a\nb\tc" }},
		{"note-too-long", "example", func(m mtree) { n(m, "n4")["note"] = strings.Repeat("x", 2001) }},
		{"note-control", "example", func(m mtree) { n(m, "n4")["note"] = "a\u0001" }},
		{"note-direction", "example", func(m mtree) { mirrorPlate(m, "p1")["note"] = "a\u2066" }},
		// plates
		{"rows-none", "example", func(m mtree) { mirrorPlate(m, "rows")["kind"] = "group"; mirrorPlate(m, "rows")["id"] = "p9" }},
		{"rows-two", "example", func(m mtree) { mirrorAddPlate(m, mtree{"id": "r2", "kind": "rows", "label": "More"}) }},
		{"plate-unknown-kind", "example", func(m mtree) { mirrorPlate(m, "p1")["kind"] = "box" }},
		{"plate-bad-form", "example", func(m mtree) { mirrorPlate(m, "p1")["form"] = "sideways" }},
		{"plate-parent-unknown", "example", func(m mtree) { mirrorPlate(m, "p1")["parent"] = "zz" }},
		{"plate-parent-cycle", "rich", func(m mtree) { mirrorPlate(m, "p1")["parent"] = "p2" }},
		{"plate-parent-self", "rich", func(m mtree) { mirrorPlate(m, "p2")["parent"] = "p2" }},
		{"plate-parent-rows", "rich", func(m mtree) { mirrorPlate(m, "p2")["parent"] = "rows" }},
		{"plate-too-deep", "rich", func(m mtree) {
			prev := "p2"
			for i := 3; i <= 6; i++ {
				id := fmt.Sprintf("q%d", i)
				kid := fmt.Sprintf("dk%d", i)
				mirrorAddNode(m, mtree{"id": kid, "kind": "data", "label": "K", "plate": "rows", "type": "group_index", "target": id})
				mirrorAddPlate(m, mtree{"id": id, "kind": "group", "label": "Level", "parent": prev, "key": kid})
				mirrorAddNode(m, mtree{"id": "z" + id, "kind": "parameter", "label": "Z", "plate": id, "prior": mtree{"dist": "normal", "args": mtree{"mu": mex("0"), "sigma": mex("1")}}})
				prev = id
			}
		}},
		{"plate-key-missing", "example", func(m mtree) { delete(mirrorPlate(m, "p1"), "key") }},
		{"plate-key-unknown", "example", func(m mtree) { mirrorPlate(m, "p1")["key"] = "zz" }},
		{"plate-key-not-index", "example", func(m mtree) { mirrorPlate(m, "p1")["key"] = "n2" }},
		{"plate-key-other-target", "rich", func(m mtree) { mirrorPlate(m, "p1")["key"] = "d3" }},
		{"plate-key-not-on-rows", "example", func(m mtree) { n(m, "n3")["plate"] = "p1" }},
		{"plate-key-several", "example", func(m mtree) {
			mirrorAddNode(m, mtree{"id": "n10", "kind": "data", "label": "Again", "plate": "rows", "type": "group_index", "target": "p1"})
		}},
		{"plate-key-target-not-group", "example", func(m mtree) { n(m, "n3")["target"] = "rows" }},
		{"plate-empty", "example", func(m mtree) { n(m, "n6")["plate"] = nil; n(m, "n9")["args"].(mtree)["mu"] = mex("beta") }},
		{"series-time-unknown", "ar2", func(m mtree) { mirrorPlate(m, "rows")["series"].(mtree)["time"] = "zz" }},
		{"series-time-not-time", "ar2", func(m mtree) { mirrorPlate(m, "rows")["series"].(mtree)["time"] = "d2" }},
		{"series-by-unknown", "ar2", func(m mtree) { mirrorPlate(m, "rows")["series"].(mtree)["by"] = "zz" }},
		{"series-by-group", "rich", func(m mtree) { mirrorPlate(m, "rows")["series"] = mtree{"time": "d1", "by": "p1"} }},
		// nodes and data
		{"node-plate-unknown", "example", func(m mtree) { n(m, "n4")["plate"] = "zz" }},
		{"node-plate-param-on-rows", "example", func(m mtree) { n(m, "n4")["plate"] = "rows" }},
		{"node-plate-state-group", "example", func(m mtree) {
			mirrorAddNode(m, mtree{"id": "s1", "kind": "state", "label": "S", "plate": "p1", "process": "random_walk", "form": "centred", "init": mtree{"dist": "normal", "args": mtree{"mu": mex("0"), "sigma": mex("1")}}, "scale": mex("1")})
		}},
		{"node-plate-likelihood-other", "example", func(m mtree) { n(m, "n9")["plate"] = "p1" }},
		{"data-unknown-type", "example", func(m mtree) { n(m, "n1")["type"] = "float" }},
		{"data-int-no-support", "example", func(m mtree) { n(m, "n1")["type"] = "int" }},
		{"data-unknown-support", "example", func(m mtree) { n(m, "n1")["type"] = "int"; n(m, "n1")["support"] = "odd" }},
		{"data-support-on-real", "example", func(m mtree) { n(m, "n1")["support"] = "binary" }},
		{"data-target-on-real", "example", func(m mtree) { n(m, "n1")["target"] = "p1" }},
		{"data-time-on-group", "example", func(m mtree) {
			n(m, "n2")["type"] = "time"
			n(m, "n2")["plate"] = "p1"
			delete(n(m, "n2"), "standardize")
		}},
		{"standardize-int", "regression", func(m mtree) { n(m, "d3")["standardize"] = true }},
		{"standardize-observed", "example", func(m mtree) { n(m, "n1")["standardize"] = true }},
		// priors, bounds
		{"prior-missing", "example", func(m mtree) { delete(n(m, "n4"), "prior") }},
		{"prior-unknown-dist", "example", func(m mtree) { n(m, "n4")["prior"].(mtree)["dist"] = "weird" }},
		{"prior-lkj", "example", func(m mtree) { n(m, "n4")["prior"].(mtree)["dist"] = "lkj_corr_cholesky" }},
		{"prior-likelihood-dist", "example", func(m mtree) { n(m, "n4")["prior"].(mtree)["dist"] = "poisson_log" }},
		{"prior-internal", "example", func(m mtree) { n(m, "n4")["prior"].(mtree)["dist"] = "std_normal" }},
		{"prior-missing-arg", "example", func(m mtree) { delete(n(m, "n4")["prior"].(mtree)["args"].(mtree), "sigma") }},
		{"prior-unknown-arg", "example", func(m mtree) { n(m, "n4")["prior"].(mtree)["args"].(mtree)["zeta"] = mex("1") }},
		{"bounds-order", "example", func(m mtree) { n(m, "n5")["bounds"] = mtree{"lower": 3.0, "upper": 1.0} }},
		{"bounds-equal", "example", func(m mtree) { n(m, "n5")["bounds"] = mtree{"lower": 1.0, "upper": 1.0} }},
		{"bounds-too-large", "example", func(m mtree) { n(m, "n5")["bounds"] = mtree{"lower": 2e15} }},
		{"bounds-empty-range", "rich", func(m mtree) { n(m, "m8")["bounds"] = mtree{"upper": -1.0} }},
		{"bounds-point-range", "rich", func(m mtree) { n(m, "m10")["bounds"] = mtree{"lower": 1.0} }},
		{"bounds-not-truncatable", "rich", func(m mtree) { n(m, "m6")["bounds"] = mtree{"lower": 0.0} }},
		{"bounds-truncate-in-program", "rich", func(m mtree) { n(m, "m3")["bounds"] = mtree{"upper": 3.0} }},
		{"bounds-both", "example", func(m mtree) { n(m, "n4")["bounds"] = mtree{"lower": -2.0, "upper": 2.5} }},
		{"bounds-empty-object", "example", func(m mtree) { n(m, "n4")["bounds"] = mtree{} }},
		{"uniform-unordered", "rich", func(m mtree) { n(m, "m9")["prior"].(mtree)["args"].(mtree)["alpha"] = mex("2") }},
		{"uniform-expression", "rich", func(m mtree) { n(m, "m9")["prior"].(mtree)["args"].(mtree)["beta"] = mex("1 + 1") }},
		{"uniform-negated", "rich", func(m mtree) { n(m, "m9")["prior"].(mtree)["args"].(mtree)["alpha"] = mex("--1") }},
		{"uniform-param-bound", "rich", func(m mtree) { n(m, "m9")["prior"].(mtree)["args"].(mtree)["beta"] = mex("tau_d") }},
		{"uniform-bounds", "rich", func(m mtree) { n(m, "m9")["bounds"] = mtree{"lower": 0.25} }},
		{"uniform-bounds-nonconst", "rich", func(m mtree) {
			n(m, "m9")["bounds"] = mtree{"lower": 0.25}
			n(m, "m9")["prior"].(mtree)["args"].(mtree)["beta"] = mex("1")
		}},
		// slots
		{"slot-missing-value", "logistic", func(m mtree) { delete(n(m, "t1"), "value") }},
		{"slot-empty", "example", func(m mtree) { n(m, "n4")["prior"].(mtree)["args"].(mtree)["mu"] = mtree{} }},
		{"slot-both", "example", func(m mtree) { n(m, "n4")["prior"].(mtree)["args"].(mtree)["mu"] = mtree{"expr": "0", "ref": "n7"} }},
		{"slot-unknown-ref", "example", func(m mtree) { n(m, "n4")["prior"].(mtree)["args"].(mtree)["mu"] = mrf("zz") }},
		{"slot-ref-not-number", "logistic", func(m mtree) { n(m, "m1")["prior"].(mtree)["args"].(mtree)["mu"] = mrf("d3") }},
		{"slot-ref-correlation", "logistic", func(m mtree) { n(m, "m1")["prior"].(mtree)["args"].(mtree)["mu"] = mrf("e1.correlation") }},
		{"slot-ref-component", "logistic", func(m mtree) { n(m, "t1")["value"] = mrf("e1.c1") }},
		{"slot-ref-plate", "rich", func(m mtree) { n(m, "m1")["prior"].(mtree)["args"].(mtree)["mu"] = mrf("m4") }},
		{"slot-ref-to-plate", "example", func(m mtree) { n(m, "n4")["prior"].(mtree)["args"].(mtree)["mu"] = mrf("p1") }},
		{"shape-scalar-state-scale", "rich", func(m mtree) {
			mirrorAddNode(m, mtree{"id": "s9", "kind": "state", "label": "S", "plate": "rows", "process": "random_walk", "form": "centred", "init": mtree{"dist": "normal", "args": mtree{"mu": mex("0"), "sigma": mex("1")}}, "scale": mex("mu_d[district]")})
		}},
		{"shape-context", "rich", func(m mtree) { n(m, "m1")["prior"].(mtree)["args"].(mtree)["mu"] = mex("a_s[school]") }},
		{"shape-component-mean", "logistic", func(m mtree) { n(m, "e1")["components"].([]any)[0].(mtree)["mean"] = mex("slope") }},
		{"interval-negative-sigma", "example", func(m mtree) { n(m, "n4")["prior"].(mtree)["args"].(mtree)["sigma"] = mex("-1") }},
		{"interval-zero-sigma", "example", func(m mtree) { n(m, "n4")["prior"].(mtree)["args"].(mtree)["sigma"] = mex("0") }},
		{"interval-maybe-sigma", "example", func(m mtree) { n(m, "n4")["prior"].(mtree)["args"].(mtree)["sigma"] = mex("beta") }},
		{"interval-exp-sigma", "example", func(m mtree) { n(m, "n4")["prior"].(mtree)["args"].(mtree)["sigma"] = mex("exp(beta)") }},
		{"interval-log-negative", "example", func(m mtree) { n(m, "n4")["prior"].(mtree)["args"].(mtree)["mu"] = mex("log(-2)") }},
		{"interval-log-maybe", "example", func(m mtree) {
			n(m, "n4")["prior"].(mtree)["args"].(mtree)["mu"] = mex("log(beta) + log(beta) + sqrt(beta)")
		}},
		{"interval-log1p", "example", func(m mtree) {
			n(m, "n4")["prior"].(mtree)["args"].(mtree)["mu"] = mex("log1p(-3) + logit(2) + log1p(beta)")
		}},
		{"interval-count", "rich", func(m mtree) { n(m, "m6")["prior"].(mtree)["args"].(mtree)["nu"] = mex("-1") }},
		{"expr-syntax", "example", func(m mtree) { n(m, "n4")["prior"].(mtree)["args"].(mtree)["mu"] = mex("1 +") }},
		{"expr-syntax-unicode", "example", func(m mtree) { n(m, "n4")["prior"].(mtree)["args"].(mtree)["mu"] = mex("μ + 1") }},
		{"expr-unknown-name", "example", func(m mtree) { n(m, "n4")["prior"].(mtree)["args"].(mtree)["mu"] = mex("nothing") }},
		{"expr-needs-index", "example", func(m mtree) { n(m, "n9")["args"].(mtree)["mu"] = mex("alpha_school + beta") }},
		{"expr-too-big-total", "example", func(m mtree) {
			long := strings.Repeat("1 + ", 120) + "1"
			for i := 0; i < 20; i++ {
				mirrorAddNode(m, mtree{"id": fmt.Sprintf("t%d", i), "kind": "transformed", "label": "T", "plate": nil, "value": mex(long)})
			}
		}},
		// likelihood
		{"likelihood-none", "example", func(m mtree) { mirrorDrop(m, "n9") }},
		{"likelihood-missing-observed", "example", func(m mtree) { delete(n(m, "n9"), "observed") }},
		{"likelihood-unknown-observed", "example", func(m mtree) { n(m, "n9")["observed"] = "zz" }},
		{"likelihood-observed-not-data", "example", func(m mtree) { n(m, "n9")["observed"] = "n4" }},
		{"likelihood-observed-twice", "example", func(m mtree) {
			mirrorAddNode(m, mtree{"id": "n10", "kind": "likelihood", "label": "L", "observed": "n1", "dist": "normal", "args": mtree{"mu": mex("mu"), "sigma": mrf("n8")}})
		}},
		{"likelihood-missing-dist", "example", func(m mtree) { delete(n(m, "n9"), "dist") }},
		{"likelihood-unknown-dist", "example", func(m mtree) { n(m, "n9")["dist"] = "weird" }},
		{"likelihood-prior-dist", "example", func(m mtree) { n(m, "n9")["dist"] = "gamma" }},
		{"likelihood-missing-arg", "example", func(m mtree) { delete(n(m, "n9")["args"].(mtree), "sigma") }},
		{"likelihood-unknown-arg", "example", func(m mtree) { n(m, "n9")["args"].(mtree)["zeta"] = mex("1") }},
		{"likelihood-real-poisson", "example", func(m mtree) { n(m, "n9")["dist"] = "poisson_log"; n(m, "n9")["args"] = mtree{"alpha": mex("mu")} }},
		{"likelihood-int-normal", "rich", func(m mtree) {
			n(m, "l2")["dist"] = "normal"
			n(m, "l2")["args"] = mtree{"mu": mex("logit_p"), "sigma": mex("sigma")}
		}},
		{"likelihood-int-any", "rich", func(m mtree) { n(m, "d6")["support"] = "any" }},
		{"likelihood-binary-binomial", "rich", func(m mtree) { n(m, "d6")["support"] = "binary" }},
		{"likelihood-n-literal", "rich", func(m mtree) { n(m, "l2")["args"].(mtree)["N"] = mex("10") }},
		{"likelihood-n-ref", "rich", func(m mtree) { n(m, "l2")["args"].(mtree)["N"] = mrf("d7") }},
		{"likelihood-n-ref-unknown", "rich", func(m mtree) { n(m, "l2")["args"].(mtree)["N"] = mrf("zz") }},
		{"likelihood-n-real", "rich", func(m mtree) { n(m, "l2")["args"].(mtree)["N"] = mex("hours") }},
		{"likelihood-n-int-any", "rich", func(m mtree) { n(m, "d7")["support"] = "any" }},
		{"likelihood-n-param", "rich", func(m mtree) { n(m, "l2")["args"].(mtree)["N"] = mex("sigma") }},
		{"likelihood-n-plate", "rich", func(m mtree) { n(m, "d7")["plate"] = "p1" }},
		{"self-predictor-direct", "example", func(m mtree) { n(m, "n9")["args"].(mtree)["mu"] = mex("alpha_school[school] + score") }},
		{"self-predictor-through", "regression", func(m mtree) { n(m, "p1")["intercept"] = mex("price") }},
		{"self-predictor-term", "regression", func(m mtree) {
			n(m, "p1")["terms"] = append(n(m, "p1")["terms"].([]any), mtree{"data": "d1", "coef": mex("b_area")})
		}},
		{"self-predictor-lag", "ar2", func(m mtree) { n(m, "t1")["value"] = mex("c + lag(sales, 1)") }},
		{"self-predictor-through-transformed", "ar2", func(m mtree) { n(m, "t1")["value"] = mex("c + sales") }},
		// terms
		{"terms-not-data", "regression", func(m mtree) { n(m, "p1")["terms"].([]any)[0].(mtree)["data"] = "m1" }},
		{"terms-unknown-data", "regression", func(m mtree) { n(m, "p1")["terms"].([]any)[0].(mtree)["data"] = "zz" }},
		{"terms-duplicate", "regression", func(m mtree) { n(m, "p1")["terms"].([]any)[1].(mtree)["data"] = "d2" }},
		{"terms-group-index", "rich", func(m mtree) { n(m, "p3")["terms"].([]any)[0].(mtree)["data"] = "d3" }},
		{"terms-time", "ar2", func(m mtree) {
			mirrorAddNode(m, mtree{"id": "p1", "kind": "linear_predictor", "label": "P", "plate": "rows", "terms": []any{mtree{"data": "d1", "coef": mex("c")}}})
		}},
		{"terms-plate", "regression", func(m mtree) { n(m, "p1")["plate"] = nil }},
		{"terms-coef-expression", "regression", func(m mtree) { n(m, "p1")["terms"].([]any)[0].(mtree)["coef"] = mex("b_area * 2") }},
		{"terms-coef-data", "regression", func(m mtree) { n(m, "p1")["terms"].([]any)[0].(mtree)["coef"] = mex("rooms") }},
		{"terms-coef-category-scalar", "rich", func(m mtree) { n(m, "p3")["terms"].([]any)[1].(mtree)["coef"] = mex("b_hours") }},
		{"terms-coef-scalar-levels", "rich", func(m mtree) { n(m, "p3")["terms"].([]any)[0].(mtree)["coef"] = mex("b_track") }},
		// effects, states
		{"effects-one", "logistic", func(m mtree) { c := n(m, "e1"); c["components"] = c["components"].([]any)[:1] }},
		{"effects-eleven", "logistic", func(m mtree) {
			c := n(m, "e1")
			for i := 3; i <= 11; i++ {
				c["components"] = append(c["components"].([]any), mtree{"id": fmt.Sprintf("c%d", i), "label": "K", "mean": mex("0"), "scale": mex("tau_a")})
			}
		}},
		{"effects-no-correlation", "logistic", func(m mtree) { delete(n(m, "e1"), "correlation") }},
		{"effects-eta-param", "logistic", func(m mtree) { n(m, "e1")["correlation"].(mtree)["eta"] = mex("tau_a") }},
		{"effects-eta-zero", "logistic", func(m mtree) { n(m, "e1")["correlation"].(mtree)["eta"] = mex("0") }},
		{"effects-scale-negative", "logistic", func(m mtree) { n(m, "e1")["components"].([]any)[0].(mtree)["scale"] = mex("-1") }},
		{"effects-mean-data", "logistic", func(m mtree) { n(m, "e1")["components"].([]any)[0].(mtree)["mean"] = mex("hours") }},
		{"effects-centred-plate", "logistic", func(m mtree) { mirrorPlate(m, "p1")["form"] = "centred" }},
		{"effects-on-rows", "logistic", func(m mtree) { n(m, "e1")["plate"] = "rows" }},
		{"state-not-series", "locallevel", func(m mtree) { delete(mirrorPlate(m, "rows"), "series") }},
		{"state-bad-process", "locallevel", func(m mtree) { n(m, "s1")["process"] = "ou" }},
		{"state-bad-form", "locallevel", func(m mtree) { n(m, "s1")["form"] = "weird" }},
		{"state-init-missing", "locallevel", func(m mtree) { delete(n(m, "s1"), "init") }},
		{"state-init-unknown", "locallevel", func(m mtree) { n(m, "s1")["init"].(mtree)["dist"] = "weird" }},
		{"state-init-lkj", "locallevel", func(m mtree) { n(m, "s1")["init"].(mtree)["dist"] = "lkj_corr_cholesky" }},
		{"state-init-likelihood", "locallevel", func(m mtree) { n(m, "s1")["init"].(mtree)["dist"] = "poisson_log" }},
		{"state-init-missing-arg", "locallevel", func(m mtree) { delete(n(m, "s1")["init"].(mtree)["args"].(mtree), "sigma") }},
		{"state-init-unknown-arg", "locallevel", func(m mtree) { n(m, "s1")["init"].(mtree)["args"].(mtree)["zeta"] = mex("1") }},
		{"state-scale-missing", "locallevel", func(m mtree) { delete(n(m, "s1"), "scale") }},
		{"state-scale-data", "locallevel", func(m mtree) { n(m, "s1")["scale"] = mex("flow") }},
		{"state-scale-negative", "locallevel", func(m mtree) { n(m, "s1")["scale"] = mex("-1") }},
		{"state-scale-ref", "locallevel", func(m mtree) { n(m, "s1")["scale"] = mrf("m1") }},
		{"state-form-centred-warning", "state", func(m mtree) { n(m, "s1")["form"] = "non_centred" }},
		// cycles, unused, forms
		{"cycle-self", "example", func(m mtree) { n(m, "n4")["prior"].(mtree)["args"].(mtree)["mu"] = mex("mu") }},
		{"cycle-two", "example", func(m mtree) {
			n(m, "n4")["prior"].(mtree)["args"].(mtree)["mu"] = mex("tau")
			n(m, "n5")["prior"].(mtree)["args"].(mtree)["mu"] = mex("mu")
		}},
		{"cycle-three-with-refs", "regression", func(m mtree) {
			mirrorAddNode(m, mtree{"id": "t1", "kind": "transformed", "label": "T1", "plate": nil, "value": mrf("t2")})
			mirrorAddNode(m, mtree{"id": "t2", "kind": "transformed", "label": "T2", "plate": nil, "value": mex("t3")})
			mirrorAddNode(m, mtree{"id": "t3", "kind": "transformed", "name": "t3", "label": "T3", "plate": nil, "value": mex("t1x")})
			mirrorAddNode(m, mtree{"id": "t4", "kind": "transformed", "name": "t1x", "label": "T4", "plate": nil, "value": mrf("t1")})
			n(m, "t2")["name"] = "t2"
		}},
		{"cycle-through-lag", "ar2", func(m mtree) { n(m, "t1")["value"] = mex("c + lag(mean_sales, 1)") }},
		{"cycle-component", "logistic", func(m mtree) { n(m, "e1")["components"].([]any)[0].(mtree)["mean"] = mex("slope") }},
		{"cycle-two-groups", "regression", func(m mtree) {
			n(m, "m1")["prior"].(mtree)["args"].(mtree)["mu"] = mex("eta")
			n(m, "p1")["intercept"] = mex("alpha")
		}},
		{"unused-data", "example", func(m mtree) {
			mirrorAddNode(m, mtree{"id": "n10", "kind": "data", "name": "extra", "label": "Extra", "plate": "rows", "type": "real"})
		}},
		{"unused-data-time", "ar2", func(m mtree) { mirrorPlate(m, "rows")["series"] = nil }},
		{"unused-parameter", "example", func(m mtree) {
			mirrorAddNode(m, mtree{"id": "n10", "kind": "parameter", "name": "idle", "label": "Idle", "prior": mtree{"dist": "normal", "args": mtree{"mu": mex("0"), "sigma": mex("1")}}})
		}},
		{"unused-parameter-chain", "example", func(m mtree) {
			mirrorAddNode(m, mtree{"id": "n10", "kind": "parameter", "name": "idle", "label": "Idle", "prior": mtree{"dist": "normal", "args": mtree{"mu": mex("0"), "sigma": mex("1")}}})
			mirrorAddNode(m, mtree{"id": "n11", "kind": "parameter", "name": "idle2", "label": "Idle", "prior": mtree{"dist": "normal", "args": mtree{"mu": mex("idle"), "sigma": mex("1")}}})
		}},
		{"form-centred-bounded", "example", func(m mtree) { n(m, "n6")["bounds"] = mtree{"lower": -3.0} }},
		{"form-centred-gamma", "example", func(m mtree) {
			n(m, "n6")["prior"] = mtree{"dist": "gamma", "args": mtree{"alpha": mex("2"), "beta": mex("tau")}}
		}},
		{"form-centred-both", "example", func(m mtree) {
			n(m, "n6")["prior"] = mtree{"dist": "gamma", "args": mtree{"alpha": mex("2"), "beta": mex("tau")}}
			n(m, "n6")["bounds"] = mtree{"lower": 1.0}
		}},
		{"form-centred-explicit-centred", "example", func(m mtree) { mirrorPlate(m, "p1")["form"] = "centred"; n(m, "n6")["bounds"] = mtree{"lower": -3.0} }},
		{"form-centred-default-form", "example", func(m mtree) { delete(mirrorPlate(m, "p1"), "form"); n(m, "n6")["bounds"] = mtree{"lower": -3.0} }},
		{"role-hyperprior-transformed", "example", func(m mtree) {
			mirrorAddNode(m, mtree{"id": "n10", "kind": "transformed", "name": "twice", "label": "Twice", "plate": nil, "value": mex("2 * mu")})
			n(m, "n7")["prior"].(mtree)["args"].(mtree)["sigma"] = mex("exp(twice)")
		}},
		{"prior-data-direct", "example", func(m mtree) { n(m, "n4")["prior"].(mtree)["args"].(mtree)["mu"] = mex("hours") }},
		{"prior-data-through", "regression", func(m mtree) {
			n(m, "m1")["prior"].(mtree)["args"].(mtree)["mu"] = mex("eta")
		}},
		{"prior-data-through-two", "regression", func(m mtree) {
			mirrorAddNode(m, mtree{"id": "t1", "kind": "transformed", "name": "t1x", "label": "T1", "plate": "rows", "value": mex("eta + 1")})
			mirrorAddNode(m, mtree{"id": "t2", "kind": "transformed", "name": "t2x", "label": "T2", "plate": "rows", "value": mex("t1x")})
			n(m, "m1")["prior"].(mtree)["args"].(mtree)["mu"] = mex("t2x")
		}},
		{"prior-data-index-ok", "rich", func(m mtree) { n(m, "m5")["prior"].(mtree)["args"].(mtree)["mu"] = mex("mu_d[district]") }},
		{"prior-data-state-scale", "locallevel", func(m mtree) { n(m, "s1")["scale"] = mex("flow") }},
		{"prior-data-component", "logistic", func(m mtree) { n(m, "e1")["components"].([]any)[0].(mtree)["scale"] = mex("hours") }},
		{"lag-in-prior", "ar2", func(m mtree) { n(m, "m1")["prior"].(mtree)["args"].(mtree)["mu"] = mex("lag(sales, 1)") }},
		{"layout-ignored", "example", func(m mtree) { m["layout"] = mtree{"p1": mtree{"x": 1.5, "y": 2, "w": 3, "h": 4}} }},
		{"null-things", "example", func(m mtree) {
			n(m, "n4")["name"] = nil
			n(m, "n4")["note"] = nil
			mirrorPlate(m, "p1")["parent"] = nil
			m["layout"] = nil
		}},
	}
	return cases
}

// mirrorRaw are documents that are not (or are barely) graphs.
func mirrorRaw() []struct{ id, input string } {
	deep := strings.Repeat("[", 70) + strings.Repeat("]", 70)
	return []struct{ id, input string }{
		{"raw-empty", ``},
		{"raw-whitespace", "  \n "},
		{"raw-not-json", `{"format_version": 1,`},
		{"raw-array", `[]`},
		{"raw-string", `"x"`},
		{"raw-trailing", `{"format_version":1,"plates":[],"nodes":[]} {}`},
		{"raw-duplicate-key", `{"format_version":1,"plates":[],"nodes":[],"nodes":[]}`},
		{"raw-duplicate-nested", `{"format_version":1,"plates":[{"id":"rows","kind":"rows","label":"R","label":"S"}],"nodes":[]}`},
		{"raw-case-folded", `{"Format_Version":1,"plates":[],"nodes":[]}`},
		{"raw-unknown-key", `{"format_version":1,"plates":[],"nodes":[],"extra":1}`},
		{"raw-no-version", `{"plates":[],"nodes":[]}`},
		{"raw-version-zero", `{"format_version":0,"plates":[],"nodes":[]}`},
		{"raw-version-float", `{"format_version":1.0,"plates":[],"nodes":[]}`},
		{"raw-version-string", `{"format_version":"1","plates":[],"nodes":[]}`},
		{"raw-version-newer", `{"format_version":2,"plates":[],"nodes":[]}`},
		{"raw-minimal", `{"format_version":1,"plates":[],"nodes":[]}`},
		{"raw-null-lists", `{"format_version":1,"plates":null,"nodes":null,"layout":null}`},
		{"raw-only-rows", `{"format_version":1,"plates":[{"id":"rows","kind":"rows","label":"R"}],"nodes":[]}`},
		{"raw-nesting", `{"format_version":1,"plates":[],"nodes":[],"layout":` + deep + `}`},
		{"raw-plates-not-array", `{"format_version":1,"plates":{},"nodes":[]}`},
		{"raw-plate-not-object", `{"format_version":1,"plates":[1],"nodes":[]}`},
		{"raw-plate-unknown-kind", `{"format_version":1,"plates":[{"id":"x","kind":"levels","label":"R"}],"nodes":[]}`},
		{"raw-plate-wrong-key", `{"format_version":1,"plates":[{"id":"rows","kind":"rows","label":"R","key":"d1"}],"nodes":[]}`},
		{"raw-series-not-object", `{"format_version":1,"plates":[{"id":"rows","kind":"rows","label":"R","series":3}],"nodes":[]}`},
		{"raw-node-unknown-kind", `{"format_version":1,"plates":[],"nodes":[{"id":"a1","kind":"widget","label":"A"}]}`},
		{"raw-node-kind-number", `{"format_version":1,"plates":[],"nodes":[{"id":"a1","kind":3,"label":"A"}]}`},
		{"raw-node-key-of-another-kind", `{"format_version":1,"plates":[],"nodes":[{"id":"a1","kind":"data","label":"A","prior":{"dist":"normal","args":{}}}]}`},
		{"raw-node-label-number", `{"format_version":1,"plates":[],"nodes":[{"id":"a1","kind":"data","label":5}]}`},
		{"raw-node-standardize-string", `{"format_version":1,"plates":[],"nodes":[{"id":"a1","kind":"data","label":"A","standardize":"yes"}]}`},
		{"raw-slot-extra-key", `{"format_version":1,"plates":[],"nodes":[{"id":"a1","kind":"transformed","label":"A","value":{"expr":"1","x":2}}]}`},
		{"raw-slot-expr-number", `{"format_version":1,"plates":[],"nodes":[{"id":"a1","kind":"transformed","label":"A","value":{"expr":1}}]}`},
		{"raw-bounds-string", `{"format_version":1,"plates":[],"nodes":[{"id":"a1","kind":"parameter","label":"A","bounds":{"lower":"0"}}]}`},
		{"raw-bounds-overflow", `{"format_version":1,"plates":[],"nodes":[{"id":"a1","kind":"parameter","label":"A","bounds":{"lower":1e999}}]}`},
		{"raw-terms-not-object", `{"format_version":1,"plates":[],"nodes":[{"id":"a1","kind":"linear_predictor","label":"A","terms":[1]}]}`},
		{"raw-args-not-object", `{"format_version":1,"plates":[],"nodes":[{"id":"a1","kind":"likelihood","label":"A","args":[]}]}`},
		{"raw-layout-not-object", `{"format_version":1,"plates":[],"nodes":[],"layout":{"a":1}}`},
		{"raw-layout-bad-key", `{"format_version":1,"plates":[],"nodes":[],"layout":{"a":{"x":1,"y":2,"z":3}}}`},
		{"raw-escapes", `{"format_version":1,"plates":[{"id":"rows","kind":"rows","label":"aé😀\n"}],"nodes":[]}`},
		{"raw-lone-surrogate", `{"format_version":1,"plates":[{"id":"rows","kind":"rows","label":"a\ud800b"}],"nodes":[]}`},
		{"raw-component-extra-key", `{"format_version":1,"plates":[],"nodes":[{"id":"e1","kind":"correlated_effects","label":"E","components":[{"id":"c1","label":"C","mean":{"expr":"0"},"scale":{"expr":"1"},"x":1}]}]}`},
	}
}

// mirrorSweep: single edits of the bases, in a fixed order.
func mirrorSweep(t *testing.T, bases map[string]string) []mirrorCase {
	names := make([]string, 0, len(bases))
	for k := range bases {
		names = append(names, k)
	}
	sort.Strings(names)
	var out []mirrorCase
	for _, bn := range names {
		base := mirrorParse(t, bases[bn])
		ids, exprNames := []string{}, []string{}
		for _, n := range base["nodes"].([]any) {
			o := n.(mtree)
			ids = append(ids, o["id"].(string))
			if s, ok := o["name"].(string); ok {
				exprNames = append(exprNames, s)
			}
		}
		slotProbes := []mtree{mex("1"), mex("-1"), mex("0"), mex(""), {}, {"expr": "1", "ref": "zz"}, mrf("zz"), mex("1 +"), mex("unknown_thing"), mex("log(-1)"), mex("sqrt(-1) + exp(2)"), mex("lag(zz, 1)"), mex("x[y]")}
		for _, id := range ids {
			slotProbes = append(slotProbes, mrf(id))
		}
		for _, s := range exprNames {
			slotProbes = append(slotProbes, mex(s))
		}
		var edits []struct {
			id string
			fn func(m mtree)
		}
		add := func(id string, fn func(m mtree)) {
			edits = append(edits, struct {
				id string
				fn func(m mtree)
			}{bn + "-" + id, fn})
		}

		// every slot of the base, by its path
		type pathStep struct {
			key any
		}
		var slotPaths [][]any
		var walk func(v any, path []any)
		walk = func(v any, path []any) {
			switch x := v.(type) {
			case mtree:
				_, hasExpr := x["expr"]
				_, hasRef := x["ref"]
				if hasExpr || hasRef {
					slotPaths = append(slotPaths, append([]any(nil), path...))
					return
				}
				keys := make([]string, 0, len(x))
				for k := range x {
					keys = append(keys, k)
				}
				sort.Strings(keys)
				for _, k := range keys {
					walk(x[k], append(path, k))
				}
			case []any:
				for i, e := range x {
					walk(e, append(path, i))
				}
			}
		}
		for i, n := range base["nodes"].([]any) {
			walk(n, []any{"nodes", i})
		}
		setAt := func(m mtree, path []any, v any) {
			var cur any = m
			for _, p := range path[:len(path)-1] {
				switch k := p.(type) {
				case string:
					cur = cur.(mtree)[k]
				case int:
					cur = cur.([]any)[k]
				}
			}
			switch k := path[len(path)-1].(type) {
			case string:
				cur.(mtree)[k] = v
			case int:
				cur.([]any)[k] = v
			}
		}
		for _, p := range slotPaths {
			p := p
			for j, probe := range slotProbes {
				probe := probe
				add(fmt.Sprintf("slot-%v-%d", p, j), func(m mtree) {
					b, _ := json.Marshal(probe)
					var c mtree
					_ = json.Unmarshal(b, &c)
					setAt(m, p, c)
				})
			}
		}
		strProbes := []string{"", "zz", "rows", "p1", "d1", "m1", "n1", "normal", "real", "int", "group", "levels:d5"}
		for _, n := range base["nodes"].([]any) {
			o := n.(mtree)
			id := o["id"].(string)
			add("drop-"+id, func(m mtree) { mirrorDrop(m, id) })
			for _, pl := range []any{nil, "rows", "p1", "p2", "zz", "levels:d5"} {
				pl := pl
				add(fmt.Sprintf("plate-%s-%v", id, pl), func(m mtree) { mirrorNode(m, id)["plate"] = pl })
			}
			keys := make([]string, 0, len(o))
			for k, v := range o {
				if _, ok := v.(string); ok && k != "id" && k != "kind" {
					keys = append(keys, k)
				}
			}
			sort.Strings(keys)
			for _, k := range keys {
				k := k
				for _, sp := range strProbes {
					sp := sp
					add(fmt.Sprintf("set-%s-%s-%s", id, k, sp), func(m mtree) { mirrorNode(m, id)[k] = sp })
				}
			}
			for _, nm := range []string{"zz", "mu", "sigma", "y"} {
				nm := nm
				add(fmt.Sprintf("name-%s-%s", id, nm), func(m mtree) { mirrorNode(m, id)["name"] = nm })
			}
			add("noname-"+id, func(m mtree) { delete(mirrorNode(m, id), "name") })
		}
		for _, p := range base["plates"].([]any) {
			o := p.(mtree)
			id := o["id"].(string)
			for _, k := range []string{"key", "parent", "form", "label"} {
				k := k
				for _, sp := range strProbes {
					sp := sp
					add(fmt.Sprintf("pset-%s-%s-%s", id, k, sp), func(m mtree) { mirrorPlate(m, id)[k] = sp })
				}
			}
		}
		for _, e := range edits {
			m := mirrorParse(t, bases[bn])
			e.fn(m)
			out = append(out, mirrorOf(e.id, mirrorDump(t, m)))
		}
	}
	return out
}

func mirrorSigs(c mirrorCase) []string {
	var s []string
	for _, f := range c.Findings {
		reason, _ := f.Args["reason"].(string)
		slot := f.Slot
		if i := strings.IndexAny(slot, ".["); i > 0 {
			slot = slot[:i]
		}
		extra := ""
		if _, ok := f.Args["through"]; ok {
			extra = "+through"
		}
		for _, k := range []string{"need", "function", "kind"} {
			if v, ok := f.Args[k].(string); ok {
				extra += "+" + k + "=" + v
			}
		}
		s = append(s, f.Code+"|"+slot+"|"+reason+extra)
	}
	if c.Model != nil {
		for id, p := range c.Model.Params {
			_ = id
			s = append(s, fmt.Sprintf("param|%s|%v|%v|%v|%v", p.Dist, p.Truncated, p.TruncateInProgram, p.Role, p.NonCentred))
		}
	}
	return s
}

func buildMirrorCases(t *testing.T) []mirrorCase {
	t.Helper()
	bases := mirrorBases()
	var out []mirrorCase
	names := make([]string, 0, len(bases))
	for k := range bases {
		names = append(names, k)
	}
	sort.Strings(names)
	for _, n := range names {
		out = append(out, mirrorOf("base-"+n, mirrorDump(t, mirrorParse(t, bases[n]))))
	}
	for _, h := range mirrorHand() {
		m := mirrorParse(t, bases[h.base])
		h.edit(m)
		out = append(out, mirrorOf("hand-"+h.id, mirrorDump(t, m)))
	}
	for _, r := range mirrorRaw() {
		out = append(out, mirrorOf(r.id, r.input))
	}
	seen := map[string]bool{}
	for _, c := range out {
		for _, s := range mirrorSigs(c) {
			seen[s] = true
		}
	}
	// the mirrorSweep keeps a case when it brings a signature not seen yet, and nothing else
	for _, c := range mirrorSweep(t, bases) {
		fresh := false
		for _, s := range mirrorSigs(c) {
			if !seen[s] {
				seen[s] = true
				fresh = true
			}
		}
		if fresh {
			out = append(out, c)
		}
	}
	return out
}

func marshalMirror(cases []mirrorCase) []byte {
	var buf bytes.Buffer
	buf.WriteString("{\"cases\":[\n")
	for i, c := range cases {
		var b bytes.Buffer
		enc := json.NewEncoder(&b)
		enc.SetEscapeHTML(false)
		_ = enc.Encode(c)
		buf.Write(bytes.TrimRight(b.Bytes(), "\n"))
		if i < len(cases)-1 {
			buf.WriteString(",")
		}
		buf.WriteString("\n")
	}
	buf.WriteString("]}\n")
	return buf.Bytes()
}

func TestMirrorCorpus(t *testing.T) {
	cases := buildMirrorCases(t)
	ids := map[string]bool{}
	for _, c := range cases {
		if ids[c.ID] {
			t.Fatalf("duplicate case id %s", c.ID)
		}
		ids[c.ID] = true
	}
	// every code the spec lists is in the corpus
	have := map[string]bool{}
	for _, c := range cases {
		for _, f := range c.Findings {
			have[f.Code] = true
		}
	}
	for _, code := range []string{
		"graph.format", "graph.too_big", "graph.id", "graph.name", "graph.name_taken", "graph.name_function", "graph.label",
		"graph.note", "graph.rows", "graph.plate_parent", "graph.plate_key", "graph.series", "graph.plate_empty", "graph.node_plate",
		"graph.data_type", "graph.standardize", "graph.prior", "graph.slot", "expr.unknown_name", "graph.slot_shape", "graph.slot_interval",
		"graph.slot_maybe", "graph.prior_data", "graph.constant", "graph.bounds", "graph.cycle", "graph.likelihood", "graph.self_predictor",
		"graph.unused_data", "graph.unused_parameter", "graph.form_centred", "graph.effects", "graph.state", "graph.terms",
	} {
		if !have[code] {
			t.Errorf("no case of the mirror corpus gives %s", code)
		}
	}
	got := marshalMirror(cases)
	if os.Getenv("BAYES_MIRROR_WRITE") != "" {
		if err := os.MkdirAll(filepath.Dir(mirrorFile), 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(mirrorFile, got, 0o644); err != nil {
			t.Fatal(err)
		}
		t.Logf("wrote %d cases, %d bytes", len(cases), len(got))
		return
	}
	want, err := os.ReadFile(mirrorFile)
	if err != nil {
		t.Fatalf("%v: write it with BAYES_MIRROR_WRITE=1 go test ./internal/bayesgraph -run TestMirrorCorpus", err)
	}
	if !bytes.Equal(got, want) {
		t.Errorf("%s is not what the cases and the validator give now (%d cases, %d bytes against %d): regenerate it with BAYES_MIRROR_WRITE=1 go test ./internal/bayesgraph -run TestMirrorCorpus", mirrorFile, len(cases), len(got), len(want))
	}
}
