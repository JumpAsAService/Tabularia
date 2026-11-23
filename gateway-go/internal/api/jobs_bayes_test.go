package api

import (
	"errors"
	"net/http"
	"strings"
	"testing"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/flowresolver"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
)

func TestABayesNodesRunsHaveTheModelsKind(t *testing.T) {
	if bayesRunKind("bayes_model") != "model" || bayesRunKind("model") != "model" || bayesRunKind("script") != "script" {
		t.Error("the kind of a node's runs")
	}
}

// Until lot F2 the step refuses with its sentence and touches nothing.
func TestRunBayesRefusesNotBuilt(t *testing.T) {
	node := pyjson.Object{{Key: "id", Value: "b"}, {Key: "type", Value: "bayes_model"},
		{Key: "data", Value: pyjson.Object{{Key: "label", Value: "School scores"}}}}
	q := &queue{}
	_, err := q.runBayes(nil, &scriptNodeRun{q: q, node: node, outputs: flowresolver.ScriptOutputs{}})
	var sf *scriptFailed
	if !errors.As(err, &sf) || !strings.Contains(sf.msg, "Bayesian model «School scores»: not built") {
		t.Errorf("%v", err)
	}
}

func (f *modelRunFixture) bayesFlow() int64 {
	f.t.Helper()
	text := `{"nodes": [{"id": "s", "type": "source", "data": {"datasourceId": 1}},
		{"id": "b", "type": "bayes_model", "data": {"label": "School scores", "bayesModelId": 3, "bayesModelVersion": 1}},
		{"id": "o", "type": "output", "data": {"name": "summary"}}], "edges": [{"source": "s", "target": "b"}, {"source": "b", "target": "o"}]}`
	var id int64
	if err := f.db.QueryRow(f.ctx, `INSERT INTO flows (name, description, project_id, owner_id, definition, engine, created_at, updated_at)
		VALUES ('Bayes', '', $1, $2, $3, 'duckdb', now(), now()) RETURNING id`, f.folder, f.ann.ID, text).Scan(&id); err != nil {
		f.t.Fatal(err)
	}
	return id
}

// A flow with the node is refused with the sentence, and no run is left behind (the editor's trial and a flow's step).
func TestABayesNodeIsRefusedAndLeavesNoRun(t *testing.T) {
	f := newModelRunFixture(t)
	flow := f.bayesFlow()
	if w := f.trial(f.ann, flow, "b"); w.Code != http.StatusUnprocessableEntity || !strings.Contains(w.Body.String(), "Bayesian model «School scores»: not built") {
		t.Errorf("trial: %d %s", w.Code, w.Body)
	}
	var text string
	_ = f.db.QueryRow(f.ctx, `SELECT definition FROM flows WHERE id = $1`, flow).Scan(&text)
	definition := mustDef(t, text)
	var node pyjson.Object
	for _, n := range objectList(definition.Value("nodes")) {
		if n.Value("id") == "b" {
			node = n
		}
	}
	jf := &jobFlow{ID: flow, ProjectID: f.folder, Name: "Bayes", Definition: text, Engine: "duckdb"}
	_, err := f.q.runBayes(f.ctx, &scriptNodeRun{q: f.q, user: f.ann, flow: jf, definition: definition, node: node, trigger: "manual",
		mode: "development", outputs: flowresolver.ScriptOutputs{}})
	var sf *scriptFailed
	if !errors.As(err, &sf) || !strings.Contains(sf.msg, "not built") {
		t.Errorf("step: %v", err)
	}
	var n int64
	_ = f.db.QueryRow(f.ctx, `SELECT count(*) FROM runs WHERE flow_id = $1`, flow).Scan(&n)
	if n != 0 {
		t.Errorf("%d runs left behind", n)
	}
}

// A model node never runs the Bayesian model: it is a node of its own.
func TestAModelNodeNeverRunsTheBayesianModel(t *testing.T) {
	f := newModelRunFixture(t)
	_, definition, node := f.flow(`{"outcome": "y"}`, false)
	_ = definition
	data := nodeData(node)
	data.Set("model", "bayes_model")
	_, err := f.q.planModel(f.ctx, f.ann, node)
	var sf *scriptFailed
	if !errors.As(err, &sf) || !strings.Contains(sf.msg, "add «Bayesian model» from the palette") {
		t.Errorf("model naming bayes_model: %v", err)
	}
	node.Set("type", "bayes_model")
	if _, err = f.q.planModel(f.ctx, f.ann, node); !errors.As(err, &sf) || !strings.Contains(sf.msg, "a node of its own") {
		t.Errorf("a Bayesian node given to planModel: %v", err)
	}
}
