package bayesgraph

import (
	"math"
	"math/rand"
	"reflect"
	"strings"
	"testing"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/bayescatalog"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/bayesexpr"
)

// validated decodes the example edited by edit and validates it.
func validated(t *testing.T, edit func(m map[string]any)) *validation {
	t.Helper()
	return validate(mustDecode(t, withDoc(t, edit)))
}

func vfind(t *testing.T, edit func(m map[string]any)) []Finding {
	t.Helper()
	return validated(t, edit).out
}

// nodeByID is the example's node with the id, as a generic object.
func nodeByID(m map[string]any, id string) map[string]any {
	for _, n := range m["nodes"].([]any) {
		if o := n.(map[string]any); o["id"] == id {
			return o
		}
	}
	panic("no node " + id)
}

func addNodes(m map[string]any, ns ...map[string]any) {
	for _, n := range ns {
		m["nodes"] = append(m["nodes"].([]any), n)
	}
}

func expr(s string) map[string]any { return map[string]any{"expr": s} }
func ref(s string) map[string]any  { return map[string]any{"ref": s} }

func normalPrior(mu, sigma map[string]any) map[string]any {
	return map[string]any{"dist": "normal", "args": map[string]any{"mu": mu, "sigma": sigma}}
}

// find is the first finding with the code on the node and slot ("" for any slot).
func find(fs []Finding, code, node, slot string) (Finding, bool) {
	for _, f := range fs {
		if f.Code == code && f.Node == node && (slot == "" || f.Slot == slot) {
			return f, true
		}
	}
	return Finding{}, false
}

func want(t *testing.T, what string, fs []Finding, code, node, slot string) Finding {
	t.Helper()
	f, ok := find(fs, code, node, slot)
	if !ok {
		t.Errorf("%s: want %s on %s/%s, got %s", what, code, node, slot, codes(fs))
	}
	return f
}

func wantNot(t *testing.T, what string, fs []Finding, code string) {
	t.Helper()
	if hasCode(fs, code, "") {
		t.Errorf("%s: unexpected %s: %s", what, code, codes(fs))
	}
}

func wantClean(t *testing.T, what string, fs []Finding) {
	t.Helper()
	if len(fs) != 0 {
		t.Errorf("%s: want no finding, got %s", what, codes(fs))
	}
}

// withoutUnused drops the findings about nodes a test's edit left unused, which that test is not about.
func withoutUnused(fs []Finding) []Finding {
	var out []Finding
	for _, f := range fs {
		if f.Code != "graph.unused_data" && f.Code != "graph.unused_parameter" {
			out = append(out, f)
		}
	}
	return out
}

func TestExampleValidatesClean(t *testing.T) {
	v := validated(t, func(map[string]any) {})
	wantClean(t, "example", v.out)
	tau := v.params["n5"]
	if !tau.Truncated || tau.TruncateInProgram {
		t.Errorf("tau (half-normal with constant arguments): %+v", tau)
	}
	if tau.Range.Lo != 0 || !tau.Range.LoOpen || !math.IsInf(tau.Range.Hi, 1) {
		t.Errorf("tau's range: %+v", tau.Range)
	}
	if p := v.params["n4"]; p.Truncated || p.TruncateInProgram || !sameInterval(p.Range, bayesexpr.Unbounded()) {
		t.Errorf("mu: %+v", p)
	}
	if p := v.params["n8"]; p.Truncated || !sameInterval(p.Range, bayesexpr.NamedInterval("positive")) {
		t.Errorf("n8 (exponential): %+v", p)
	}
	if p := v.params["n6"]; p.Truncated || p.TruncateInProgram {
		t.Errorf("alpha_school (normal(mu, tau), no bounds): %+v", p)
	}
	s, ok := v.slots[SlotKey{"n9", "args.mu"}]
	if !ok || s.Context != "rows" || s.Type.String() != "real[rows]" || !reflect.DeepEqual(s.Refs, []string{"n2", "n6", "n7"}) ||
		!reflect.DeepEqual(s.Indices, []string{"n3"}) {
		t.Errorf("the likelihood's mu: %+v", s)
	}
	if s := v.slots[SlotKey{"n9", "args.sigma"}]; s.Ref != "n8" || s.Type.String() != "real" || !reflect.DeepEqual(s.Refs, []string{"n8"}) {
		t.Errorf("the likelihood's sigma: %+v", s)
	}
	if s := v.slots[SlotKey{"n6", "prior.sigma"}]; s.Interval.Within(bayesexpr.NamedInterval("positive")) != bayesexpr.Inside {
		t.Errorf("tau's interval in alpha_school's prior: %+v", s.Interval)
	}
	if s := v.slots[SlotKey{"n4", "prior.sigma"}]; s.Constant == nil || *s.Constant != 10 {
		t.Errorf("a constant slot: %+v", s)
	}
}

func TestStructureFirstAndGate(t *testing.T) {
	// a duplicate id: the ids cannot be trusted, only the structure's findings
	fs := vfind(t, func(m map[string]any) {
		node(m, 1)["id"] = "n1"
		nodeByID(m, "n4")["prior"] = normalPrior(expr("0"), expr("-1"))
	})
	want(t, "duplicate id", fs, "graph.id", "n1", "")
	wantNot(t, "duplicate id", fs, "graph.slot_interval")
	// beside: a structural finding that is not about ids still lets the slots be checked
	fs = vfind(t, func(m map[string]any) {
		node(m, 0)["label"] = ""
		nodeByID(m, "n4")["prior"] = normalPrior(expr("0"), expr("-1"))
	})
	want(t, "label", fs, "graph.label", "n1", "label")
	want(t, "label and a slot", fs, "graph.slot_interval", "n4", "prior.sigma")
}

