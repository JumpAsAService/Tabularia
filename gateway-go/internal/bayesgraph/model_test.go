package bayesgraph

import (
	"encoding/json"
	"fmt"
	"reflect"
	"strings"
	"testing"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/bayescatalog"
)

// The four families of the map, as graphs.

// regression: a linear regression with a standardized predictor, written as a linear predictor.
const regression = `{
  "format_version": 1,
  "plates": [{"id": "rows", "kind": "rows", "label": "Houses"}],
  "nodes": [
    {"id": "d1", "kind": "data", "name": "price", "label": "Price", "plate": "rows", "type": "real"},
    {"id": "d2", "kind": "data", "name": "area", "label": "Area", "plate": "rows", "type": "real", "standardize": true},
    {"id": "d3", "kind": "data", "name": "rooms", "label": "Rooms", "plate": "rows", "type": "int", "support": "nonnegative"},
    {"id": "m1", "kind": "parameter", "name": "alpha", "label": "Intercept", "prior": {"dist": "normal", "args": {"mu": {"expr": "0"}, "sigma": {"expr": "10"}}}},
    {"id": "m2", "kind": "parameter", "name": "b_area", "label": "Area effect", "prior": {"dist": "normal", "args": {"mu": {"expr": "0"}, "sigma": {"expr": "1"}}}},
    {"id": "m3", "kind": "parameter", "name": "b_rooms", "label": "Rooms effect", "prior": {"dist": "normal", "args": {"mu": {"expr": "0"}, "sigma": {"expr": "1"}}}},
    {"id": "m4", "kind": "parameter", "name": "sigma", "label": "Noise", "prior": {"dist": "exponential", "args": {"beta": {"expr": "1"}}}},
    {"id": "p1", "kind": "linear_predictor", "name": "eta", "label": "Expected price", "plate": "rows", "intercept": {"expr": "alpha"},
     "terms": [{"data": "d2", "coef": {"expr": "b_area"}}, {"data": "d3", "coef": {"ref": "m3"}}]},
    {"id": "l1", "kind": "likelihood", "label": "Prices", "observed": "d1", "dist": "normal", "args": {"mu": {"expr": "eta"}, "sigma": {"expr": "sigma"}}}
  ]
}`

// logistic: a hierarchical logistic regression with correlated intercept and slope per school,
// non-centred.
const logistic = `{
  "format_version": 1,
  "plates": [
    {"id": "rows", "kind": "rows", "label": "Pupils"},
    {"id": "p1", "kind": "group", "label": "School", "key": "d3", "form": "non_centred"}
  ],
  "nodes": [
    {"id": "d1", "kind": "data", "name": "passed", "label": "Passed", "plate": "rows", "type": "int", "support": "binary"},
    {"id": "d2", "kind": "data", "name": "hours", "label": "Hours", "plate": "rows", "type": "real", "standardize": true},
    {"id": "d3", "kind": "data", "name": "school", "label": "School", "plate": "rows", "type": "group_index", "target": "p1"},
    {"id": "m1", "kind": "parameter", "name": "mu_a", "label": "Mean intercept", "prior": {"dist": "normal", "args": {"mu": {"expr": "0"}, "sigma": {"expr": "2"}}}},
    {"id": "m2", "kind": "parameter", "name": "mu_b", "label": "Mean slope", "prior": {"dist": "normal", "args": {"mu": {"expr": "0"}, "sigma": {"expr": "1"}}}},
    {"id": "m3", "kind": "parameter", "name": "tau_a", "label": "Intercept spread", "prior": {"dist": "exponential", "args": {"beta": {"expr": "1"}}}},
    {"id": "m4", "kind": "parameter", "name": "tau_b", "label": "Slope spread", "prior": {"dist": "exponential", "args": {"beta": {"expr": "1"}}}},
    {"id": "e1", "kind": "correlated_effects", "label": "School effects", "plate": "p1",
     "components": [
       {"id": "c1", "name": "icpt", "label": "Intercept", "mean": {"expr": "mu_a"}, "scale": {"expr": "tau_a"}},
       {"id": "c2", "name": "slope", "label": "Slope", "mean": {"ref": "m2"}, "scale": {"expr": "tau_b"}}],
     "correlation": {"name": "omega", "label": "Correlation", "eta": {"expr": "2"}}},
    {"id": "t1", "kind": "transformed", "name": "logit_p", "label": "Log-odds", "plate": "rows", "value": {"expr": "icpt[school] + slope[school] * hours"}},
    {"id": "l1", "kind": "likelihood", "label": "Passes", "observed": "d1", "dist": "bernoulli_logit", "args": {"alpha": {"expr": "logit_p"}}}
  ]
}`

// ar2: an autoregression of order 2 with lag, on a series.
const ar2 = `{
  "format_version": 1,
  "plates": [{"id": "rows", "kind": "rows", "label": "Months", "series": {"time": "d1", "by": null}}],
  "nodes": [
    {"id": "d1", "kind": "data", "name": "month", "label": "Month", "plate": "rows", "type": "time"},
    {"id": "d2", "kind": "data", "name": "sales", "label": "Sales", "plate": "rows", "type": "real"},
    {"id": "m1", "kind": "parameter", "name": "c", "label": "Constant", "prior": {"dist": "normal", "args": {"mu": {"expr": "0"}, "sigma": {"expr": "5"}}}},
    {"id": "m2", "kind": "parameter", "name": "phi1", "label": "First lag", "prior": {"dist": "normal", "args": {"mu": {"expr": "0"}, "sigma": {"expr": "1"}}}},
    {"id": "m3", "kind": "parameter", "name": "phi2", "label": "Second lag", "prior": {"dist": "normal", "args": {"mu": {"expr": "0"}, "sigma": {"expr": "1"}}}},
    {"id": "m4", "kind": "parameter", "name": "sigma", "label": "Noise", "prior": {"dist": "exponential", "args": {"beta": {"expr": "1"}}}},
    {"id": "t1", "kind": "transformed", "name": "mean_sales", "label": "Expected sales", "plate": "rows", "value": {"expr": "c + phi1 * lag(sales, 1) + phi2 * lag(sales, 2)"}},
    {"id": "l1", "kind": "likelihood", "label": "Sales", "observed": "d2", "dist": "normal", "args": {"mu": {"expr": "mean_sales"}, "sigma": {"expr": "sigma"}}}
  ]
}`

// locallevel: a local-level model, a random walk observed with noise.
const locallevel = `{
  "format_version": 1,
  "plates": [{"id": "rows", "kind": "rows", "label": "Days", "series": {"time": "d1", "by": null}}],
  "nodes": [
    {"id": "d1", "kind": "data", "name": "day", "label": "Day", "plate": "rows", "type": "time"},
    {"id": "d2", "kind": "data", "name": "flow", "label": "Flow", "plate": "rows", "type": "real"},
    {"id": "m1", "kind": "parameter", "name": "sigma_level", "label": "Level drift", "prior": {"dist": "exponential", "args": {"beta": {"expr": "1"}}}},
    {"id": "m2", "kind": "parameter", "name": "sigma_obs", "label": "Noise", "prior": {"dist": "exponential", "args": {"beta": {"expr": "1"}}}},
    {"id": "s1", "kind": "state", "name": "level", "label": "Level", "plate": "rows", "process": "random_walk", "form": "non_centred",
     "init": {"dist": "normal", "args": {"mu": {"expr": "0"}, "sigma": {"expr": "10"}}}, "scale": {"expr": "sigma_level"}},
    {"id": "l1", "kind": "likelihood", "label": "Flow", "observed": "d2", "dist": "normal", "args": {"mu": {"expr": "level"}, "sigma": {"expr": "sigma_obs"}}}
  ]
}`

