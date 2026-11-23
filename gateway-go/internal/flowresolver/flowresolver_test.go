package flowresolver

import (
	"errors"
	"testing"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyval"
)

func def(t *testing.T, s string) any {
	v, err := pyjson.Loads(s)
	if err != nil {
		t.Fatal(err)
	}
	return v
}

func resolveDS(id any) (*Source, error) {
	if pyval.Eq(id, 1) {
		return &Source{Bucket: "b", Key: "datasets/1.parquet"}, nil
	}
	return nil, nil
}

func TestChainsWithJoinFiltersAndIDs(t *testing.T) {
	d := def(t, `{"nodes": [
		{"id": "s", "type": "source", "data": {"datasourceId": 1, "filters": [{"column": "x", "operator": "eq", "value": 1}, {"column": "", "operator": "eq", "value": 1}], "sample": {"mode": "first", "rows": 5}}},
		{"id": "u", "type": "source", "data": {"parquetKey": "up.parquet"}},
		{"id": "j", "type": "operation", "data": {"opType": "join", "params": {"on": ["id"]}}},
		{"id": "o", "type": "output", "data": {"name": " out ", "projectId": 3, "sortKeys": ["id"], "timezone": "Europe/Rome", "mirrorEnabled": true, "mirrorConnectionId": 4}}],
		"edges": [{"source": "s", "target": "j"}, {"source": "u", "target": "j", "targetHandle": "right"}, {"source": "j", "target": "o"}]}`)
	chains, err := ResolveOutputChains(d, resolveDS, "data-prep", nil)
	if err != nil || len(chains) != 1 {
		t.Fatal(err, chains)
	}
	c := chains[0]
	if len(c.OpIDs) != 2 || c.OpIDs[0] != "s#head" || c.OpIDs[1] != "j" {
		t.Errorf("ids %v", c.OpIDs)
	}
	ops := c.Body.Value("operations").([]any)
	if len(ops) != 2 { // the filter, not the sample (production), then the join
		t.Fatalf("ops %v", ops)
	}
	right := ops[1].(pyjson.Object).Value("params").(pyjson.Object).Value("right").(pyjson.Object)
	if right.Value("source").(pyjson.Object).Value("bucket") != nil {
		t.Error("an upload without a bucket keeps None")
	}
	if c.Body.Value("publish").(pyjson.Object).Value("name") != "out" || c.Body.Value("mirror").(pyjson.Object).Value("timezone") != "Europe/Rome" {
		t.Errorf("body %v", c.Body)
	}
	// development: the sample after the filters
	body, err := ResolveOutputRequest(d, pyval.Index(d.(pyjson.Object).Value("nodes").([]any)[3], "id").(string), resolveDS, "x", Mode{Development: true}, nil)
	_ = body
	if err == nil {
		t.Error("a node id is not a node: AttributeError expected")
	}
}

func TestLoopAndMissingID(t *testing.T) {
	d := def(t, `{"nodes": [{"id": "s", "type": "source", "data": {"datasourceId": 1}}, {"id": "j", "type": "operation", "data": {"opType": "join", "label": "J"}},
		{"id": "o", "type": "output"}], "edges": [{"source": "s", "target": "j"}, {"source": "j", "target": "j", "targetHandle": "right"}, {"source": "j", "target": "o"}]}`)
	_, err := ResolveOutputChains(d, resolveDS, "b", nil)
	var fe *Error
	if !errors.As(err, &fe) || fe.Code != "flow_loop" || fe.Params.Value("node") != "J" {
		t.Errorf("loop: %v", err)
	}
	_, err = ResolveOutputChains(def(t, `{"nodes": [{"type": "output"}]}`), resolveDS, "b", nil)
	var pe *pyval.Error
	if !errors.As(err, &pe) || pe.Type != "KeyError" || pe.Msg != "'id'" {
		t.Errorf("missing id: %v", err)
	}
	_, err = ResolveOutputChains(def(t, `{"nodes": [{"id": "o", "type": "output"}]}`), resolveDS, "b", nil)
	if !errors.As(err, &fe) || fe.Code != "output_no_source" || fe.Params.Value("output") != "o" {
		t.Errorf("no source: %v", err)
	}
}

func TestStripAndSequence(t *testing.T) {
	ops := def(t, `[{"type": "limit", "params": {"n": 1, "_dev_sample": true}}, {"type": "join", "params": {"right": {"operations": [{"type": "sample", "params": {"_dev_sample": true}}]}}}]`).([]any)
	out, n := StripDevSampleOps(ops)
	if n != 2 || len(out) != 1 {
		t.Errorf("%v %d", out, n)
	}
	order, err := SequenceOrder(def(t, `{"nodes": [{"id": "r", "type": "runflow"}, {"id": "o", "type": "output"}, {"id": "f", "type": "refresh"},
		{"id": "p", "type": "script"}, {"id": "o2", "type": "output"}], "edges": [{"source": "o2", "target": "o", "targetHandle": "seq-in"}, {"source": "p", "target": "o2"}]}`))
	if err != nil {
		t.Fatal(err)
	}
	var ids []any
	for _, n := range order {
		ids = append(ids, n.(pyjson.Object).Value("id"))
	}
	want := []any{"f", "p", "o2", "o", "r"}
	for i := range want {
		if ids[i] != want[i] {
			t.Fatalf("order %v, want %v", ids, want)
		}
	}
}