func TestNodePlate(t *testing.T) {
	cases := []struct {
		what  string
		edit  func(m map[string]any)
		node  string
		trips bool
	}{
		{"parameter on rows", func(m map[string]any) { nodeByID(m, "n7")["plate"] = "rows" }, "n7", true},
		{"parameter on a group", func(m map[string]any) { nodeByID(m, "n7")["plate"] = "p1" }, "n7", false},
		{"unknown plate", func(m map[string]any) { nodeByID(m, "n7")["plate"] = "p9" }, "n7", true},
		{"data on no plate", func(m map[string]any) { nodeByID(m, "n2")["plate"] = nil }, "n2", true},
		{"real data on a group", func(m map[string]any) { nodeByID(m, "n2")["plate"] = "p1" }, "n2", false},
		{"likelihood off its observed plate", func(m map[string]any) { nodeByID(m, "n9")["plate"] = "p1" }, "n9", true},
		{"likelihood on its observed plate", func(m map[string]any) { nodeByID(m, "n9")["plate"] = "rows" }, "n9", false},
		{"transformed on a levels plate", func(m map[string]any) {
			addNodes(m, map[string]any{"id": "c1", "kind": "data", "name": "region", "label": "Region", "plate": "rows", "type": "category"},
				map[string]any{"id": "t1", "kind": "transformed", "label": "T", "plate": "levels:c1", "value": expr("1")})
		}, "t1", true},
		{"parameter on a levels plate", func(m map[string]any) {
			addNodes(m, map[string]any{"id": "c1", "kind": "data", "name": "region", "label": "Region", "plate": "rows", "type": "category"},
				map[string]any{"id": "t1", "kind": "parameter", "label": "T", "plate": "levels:c1", "prior": normalPrior(expr("0"), expr("1"))})
		}, "t1", false},
		{"correlated effects on no plate", func(m map[string]any) { addNodes(m, effects(nil, "2")) }, "e1", true},
		{"correlated effects on a group", func(m map[string]any) { addNodes(m, effects("p1", "2")) }, "e1", false},
		{"state off rows", func(m map[string]any) { addNodes(m, state("p1")) }, "s1", true},
		{"state on rows", func(m map[string]any) { addNodes(m, state("rows")) }, "s1", false},
	}
	for _, c := range cases {
		fs := vfind(t, c.edit)
		_, got := find(fs, "graph.node_plate", c.node, "plate")
		if got != c.trips {
			t.Errorf("%s: node_plate %v, want %v: %s", c.what, got, c.trips, codes(fs))
		}
	}
}

func effects(plate any, eta string) map[string]any {
	return map[string]any{"id": "e1", "kind": "correlated_effects", "label": "Effects", "plate": plate,
		"components": []any{
			map[string]any{"id": "c1", "name": "icpt", "label": "Intercept", "mean": expr("0"), "scale": expr("tau")},
			map[string]any{"id": "c2", "name": "slope", "label": "Slope", "mean": expr("0"), "scale": ref("n8")},
		}, "correlation": map[string]any{"name": "omega", "label": "Correlation", "eta": expr(eta)}}
}

func state(plate any) map[string]any {
	return map[string]any{"id": "s1", "kind": "state", "label": "Level", "plate": plate, "process": "random_walk", "form": "centred",
		"init": normalPrior(expr("0"), expr("1")), "scale": expr("tau")}
}

func TestDataType(t *testing.T) {
	trip := []struct {
		what string
		edit func(m map[string]any)
		slot string
	}{
		{"unknown type", func(m map[string]any) { nodeByID(m, "n2")["type"] = "float" }, "type"},
		{"int without support", func(m map[string]any) { nodeByID(m, "n2")["type"] = "int"; delete(nodeByID(m, "n2"), "standardize") }, "support"},
		{"unknown support", func(m map[string]any) {
			nodeByID(m, "n2")["type"] = "int"
			nodeByID(m, "n2")["support"] = "positive"
			delete(nodeByID(m, "n2"), "standardize")
		}, "support"},
		{"support on real", func(m map[string]any) { nodeByID(m, "n2")["support"] = "any" }, "support"},
		{"target on real", func(m map[string]any) { nodeByID(m, "n2")["target"] = "p1" }, "target"},
		{"time on a group", func(m map[string]any) {
			addNodes(m, map[string]any{"id": "d1", "kind": "data", "label": "T", "plate": "p1", "type": "time"})
		}, "plate"},
	}
	for _, c := range trip {
		fs := vfind(t, c.edit)
		node := "n2"
		if c.slot == "plate" {
			node = "d1"
		}
		want(t, c.what, fs, "graph.data_type", node, c.slot)
	}
	beside := []struct {
		what string
		edit func(m map[string]any)
	}{
		{"int with support", func(m map[string]any) {
			nodeByID(m, "n2")["type"] = "int"
			nodeByID(m, "n2")["support"] = "nonnegative"
			delete(nodeByID(m, "n2"), "standardize")
		}},
		{"time on rows", func(m map[string]any) {
			addNodes(m, map[string]any{"id": "d1", "kind": "data", "label": "T", "plate": "rows", "type": "time"})
		}},
		{"int data on a group", func(m map[string]any) {
			addNodes(m, map[string]any{"id": "d1", "kind": "data", "label": "Size", "plate": "p1", "type": "int", "support": "binary"})
		}},
	}
	for _, c := range beside {
		wantNot(t, c.what, vfind(t, c.edit), "graph.data_type")
	}
}

func TestStandardize(t *testing.T) {
	fs := vfind(t, func(m map[string]any) { nodeByID(m, "n1")["standardize"] = true })
	if f := want(t, "observed", fs, "graph.standardize", "n1", "standardize"); f.Args["likelihood"] != "n9" {
		t.Errorf("observed: %+v", f)
	}
	fs = vfind(t, func(m map[string]any) {
		nodeByID(m, "n2")["type"] = "int"
		nodeByID(m, "n2")["support"] = "any"
	})
	want(t, "int", fs, "graph.standardize", "n2", "standardize")
	// beside: the example's hours is a standardized predictor
	wantNot(t, "predictor", vfind(t, func(map[string]any) {}), "graph.standardize")
}