// edit decodes src as a generic tree, edits it and decodes it as a graph.
func edit(t *testing.T, src string, fn func(m map[string]any)) *Graph {
	t.Helper()
	var m map[string]any
	if err := json.Unmarshal([]byte(src), &m); err != nil {
		t.Fatal(err)
	}
	if fn != nil {
		fn(m)
	}
	b, err := json.Marshal(m)
	if err != nil {
		t.Fatal(err)
	}
	return mustDecode(t, string(b))
}

func findings(t *testing.T, src string, fn func(m map[string]any)) []Finding {
	t.Helper()
	return Validate(edit(t, src, fn))
}

func removeNode(m map[string]any, id string) {
	var keep []any
	for _, n := range m["nodes"].([]any) {
		if n.(map[string]any)["id"] != id {
			keep = append(keep, n)
		}
	}
	m["nodes"] = keep
}

func dataNode(id, name, typ string, plate any) map[string]any {
	n := map[string]any{"id": id, "kind": "data", "label": strings.ToUpper(id), "plate": plate, "type": typ}
	if name != "" {
		n["name"] = name
	}
	return n
}

func param(id, name string, plate any) map[string]any {
	n := map[string]any{"id": id, "kind": "parameter", "label": strings.ToUpper(id), "plate": plate,
		"prior": normalPrior(expr("0"), expr("1"))}
	if name != "" {
		n["name"] = name
	}
	return n
}

func transformed(id, name, value string, plate any) map[string]any {
	n := map[string]any{"id": id, "kind": "transformed", "label": strings.ToUpper(id), "plate": plate, "value": expr(value)}
	if name != "" {
		n["name"] = name
	}
	return n
}

func args(f Finding) string { b, _ := json.Marshal(f.Args); return string(b) }

func TestFourFamiliesBuildValidModels(t *testing.T) {
	for name, src := range map[string]string{"regression": regression, "logistic": logistic, "ar2": ar2, "locallevel": locallevel} {
		m := BuildModel(mustDecode(t, src))
		if !m.Valid() || len(m.Findings) != 0 {
			t.Errorf("%s: %s", name, codes(m.Findings))
		}
		if len(m.Order) != len(m.Graph.Nodes) {
			t.Errorf("%s: order %v", name, m.Order)
		}
	}
}

func TestModelRegression(t *testing.T) {
	m := BuildModel(mustDecode(t, regression))
	if !reflect.DeepEqual(m.Plates, []ModelPlate{{ID: "rows", Kind: PlateRows, Label: "Houses"}}) {
		t.Errorf("plates: %+v", m.Plates)
	}
	// what a node reads comes first, ties by id: the data and the parameters, the predictor, the likelihood
	if want := []string{"d1", "d2", "d3", "m1", "m2", "m3", "m4", "p1", "l1"}; !reflect.DeepEqual(m.Order, want) {
		t.Errorf("order: %v", m.Order)
	}
	for _, id := range []string{"m1", "m2", "m3", "m4"} {
		if p := m.Params[id]; p.Role != RolePrior || p.NonCentred {
			t.Errorf("%s: %+v", id, p)
		}
	}
	// the standardized area gives a pair; the integer rooms do not; the intercept is shifted
	if want := []ScalePair{{Node: "p1", Term: 0, Data: "d2"}, {Node: "p1", Term: -1, Intercept: true}}; !reflect.DeepEqual(m.Pairs, want) {
		t.Errorf("pairs: %+v", m.Pairs)
	}
	want := []Binding{
		{Node: "d1", Name: "price", Label: "Price", Type: "real", Plate: "rows", Observed: true},
		{Node: "d2", Name: "area", Label: "Area", Type: "real", Plate: "rows", Standardize: true},
		{Node: "d3", Name: "rooms", Label: "Rooms", Type: "int", Support: "nonnegative", Plate: "rows"},
	}
	if !reflect.DeepEqual(m.Bindings, want) {
		t.Errorf("bindings: %+v", m.Bindings)
	}
	if len(m.Lags) != 0 || len(m.Lifts) != 0 {
		t.Errorf("lags %v lifts %v", m.Lags, m.Lifts)
	}
	// the edges: the predictor reads alpha, the terms' data and coefficients; the likelihood the predictor
	has := func(e Edge) bool {
		for _, x := range m.Edges {
			if x == e {
				return true
			}
		}
		return false
	}
	for _, e := range []Edge{
		{"m1", "p1", SlotKey{"p1", "intercept"}, EdgeRead},
		{"m2", "p1", SlotKey{"p1", "terms[0].coef"}, EdgeRead},
		{"m3", "p1", SlotKey{"p1", "terms[1].coef"}, EdgeRead},
		{"d2", "p1", SlotKey{"p1", "terms[0].data"}, EdgeTerm},
		{"d3", "p1", SlotKey{"p1", "terms[1].data"}, EdgeTerm},
		{"p1", "l1", SlotKey{"l1", "args.mu"}, EdgeRead},
		{"d1", "l1", SlotKey{"l1", "observed"}, EdgeObserved},
	} {
		if !has(e) {
			t.Errorf("missing edge %+v", e)
		}
	}
	if s := m.Slots[SlotKey{"p1", "terms[1].coef"}]; s.Ref != "m3" {
		t.Errorf("a typed ref: %+v", s)
	}
	// no pair when the data is not standardized
	m = BuildModel(edit(t, regression, func(m map[string]any) { delete(nodeByID(m, "d2"), "standardize") }))
	if len(m.Pairs) != 0 || !m.Valid() {
		t.Errorf("not standardized: %+v %s", m.Pairs, codes(m.Findings))
	}
}

