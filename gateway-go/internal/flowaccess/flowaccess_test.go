package flowaccess

import (
	"strconv"
	"strings"
	"testing"
)

func TestWidens(t *testing.T) {
	before := `{"nodes": [{"type": "source", "data": {"datasourceId": 3, "key": "datasets/a.parquet"}}]}`
	if n := len(Widens(before, before)); n != 0 {
		t.Fatalf("the same definition widens by %d", n)
	}
	after := `{"nodes": [{"type": "source", "data": {"datasourceId": "3", "key": "datasets/a.parquet"}},
		{"type": "output", "data": {"connectionId": 7, "flowId": true}}]}`
	got := Widens(before, after)
	if len(got) != 1 || got[0] != (Ref{"connection", "7"}) {
		t.Fatalf("widens: %v", got)
	}
	script := `{"nodes": [{"id": "s", "type": "script", "data": {"files": [{"path": "a.py", "content": "x"}]}}]}`
	changed := `{"nodes": [{"id": "s", "type": "script", "data": {"files": [{"path": "a.py", "content": "y"}]}}]}`
	if len(Widens(script, changed)) != 1 {
		t.Fatal("new code does not widen")
	}
}

func TestDigits(t *testing.T) {
	for _, c := range []struct {
		v  any
		ok bool
	}{{int64(4), true}, {"12", true}, {"-1", false}, {int64(-1), false}, {true, false}, {"1.0", false}, {"", false}, {1.0, false}} {
		if _, ok := Digits(c.v); ok != c.ok {
			t.Errorf("Digits(%#v) = %v", c.v, ok)
		}
	}
}

// A model node added reaches further (the group's image), and so does its MLflow connection.
func TestAModelNodeWidens(t *testing.T) {
	before := `{"nodes": [{"id": "s", "type": "source", "data": {"datasourceId": 1}}]}`
	after := `{"nodes": [{"id": "s", "type": "source", "data": {"datasourceId": 1}},
		{"id": "m", "type": "model", "data": {"model": "linear", "mlflow": {"connectionId": 12}}}]}`
	got := map[Ref]bool{}
	for _, r := range Widens(before, after) {
		got[r] = true
	}
	if len(got) != 2 || !got[Ref{"model", "linear"}] || !got[Ref{"connection", "12"}] {
		t.Errorf("%v", got)
	}
	if n := len(Widens(after, after)); n != 0 {
		t.Errorf("the same flow widens nothing: %d", n)
	}
	if n := len(ModelNodes(after)); n != 1 {
		t.Errorf("model nodes of a text: %d", n)
	}
}

// A Bayesian model node is one of the model nodes (the lists that read them), but it names no catalogue model:
// its reach is the version it pins (TestABayesModelPinIsReach).
func TestABayesModelNodeIsAModelNode(t *testing.T) {
	def := `{"nodes": [{"id": "s", "type": "source", "data": {"datasourceId": 1}},
		{"id": "b", "type": "bayes_model", "data": {"bayesModelId": 3, "bayesModelVersion": 1}}]}`
	if n := len(ModelNodes(def)); n != 1 {
		t.Errorf("model nodes of a text: %d", n)
	}
	for r := range ReachOf(def) {
		if r.Kind == "model" {
			t.Errorf("a Bayesian node is not a catalogue model: %v", r)
		}
	}
}

// A Bayesian model node reaches the (model, version) it pins: adding one or moving its pin widens
// (someone else's schedule stops), its bindings and sampler do not.
func TestABayesModelPinIsReach(t *testing.T) {
	node := func(id string, model, version int, extra string) string {
		return `{"id": "` + id + `", "type": "bayes_model", "data": {"bayesModelId": ` + strconv.Itoa(model) +
			`, "bayesModelVersion": ` + strconv.Itoa(version) + extra + `}}`
	}
	def := func(nodes ...string) string {
		return `{"nodes": [{"id": "s", "type": "source", "data": {"datasourceId": 1}}` + strings.Join(append([]string{""}, nodes...), ", ") + `]}`
	}
	base := def()
	pinned := def(node("b", 3, 1, ""))
	if !ReachOf(pinned)[Ref{"bayes_model", "3@1"}] {
		t.Fatalf("the pin is not reach: %v", ReachOf(pinned))
	}
	for _, c := range []struct {
		name          string
		before, after string
		want          []Ref
	}{
		{"a node added", base, pinned, []Ref{{"bayes_model", "3@1"}}},
		{"the version moved", pinned, def(node("b", 3, 2, "")), []Ref{{"bayes_model", "3@2"}}},
		{"another model", pinned, def(node("b", 4, 1, "")), []Ref{{"bayes_model", "4@1"}}},
		{"the bindings and the sampler", pinned, def(node("b", 3, 1, `, "bindings": {"n1": "x"}, "sampler": {"chains": 2}, "label": "L"`)), nil},
		{"a node removed", pinned, base, nil},
		{"a second node of the same pin", pinned, def(node("b", 3, 1, ""), node("c", 3, 1, "")), nil},
	} {
		got := Widens(c.before, c.after)
		if len(got) != len(c.want) || (len(got) == 1 && got[0] != c.want[0]) {
			t.Errorf("%s: widens %v, want %v", c.name, got, c.want)
		}
	}
	// a model named by text is no pin (bayesmodels.Pinned: an integer id), and names no catalogue model either
	for r := range ReachOf(def(`{"id": "b", "type": "bayes_model", "data": {"bayesModelId": "3", "bayesModelVersion": 1}}`)) {
		if r.Kind == "bayes_model" || r.Kind == "model" {
			t.Errorf("a text id reaches %v", r)
		}
	}
}