func TestPrior(t *testing.T) {
	cases := []struct {
		what, slot, reason string
		prior              any
	}{
		{"missing", "prior", "missing", nil},
		{"unknown", "prior", "unknown_dist", map[string]any{"dist": "laplace", "args": map[string]any{}}},
		{"lkj outside effects", "prior", "effects_only", map[string]any{"dist": "lkj_corr_cholesky", "args": map[string]any{"eta": expr("2")}}},
		{"a likelihood", "prior", "not_prior", map[string]any{"dist": "poisson_log", "args": map[string]any{"alpha": expr("0")}}},
		{"internal", "prior", "not_prior", map[string]any{"dist": "std_normal", "args": map[string]any{}}},
		{"missing argument", "prior.sigma", "missing_arg", map[string]any{"dist": "normal", "args": map[string]any{"mu": expr("0")}}},
		{"unknown argument", "prior.scale", "unknown_arg", map[string]any{"dist": "normal", "args": map[string]any{"mu": expr("0"), "sigma": expr("1"), "scale": expr("1")}}},
	}
	for _, c := range cases {
		fs := vfind(t, func(m map[string]any) {
			if c.prior == nil {
				delete(nodeByID(m, "n7"), "prior")
			} else {
				nodeByID(m, "n7")["prior"] = c.prior
			}
		})
		if f := want(t, c.what, fs, "graph.prior", "n7", c.slot); f.Args["reason"] != c.reason {
			t.Errorf("%s: reason %v, want %s", c.what, f.Args["reason"], c.reason)
		}
		if _, ok := validated(t, func(m map[string]any) { delete(nodeByID(m, "n7"), "prior") }).params["n7"]; ok {
			t.Errorf("%s: a Param despite graph.prior", c.what)
		}
	}
	// beside: every prior of the catalogue, with its arguments
	for _, d := range bayescatalog.Declared().Distributions {
		if !d.HasUse(bayescatalog.Prior) || d.Support == bayescatalog.CholeskyCorrelation {
			continue
		}
		args := map[string]any{}
		for i, p := range d.Params {
			args[p.Name] = expr([]string{"1", "2"}[i%2])
		}
		fs := vfind(t, func(m map[string]any) { nodeByID(m, "n7")["prior"] = map[string]any{"dist": d.ID, "args": args} })
		wantClean(t, d.ID, fs)
	}
}

func TestBoundsAndTruncation(t *testing.T) {
	bounded := func(prior any, bounds map[string]any) func(m map[string]any) {
		return func(m map[string]any) {
			n := nodeByID(m, "n7")
			n["prior"] = prior
			n["bounds"] = bounds
		}
	}
	reasons := []struct {
		what, slot, reason string
		edit               func(m map[string]any)
	}{
		{"too large", "bounds.lower", "too_large", bounded(normalPrior(expr("0"), expr("1")), map[string]any{"lower": 1e16})},
		{"not ordered", "bounds", "order", bounded(normalPrior(expr("0"), expr("1")), map[string]any{"lower": 1, "upper": 1})},
		{"empty", "bounds", "empty", bounded(map[string]any{"dist": "exponential", "args": map[string]any{"beta": expr("1")}}, map[string]any{"upper": 0})},
		{"empty unit", "bounds", "empty", bounded(map[string]any{"dist": "beta", "args": map[string]any{"alpha": expr("1"), "beta": expr("1")}}, map[string]any{"lower": 1})},
		{"empty uniform", "bounds", "empty", bounded(map[string]any{"dist": "uniform", "args": map[string]any{"alpha": expr("0"), "beta": expr("1")}}, map[string]any{"upper": -1})},
	}
	for _, c := range reasons {
		v := validated(t, c.edit)
		if f := want(t, c.what, v.out, "graph.bounds", "n7", c.slot); f.Args["reason"] != c.reason {
			t.Errorf("%s: reason %v, want %s", c.what, f.Args["reason"], c.reason)
		}
		if _, ok := v.params["n7"]; ok {
			t.Errorf("%s: a Param despite graph.bounds", c.what)
		}
	}
	// a bound that cannot be decoded from JSON: not finite
	g := mustDecode(t, example)
	inf := math.Inf(1)
	g.Nodes[6].Bounds = &Bounds{Upper: &inf}
	want(t, "infinite", Validate(g), "graph.bounds", "n7", "bounds.upper")
	nan := math.NaN()
	g.Nodes[6].Bounds = &Bounds{Lower: &nan}
	want(t, "NaN", Validate(g), "graph.bounds", "n7", "bounds.lower")

	flags := []struct {
		what               string
		edit               func(m map[string]any)
		truncated, inProgr bool
		lo, hi             float64
	}{
		{"half-normal, constant arguments", bounded(normalPrior(expr("0"), expr("1")), map[string]any{"lower": 0}), true, false, 0, math.Inf(1)},
		{"half-normal, a hyperprior's scale", bounded(normalPrior(expr("0"), expr("tau")), map[string]any{"lower": 0}), true, true, 0, math.Inf(1)},
		{"a ref is not a constant", bounded(normalPrior(expr("0"), ref("n8")), map[string]any{"upper": 3}), true, true, math.Inf(-1), 3},
		{"-1 is a constant", bounded(normalPrior(expr("-1"), expr("2")), map[string]any{"lower": -5, "upper": 5}), true, false, -5, 5},
		{"2 * 3 is not", bounded(normalPrior(expr("2 * 3"), expr("2")), map[string]any{"lower": -5, "upper": 5}), true, true, -5, 5},
		{"no bounds, a hyperprior", bounded(normalPrior(expr("mu"), expr("tau")), nil), false, false, math.Inf(-1), math.Inf(1)},
		{"a bound outside the support", bounded(map[string]any{"dist": "gamma", "args": map[string]any{"alpha": expr("2"), "beta": expr("mu")}}, map[string]any{"lower": -5}), false, false, 0, math.Inf(1)},
		{"a bound at the support's end", bounded(map[string]any{"dist": "beta", "args": map[string]any{"alpha": expr("2"), "beta": expr("tau")}}, map[string]any{"lower": 0, "upper": 1}), false, false, 0, 1},
		{"a uniform's own range", bounded(map[string]any{"dist": "uniform", "args": map[string]any{"alpha": expr("-1"), "beta": expr("3")}}, nil), false, false, -1, 3},
		{"a uniform cut", bounded(map[string]any{"dist": "uniform", "args": map[string]any{"alpha": expr("-1"), "beta": expr("3")}}, map[string]any{"lower": 0}), true, false, 0, 3},
	}
	for _, c := range flags {
		v := validated(t, c.edit)
		p, ok := v.params["n7"]
		if !ok {
			t.Errorf("%s: no Param: %s", c.what, codes(v.out))
			continue
		}
		if p.Truncated != c.truncated || p.TruncateInProgram != c.inProgr || p.Range.Lo != c.lo || p.Range.Hi != c.hi || !p.Range.LoOpen || !p.Range.HiOpen {
			t.Errorf("%s: %+v", c.what, p)
		}
		wantNot(t, c.what, v.out, "graph.bounds")
	}
}