func TestModelLogisticHierarchicalNonCentred(t *testing.T) {
	m := BuildModel(mustDecode(t, logistic))
	wantPlates := []ModelPlate{
		{ID: "rows", Kind: PlateRows, Label: "Pupils"},
		{ID: "p1", Kind: PlateGroup, Label: "School", Depth: 1, Key: "d3", NonCentred: true},
	}
	if !reflect.DeepEqual(m.Plates, wantPlates) {
		t.Errorf("plates: %+v", m.Plates)
	}
	// the parameters that feed the components' means and scales are hyperpriors
	for _, id := range []string{"m1", "m2", "m3", "m4"} {
		if p := m.Params[id]; p.Role != RoleHyperprior || p.NonCentred {
			t.Errorf("%s: %+v", id, p)
		}
	}
	if e := m.Effects["e1"]; e != (EffectsInfo{Plate: "p1", Components: 2, NonCentred: true}) {
		t.Errorf("effects: %+v", e)
	}
	// no pair: the standardized predictor is read in a free expression
	if len(m.Pairs) != 0 {
		t.Errorf("pairs: %+v", m.Pairs)
	}
	if want := []string{"d1", "d2", "d3", "m1", "m2", "m3", "m4", "e1", "t1", "l1"}; !reflect.DeepEqual(m.Order, want) {
		t.Errorf("order: %v", m.Order)
	}
	// the component slots are typed over the school plate
	if s := m.Slots[SlotKey{"e1.c1", "mean"}]; s.Context != "p1" || s.Type.String() != "real" {
		t.Errorf("component mean: %+v", s)
	}
	if s := m.Slots[SlotKey{"t1", "value"}]; s.Context != "rows" || !reflect.DeepEqual(s.Indices, []string{"d3"}) || !reflect.DeepEqual(s.Refs, []string{"d2", "e1.c1", "e1.c2"}) {
		t.Errorf("the log-odds: %+v", s)
	}
	// the form of the plate decides the effects' form
	m = BuildModel(edit(t, logistic, func(m map[string]any) { plate(m, 1)["form"] = "centred" }))
	if e := m.Effects["e1"]; e.NonCentred || m.Plates[1].NonCentred {
		t.Errorf("centred: %+v %+v", e, m.Plates[1])
	}
}

func TestModelAR2(t *testing.T) {
	m := BuildModel(mustDecode(t, ar2))
	if !reflect.DeepEqual(m.Lags, []int{1, 2}) {
		t.Errorf("lags: %v", m.Lags)
	}
	if m.Plates[0].Series == nil || m.Plates[0].Series.Time != "d1" {
		t.Errorf("series: %+v", m.Plates[0])
	}
	// the lagged data is read through lag edges, the time only by the series
	lag := 0
	for _, e := range m.Edges {
		if e.Kind == EdgeLag && e.From == "d2" && e.To == "t1" {
			lag++
		}
	}
	if lag != 1 {
		t.Errorf("lag edges: %+v", m.Edges)
	}
	if want := []string{"d1", "d2", "m1", "m2", "m3", "m4", "t1", "l1"}; !reflect.DeepEqual(m.Order, want) {
		t.Errorf("order: %v", m.Order)
	}
	if !m.Bindings[1].Observed || m.Bindings[0].Type != "time" {
		t.Errorf("bindings: %+v", m.Bindings)
	}
	// a lag is not a prediction of the observed value by itself, and not a cycle
	wantClean(t, "lag", m.Findings)
}

func TestModelLocalLevel(t *testing.T) {
	m := BuildModel(mustDecode(t, locallevel))
	if s := m.States["s1"]; !s.NonCentred {
		t.Errorf("state: %+v", s)
	}
	// the state's scale feeds a distribution of the state: a hyperprior; the noise is a prior
	if m.Params["m1"].Role != RoleHyperprior || m.Params["m2"].Role != RolePrior {
		t.Errorf("roles: %+v %+v", m.Params["m1"], m.Params["m2"])
	}
	if want := []string{"d1", "d2", "m1", "m2", "s1", "l1"}; !reflect.DeepEqual(m.Order, want) {
		t.Errorf("order: %v", m.Order)
	}
	if s := m.Slots[SlotKey{"s1", "scale"}]; s.Context != "rows" || s.Ref != "" || !reflect.DeepEqual(s.Refs, []string{"m1"}) {
		t.Errorf("state scale: %+v", s)
	}
	// centred on request, or with an init that is not location-scale
	m = BuildModel(edit(t, locallevel, func(m map[string]any) { nodeByID(m, "s1")["form"] = "centred" }))
	if m.States["s1"].NonCentred {
		t.Error("centred form asked")
	}
	m = BuildModel(edit(t, locallevel, func(m map[string]any) {
		nodeByID(m, "s1")["init"] = map[string]any{"dist": "gamma", "args": map[string]any{"alpha": expr("2"), "beta": expr("1")}}
	}))
	if m.States["s1"].NonCentred || !m.Valid() {
		t.Errorf("gamma init: %+v %s", m.States["s1"], codes(m.Findings))
	}
}

func TestModelNestedPlatesLiftsAndLevels(t *testing.T) {
	g := mustDecode(t, strings.Replace(strings.Replace(nested, `"label": "School", "plate": "rows"`, `"name": "school", "label": "School", "plate": "rows"`, 1), "d2x", "school", 1))
	m := BuildModel(g)
	var ids []string
	for _, p := range m.Plates {
		ids = append(ids, p.ID+":"+p.Kind)
	}
	if want := []string{"rows:rows", "p1:group", "p2:group", "levels:d3:levels"}; !reflect.DeepEqual(ids, want) {
		t.Errorf("plates: %v", ids)
	}
	if p := m.Plates[2]; p.Parent != "p1" || p.Depth != 2 || p.Key != "d2" {
		t.Errorf("the school plate: %+v", p)
	}
	if p := m.Plates[3]; p.Category != "d3" || p.Label != "Region" {
		t.Errorf("the levels plate: %+v", p)
	}
	if len(m.Lifts) != 1 || m.Lifts[0].ID != "d1" || m.Lifts[0].Context != "p2" {
		t.Errorf("lifts: %+v", m.Lifts)
	}
	if !reflect.DeepEqual(m.Lags, []int{1, 2}) {
		t.Errorf("lags: %v", m.Lags)
	}
	// mu feeds mu_district's prior, mu_district feeds mu_school's: both hyperpriors; the levels parameter is not
	for id, role := range map[string]string{"m1": RoleHyperprior, "m2": RoleHyperprior, "m3": RolePrior, "m4": RolePrior, "m5": RolePrior} {
		if m.Params[id].Role != role {
			t.Errorf("%s: %+v", id, m.Params[id])
		}
	}
	// a group plate lists its parent before it, depth then id
	m = BuildModel(edit(t, nested, func(m map[string]any) {
		plate(m, 1)["id"] = "q9"
		node(m, 0)["target"] = "q9"
		plate(m, 2)["parent"] = "q9"
	}))
	if m.Plates[1].ID != "q9" || m.Plates[2].ID != "p2" {
		t.Errorf("order of plates: %+v", m.Plates)
	}
}

func TestModelWithErrorsAndGate(t *testing.T) {
	m := BuildModel(edit(t, regression, func(m map[string]any) { nodeByID(m, "m4")["prior"] = nil }))
	if m.Valid() || m.Params["m4"].Dist != "" || len(m.Order) != 9 {
		t.Errorf("a missing prior: valid %v, %+v", m.Valid(), m.Params["m4"])
	}
	// the gate: a duplicate id leaves the findings alone
	m = BuildModel(edit(t, regression, func(m map[string]any) { node(m, 1)["id"] = "d1" }))
	if m.Valid() || m.Slots != nil || m.Order != nil || !hasCode(m.Findings, "graph.id", "d1") {
		t.Errorf("gate: %s", codes(m.Findings))
	}
}