// A model node is a step boundary (docs/specs/statistical-models.md, «What a model node is»):
// the chain before it ends at its input, the nodes after it read what it wrote, and a run
// orders it after the steps it reads and before the outputs that read it.
func TestAModelNodeIsAStep(t *testing.T) {
	d := def(t, `{"nodes": [
		{"id": "s", "type": "source", "data": {"datasourceId": 1, "sample": {"mode": "first", "rows": 5}}},
		{"id": "f", "type": "operation", "data": {"opType": "filter", "params": {"column": "x"}}},
		{"id": "m", "type": "model", "data": {"model": "linear", "params": {"outcome": "y", "predictors": ["x"]}}},
		{"id": "g", "type": "operation", "data": {"opType": "sort", "params": {"by": ["stat"]}}},
		{"id": "o", "type": "output", "data": {"name": "coefficients"}},
		{"id": "p", "type": "model_predict", "data": {}},
		{"id": "o2", "type": "output", "data": {"name": "predicted"}}],
		"edges": [{"source": "s", "target": "f"}, {"source": "f", "target": "m"}, {"source": "m", "target": "g"}, {"source": "g", "target": "o"},
			{"source": "s", "target": "p"}, {"source": "p", "target": "o2"}]}`)
	// the input: the source (and its development sample), the filter; the model itself is not an operation
	body, err := ResolveScriptInput(d, def(t, `{"id": "m", "type": "model"}`), resolveDS, "b", Mode{Development: true, DefaultSampleRows: 100}, nil)
	if err != nil || body == nil {
		t.Fatal(err, body)
	}
	ops := body.Value("operations").([]any)
	if body.Value("input_key") != "datasets/1.parquet" || len(ops) != 2 || ops[1].(pyjson.Object).Value("type") != "filter" {
		t.Errorf("the model's input: %v", body)
	}
	// after it: what it wrote, then the sort; nothing of the chain before it
	if _, err := ResolveOutputRequest(d, def(t, `{"id": "o", "type": "output", "data": {"name": "coefficients"}}`), resolveDS, "b", Mode{}, nil); err == nil {
		t.Error("before the model ran, the output after it has no source")
	}
	out := ScriptOutputs{ScriptOutputKey("m"): {Bucket: "b", Key: "out/results.parquet"}}
	req, err := ResolveOutputRequest(d, def(t, `{"id": "o", "type": "output", "data": {"name": "coefficients"}}`), resolveDS, "b", Mode{}, out)
	if err != nil {
		t.Fatal(err)
	}
	after := req.Value("operations").([]any)
	if req.Value("input_key") != "out/results.parquet" || len(after) != 1 || after[0].(pyjson.Object).Value("type") != "sort" {
		t.Errorf("after the model: %v", req)
	}
	// the order of a run: the model and Predict before the outputs that read them
	order, err := SequenceOrder(d)
	if err != nil {
		t.Fatal(err)
	}
	var ids []any
	for _, n := range order {
		ids = append(ids, n.(pyjson.Object).Value("id"))
	}
	at := map[any]int{}
	for i, id := range ids {
		at[id] = i
	}
	if len(ids) != 4 || at["m"] > at["o"] || at["p"] > at["o2"] {
		t.Fatalf("order %v: each step before the output that reads it", ids)
	}
	up, err := ScriptsUpstream("o", mustNodes(t, d), d.(pyjson.Object).Value("edges").([]any))
	if err != nil || len(up) != 1 || up[0] != "m" {
		t.Errorf("the steps an output reads: %v %v", up, err)
	}
}

func mustNodes(t *testing.T, d any) *pyval.Dict {
	t.Helper()
	n, err := NodesByID(d.(pyjson.Object).Value("nodes"))
	if err != nil {
		t.Fatal(err)
	}
	return n
}

// A Bayesian model node is a step boundary as the models are, and runs in the models' priority.
func TestABayesModelNodeIsAStep(t *testing.T) {
	d := def(t, `{"nodes": [
		{"id": "s", "type": "source", "data": {"datasourceId": 1}},
		{"id": "b", "type": "bayes_model", "data": {"bayesModelId": 3, "bayesModelVersion": 1}},
		{"id": "o", "type": "output", "data": {"name": "summary"}}],
		"edges": [{"source": "s", "target": "b"}, {"source": "b", "target": "o"}]}`)
	found := false
	for _, typ := range StepNodeTypes {
		found = found || typ == "bayes_model"
	}
	if !found {
		t.Fatal("StepNodeTypes lacks bayes_model")
	}
	if !inStrings("bayes_model", actionTypes...) || defaultPriority["bayes_model"] != defaultPriority["model"] {
		t.Error("the action types and the priority list the node")
	}
	order, err := SequenceOrder(d)
	if err != nil {
		t.Fatal(err)
	}
	var ids []any
	for _, n := range order {
		ids = append(ids, n.(pyjson.Object).Value("id"))
	}
	if len(ids) != 2 || ids[0] != "b" || ids[1] != "o" {
		t.Errorf("order %v: the node before the output that reads it", ids)
	}
	up, err := ScriptsUpstream("o", mustNodes(t, d), d.(pyjson.Object).Value("edges").([]any))
	if err != nil || len(up) != 1 || up[0] != "b" {
		t.Errorf("the steps an output reads: %v %v", up, err)
	}
}