func TestNotTruncatable(t *testing.T) {
	// No prior of the catalogue lacks its CDFs; the rule is checked on one that does (rule 5).
	nb, _ := bayescatalog.LookupDistribution("neg_binomial_2_log")
	nonNeg := bayesexpr.NamedInterval("non_negative")
	up := 10.0
	if _, _, reason := paramRange(nb, nonNeg, &Bounds{Upper: &up}); reason != "not_truncatable" {
		t.Errorf("a bound cutting neg_binomial_2_log: %q", reason)
	}
	lo := -1.0
	if _, tr, reason := paramRange(nb, nonNeg, &Bounds{Lower: &lo}); reason != "" || tr {
		t.Errorf("a bound outside its support: %q %v", reason, tr)
	}
	normal, _ := bayescatalog.LookupDistribution("normal")
	if _, tr, reason := paramRange(normal, bayesexpr.Unbounded(), &Bounds{Upper: &up}); reason != "" || !tr {
		t.Errorf("normal cut: %q %v", reason, tr)
	}
}

func TestUniformArguments(t *testing.T) {
	uniform := func(a, b map[string]any) func(m map[string]any) {
		return func(m map[string]any) {
			nodeByID(m, "n7")["prior"] = map[string]any{"dist": "uniform", "args": map[string]any{"alpha": a, "beta": b}}
		}
	}
	fs := vfind(t, uniform(expr("3"), expr("1")))
	if f := want(t, "not ordered", fs, "graph.slot_interval", "n7", "prior.beta"); f.Args["need"] != "above_alpha" {
		t.Errorf("not ordered: %+v", f)
	}
	fs = vfind(t, uniform(expr("1"), expr("1")))
	want(t, "equal", fs, "graph.slot_interval", "n7", "prior.beta")
	wantClean(t, "ordered", vfind(t, uniform(expr("-3"), expr("-1"))))
	fs = vfind(t, uniform(expr("mu"), expr("1")))
	want(t, "not a constant", fs, "graph.constant", "n7", "prior.alpha")
	fs = vfind(t, uniform(expr("0"), ref("n8")))
	want(t, "a ref", fs, "graph.constant", "n7", "prior.beta")
	wantNot(t, "a ref", fs, "graph.slot_interval")
}

func TestSlot(t *testing.T) {
	cases := []struct {
		what, reason string
		slot         any
	}{
		{"empty", "empty", map[string]any{}},
		{"both", "both", map[string]any{"expr": "1", "ref": "n8"}},
		{"unknown ref", "unknown_ref", ref("n99")},
		{"a ref to an index", "not_number", ref("n3")},
		{"a ref to a likelihood", "not_number", ref("n9")},
		{"a ref to a plate", "unknown_ref", ref("p1")},
	}
	for _, c := range cases {
		fs := vfind(t, func(m map[string]any) { nodeByID(m, "n9")["args"].(map[string]any)["sigma"] = c.slot })
		if f := want(t, c.what, fs, "graph.slot", "n9", "args.sigma"); f.Args["reason"] != c.reason {
			t.Errorf("%s: %+v", c.what, f)
		}
	}
	fs := vfind(t, func(m map[string]any) {
		addNodes(m, map[string]any{"id": "t1", "kind": "transformed", "label": "T"})
	})
	if f := want(t, "missing value", fs, "graph.slot", "t1", "value"); f.Args["reason"] != "missing" {
		t.Errorf("missing value: %+v", f)
	}
	// beside: a ref to a named parameter, to a component, a transformed with its value
	wantClean(t, "ref to a named node", withoutUnused(vfind(t, func(m map[string]any) { nodeByID(m, "n9")["args"].(map[string]any)["sigma"] = ref("n5") })))
	fs = vfind(t, func(m map[string]any) {
		addNodes(m, effects("p1", "2"), map[string]any{"id": "t1", "kind": "transformed", "label": "T", "plate": "p1", "value": ref("e1.c1")})
	})
	wantNot(t, "ref to a component", fs, "graph.slot")
	fs = vfind(t, func(m map[string]any) {
		addNodes(m, effects("p1", "2"), map[string]any{"id": "t1", "kind": "transformed", "label": "T", "plate": "p1", "value": ref("e1.correlation")})
	})
	if f := want(t, "ref to a correlation", fs, "graph.slot", "t1", "value"); f.Args["reason"] != "not_number" {
		t.Errorf("ref to a correlation: %+v", f)
	}
}