func TestOrderTiesAndCycleLeftAtTheEnd(t *testing.T) {
	// ids decide among nodes that can go first, and a node waits for what it reads
	m := BuildModel(edit(t, regression, func(m map[string]any) {
		nodeByID(m, "m1")["prior"] = normalPrior(expr("0"), expr("sigma"))
	}))
	pos := map[string]int{}
	for i, id := range m.Order {
		pos[id] = i
	}
	if pos["m4"] > pos["m1"] || m.Order[0] != "d1" {
		t.Errorf("order: %v", m.Order)
	}
	m = BuildModel(edit(t, regression, func(m map[string]any) {
		addNodes(m, transformed("t1", "ta", "tb", "rows"), transformed("t2", "tb", "ta", "rows"))
	}))
	if m.Valid() || !reflect.DeepEqual(m.Order[len(m.Order)-2:], []string{"t1", "t2"}) {
		t.Errorf("cycle at the end: %v", m.Order)
	}
}

func TestEdgesAreOnePerSlotAndKind(t *testing.T) {
	m := BuildModel(edit(t, regression, func(m map[string]any) {
		nodeByID(m, "l1")["args"].(map[string]any)["mu"] = expr("eta + eta * 2")
	}))
	n := 0
	for _, e := range m.Edges {
		if e.From == "p1" && e.To == "l1" {
			n++
		}
	}
	if n != 1 {
		t.Errorf("edges: %+v", m.Edges)
	}
	// a component read by its owner's slot is an edge from the component
	m = BuildModel(mustDecode(t, logistic))
	found := false
	for _, e := range m.Edges {
		if e.From == "e1.c1" && e.To == "t1" {
			found = true
		}
		if e.To == "e1" && e.From == "m2" && e.Slot == (SlotKey{"e1.c2", "mean"}) {
			found = found && true
		}
	}
	if !found {
		t.Errorf("component edge: %+v", m.Edges)
	}
}

func TestLikelihoodStructure(t *testing.T) {
	// none
	fs := findings(t, example, func(m map[string]any) { removeNode(m, "n9") })
	if f := want(t, "none", fs, "graph.likelihood", "", ""); f.Args["reason"] != "none" {
		t.Errorf("none: %+v", f)
	}
	wantNot(t, "no likelihood", fs, "graph.unused_parameter")
	// beside: one
	wantNot(t, "one", findings(t, example, nil), "graph.likelihood")
	obs := func(id any) func(m map[string]any) {
		return func(m map[string]any) { nodeByID(m, "n9")["observed"] = id }
	}
	for _, c := range []struct {
		what, id, reason string
	}{{"unknown", "n99", "unknown_node"}, {"not data", "n4", "not_data"}, {"missing", "", "missing"}} {
		f := want(t, c.what, findings(t, example, obs(c.id)), "graph.likelihood", "n9", "observed")
		if f.Args["reason"] != c.reason {
			t.Errorf("%s: %+v", c.what, f)
		}
	}
	// observed twice: reported on both; beside: two likelihoods of two data nodes
	two := func(second string) func(m map[string]any) {
		return func(m map[string]any) {
			addNodes(m, dataNode("n10", "other", "real", "rows"), map[string]any{"id": "n11", "kind": "likelihood", "label": "L2", "observed": second,
				"dist": "normal", "args": map[string]any{"mu": expr("beta * hours"), "sigma": expr("1")}})
		}
	}
	fs = findings(t, example, two("n1"))
	for _, id := range []string{"n9", "n11"} {
		if f := want(t, "twice", fs, "graph.likelihood", id, "observed"); f.Args["reason"] != "observed_twice" || f.Args["observed"] != "n1" || f.Args["count"] != 2 {
			t.Errorf("twice %s: %+v", id, f)
		}
	}
	wantNot(t, "two data nodes", findings(t, example, two("n10")), "graph.likelihood")
}

func TestLikelihoodDistributionAndArguments(t *testing.T) {
	dist := func(d string) func(m map[string]any) {
		return func(m map[string]any) { nodeByID(m, "n9")["dist"] = d }
	}
	for _, c := range []struct{ d, reason string }{{"", "missing"}, {"nope", "unknown_dist"}, {"student_t", "not_likelihood"}, {"lkj_corr_cholesky", "not_likelihood"}} {
		f := want(t, c.d, findings(t, example, dist(c.d)), "graph.likelihood", "n9", "dist")
		if f.Args["reason"] != c.reason {
			t.Errorf("%q: %+v", c.d, f)
		}
	}
	wantNot(t, "normal", findings(t, example, dist("normal")), "graph.likelihood")
	fs := findings(t, example, func(m map[string]any) { delete(nodeByID(m, "n9")["args"].(map[string]any), "sigma") })
	if f := want(t, "missing arg", fs, "graph.likelihood", "n9", "args.sigma"); f.Args["reason"] != "missing_arg" || f.Args["arg"] != "sigma" {
		t.Errorf("missing arg: %+v", f)
	}
	fs = findings(t, example, func(m map[string]any) { nodeByID(m, "n9")["args"].(map[string]any)["tau"] = expr("1") })
	if f := want(t, "unknown arg", fs, "graph.likelihood", "n9", "args.tau"); f.Args["reason"] != "unknown_arg" {
		t.Errorf("unknown arg: %+v", f)
	}
}

func TestLikelihoodOutcomes(t *testing.T) {
	// the example's score (n1) as each kind of data, observed by each distribution
	run := func(typ, support, d string, alpha bool) []Finding {
		return findings(t, example, func(m map[string]any) {
			n := nodeByID(m, "n1")
			n["type"] = typ
			if support != "" {
				n["support"] = support
			}
			l := nodeByID(m, "n9")
			l["dist"] = d
			a := map[string]any{"mu": expr("alpha_school[school] + beta * hours"), "sigma": ref("n8")}
			switch d {
			case "bernoulli_logit", "poisson_log":
				a = map[string]any{"alpha": expr("alpha_school[school] + beta * hours")}
			case "neg_binomial_2_log":
				a = map[string]any{"eta": expr("alpha_school[school] + beta * hours"), "phi": ref("n8")}
			}
			l["args"] = a
		})
	}
	cases := []struct {
		typ, support, dist string
		trips              bool
	}{
		{"real", "", "normal", false},
		{"int", "binary", "normal", true},
		{"int", "nonnegative", "normal", true},
		{"int", "binary", "bernoulli_logit", false},
		{"real", "", "bernoulli_logit", true},
		{"int", "nonnegative", "bernoulli_logit", true},
		{"int", "nonnegative", "poisson_log", false},
		{"int", "binary", "poisson_log", false},
		{"int", "any", "poisson_log", true},
		{"real", "", "poisson_log", true},
		{"int", "nonnegative", "neg_binomial_2_log", false},
		{"int", "any", "neg_binomial_2_log", true},
		{"real", "", "neg_binomial_2_log", true},
	}
	for _, c := range cases {
		fs := run(c.typ, c.support, c.dist, true)
		f, got := find(fs, "graph.likelihood", "n9", "observed")
		if got != c.trips {
			t.Errorf("%s/%s observed by %s: tripped %v, findings %s", c.typ, c.support, c.dist, got, codes(fs))
		}
		if got && (f.Args["reason"] != "outcome" || f.Args["dist"] != c.dist || f.Args["type"] != c.typ) {
			t.Errorf("%s/%s observed by %s: %s", c.typ, c.support, c.dist, args(f))
		}
	}
	f, _ := find(run("int", "nonnegative", "normal", true), "graph.likelihood", "n9", "observed")
	if f.Args["need"] != "real" || f.Args["support"] != "nonnegative" {
		t.Errorf("args: %s", args(f))
	}
	if f, _ := find(run("real", "", "poisson_log", true), "graph.likelihood", "n9", "observed"); f.Args["need"] != "count" {
		t.Errorf("args: %s", args(f))
	}
}

