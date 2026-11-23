package modelgroups

import (
	"encoding/json"
	"testing"
)

// sampleTaskForBytes is a task of another group with every field set: what its task.json holds must not change when the group
// `bayes` adds its key (docs/specs/bayes-contract.md, «task.json»: `bayes` is left out when it is nil).
func sampleTaskForBytes() Task {
	return Task{Protocol: 1, Mode: "fit", Group: "regression", GroupVersion: 7, Model: "linear", Variant: "ols",
		Params: map[string]any{"outcome": "y", "predictors": []any{"x1", "x2"}, "alpha": 0.05}, Output: "rows", WriteOutput: true,
		RowsFrom: "trial", MLflow: true, Limits: map[string]int{"max_result_rows": 100000, "time_budget_seconds": 900},
		InputRows: 600, IDs: IDs{FlowID: 42, NodeID: "n7", RunID: 9811, StepRunID: 9812},
		Licence: &TaskLicence{By: "someone@example.org", At: "2026-11-23T10:00:00Z"}, RecipeHash: "r", RunnerHash: "h"}
}

// taskBytesBefore is sampleTaskForBytes as task.json before the group `bayes` had a key (lot K0), byte for byte.
const taskBytesBefore = `{"protocol":1,"mode":"fit","group":"regression","group_version":7,"model":"linear","variant":"ols",` +
	`"params":{"alpha":0.05,"outcome":"y","predictors":["x1","x2"]},"output":"rows","write_output":true,"rows_from":"trial",` +
	`"mlflow":true,"limits":{"max_result_rows":100000,"time_budget_seconds":900},"input_rows":600,` +
	`"ids":{"flow_id":42,"node_id":"n7","run_id":9811,"step_run_id":9812},` +
	`"licence":{"by":"someone@example.org","at":"2026-11-23T10:00:00Z"},"recipe_hash":"r","runner_hash":"h"}`

func TestAnotherGroupsTaskKeepsItsBytes(t *testing.T) {
	b, err := json.Marshal(sampleTaskForBytes())
	if err != nil {
		t.Fatal(err)
	}
	if string(b) != taskBytesBefore {
		t.Errorf("the task.json of a group without `bayes` changed:\n  before %s\n  now    %s", taskBytesBefore, b)
	}
}