func TestExprFindings(t *testing.T) {
	fs := vfind(t, func(m map[string]any) { nodeByID(m, "n9")["args"].(map[string]any)["mu"] = expr("beta * hours +") })
	f := want(t, "syntax", fs, "expr.unexpected", "n9", "args.mu")
	if f.Args["pos"] != 14 || f.Args["len"] != 0 || f.Args["expected"] != "value" {
		t.Errorf("syntax: %+v", f)
	}
	fs = vfind(t, func(m map[string]any) { nodeByID(m, "n9")["args"].(map[string]any)["mu"] = expr("beta * hourz") })
	f = want(t, "unknown name", fs, "expr.unknown_name", "n9", "args.mu")
	if f.Args["name"] != "hourz" || f.Args["pos"] != 7 || f.Args["len"] != 5 {
		t.Errorf("unknown name: %+v", f)
	}
	fs = vfind(t, func(m map[string]any) { nodeByID(m, "n9")["args"].(map[string]any)["mu"] = expr("alpha_school + beta") })
	want(t, "needs index", fs, "expr.needs_index", "n9", "args.mu")
	fs = vfind(t, func(m map[string]any) {
		addNodes(m, effects("p1", "2"), map[string]any{"id": "t1", "kind": "transformed", "label": "T", "plate": "p1", "value": expr("omega + 1")})
	})
	want(t, "a correlation in an expression", fs, "expr.not_number", "t1", "value")
	// beside: the same with the index, the component by name
	wantClean(t, "indexed", withoutUnused(vfind(t, func(m map[string]any) {
		nodeByID(m, "n9")["args"].(map[string]any)["mu"] = expr("alpha_school[school] + beta")
	})))
	fs = vfind(t, func(m map[string]any) {
		addNodes(m, effects("p1", "2"), map[string]any{"id": "t1", "kind": "transformed", "label": "T", "plate": "p1", "value": expr("icpt + 1")})
	})
	wantNot(t, "a component by name", fs, "expr.not_number")
}

func TestTotalExpressionNodes(t *testing.T) {
	// 128 ones and 127 pluses: 255 nodes per expression
	big := strings.TrimSuffix(strings.Repeat("1+", 128), "+")
	with := func(k int) func(m map[string]any) {
		return func(m map[string]any) {
			for i := 0; i < k; i++ {
				addNodes(m, map[string]any{"id": "t" + string(rune('a'+i)), "kind": "transformed", "label": "T", "value": expr(big)})
			}
		}
	}
	max := bayescatalog.GetLimits().MaxTotalExpressionNode
	k := max / 255 // the example has a few nodes more
	fs := vfind(t, with(k+1))
	f := want(t, "over", fs, "graph.too_big", "", "")
	if f.Args["what"] != "expression_nodes" {
		t.Errorf("over: %+v", f)
	}
	wantNot(t, "under", vfind(t, with(k-1)), "graph.too_big")
}

func TestSlotShape(t *testing.T) {
	// a ref cannot be indexed: a per-school value referenced from the rows
	fs := vfind(t, func(m map[string]any) { nodeByID(m, "n9")["args"].(map[string]any)["sigma"] = ref("n6") })
	if f := want(t, "ref over another plate", fs, "graph.slot_shape", "n9", "args.sigma"); f.Args["need"] != "context" || f.Args["plate"] != "p1" || f.Args["context"] != "rows" {
		t.Errorf("ref over another plate: %+v", f)
	}
	// a single value is needed: a component's scale, a state's scale
	fs = vfind(t, func(m map[string]any) {
		e := effects("p1", "2")
		e["components"].([]any)[0].(map[string]any)["scale"] = expr("exp(alpha_school)")
		addNodes(m, e)
	})
	if f := want(t, "component scale over the plate", fs, "graph.slot_shape", "e1.c1", "scale"); f.Args["need"] != "scalar" {
		t.Errorf("component scale: %+v", f)
	}
	fs = vfind(t, func(m map[string]any) {
		s := state("rows")
		s["scale"] = expr("exp(hours)")
		addNodes(m, s)
	})
	want(t, "state scale over rows", fs, "graph.slot_shape", "s1", "scale")
	// beside: a value over the context where one may be; single values where one is needed
	fs = vfind(t, func(m map[string]any) {
		addNodes(m, effects("p1", "2"), state("rows"),
			map[string]any{"id": "t1", "kind": "transformed", "label": "T", "plate": "p1", "value": ref("n6")})
	})
	wantNot(t, "fits", fs, "graph.slot_shape")
}

func TestSlotIntervals(t *testing.T) {
	sigma := func(s map[string]any) func(m map[string]any) {
		return func(m map[string]any) {
			nodeByID(m, "n6")["prior"].(map[string]any)["args"].(map[string]any)["sigma"] = s
		}
	}
	fs := vfind(t, sigma(expr("-1")))
	if f := want(t, "negative", fs, "graph.slot_interval", "n6", "prior.sigma"); f.Severity != Error || f.Args["need"] != "positive" {
		t.Errorf("negative: %+v", f)
	}
	want(t, "zero", vfind(t, sigma(expr("0"))), "graph.slot_interval", "n6", "prior.sigma")
	fs = vfind(t, sigma(expr("tau - 1")))
	if f := want(t, "maybe", fs, "graph.slot_maybe", "n6", "prior.sigma"); f.Severity != Warning {
		t.Errorf("maybe: %+v", f)
	}
	wantNot(t, "maybe", fs, "graph.slot_interval")
	want(t, "a real parameter", vfind(t, sigma(expr("mu"))), "graph.slot_maybe", "n6", "prior.sigma")
	// beside
	for _, s := range []string{"tau", "tau + 1", "exp(mu)", "square(mu) + 0.1", "0.5"} {
		wantClean(t, s, withoutUnused(vfind(t, sigma(expr(s)))))
	}
	wantClean(t, "a ref to a positive parameter", withoutUnused(vfind(t, sigma(ref("n8")))))
	// a likelihood's sigma, a component's scale, a correlation's eta
	fs = vfind(t, func(m map[string]any) { nodeByID(m, "n9")["args"].(map[string]any)["sigma"] = expr("beta") })
	want(t, "likelihood sigma", fs, "graph.slot_maybe", "n9", "args.sigma")
	fs = vfind(t, func(m map[string]any) { addNodes(m, effects("p1", "-2")) })
	want(t, "negative eta", fs, "graph.slot_interval", "e1.correlation", "eta")
	fs = vfind(t, func(m map[string]any) {
		e := effects("p1", "2")
		e["components"].([]any)[0].(map[string]any)["scale"] = expr("mu")
		addNodes(m, e)
	})
	want(t, "component scale", fs, "graph.slot_maybe", "e1.c1", "scale")
	wantClean(t, "clean effects", vfind(t, func(m map[string]any) { addNodes(m, effects("p1", "2")) }))
}