func TestBinomialN(t *testing.T) {
	binomial := func(n any, trials func(m map[string]any)) func(m map[string]any) {
		return func(m map[string]any) {
			d := nodeByID(m, "n1")
			d["type"], d["support"] = "int", "nonnegative"
			if trials != nil {
				trials(m)
			}
			l := nodeByID(m, "n9")
			l["dist"] = "binomial_logit"
			l["args"] = map[string]any{"N": n, "alpha": expr("alpha_school[school] + beta * hours")}
		}
	}
	trialsOn := func(typ, support string, plate any) func(m map[string]any) {
		return func(m map[string]any) {
			n := dataNode("n10", "trials", typ, plate)
			if support != "" {
				n["support"] = support
			}
			addNodes(m, n)
		}
	}
	// beside: a nonnegative integer data node on the observed plate, by name or by ref
	wantNot(t, "by name", findings(t, example, binomial(expr("trials"), trialsOn("int", "nonnegative", "rows"))), "graph.likelihood")
	wantNot(t, "by ref", findings(t, example, binomial(ref("n10"), trialsOn("int", "nonnegative", "rows"))), "graph.likelihood")
	wantNot(t, "binary", findings(t, example, binomial(expr("trials"), trialsOn("int", "binary", "rows"))), "graph.likelihood")
	for _, c := range []struct {
		what, reason string
		n            any
		trials       func(m map[string]any)
		extra        string
	}{
		{"a literal", "arg_not_data", expr("10"), nil, ""},
		{"an expression", "arg_not_data", expr("trials + 1"), trialsOn("int", "nonnegative", "rows"), ""},
		{"a parameter", "arg_not_data", expr("beta"), nil, ""},
		{"a real", "arg_type", expr("trials"), trialsOn("real", "", "rows"), ""},
		{"an integer of any sign", "arg_type", expr("trials"), trialsOn("int", "any", "rows"), ""},
		{"a group plate", "arg_plate", expr("trials"), trialsOn("int", "nonnegative", "p1"), ""},
	} {
		fs := findings(t, example, binomial(c.n, c.trials))
		if f := want(t, c.what, fs, "graph.likelihood", "n9", "args.N"); f.Args["reason"] != c.reason {
			t.Errorf("%s: %s", c.what, args(f))
		}
	}
}

func TestSelfPredictor(t *testing.T) {
	// the observed value in its own mean: directly, or through a transformed node
	fs := findings(t, ar2, func(m map[string]any) {
		nodeByID(m, "l1")["args"].(map[string]any)["mu"] = expr("mean_sales + sales")
	})
	if f := want(t, "direct", fs, "graph.self_predictor", "l1", "args.mu"); f.Args["observed"] != "d2" || f.Args["through"] != nil {
		t.Errorf("direct: %s", args(f))
	}
	fs = findings(t, ar2, func(m map[string]any) {
		nodeByID(m, "t1")["value"] = expr("c + phi1 * sales")
	})
	if f := want(t, "through", fs, "graph.self_predictor", "l1", "args.mu"); f.Args["through"] != "t1" {
		t.Errorf("through: %s", args(f))
	}
	// two steps, and a linear predictor's term
	fs = findings(t, ar2, func(m map[string]any) {
		nodeByID(m, "t1")["value"] = expr("c + lp")
		addNodes(m, map[string]any{"id": "p1", "kind": "linear_predictor", "name": "lp", "label": "LP", "plate": "rows",
			"terms": []any{map[string]any{"data": "d2", "coef": expr("phi1")}}})
	})
	if f := want(t, "term", fs, "graph.self_predictor", "l1", "args.mu"); f.Args["through"] != "t1" {
		t.Errorf("term: %s", args(f))
	}
	// beside: with lag, even through a transformed node, it is the previous row
	wantNot(t, "lag", findings(t, ar2, nil), "graph.self_predictor")
	wantNot(t, "lag through a node", findings(t, ar2, func(m map[string]any) {
		nodeByID(m, "t1")["value"] = expr("c + phi1 * lag(doubled, 1)")
		addNodes(m, transformed("t2", "doubled", "2 * sales", "rows"))
	}), "graph.self_predictor")
	// another data node is not the observed one
	wantNot(t, "another data", findings(t, example, nil), "graph.self_predictor")
}

func TestCycle(t *testing.T) {
	// two transformed nodes that read each other, a node that reads itself through lag, three in a ring
	fs := findings(t, ar2, func(m map[string]any) {
		addNodes(m, transformed("t2", "tb", "tc", "rows"), transformed("t3", "tc", "td", "rows"), transformed("t4", "td", "tb", "rows"))
	})
	f := want(t, "ring", fs, "graph.cycle", "t2", "value")
	if !reflect.DeepEqual(f.Args["names"], []string{"tb", "tc", "td"}) || !reflect.DeepEqual(f.Args["cycle"], []string{"t2", "t3", "t4"}) {
		t.Errorf("ring: %s", args(f))
	}
	if countCode(fs, "graph.cycle") != 1 {
		t.Errorf("one finding per ring: %s", codes(fs))
	}
	fs = findings(t, ar2, func(m map[string]any) {
		nodeByID(m, "t1")["value"] = expr("c + lag(mean_sales, 1)")
	})
	f = want(t, "lag of itself", fs, "graph.cycle", "t1", "value")
	if !reflect.DeepEqual(f.Args["names"], []string{"mean_sales"}) {
		t.Errorf("lag of itself: %s", args(f))
	}
	fs = findings(t, ar2, func(m map[string]any) { nodeByID(m, "t1")["value"] = expr("c + mean_sales") })
	want(t, "itself", fs, "graph.cycle", "t1", "value")
	// parameters that are each other's prior
	fs = findings(t, regression, func(m map[string]any) {
		nodeByID(m, "m2")["prior"] = normalPrior(expr("0"), expr("exp(b_rooms)"))
		nodeByID(m, "m3")["prior"] = normalPrior(expr("0"), expr("exp(b_area)"))
	})
	f = want(t, "parameters", fs, "graph.cycle", "m2", "prior.sigma")
	if !reflect.DeepEqual(f.Args["names"], []string{"b_area", "b_rooms"}) {
		t.Errorf("parameters: %s", args(f))
	}
	// a cycle through a component of correlated effects: the slot is the component's
	fs = findings(t, logistic, func(m map[string]any) { nodeByID(m, "m3")["prior"] = normalPrior(expr("0"), ref("e1.c1")) })
	f = want(t, "component", fs, "graph.cycle", "e1.c1", "scale")
	if !reflect.DeepEqual(f.Args["cycle"], []string{"e1", "m3"}) || !reflect.DeepEqual(f.Args["names"], []string{"School effects", "tau_a"}) {
		t.Errorf("component: %s", args(f))
	}
	// beside: lag of an observed value, a chain, a state read by a lag
	wantNot(t, "ar2", findings(t, ar2, nil), "graph.cycle")
	wantNot(t, "the four families", findings(t, locallevel, nil), "graph.cycle")
	wantNot(t, "lag of data in a chain", findings(t, ar2, func(m map[string]any) {
		addNodes(m, transformed("t2", "tb", "lag(mean_sales, 1)", "rows"))
	}), "graph.cycle")
}