func TestIntegerIntervals(t *testing.T) {
	// binomial's N needs a non-negative integer: an int nonnegative data node is inside, int any overlaps
	lik := func(support string) func(m map[string]any) {
		return func(m map[string]any) {
			addNodes(m,
				map[string]any{"id": "d1", "kind": "data", "name": "trials", "label": "Trials", "plate": "rows", "type": "int", "support": support},
				map[string]any{"id": "d2", "kind": "data", "name": "wins", "label": "Wins", "plate": "rows", "type": "int", "support": "nonnegative"},
				map[string]any{"id": "l2", "kind": "likelihood", "label": "Wins", "observed": "d2", "dist": "binomial_logit",
					"args": map[string]any{"N": expr("trials"), "alpha": expr("beta")}})
		}
	}
	wantNot(t, "nonnegative", vfind(t, lik("nonnegative")), "graph.slot_maybe")
	want(t, "any", vfind(t, lik("any")), "graph.slot_maybe", "l2", "args.N")
	wantNot(t, "binary", vfind(t, lik("binary")), "graph.slot_maybe")
}

func TestFunctionDomains(t *testing.T) {
	mu := func(s string) func(m map[string]any) {
		return func(m map[string]any) { nodeByID(m, "n9")["args"].(map[string]any)["mu"] = expr(s) }
	}
	fs := vfind(t, mu("log(beta) + log(beta * 2)"))
	if f := want(t, "log of a real", fs, "graph.slot_maybe", "n9", "args.mu"); f.Args["function"] != "log" || f.Args["need"] != "positive" {
		t.Errorf("log of a real: %+v", f)
	}
	if n := countCode(fs, "graph.slot_maybe"); n != 1 {
		t.Errorf("one warning per function and relation, got %d: %s", n, codes(fs))
	}
	fs = vfind(t, mu("log(-1) + beta"))
	if f := want(t, "log of a negative", fs, "graph.slot_interval", "n9", "args.mu"); f.Args["function"] != "log" {
		t.Errorf("log of a negative: %+v", f)
	}
	fs = vfind(t, mu("logit(tau) + log1p(-2) + sqrt(beta)"))
	want(t, "logit", fs, "graph.slot_maybe", "n9", "args.mu")
	want(t, "log1p", fs, "graph.slot_interval", "n9", "args.mu")
	// beside
	for _, s := range []string{"log(tau)", "log(exp(beta))", "logit(inv_logit(beta))", "sqrt(square(beta))", "log1p(tau)", "exp(beta)"} {
		fs := vfind(t, mu(s))
		wantNot(t, s, fs, "graph.slot_maybe")
		wantNot(t, s, fs, "graph.slot_interval")
	}
}

func TestConstant(t *testing.T) {
	fs := vfind(t, func(m map[string]any) { addNodes(m, effects("p1", "tau")) })
	want(t, "eta a parameter", fs, "graph.constant", "e1.correlation", "eta")
	fs = vfind(t, func(m map[string]any) { addNodes(m, effects("p1", "1 + 1")) })
	want(t, "eta an expression", fs, "graph.constant", "e1.correlation", "eta")
	wantNot(t, "eta a number", vfind(t, func(m map[string]any) { addNodes(m, effects("p1", "2")) }), "graph.constant")
	wantNot(t, "eta in parentheses", vfind(t, func(m map[string]any) { addNodes(m, effects("p1", "(2)")) }), "graph.constant")
}

// nested is a graph of pupils in schools in districts, with a category and a series.
const nested = `{
  "format_version": 1,
  "plates": [
    {"id": "rows", "kind": "rows", "label": "Pupils", "series": {"time": "d4", "by": null}},
    {"id": "p1", "kind": "group", "label": "District", "key": "d1"},
    {"id": "p2", "kind": "group", "label": "School", "parent": "p1", "key": "d2"}
  ],
  "nodes": [
    {"id": "d1", "kind": "data", "name": "district", "label": "District", "plate": "rows", "type": "group_index", "target": "p1"},
    {"id": "d2", "kind": "data", "label": "School", "plate": "rows", "type": "group_index", "target": "p2"},
    {"id": "d3", "kind": "data", "name": "region", "label": "Region", "plate": "rows", "type": "category"},
    {"id": "d4", "kind": "data", "name": "day", "label": "Day", "plate": "rows", "type": "time"},
    {"id": "d5", "kind": "data", "name": "score", "label": "Score", "plate": "rows", "type": "real"},
    {"id": "d6", "kind": "data", "name": "size", "label": "District size", "plate": "p1", "type": "real", "standardize": true},
    {"id": "m1", "kind": "parameter", "name": "mu", "label": "Mean", "prior": {"dist": "normal", "args": {"mu": {"expr": "0"}, "sigma": {"expr": "1"}}}},
    {"id": "m2", "kind": "parameter", "name": "mu_district", "label": "District mean", "plate": "p1",
     "prior": {"dist": "normal", "args": {"mu": {"expr": "mu"}, "sigma": {"expr": "1"}}}},
    {"id": "m3", "kind": "parameter", "name": "mu_school", "label": "School mean", "plate": "p2",
     "prior": {"dist": "normal", "args": {"mu": {"expr": "mu_district[district]"}, "sigma": {"expr": "1"}}}},
    {"id": "m4", "kind": "parameter", "name": "b_region", "label": "Region effect", "plate": "levels:d3",
     "prior": {"dist": "normal", "args": {"mu": {"expr": "0"}, "sigma": {"expr": "1"}}}},
    {"id": "m5", "kind": "parameter", "name": "sigma", "label": "Noise", "prior": {"dist": "exponential", "args": {"beta": {"expr": "1"}}}},
    {"id": "t1", "kind": "transformed", "name": "trend", "label": "Trend", "plate": "rows", "value": {"expr": "lag(score, 1) + lag(score, 2)"}},
    {"id": "l1", "kind": "likelihood", "label": "Scores", "observed": "d5", "dist": "normal",
     "args": {"mu": {"expr": "mu_school[d2x] + b_region[region] + trend"}, "sigma": {"expr": "sigma"}}}
  ]
}`