func TestUnusedData(t *testing.T) {
	extra := func(m map[string]any) { addNodes(m, dataNode("n10", "extra", "real", "rows")) }
	fs := findings(t, example, extra)
	f := want(t, "unused", fs, "graph.unused_data", "n10", "")
	if f.Severity != Error {
		t.Errorf("severity: %+v", f)
	}
	// beside: read by a slot, by lag, as an index, observed, a plate's key, a series' time, a term's data
	wantNot(t, "read", findings(t, example, func(m map[string]any) {
		extra(m)
		nodeByID(m, "n9")["args"].(map[string]any)["mu"] = expr("alpha_school[school] + beta * hours + extra")
	}), "graph.unused_data")
	want(t, "lag beside", findings(t, ar2, func(m map[string]any) { addNodes(m, dataNode("n10", "extra", "real", "rows")) }), "graph.unused_data", "n10", "")
	fs = findings(t, ar2, func(m map[string]any) {
		addNodes(m, dataNode("n10", "extra", "real", "rows"))
		nodeByID(m, "t1")["value"] = expr("c + lag(extra, 1) + phi1 * lag(sales, 1)")
	})
	wantNot(t, "lag of data", fs, "graph.unused_data")
	wantNot(t, "a term's data", findings(t, regression, nil), "graph.unused_data")
	wantNot(t, "the example's key and the observed", findings(t, example, nil), "graph.unused_data")
	want(t, "a term removed", findings(t, regression, func(m map[string]any) {
		nodeByID(m, "p1")["terms"] = nodeByID(m, "p1")["terms"].([]any)[:1]
	}), "graph.unused_data", "d3", "")
	want(t, "a series' time not used", findings(t, ar2, func(m map[string]any) { plate(m, 0)["series"] = nil }), "graph.unused_data", "d1", "")
}

func TestUnusedParameter(t *testing.T) {
	fs := findings(t, example, func(m map[string]any) { addNodes(m, param("n10", "extra", nil)) })
	f := want(t, "unused", fs, "graph.unused_parameter", "n10", "")
	if f.Severity != Warning {
		t.Errorf("severity: %+v", f)
	}
	// beside: a hyperparameter that only feeds a prior is on the path; so is a parameter read through transformed nodes
	wantNot(t, "hyperparameter", findings(t, example, nil), "graph.unused_parameter")
	wantNot(t, "through a transformed", findings(t, example, func(m map[string]any) {
		addNodes(m, param("n10", "extra", nil), transformed("t1", "doubled", "2 * extra", nil))
		nodeByID(m, "n9")["args"].(map[string]any)["mu"] = expr("alpha_school[school] + beta * hours + doubled")
	}), "graph.unused_parameter")
	// only used by an unused node
	want(t, "used by a dangling transformed", findings(t, example, func(m map[string]any) {
		addNodes(m, param("n10", "extra", nil), transformed("t1", "doubled", "2 * extra", nil))
	}), "graph.unused_parameter", "n10", "")
	// a component's hyperparameter, a state's scale
	wantNot(t, "effects", findings(t, logistic, nil), "graph.unused_parameter")
	wantNot(t, "state", findings(t, locallevel, nil), "graph.unused_parameter")
}

func TestFormCentred(t *testing.T) {
	// the example's alpha_school (n6) is on a non-centred plate with a normal prior and no bounds: it stays non-centred
	v := validate(mustDecode(t, example))
	wantNot(t, "example", v.out, "graph.form_centred")
	if !v.params["n6"].NonCentred || v.params["n4"].NonCentred {
		t.Errorf("params: %+v %+v", v.params["n6"], v.params["n4"])
	}
	// bounds
	fs := findings(t, example, func(m map[string]any) { nodeByID(m, "n6")["bounds"] = map[string]any{"lower": 0} })
	f := want(t, "bounds", fs, "graph.form_centred", "n6", "bounds")
	if f.Severity != Warning || f.Args["reason"] != "bounded" || f.Args["plate"] != "p1" {
		t.Errorf("bounds: %+v", f)
	}
	if validate(edit2(t, example, func(m map[string]any) { nodeByID(m, "n6")["bounds"] = map[string]any{"lower": 0} })).params["n6"].NonCentred {
		t.Error("a bounded parameter is centred")
	}
	// a prior that is not location-scale
	fs = findings(t, example, func(m map[string]any) {
		nodeByID(m, "n6")["prior"] = map[string]any{"dist": "gamma", "args": map[string]any{"alpha": expr("2"), "beta": expr("1")}}
	})
	f = want(t, "gamma", fs, "graph.form_centred", "n6", "prior")
	if f.Args["reason"] != "not_location_scale" || f.Args["dist"] != "gamma" {
		t.Errorf("gamma: %+v", f)
	}
	// both: two findings
	fs = findings(t, example, func(m map[string]any) {
		nodeByID(m, "n6")["prior"] = map[string]any{"dist": "gamma", "args": map[string]any{"alpha": expr("2"), "beta": expr("1")}}
		nodeByID(m, "n6")["bounds"] = map[string]any{"lower": 0}
	})
	if countCode(fs, "graph.form_centred") != 2 {
		t.Errorf("both: %s", codes(fs))
	}
	// beside: a centred plate asks for nothing; a scalar parameter is not on a plate; student_t, cauchy are location-scale
	for _, edit := range []func(m map[string]any){
		func(m map[string]any) {
			plate(m, 1)["form"] = "centred"
			nodeByID(m, "n6")["bounds"] = map[string]any{"lower": 0}
		},
		func(m map[string]any) { nodeByID(m, "n4")["bounds"] = map[string]any{"lower": 0} },
		func(m map[string]any) {
			nodeByID(m, "n6")["prior"] = map[string]any{"dist": "student_t", "args": map[string]any{"nu": expr("4"), "mu": expr("mu"), "sigma": expr("tau")}}
		},
		func(m map[string]any) { delete(plate(m, 1), "form") }, // the default is non-centred: the same as the example
	} {
		wantNot(t, "beside", findings(t, example, edit), "graph.form_centred")
	}
	// the default form asks too
	fs = findings(t, example, func(m map[string]any) {
		delete(plate(m, 1), "form")
		nodeByID(m, "n6")["bounds"] = map[string]any{"upper": 5}
	})
	want(t, "default form", fs, "graph.form_centred", "n6", "bounds")
	// a parameter on a levels plate has no form
	wantNot(t, "levels", Validate(mustDecode(t, nested)), "graph.form_centred")
}