func TestNestedPlatesCategoryAndSeries(t *testing.T) {
	// d2 has no name: the likelihood cannot index with it by name
	v := validate(mustDecode(t, nested))
	want(t, "an unnamed index", v.out, "expr.unknown_name", "l1", "args.mu")
	wantNot(t, "a lifted index in a prior", v.out, "graph.prior_data")

	g := mustDecode(t, strings.Replace(strings.Replace(nested, `"label": "School", "plate": "rows"`, `"name": "school", "label": "School", "plate": "rows"`, 1), "d2x", "school", 1))
	v = validate(g)
	wantClean(t, "nested", withoutUnused(v.out))
	s := v.slots[SlotKey{"m3", "prior.mu"}]
	if s.Context != "p2" || len(s.Expr.Lifts) != 1 || s.Expr.Lifts[0].ID != "d1" || s.Expr.Lifts[0].Context != "p2" ||
		!reflect.DeepEqual(s.Indices, []string{"d1"}) || !reflect.DeepEqual(s.Refs, []string{"m2"}) {
		t.Errorf("the lifted index: %+v", s)
	}
	if s := v.slots[SlotKey{"t1", "value"}]; !reflect.DeepEqual(s.Expr.Lags, []int{1, 2}) {
		t.Errorf("lags: %+v", s.Expr)
	}
	if p := v.params["m4"]; p.Truncated {
		t.Errorf("a parameter on levels: %+v", p)
	}
	// a district's size is data on a group plate: a prior that reads it reaches data
	g = mustDecode(t, strings.Replace(nested, `"mu": {"expr": "mu"}, "sigma": {"expr": "1"}}}},
    {"id": "m3"`, `"mu": {"expr": "mu + size"}, "sigma": {"expr": "1"}}}},
    {"id": "m3"`, 1))
	if f := want(t, "data on a group in a prior", Validate(g), "graph.prior_data", "m2", "prior.mu"); f.Args["data"] != "d6" || f.Args["through"] != nil {
		t.Errorf("direct data: %+v", f)
	}
}

func TestPriorData(t *testing.T) {
	// a district-level example: size on p1, transformed and linear predictors on p1
	base := func(m map[string]any) {
		addNodes(m,
			map[string]any{"id": "d1", "kind": "data", "name": "size", "label": "Size", "plate": "p1", "type": "real"},
			map[string]any{"id": "t1", "kind": "transformed", "name": "t_one", "label": "T1", "plate": "p1", "value": expr("size * 2")},
			map[string]any{"id": "t2", "kind": "transformed", "name": "t_two", "label": "T2", "plate": "p1", "value": expr("t_one + 1")},
			map[string]any{"id": "t3", "kind": "transformed", "name": "t_free", "label": "T3", "plate": "p1", "value": expr("mu * 2")},
			map[string]any{"id": "t4", "kind": "transformed", "label": "T4", "plate": "p1", "value": ref("d1")},
			map[string]any{"id": "lp", "kind": "linear_predictor", "name": "eta_d", "label": "LP", "plate": "p1",
				"intercept": expr("mu"), "terms": []any{map[string]any{"data": "d1", "coef": expr("beta")}}},
			map[string]any{"id": "lq", "kind": "linear_predictor", "name": "eta_e", "label": "LQ", "plate": "p1",
				"intercept": expr("t_free")},
			// a likelihood reading the values, so that nothing else changes
			map[string]any{"id": "y2", "kind": "data", "name": "y2", "label": "Y2", "plate": "p1", "type": "real"},
		)
	}
	mu := func(s map[string]any) func(m map[string]any) {
		return func(m map[string]any) {
			base(m)
			nodeByID(m, "n6")["prior"].(map[string]any)["args"].(map[string]any)["mu"] = s
		}
	}
	cases := []struct {
		what          string
		slot          map[string]any
		data, through string
	}{
		{"direct", expr("size"), "d1", ""},
		{"a ref to data", ref("d1"), "d1", ""},
		{"through a transformed", expr("t_one"), "d1", "t1"},
		{"through two transformed", expr("t_two + mu"), "d1", "t2"},
		{"through a ref to a transformed", ref("t4"), "d1", "t4"},
		{"through a linear predictor's term", expr("eta_d"), "d1", "lp"},
		{"direct wins over through", expr("t_one + size"), "d1", ""},
	}
	for _, c := range cases {
		fs := vfind(t, mu(c.slot))
		f := want(t, c.what, fs, "graph.prior_data", "n6", "prior.mu")
		if f.Args["data"] != c.data || (c.through == "" && f.Args["through"] != nil) || (c.through != "" && f.Args["through"] != c.through) {
			t.Errorf("%s: %+v", c.what, f)
		}
	}
	// beside: parameters and values of parameters only
	for _, s := range []map[string]any{expr("t_free"), expr("eta_e + mu"), ref("t3"), expr("mu")} {
		wantNot(t, "no data", vfind(t, mu(s)), "graph.prior_data")
	}
	// a state's scale and init, a component's mean
	fs := vfind(t, func(m map[string]any) {
		s := state("rows")
		s["scale"] = expr("exp(hours)")
		s["init"] = normalPrior(expr("hours"), expr("1"))
		addNodes(m, s)
	})
	want(t, "state scale", fs, "graph.prior_data", "s1", "scale")
	want(t, "state init", fs, "graph.prior_data", "s1", "init.mu")
	fs = vfind(t, func(m map[string]any) {
		base(m)
		e := effects("p1", "2")
		e["components"].([]any)[0].(map[string]any)["mean"] = ref("d1")
		addNodes(m, e)
	})
	want(t, "component mean", fs, "graph.prior_data", "e1.c1", "mean")
	// a likelihood and a transformed read data: no prior there
	fs = vfind(t, func(m map[string]any) { base(m) })
	wantNot(t, "likelihood and transformed", fs, "graph.prior_data")
}