func edit2(t *testing.T, src string, fn func(m map[string]any)) *Graph { return edit(t, src, fn) }

func TestFormCentredOfAState(t *testing.T) {
	gamma := func(m map[string]any) {
		nodeByID(m, "s1")["init"] = map[string]any{"dist": "gamma", "args": map[string]any{"alpha": expr("2"), "beta": expr("1")}}
	}
	f := want(t, "gamma init", findings(t, locallevel, gamma), "graph.form_centred", "s1", "init")
	if f.Args["reason"] != "not_location_scale" || f.Args["dist"] != "gamma" {
		t.Errorf("gamma init: %+v", f)
	}
	wantNot(t, "centred", findings(t, locallevel, func(m map[string]any) { gamma(m); nodeByID(m, "s1")["form"] = "centred" }), "graph.form_centred")
	wantNot(t, "normal init", findings(t, locallevel, nil), "graph.form_centred")
}

func TestEffects(t *testing.T) {
	comp := func(id string) map[string]any {
		return map[string]any{"id": id, "label": id, "mean": expr("mu_a"), "scale": expr("tau_a")}
	}
	set := func(cs ...map[string]any) func(m map[string]any) {
		return func(m map[string]any) {
			l := make([]any, len(cs))
			for i, c := range cs {
				l[i] = c
			}
			nodeByID(m, "e1")["components"] = l
		}
	}
	f := want(t, "one", findings(t, logistic, set(comp("c1"))), "graph.effects", "e1", "components")
	if f.Args["reason"] != "count" || f.Args["got"] != 1 || f.Args["min"] != 2 {
		t.Errorf("one: %s", args(f))
	}
	want(t, "none", findings(t, logistic, set()), "graph.effects", "e1", "components")
	max := bayescatalog.GetLimits().MaxEffectComponents
	var many []map[string]any
	for i := 0; i <= max; i++ {
		many = append(many, comp(fmt.Sprintf("c%d", i+1)))
	}
	f = want(t, "too many", findings(t, logistic, set(many...)), "graph.effects", "e1", "components")
	if f.Args["got"] != max+1 || f.Args["max"] != max {
		t.Errorf("too many: %s", args(f))
	}
	// beside: 2, the maximum, and components without a name
	wantNot(t, "two", findings(t, logistic, nil), "graph.effects")
	wantNot(t, "the maximum", findings(t, logistic, set(many[:max]...)), "graph.effects")
	fs := findings(t, logistic, set(comp("c1"), comp("c2")))
	wantNot(t, "unnamed", fs, "graph.effects")
	wantNot(t, "unnamed", fs, "graph.name")
	// the correlation
	want(t, "no correlation", findings(t, logistic, func(m map[string]any) { delete(nodeByID(m, "e1"), "correlation") }), "graph.effects", "e1", "correlation")
}

func TestState(t *testing.T) {
	// rows that are not a series
	f := want(t, "not series", findings(t, locallevel, func(m map[string]any) { plate(m, 0)["series"] = nil }), "graph.state", "s1", "plate")
	if f.Args["reason"] != "not_series" {
		t.Errorf("not series: %s", args(f))
	}
	wantNot(t, "series", findings(t, locallevel, nil), "graph.state")
	s := func(k string, v any) func(m map[string]any) {
		return func(m map[string]any) { nodeByID(m, "s1")[k] = v }
	}
	for _, c := range []struct {
		what, slot, reason string
		fn                 func(m map[string]any)
	}{
		{"process", "process", "process", s("process", "ar1")},
		{"no process", "process", "process", s("process", "")},
		{"form", "form", "form", s("form", "sideways")},
		{"no init", "init", "missing", func(m map[string]any) { delete(nodeByID(m, "s1"), "init") }},
		{"unknown init", "init", "unknown_dist", s("init", map[string]any{"dist": "nope", "args": map[string]any{}})},
		{"internal init", "init", "not_prior", s("init", map[string]any{"dist": "std_normal", "args": map[string]any{}})},
		{"likelihood init", "init", "not_prior", s("init", map[string]any{"dist": "poisson_log", "args": map[string]any{}})},
		{"lkj init", "init", "effects_only", s("init", map[string]any{"dist": "lkj_corr_cholesky", "args": map[string]any{"eta": expr("2")}})},
		{"missing arg", "init.sigma", "missing_arg", s("init", map[string]any{"dist": "normal", "args": map[string]any{"mu": expr("0")}})},
		{"unknown arg", "init.k", "unknown_arg", s("init", map[string]any{"dist": "normal", "args": map[string]any{"mu": expr("0"), "sigma": expr("1"), "k": expr("1")}})},
	} {
		fs := findings(t, locallevel, c.fn)
		if f := want(t, c.what, fs, "graph.state", "s1", c.slot); f.Args["reason"] != c.reason {
			t.Errorf("%s: %s", c.what, args(f))
		}
	}
	// beside: the form may be absent, and either of the two
	for _, form := range []any{nil, "centred", "non_centred"} {
		wantNot(t, fmt.Sprint(form), findings(t, locallevel, s("form", form)), "graph.state")
	}
}

func TestTerms(t *testing.T) {
	term := func(data string, coef map[string]any) map[string]any {
		return map[string]any{"data": data, "coef": coef}
	}
	terms := func(ts ...map[string]any) func(m map[string]any) {
		return func(m map[string]any) {
			l := make([]any, len(ts))
			for i, x := range ts {
				l[i] = x
			}
			nodeByID(m, "p1")["terms"] = l
		}
	}
	// beside first: the regression's terms are clean
	wantNot(t, "regression", findings(t, regression, nil), "graph.terms")
	for _, c := range []struct {
		what, slot, reason string
		fn                 func(m map[string]any)
	}{
		{"unknown data", "terms[0].data", "not_data", terms(term("zz", expr("b_area")))},
		{"not data", "terms[0].data", "not_data", terms(term("m2", expr("b_area")))},
		{"twice", "terms[0].data", "duplicate", terms(term("d2", expr("b_area")), term("d2", expr("b_rooms")), term("d3", ref("m3")))},
		{"a time", "terms[0].data", "data_type", func(m map[string]any) {
			addNodes(m, dataNode("d4", "t", "time", "rows"))
			terms(term("d4", expr("b_area")), term("d2", expr("b_area")), term("d3", expr("b_rooms")))(m)
		}},
		{"a coefficient that is not a parameter", "terms[0].coef", "coef_not_parameter", terms(term("d2", expr("2 * b_area")), term("d3", expr("b_rooms")))},
		{"a number", "terms[0].coef", "coef_not_parameter", terms(term("d2", expr("3")), term("d3", expr("b_rooms")))},
		{"a data node", "terms[0].coef", "coef_not_parameter", terms(term("d2", expr("rooms")), term("d3", expr("b_rooms")))},
		{"a coefficient over a plate", "terms[0].coef", "coef_plate", func(m map[string]any) {
			m["plates"] = append(m["plates"].([]any), map[string]any{"id": "p9", "kind": "group", "label": "G", "key": "d9"})
			addNodes(m, dataNode("d9", "g", "group_index", "rows"), param("m9", "b_group", "p9"))
			nodeByID(m, "d9")["target"] = "p9"
			terms(term("d2", ref("m9")), term("d3", expr("b_rooms")))(m)
		}},
	} {
		fs := findings(t, regression, c.fn)
		var got []Finding
		for _, f := range fs {
			if f.Code == "graph.terms" && f.Slot == c.slot {
				got = append(got, f)
			}
		}
		if len(got) == 0 || got[0].Node != "p1" || got[0].Args["reason"] != c.reason {
			t.Errorf("%s: %s", c.what, codes(fs))
		}
	}
	// the plate of the data
	fs := findings(t, regression, func(m map[string]any) { nodeByID(m, "p1")["plate"] = "rows"; nodeByID(m, "d3")["plate"] = nil })
	if f := want(t, "plate", fs, "graph.terms", "p1", "terms[1].data"); f.Args["reason"] != "plate" {
		t.Errorf("plate: %s", args(f))
	}
	// a coefficient that is a scalar parameter by ref or by name is the same
	wantNot(t, "ref and name", findings(t, regression, terms(term("d2", ref("m2")), term("d3", expr("b_rooms")))), "graph.terms")
}

func TestTermsOfACategory(t *testing.T) {
	cat := func(coef map[string]any, plate any) func(m map[string]any) {
		return func(m map[string]any) {
			addNodes(m, dataNode("d4", "kind", "category", "rows"), param("m5", "b_kind", plate))
			l := nodeByID(m, "p1")["terms"].([]any)
			nodeByID(m, "p1")["terms"] = append(l, map[string]any{"data": "d4", "coef": coef})
		}
	}
	// a parameter on the category's levels plate, by name and by ref, is the way
	fs := findings(t, regression, cat(expr("b_kind"), "levels:d4"))
	wantNot(t, "name", fs, "graph.terms")
	wantNot(t, "name", fs, "graph.slot_shape")
	wantNot(t, "ref", findings(t, regression, cat(ref("m5"), "levels:d4")), "graph.terms")
	// a scalar parameter is not
	f := want(t, "scalar", findings(t, regression, cat(expr("b_kind"), nil)), "graph.terms", "p1", "terms[2].coef")
	if f.Args["reason"] != "coef_plate" || f.Args["need"] != "levels" {
		t.Errorf("scalar: %s", args(f))
	}
	// and the levels plate of another category is not
	fs = findings(t, regression, func(m map[string]any) {
		addNodes(m, dataNode("d5", "other", "category", "rows"))
		cat(ref("m5"), "levels:d5")(m)
	})
	want(t, "other levels", fs, "graph.terms", "p1", "terms[2].coef")
	// a real predictor with a coefficient on a levels plate
	fs = findings(t, regression, func(m map[string]any) {
		addNodes(m, dataNode("d4", "kind", "category", "rows"), param("m5", "b_kind", "levels:d4"))
		nodeByID(m, "p1")["terms"] = []any{map[string]any{"data": "d2", "coef": ref("m5")}, map[string]any{"data": "d3", "coef": expr("b_rooms")}}
	})
	if f := want(t, "levels for a real", fs, "graph.terms", "p1", "terms[0].coef"); f.Args["need"] != "scalar" {
		t.Errorf("levels for a real: %s", args(f))
	}
}

func TestPlateEmpty(t *testing.T) {
	addPlate := func(on bool) func(m map[string]any) {
		return func(m map[string]any) {
			m["plates"] = append(m["plates"].([]any), map[string]any{"id": "p2", "kind": "group", "label": "District", "key": "n10"})
			n := dataNode("n10", "district", "group_index", "rows")
			n["target"] = "p2"
			addNodes(m, n)
			if on {
				addNodes(m, param("n11", "mu_district", "p2"))
			}
		}
	}
	// the key data node is not on the plate: nothing is
	fs := findings(t, example, addPlate(false))
	f := want(t, "empty", fs, "graph.plate_empty", "p2", "")
	if f.Severity != Warning {
		t.Errorf("empty: %+v", f)
	}
	wantNot(t, "p1 has nodes", fs[:0], "graph.plate_empty")
	if countCode(fs, "graph.plate_empty") != 1 {
		t.Errorf("only the empty one: %s", codes(fs))
	}
	wantNot(t, "a parameter on it", findings(t, example, addPlate(true)), "graph.plate_empty")
	wantNot(t, "example", findings(t, example, nil), "graph.plate_empty")
}

func TestPlatesCountTheLevels(t *testing.T) {
	max := bayescatalog.GetLimits().MaxPlates
	categories := func(n int) func(m map[string]any) {
		return func(m map[string]any) {
			for i := 0; i < n; i++ {
				addNodes(m, dataNode(fmt.Sprintf("c%d", i+1), "", "category", "rows"))
			}
		}
	}
	// the example has 2 plates: with max-2 categories the total is the maximum, one more is over it
	wantNot(t, "at the maximum", findings(t, example, categories(max-2)), "graph.too_big")
	fs := findings(t, example, categories(max-1))
	f := want(t, "over", fs, "graph.too_big", "", "")
	if f.Args["what"] != "plates" || f.Args["got"] != max+1 || f.Args["max"] != max {
		t.Errorf("over: %s", args(f))
	}
	// stored plates alone over the limit are CheckStructure's, once
	g := mustDecode(t, example)
	for i := 0; i < max; i++ {
		g.Plates = append(g.Plates, Plate{ID: fmt.Sprintf("q%d", i), Kind: kindGrp, Label: "Q", Key: "n3"})
	}
	g.Nodes = append(g.Nodes, Node{ID: "c1", Kind: "data", Label: "C", Plate: ptr("rows"), Type: "category"})
	if n := countCode(Validate(g), "graph.too_big"); n != 1 {
		t.Errorf("one too_big: %d", n)
	}
}

func TestM2FindingsAreDeterministic(t *testing.T) {
	for _, src := range []string{regression, logistic, ar2, locallevel, nested} {
		g := mustDecode(t, src)
		a, _ := json.Marshal(Validate(g))
		for i := 0; i < 5; i++ {
			if b, _ := json.Marshal(Validate(g)); string(a) != string(b) {
				t.Fatal("findings differ between runs")
			}
		}
		x, _ := json.Marshal(BuildModel(g).Order)
		y, _ := json.Marshal(BuildModel(g).Order)
		if string(x) != string(y) {
			t.Fatal("order differs between runs")
		}
	}
}