func TestEnv(t *testing.T) {
	g := mustDecode(t, strings.Replace(nested, `"label": "School", "plate": "rows"`, `"name": "school", "label": "School", "plate": "rows"`, 1))
	e := newGraphEnv(g, nil)
	enc := []struct {
		outer, inner bayesexpr.Plate
		want         bool
	}{
		{"p1", "p1", true}, {"p1", "p2", true}, {"p2", "p1", false}, {"p1", "rows", true}, {"p2", "rows", true},
		{"rows", "p1", false}, {"rows", "rows", true}, {"levels:d3", "rows", true}, {"levels:d3", "p1", false},
		{"p1", "levels:d3", false}, {"levels:d3", "levels:d3", true}, {"p9", "rows", false}, {"rows", "p9", false},
	}
	for _, c := range enc {
		if got := e.encloses(c.outer, c.inner); got != c.want {
			t.Errorf("Encloses(%s, %s) = %v", c.outer, c.inner, got)
		}
	}
	s := slotEnv{graphEnv: e, ctx: "rows"}
	if !s.IsSeries("rows") || s.IsSeries("p1") || s.IsSeries("levels:d3") {
		t.Error("IsSeries")
	}
	if n, ok := s.IndexOf("p1"); !ok || n != "district" {
		t.Errorf("IndexOf(p1) = %q %v", n, ok)
	}
	if n, ok := s.IndexOf("levels:d3"); !ok || n != "region" {
		t.Errorf("IndexOf(levels) = %q %v", n, ok)
	}
	if _, ok := s.IndexOf("rows"); ok {
		t.Error("IndexOf(rows)")
	}
	// an unnamed key
	e2 := newGraphEnv(mustDecode(t, nested), nil)
	if _, ok := (slotEnv{graphEnv: e2}).IndexOf("p2"); ok {
		t.Error("IndexOf of an unnamed key")
	}
	types := map[string]string{"district": "index[rows->p1]", "region": "index[rows->levels:d3]", "day": "time[rows]",
		"score": "real[rows]", "size": "real[p1]", "mu_school": "real[p2]", "b_region": "real[levels:d3]", "mu": "real", "trend": "real[rows]"}
	for name, ty := range types {
		sym, ok := s.Lookup(name)
		if !ok || sym.Type.String() != ty {
			t.Errorf("Lookup(%s) = %+v %v, want %s", name, sym, ok, ty)
		}
	}
	if _, ok := s.Lookup("nothing"); ok {
		t.Error("Lookup of an unknown name")
	}
	// a likelihood's name is not a number
	g.Nodes[len(g.Nodes)-1].Name = ptr("scores")
	if sym, ok := newGraphEnv(g, nil).byName["scores"]; !ok || sym.Type.Elem != bayesexpr.ElemNotNumber {
		t.Errorf("a likelihood's name: %+v %v", sym, ok)
	}
}

func ptr(s string) *string { return &s }

func TestEnvDataIntervalsAndTakenNames(t *testing.T) {
	fs := vfind(t, func(m map[string]any) {
		addNodes(m, map[string]any{"id": "d1", "kind": "data", "name": "flag", "label": "Flag", "plate": "rows", "type": "int", "support": "binary"},
			map[string]any{"id": "t1", "kind": "transformed", "label": "T", "plate": "rows", "value": expr("log(flag)")},
			map[string]any{"id": "t2", "kind": "transformed", "label": "T", "plate": "rows", "value": expr("log(flag + 1)")})
	})
	want(t, "log of [0, 1]", fs, "graph.slot_maybe", "t1", "value")
	if _, ok := find(fs, "graph.slot_maybe", "t2", "value"); ok {
		t.Errorf("log of [1, 2]: %s", codes(fs))
	}
	// a name held twice stands for the holder with the smallest id, whatever the order
	g := mustDecode(t, withDoc(t, func(m map[string]any) {
		addNodes(m, map[string]any{"id": "a0", "kind": "data", "name": "beta", "label": "B", "plate": "rows", "type": "time"})
	}))
	if sym := newGraphEnv(g, nil).byName["beta"]; sym.ID != "a0" {
		t.Errorf("a taken name: %+v", sym)
	}
}

func TestValidateIsDeterministic(t *testing.T) {
	edits := []func(m map[string]any){
		func(map[string]any) {},
		func(m map[string]any) {
			nodeByID(m, "n6")["prior"].(map[string]any)["args"].(map[string]any)["sigma"] = expr("log(mu) - 1")
			nodeByID(m, "n9")["args"].(map[string]any)["sigma"] = ref("n6")
			nodeByID(m, "n7")["bounds"] = map[string]any{"lower": 2, "upper": 1}
			addNodes(m, effects(nil, "mu"), state("p1"))
		},
	}
	r := rand.New(rand.NewSource(7))
	for i, edit := range edits {
		wantFs := vfind(t, edit)
		for k := 0; k < 10; k++ {
			got := vfind(t, func(m map[string]any) {
				edit(m)
				ns := m["nodes"].([]any)
				r.Shuffle(len(ns), func(a, b int) { ns[a], ns[b] = ns[b], ns[a] })
			})
			if !reflect.DeepEqual(got, wantFs) {
				t.Fatalf("edit %d, shuffle %d:\n got %s\nwant %s", i, k, codes(got), codes(wantFs))
			}
		}
	}
}
